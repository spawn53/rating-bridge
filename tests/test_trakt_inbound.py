from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import sqlite3

import httpx
import pytest

from hub.inbound.classification import classify, targets_excluding_source
from hub.inbound.models import InboundError, MovieRating, Snapshot, normalize
from hub.inbound.storage import InboundStore
from hub.inbound.trakt import InboundSettings, fetch_snapshot, main, observe
from hub.models import RatingWrite
from hub.settings import HubSettings
from hub.store import RatingStore

KEY = "movie:tmdb:550"
DATE = "2026-09-26T10:00:00Z"


def item(score=8, tmdb=550, **ids):
    return {"rating": score, "rated_at": DATE,
            "movie": {"ids": {"tmdb": tmdb, "trakt": 1, "imdb": "tt0137523", **ids}}}


def headers(page=1, pages=1, count=1, limit=250):
    return {"X-Pagination-Page": str(page), "X-Pagination-Page-Count": str(pages),
            "X-Pagination-Item-Count": str(count), "X-Pagination-Limit": str(limit)}


def snapshot(*ratings):
    return Snapshot(tuple(MovieRating(score, DATE, identity, identity) for identity, score in ratings))


def read_pages(pages, **kwargs):
    seen = []
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/users/me/ratings/movies"
        assert request.url.params["limit"] == "250"
        seen.append(int(request.url.params["page"]))
        body, hdr = pages[len(seen) - 1]
        return httpx.Response(200, json=body, headers=hdr)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_snapshot(SimpleNamespace(headers={"Authorization": "Bearer offline"}),
                                client, **kwargs)
    return result, seen


def table(store, name):
    with store.connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM " + name + " ORDER BY 1")]


def all_inbound(store):
    return {name: table(store, name) for name in (
        "inbound_state", "inbound_snapshots", "inbound_unmapped", "inbound_events"
    )}


@pytest.fixture
def store(tmp_path):
    path = str(tmp_path / "hub.sqlite3")
    RatingStore(path)
    return InboundStore(path)


def test_one_page_and_endpoints():
    result, seen = read_pages([([item()], headers())])
    assert seen == [1] and result.movies == 1
    assert result.eligible[0].content_key == KEY
    assert result.eligible[0].rated_at == "2026-09-26T10:00:00.000000+00:00"


def test_multiple_pages_complete_traversal():
    result, seen = read_pages([
        ([item(tmdb=1)], headers(1, 2, 2)),
        ([item(tmdb=550)], headers(2, 2, 2)),
    ])
    assert seen == [1, 2] and [r.tmdb_id for r in result.eligible] == [1, 550]


@pytest.mark.parametrize("pages", [0, 1])
def test_empty_snapshot(pages):
    result, _ = read_pages([([], headers(1, pages, 0))])
    assert result.movies == 0


@pytest.mark.parametrize("failure", ["missing", "wrong_page", "changed_total", "wrong_count",
                                     "zero_limit", "large_limit", "non_list", "bad_header"])
def test_invalid_pagination(failure):
    first = headers(1, 2, 2)
    second = headers(2, 2, 2)
    body = [item(tmdb=2)]
    if failure == "missing": del second["X-Pagination-Page"]
    if failure == "wrong_page": second["X-Pagination-Page"] = "1"
    if failure == "changed_total": second["X-Pagination-Page-Count"] = "3"
    if failure == "wrong_count":
        first["X-Pagination-Item-Count"] = "3"
        second["X-Pagination-Item-Count"] = "3"
    if failure == "zero_limit": second["X-Pagination-Limit"] = "0"
    if failure == "large_limit": second["X-Pagination-Limit"] = "251"
    if failure == "non_list": body = {}
    if failure == "bad_header": second["X-Pagination-Page"] = "2.0"
    with pytest.raises(InboundError):
        read_pages([([item(tmdb=1)], first), (body, second)])


def test_duplicate_key_across_pages_fails_closed():
    with pytest.raises(InboundError, match="duplicate"):
        read_pages([([item()], headers(1, 2, 2)), ([item()], headers(2, 2, 2))])


def test_mid_pagination_remaining_budget_and_timeout():
    now = [0.0]
    timeouts = []
    calls = []
    def handler(request):
        calls.append(request.url.params["page"])
        timeouts.append(request.extensions["timeout"]["read"])
        now[0] += 0.75
        page = int(request.url.params["page"])
        return httpx.Response(200, json=[item(tmdb=page)], headers=headers(page, 3, 3))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InboundError, match="deadline"):
            fetch_snapshot(SimpleNamespace(headers={}), client, timeout=1,
                           clock=lambda: now[0])
    assert calls == ["1", "2"] and timeouts == [1, 0.25]


