from __future__ import annotations

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_ENDPOINT = "https://horizon-ai-agents.onrender.com/webhooks/metricool/schedule-inventory"


def main() -> int:
    request = Request(
        _endpoint(),
        data=json.dumps({"max_products_per_run": 1}).encode("utf-8"),
        headers=_headers(),
        method="POST",
    )
    try:
        with urlopen(request, timeout=_timeout_seconds()) as response:
            response_body = response.read().decode("utf-8")
        print(response_body)
        return _result_exit_code(response_body)
    except HTTPError as exc:
        print(f"Inventory rotation endpoint failed with HTTP {exc.code} {exc.reason}", file=sys.stderr)
        body = exc.read().decode("utf-8", errors="replace").strip()
        if body:
            print(body, file=sys.stderr)
        return 1
    except URLError as exc:
        print(f"Inventory rotation endpoint request failed: {exc}", file=sys.stderr)
        return 1


def _endpoint() -> str:
    return os.getenv("INVENTORY_SCHEDULE_ENDPOINT", DEFAULT_ENDPOINT).strip() or DEFAULT_ENDPOINT


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    secret = os.getenv("WEBHOOK_SHARED_SECRET", "").strip()
    if secret:
        headers["x-horizon-secret"] = secret
    return headers


def _result_exit_code(response_body: str) -> int:
    try:
        payload = json.loads(response_body)
    except (json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    failed = payload.get("failed", 0)
    try:
        failed_count = int(failed)
    except (TypeError, ValueError):
        failed_count = 0
    if failed_count > 0 or payload.get("status") == "partial":
        print(
            f"Metricool rejected {failed_count or 'one or more'} inventory rotation post(s).",
            file=sys.stderr,
        )
        return 1
    return 0


def _timeout_seconds() -> int:
    try:
        return int(os.getenv("INVENTORY_SCHEDULE_TIMEOUT", "900"))
    except ValueError:
        return 900


if __name__ == "__main__":
    raise SystemExit(main())
