# TMDb and Trakt settle verification

Status: TMDb and Trakt verification are integrated into the explicitly gated `scripts/live_pilot.py` command. The
API and worker never invoke the pilot. Deployment of this code does not authorize
a provider write; every live pilot still requires `--confirm-live-write` and a
separate explicit operational authorization.

## Confirmed verification-design flaw

The earlier pilot accepted one immediate matching read after an upsert or
rollback. A stale or temporarily matching observation can be accepted and later
contradicted by another read. The offline regression reproduces a failed upsert
verification, an immediately matching rollback observation, and a later different
rating. This establishes a verification-design flaw; it does not establish the
cause of TMDb's inconsistent observations or a TMDb service defect.

The earlier diagnostic message was emitted only when the write/verification loop
records a failure and the rollback block completes without raising. Its exact
code-path implications are:

- The original state was read and passed the safety check.
- The upsert of 7 was attempted. The provider call may have raised, so acceptance
  by the remote service is not guaranteed.
- Verification of 7 may have failed, raised, or succeeded. Success implies the
  upsert of 9 was then attempted. The message alone does not distinguish these.
- An upsert of 9 is optional. If attempted, its delivery, read, safety check, or
  rating comparison failed; full successful verification of 9 would leave no
  loop failure and cannot produce this message.
- Rollback delivery returned without raising. It was DELETE only if the original
  rating read inside the pilot was None; otherwise it was restoration by upsert.
- The immediate rollback read equaled the original rating and passed safety.
  It saw None only if that internal original read was None. An external baseline
  does not prove the value of a later internal read.

No intermediate event list is printed when `pilot()` raises. Therefore this
message cannot reconstruct which intermediate verification event occurred.

## Integrated algorithm

`wait_for_state` takes a state reader, optional second rating reader, a policy,
and injectable monotonic clock/sleep. Readers receive the remaining I/O budget.
It polls read-only, requires consecutive matching observations and a minimum
matching duration, and resets both on read errors or disagreement. Watchlist
and library safety failures abort immediately. For Trakt rollback of an existing
rating, every matching sample must also preserve the exact original rated_at value.

Ordinary upsert policy: 60-second budget, 2-second polling interval, at least
three consecutive matches spanning at least four seconds. It may return early.
Rollback policy: 120-second budget, 5-second polling interval, at least five
consecutive matches spanning at least 20 seconds. It consumes the entire window;
an early matching streak cannot produce early success. At window end the latest
streak must still meet both requirements. The last observation can precede the
end by up to one polling interval. Errors or a rebound reset the streak.

These are conservative, configurable operational budgets, not measured TMDb or
Trakt propagation constants or published service guarantees. The two-minute rollback
budget follows the bounded investigation horizon; slower polling keeps request
volume bounded while covering a longer period than upsert verification. The
matching durations follow directly from the sample counts and intervals. Review
these defaults against observed propagation delays before a corrective
operation; a timeout is uncertainty and must stop further writes, never trigger another write retry.

For TMDb rollback, the secondary reader is mandatory. The same provider object
and session must read `account_states` and authenticated account `rated/movies`.
The pilot resolves `/account` once, read-only, before any write and supplies the
numeric identity to the helper, which checks all rated pages. It rejects
incomplete or changing pagination, malformed values, duplicate target entries,
and scan limits. Missing movies mean absence only after a complete scan. Both
surfaces must agree on the expected original rating for every matching sample.

For Trakt, the authenticated /users/me/ratings/movies reader requests at
most 250 items per page and scans every page before accepting absence. It validates
the pagination headers and stable totals, item counts, target IDs, rating values and
rated_at; malformed responses and duplicate target entries fail closed. Trakt has
one authoritative personal-rating surface, so rollback uses the entire 120-second
window. An originally unrated movie must remain absent, while an existing rating
must match both its score and original timestamp throughout the required final
streak.

Readers must enforce the supplied remaining deadline on their I/O. The TMDb
secondary reader clamps each request timeout to the remaining budget and rejects
late responses. HTTPX request timeouts apply per I/O phase; they are not a hard
wall-clock cancellation mechanism. Deadline checks prevent accepting late
results, but blocking I/O can overrun the observation budget. A hard elapsed-time limit would require cancellation-capable orchestration
before a corrective operation.

Finite observation cannot prove permanent convergence. Even full-window agreement
is evidence limited to the sampled window. A separate post-window read is useful,
and any subsequent contradiction must invalidate restoration success.

## Cache and identity review

The current `read_state()` calls a plain HTTPX client directly against TMDb's
HTTPS API. There is no Rating Hub response cache or reverse-proxy route in this
path. HTTPX connection pooling is not an application response cache. The pilot
uses one provider object/session for its reads and writes. External observations
use one provider object/session for both surfaces. Identity continuity across
separate processes is not established by historical credential snapshots.
Provider-side caching or replication behavior remains unproven.

References: [TMDb account rated movies](https://developer.themoviedb.org/reference/account-rated-movies),
[HTTPX clients](https://www.python-httpx.org/advanced/clients/),
[HTTPX proxy environment](https://www.python-httpx.org/environment_variables/).

## Review and corrective operation

Regression tests use fake time, mock transports and the suite's socket/DNS block.
They cover delayed writes, transient and eventually stable deletes, conflicting
surfaces, late rebound, timeout, read failure, safety guards, pagination and
sanitized HTTP failures. They perform no live writes.

Do not run the existing pilot to correct an unintended rating: it also attempts
new ratings. A separate explicitly authorized corrective operation should issue
one intended removal, then perform bounded read-only dual-surface verification
with the reviewed rollback policy and an independent subsequent read. It must
stop and report uncertainty if verification fails; another DELETE must require
separate authorization. Watchlist/favorite and canonical counts must be checked
before and afterward. No such correction is part of this integration or
deployment.
