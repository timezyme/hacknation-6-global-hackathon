"""Read-and-review smoke for the deployed Trust Desk app.

Exercises the exact surfaces the one-minute demo touches: health, options, results
(ranked and unranked), one receipt with attempt trails, the measurements endpoint,
and a review write/read round trip. Prints one sanitized JSON line; exits non-zero
on the first failed expectation.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from databricks.sdk import WorkspaceClient

REQUEST_TIMEOUT_SECONDS = 30


def request_json(
    url: str,
    path: str,
    headers: dict[str, str],
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    request_headers = {**headers, "Accept": "application/json"}
    data = None
    if body is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    request = Request(f"{url.rstrip('/')}{path}", data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.status, json.loads(response.read().decode() or "null")
    except HTTPError as error:
        return error.code, json.loads(error.read().decode() or "null")


def wait_for_health(url: str, headers: dict[str, str], timeout_seconds: int = 420) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            status, _ = request_json(url, "/api/health", headers)
            if status == 200:
                return
        except (URLError, TimeoutError, json.JSONDecodeError):
            pass
        if time.monotonic() > deadline:
            raise RuntimeError("health endpoint did not return 200 in time")
        time.sleep(10)


def run_smoke(url: str, headers: dict[str, str]) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    wait_for_health(url, headers)
    checks["health_200"] = True

    status, options = request_json(url, "/api/options", headers)
    assert status == 200 and options["capabilities"], "options failed"
    capability = options["capabilities"][0]
    region = options["regions_by_capability"][capability][0]
    checks["options"] = {"capabilities": len(options["capabilities"]), "model_requests": options["model_requests"]}

    status, results = request_json(
        url, f"/api/results?capability={capability}&region={region.replace(' ', '%20')}", headers
    )
    assert status == 200 and "ranking_rule" in results, "results failed"
    ranked = results["facilities"]
    checks["results"] = {"ranked": len(ranked), "unranked": len(results.get("unranked", []))}
    assert ranked or results.get("unranked"), "no rows at all for the first region"

    target = ranked[0] if ranked else results["unranked"][0]
    status, receipt = request_json(
        url, f"/api/receipts/{target['record_key']}?capability={capability}", headers
    )
    assert status == 200 and isinstance(receipt["receipt"], list), "receipt failed"
    decided = [item for item in receipt["receipt"] if item.get("outcome") == "decision"]
    checks["receipt"] = {
        "items": len(receipt["receipt"]),
        "decided": len(decided),
        "has_attempt_trail": any(len(item.get("attempts") or []) >= 2 for item in receipt["receipt"]),
        "has_similar_context": bool(receipt.get("similar")),
        "has_referee": any(item.get("referee") for item in decided),
    }

    status, methods = request_json(url, "/api/methods", headers)
    assert status == 200 and "ranking_rule" in methods, "methods failed"
    checks["methods"] = {"pilot": methods.get("pilot") is not None, "referee": methods.get("referee") is not None}

    note = "Smoke check note; safe to ignore."
    status, _saved = request_json(
        url,
        "/api/reviews",
        headers,
        method="POST",
        body={
            "record_key": target["record_key"],
            "capability": capability,
            "decision": "overridden",
            "note": note,
        },
    )
    assert status == 201, f"review write failed with {status}"
    status, read_back = request_json(
        url, f"/api/reviews/{target['record_key']}?capability={capability}", headers
    )
    assert status == 200 and read_back["note"] == note, "review read-back failed"
    checks["review_round_trip"] = True
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="trustdesk-spike")
    parser.add_argument("--app-name", default="trustdesk-spike")
    args = parser.parse_args()
    workspace = WorkspaceClient(profile=args.profile)
    app = workspace.apps.get(args.app_name)
    if not app.url:
        print(json.dumps({"status": "fail", "reason": "app has no url"}))
        return 1
    headers = workspace.config.authenticate()
    try:
        checks = run_smoke(app.url, headers)
    except (AssertionError, RuntimeError, URLError) as error:
        print(json.dumps({"status": "fail", "reason": str(error)[:200]}, sort_keys=True))
        return 1
    print(json.dumps({"status": "pass", "checks": checks}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
