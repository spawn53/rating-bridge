from __future__ import annotations

import os
import sys
import time

import httpx

BASE = "https://api.simkl.com"


def main() -> int:
    client_id = (os.getenv("SIMKL_CLIENT_ID") or "").strip()
    if not client_id:
        print("Set SIMKL_CLIENT_ID first.", file=sys.stderr)
        return 2

    with httpx.Client(timeout=30.0) as client:
        response = client.get(f"{BASE}/oauth/pin", params={"client_id": client_id, "redirect": ""})
        response.raise_for_status()
        data = response.json()
        user_code = data["user_code"]
        url = data["url"]
        interval = max(3, int(data.get("interval", 5)))
        expires_in = int(data.get("expires_in", 900))

        print(f"Open: {url}")
        print(f"Enter code: {user_code}")
        print("Waiting for authorization...")

        deadline = time.time() + expires_in
        while time.time() < deadline:
            time.sleep(interval)
            poll = client.get(f"{BASE}/oauth/pin/{user_code}", params={"client_id": client_id})
            if poll.status_code != 200:
                poll.raise_for_status()
            result = poll.json()
            if result.get("result") == "OK" and result.get("access_token"):
                print("\nSIMKL_ACCESS_TOKEN=")
                print(result["access_token"])
                print("\nStore this value as a GitHub Actions secret; do not commit it.")
                return 0
        print("Authorization expired; run the script again.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