@pytest.mark.parametrize("score", [1, 10])
def test_boundary_ratings(score):
    assert normalize(item(score)).rating == score


@pytest.mark.parametrize("score", [0, 11, 7.5, True, False, "8", None])
def test_invalid_rating(score):
    with pytest.raises(InboundError):
        normalize(item(score))


@pytest.mark.parametrize("date", [None, "", "2026-09-26", "not-a-date",
                                  "2026-09-26T10:00:00", "2026-02-30T10:00:00Z"])
def test_invalid_timestamp(date):
    data = item()
    data["rated_at"] = date
    with pytest.raises(InboundError):
        normalize(data)


@pytest.mark.parametrize("identity", [0, -1, True, 550.0, "550"])
def test_invalid_tmdb_id(identity):
    with pytest.raises(InboundError):
        normalize(item(tmdb=identity))


@pytest.mark.parametrize("identity", [0, -1, True, 1.5, "1"])
def test_invalid_trakt_id(identity):
    with pytest.raises(InboundError):
        normalize(item(trakt=identity))


@pytest.mark.parametrize("identity", ["bad", "tt", "tt123", 123])
def test_invalid_imdb_id(identity):
    with pytest.raises(InboundError):
        normalize(item(imdb=identity))


def test_missing_tmdb_is_counted_and_stored_without_event(store):
    data = item(tmdb=None)
    result, _ = read_pages([([data], headers())])
    assert result.movies == 1 and not result.eligible and len(result.unmapped) == 1
    observed = observe(store, lambda: result, baseline=True)
    assert observed["skipped"] == 1 and table(store, "inbound_unmapped")
    assert not table(store, "inbound_events")
    assert not table(store, "ratings") and not table(store, "outbox")


def test_missing_tmdb_still_validates_every_other_field():
    data = item(tmdb=None)
    data["rated_at"] = "bad"
    with pytest.raises(InboundError):
        read_pages([([data], headers())])


def test_baseline_zero_mutations_and_events(store):
    before = {n: table(store, n) for n in ("ratings", "outbox")}
    result = observe(store, lambda: snapshot((550, 8)), baseline=True)
    assert result["events"] == result["canonical_mutations"] == result["provider_writes"] == 0
    assert result["eligible"] == 1 and result["generation"] == 1
    assert store.state()["snapshot_hash"] == snapshot((550, 8)).snapshot_hash
    assert before == {n: table(store, n) for n in before}


def test_second_baseline_refused_before_read(store):
    observe(store, lambda: snapshot((550, 8)), baseline=True)
    with pytest.raises(InboundError, match="reset"):
        observe(store, lambda: pytest.fail("must not fetch"), baseline=True)


def test_reset_is_explicit_and_preserves_event_audit(store):
    observe(store, lambda: snapshot((550, 6)), baseline=True)
    observe(store, lambda: snapshot((550, 8)))
    events = table(store, "inbound_events")
    observe(store, lambda: snapshot((551, 9)), baseline=True, reset=True)
    assert store.state()["generation"] == 3
    assert table(store, "inbound_events") == events
    assert table(store, "inbound_snapshots")[0]["tmdb_id"] == 551


def test_no_baseline_never_fetches_or_infers_added(store):
    with pytest.raises(InboundError, match="baseline"):
        observe(store, lambda: pytest.fail("must not fetch"))
    assert not table(store, "inbound_events")


@pytest.mark.parametrize("old,new,kind,old_score,new_score", [
    ((), ((550, 8),), "added", None, 8),
    (((550, 6),), ((550, 8),), "changed", 6, 8),
    (((550, 8),), (), "removed", 8, None),
])
def test_delta_detection(store, old, new, kind, old_score, new_score):
    observe(store, lambda: snapshot(*old), baseline=True)
    result = observe(store, lambda: snapshot(*new))
    event = table(store, "inbound_events")[0]
    assert result[kind] == 1
    assert (event["event_type"], event["old_rating"], event["new_rating"]) == (kind, old_score, new_score)
    assert event["status"] in {"observed", "ignored"}
    assert not table(store, "ratings") and not table(store, "outbox")


def test_identical_poll_restart_is_idempotent(store):
    observe(store, lambda: snapshot(), baseline=True)
    observe(store, lambda: snapshot((550, 8)))
    before = table(store, "inbound_events")
    restarted = InboundStore(store.path)
    result = observe(restarted, lambda: snapshot((550, 8)))
    assert result["events"] == 0 and table(store, "inbound_events") == before


