from __future__ import annotations

import os
from datetime import datetime, timezone

from scripts import post_render_daily_report_email, post_render_inventory_rotation


def main() -> int:
    inventory_exit_code = post_render_inventory_rotation.main()
    report_exit_code = post_render_daily_report_email.main() if _should_send_report() else 0
    return inventory_exit_code or report_exit_code


def _should_send_report(now: datetime | None = None) -> bool:
    current_time = now or datetime.now(timezone.utc)
    try:
        report_hour = int(os.getenv("REPORT_EMAIL_UTC_HOUR", "15"))
    except ValueError:
        report_hour = 15
    return current_time.astimezone(timezone.utc).hour == max(0, min(report_hour, 23))


if __name__ == "__main__":
    raise SystemExit(main())
