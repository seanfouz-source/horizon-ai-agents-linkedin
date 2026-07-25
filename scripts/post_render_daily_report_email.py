from __future__ import annotations

import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_ENDPOINT = "https://horizon-ai-agents.onrender.com/reports/daily/email"


def main() -> int:
    request = Request(_endpoint(), headers=_headers(), method="POST")
    try:
        with urlopen(request, timeout=_timeout_seconds()) as response:
            print(response.read().decode("utf-8"))
        return 0
    except HTTPError as exc:
        print(f"Email endpoint failed with HTTP {exc.code} {exc.reason}", file=sys.stderr)
        body = exc.read().decode("utf-8", errors="replace").strip()
        if body:
            print(body, file=sys.stderr)
        return 1
    except URLError as exc:
        print(f"Email endpoint request failed: {exc}", file=sys.stderr)
        return 1


def _endpoint() -> str:
    return os.getenv("REPORT_EMAIL_ENDPOINT", DEFAULT_ENDPOINT).strip() or DEFAULT_ENDPOINT


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    secret = os.getenv("WEBHOOK_SHARED_SECRET", "").strip()
    if secret:
        headers["x-horizon-secret"] = secret
    return headers


def _timeout_seconds() -> int:
    try:
        return int(os.getenv("REPORT_EMAIL_TIMEOUT", "120"))
    except ValueError:
        return 120


if __name__ == "__main__":
    raise SystemExit(main())