def test_same_score_timestamp_and_reserialization_do_not_emit_change(store):
    first = Snapshot((MovieRating(8, DATE, 550),))
    equivalent = Snapshot((MovieRating(8, "2026-09-26T12:00:00+02:00", 550),))
    assert first.snapshot_hash == equivalent.snapshot_hash
    observe(store, lambda: first, baseline=True)
    newer = Snapshot((replace(first.eligible[0], rated_at="2026-09-27T10:00:00Z"),))
    assert observe(store, lambda: newer)["events"] == 0
    assert not table(store, "inbound_events")


def test_multiple_deltas_deterministic(store):
    observe(store, lambda: snapshot((3, 6), (1, 7)), baseline=True)
    result = observe(store, lambda: snapshot((2, 9), (1, 8)))
    assert [e["content_key"] for e in table(store, "inbound_events")] == [
        "movie:tmdb:1", "movie:tmdb:2", "movie:tmdb:3"]
    assert (result["added"], result["changed"], result["removed"]) == (1, 1, 1)
    assert snapshot((2, 9), (1, 8)).snapshot_hash == snapshot((1, 8), (2, 9)).snapshot_hash


@pytest.mark.parametrize("failure", ["malformed", "timeout"])
def test_failed_later_page_retains_every_inbound_row(store, failure):
    observe(store, lambda: snapshot((550, 6)), baseline=True)
    previous = all_inbound(store)
    def handler(request):
        page = int(request.url.params["page"])
        if page == 1:
            return httpx.Response(200, json=[item(8)], headers=headers(1, 2, 2))
        if failure == "timeout":
            raise httpx.ReadTimeout("private-token-url")
        return httpx.Response(200, json=[{"secret": "private-body"}], headers=headers(2, 2, 2))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InboundError):
            observe(store, lambda: fetch_snapshot(SimpleNamespace(headers={}), client))
    assert all_inbound(store) == previous


def test_event_and_snapshot_publish_roll_back_together_on_crash(store):
    observe(store, lambda: snapshot((550, 6)), baseline=True)
    before = all_inbound(store)
    with store.connect() as c:
        c.execute("""CREATE TRIGGER simulate_crash BEFORE INSERT ON inbound_snapshots
                     BEGIN SELECT RAISE(ABORT, 'simulated crash'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        observe(store, lambda: snapshot((550, 8)))
    assert all_inbound(store) == before
    with store.connect() as c:
        c.execute("DROP TRIGGER simulate_crash")
    restarted = InboundStore(store.path)
    observe(restarted, lambda: snapshot((550, 8)))
    observe(restarted, lambda: snapshot((550, 8)))
    assert len(table(store, "inbound_events")) == 1


def test_concurrent_stale_generation_refused(store):
    observe(store, lambda: snapshot(), baseline=True)
    version = store.state()["generation"]
    observe(store, lambda: snapshot((550, 8)))
    before = all_inbound(store)
    with pytest.raises(InboundError, match="concurrently"):
        store.publish(snapshot((550, 9)), expected_generation=version)
    assert all_inbound(store) == before


def test_repeated_real_score_cycles_preserve_distinct_occurrences(store):
    observe(store, lambda: snapshot((550, 6)), baseline=True)
    for score in (8, 6, 8):
        observe(store, lambda: snapshot((550, score)))
    events = table(store, "inbound_events")
    assert len(events) == len({e["fingerprint"] for e in events}) == 3


def canonical(rating=8, deleted=0, revision=1):
    return {"rating": rating, "deleted": deleted, "revision": revision}


def job(status="done", score=9, action="upsert", revision=1, identity=1):
    return {"content_key": KEY, "target": "trakt", "status": status,
            "action": action, "payload_json": json.dumps({"rating": score}),
            "revision": revision, "id": identity}


def test_same_as_canonical_is_echo():
    assert classify(KEY, 8, canonical()).kind == "echo"


def test_different_score_is_future_candidate():
    decision = classify(KEY, 9, canonical())
    assert decision.kind == "candidate" and decision.future_action == "upsert"


@pytest.mark.parametrize("status", ["pending", "processing", "failed"])
def test_unsettled_outbox_deferred(status):
    assert classify(KEY, 8, canonical(), [job(status)]).kind == "defer"


def test_latest_completed_echo_and_older_job_not_selected():
    assert classify(KEY, 9, canonical(revision=2), [job(score=7), job(score=9, revision=2, identity=2)]).kind == "echo"
    assert classify(KEY, 7, canonical(revision=2), [job(score=7), job(score=9, revision=2, identity=2)]).kind == "candidate"


def test_completed_remove_can_be_an_echo():
    assert classify(KEY, None, canonical(), [job(action="remove")]).kind == "echo"


def test_malformed_completed_audit_is_deferred():
    bad = job()
    bad["payload_json"] = "private-secret-marker"
    assert classify(KEY, 9, canonical(), [bad]).kind == "defer"


@pytest.mark.parametrize("current", [None, canonical(None, 1)])
def test_baseline_era_removal_has_no_delete_candidate(current):
    decision = classify(KEY, None, current)
    assert decision.kind == "noop" and decision.future_action is None


def test_active_canonical_removal_is_future_delete_candidate():
    decision = classify(KEY, None, canonical())
    assert decision.kind == "candidate" and decision.future_action == "delete"


def test_observed_removal_no_canonical_does_not_create_tombstone(store):
    observe(store, lambda: snapshot((550, 8)), baseline=True)
    observe(store, lambda: snapshot())
    event = table(store, "inbound_events")[0]
    assert event["reason"] == "removal_no_canonical_state"
    assert event["future_action"] is None and event["status"] == "ignored"
    assert not table(store, "ratings") and not table(store, "outbox")


def test_store_context_reads_active_canonical_and_pending_job_without_mutation(store):
    hub = RatingStore(store.path)
    hub.upsert_rating(RatingWrite(media_type="movie", rating=8, tmdb_id=550), ("trakt",))
    before = {n: table(store, n) for n in ("ratings", "outbox")}
    observe(store, lambda: snapshot((550, 6)), baseline=True)
    result = observe(store, lambda: snapshot((550, 9)))
    assert result["deferred"] == 1
    assert table(store, "inbound_events")[0]["classification"] == "defer"
    assert before == {n: table(store, n) for n in before}


def test_origin_exclusion_does_not_change_normal_outbound(monkeypatch):
    targets = ("tmdb", "trakt", "simkl", "mdblist")
    monkeypatch.setenv("RATING_HUB_TARGETS", ",".join(targets))
    assert targets_excluding_source(targets, "trakt") == ("tmdb", "simkl", "mdblist")
    assert HubSettings.from_env().targets == targets


def test_inbound_settings_safe_defaults(monkeypatch):
    for key in ("TRAKT_INBOUND_ENABLED", "TRAKT_INBOUND_MEDIA_TYPES", "TRAKT_INBOUND_POLL_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    assert InboundSettings.from_env() == InboundSettings(False, ("movie",), 300)


@pytest.mark.parametrize("media", ["show", "episode", "movie,show", ""])
def test_runtime_rejects_nonmovie_configuration(monkeypatch, media):
    monkeypatch.setenv("TRAKT_INBOUND_MEDIA_TYPES", media)
    with pytest.raises(InboundError):
        InboundSettings.from_env()


@pytest.mark.parametrize("argv", [["--once"], ["--once", "--observe-only", "--reset"],
                                 ["--baseline", "--observe-only"]])
def test_cli_requires_manual_safe_mode(argv, capsys):
    assert main(argv) == 2
    assert "refused" in capsys.readouterr().out


def test_cli_baseline_and_observe_make_only_gets_and_sanitized_totals(store, monkeypatch, capsys):
    from hub.providers import registry
    real_client = httpx.Client
    calls = []
    secret = "private-user-secret"
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=[item()], headers=headers())
    def client_factory(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setenv("RATING_HUB_DB", store.path)
    monkeypatch.setattr("hub.inbound.trakt.httpx.Client", client_factory)
    monkeypatch.setattr(registry, "get_provider",
                        lambda name: SimpleNamespace(headers={"Authorization": secret},
                                                      deliver=lambda *args: pytest.fail("provider mutation")))
    assert main(["--baseline"]) == 0
    assert main(["--once", "--observe-only"]) == 0
    output = capsys.readouterr().out
    assert calls == ["GET", "GET"]
    assert "added=0" in output and "changed=0" in output and "removed=0" in output
    assert secret not in output and KEY not in output and DATE not in output
    assert not table(store, "ratings") and not table(store, "outbox")


def test_http_failure_suppresses_credentials_body_and_url(store, monkeypatch, capsys):
    from hub.providers import registry
    real_client = httpx.Client
    marker = "private-token-response-body"
    def handler(request):
        return httpx.Response(401, text=marker)
    monkeypatch.setenv("RATING_HUB_DB", store.path)
    monkeypatch.setattr("hub.inbound.trakt.httpx.Client",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(registry, "get_provider", lambda name: SimpleNamespace(headers={"Authorization": marker}))
    assert main(["--baseline"]) == 1
    output = capsys.readouterr().out
    assert marker not in output and "api.trakt.tv" not in output
    assert store.state() is None
