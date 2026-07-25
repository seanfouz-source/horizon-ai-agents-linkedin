from email.message import EmailMessage
from io import BytesIO
import json
from urllib.error import HTTPError

import pytest

import app.report_email as report_email
import scripts.post_render_hourly_tasks as hourly_tasks
import scripts.post_render_inventory_rotation as inventory_rotation
from app.report_email import send_gmail_message
from scripts.send_daily_report_email import build_message, build_report_url, split_addresses


def test_build_report_url_uses_default_endpoint_without_date(monkeypatch):
    monkeypatch.delenv("REPORT_BASE_URL", raising=False)
    monkeypatch.delenv("REPORT_DATE", raising=False)

    assert build_report_url() == "https://horizon-ai-agents.onrender.com/webhooks/zapier/daily-report"


def test_build_report_url_adds_optional_date(monkeypatch):
    monkeypatch.setenv("REPORT_BASE_URL", "https://example.com/")
    monkeypatch.setenv("REPORT_DATE", "2026-06-14")

    assert build_report_url() == "https://example.com/webhooks/zapier/daily-report?date=2026-06-14"


def test_hourly_inventory_rotation_posts_one_item_directly_to_render(monkeypatch):
    requests = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return b'{"scheduled":1}'

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(inventory_rotation, "urlopen", fake_urlopen)
    monkeypatch.setenv("INVENTORY_SCHEDULE_ENDPOINT", "https://render.example/schedule-inventory")
    monkeypatch.setenv("WEBHOOK_SHARED_SECRET", "shared-secret")

    assert inventory_rotation.main() == 0

    request, timeout = requests[0]
    assert request.full_url == "https://render.example/schedule-inventory"
    assert request.method == "POST"
    assert json.loads(request.data) == {"max_products_per_run": 1}
    assert request.headers["X-horizon-secret"] == "shared-secret"
    assert timeout == 900


def test_hourly_inventory_rotation_fails_job_when_metricool_rejects_image(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return b'{"status":"partial","scheduled":0,"failed":1}'

    monkeypatch.setattr(inventory_rotation, "urlopen", lambda request, timeout: FakeResponse())

    assert inventory_rotation.main() == 1


def test_hourly_render_tasks_refill_inventory_and_send_report_at_configured_hour(monkeypatch):
    calls = []
    monkeypatch.setattr(hourly_tasks.post_render_inventory_rotation, "main", lambda: calls.append("inventory") or 0)
    monkeypatch.setattr(hourly_tasks.post_render_daily_report_email, "main", lambda: calls.append("report") or 0)
    monkeypatch.setattr(hourly_tasks, "_should_send_report", lambda: True)

    assert hourly_tasks.main() == 0
    assert calls == ["inventory", "report"]


def test_hourly_render_tasks_skip_report_outside_configured_hour(monkeypatch):
    calls = []
    monkeypatch.setattr(hourly_tasks.post_render_inventory_rotation, "main", lambda: calls.append("inventory") or 0)
    monkeypatch.setattr(hourly_tasks.post_render_daily_report_email, "main", lambda: calls.append("report") or 0)
    monkeypatch.setattr(hourly_tasks, "_should_send_report", lambda: False)

    assert hourly_tasks.main() == 0
    assert calls == ["inventory"]


def test_build_message_attaches_pdf(monkeypatch):
    monkeypatch.setenv("REPORT_EMAIL_TO", "sean.fouz@gmail.com; horizonwirelesstx@gmail.com")
    monkeypatch.setenv("REPORT_EMAIL_FROM", "reports@example.com")

    message = build_message(
        {
            "subject": "Daily report",
            "email_body": "Report body",
            "attachment_filename": "report.pdf",
        },
        b"%PDF-1.4",
    )

    assert isinstance(message, EmailMessage)
    assert message["To"] == "sean.fouz@gmail.com, horizonwirelesstx@gmail.com"
    assert message["Subject"] == "Daily report"
    assert message["From"] == "Horizon AI Agents <reports@example.com>"
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "report.pdf"
    assert attachments[0].get_content_type() == "application/pdf"


def test_split_addresses_requires_value():
    with pytest.raises(RuntimeError, match="at least one recipient"):
        split_addresses(" , ; ")


def test_send_gmail_message_refreshes_token_and_sends_raw_message(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, payload: bytes = b"{}"):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return self.payload

    def fake_urlopen(request, timeout=60):
        calls.append(request)
        if request.full_url == "https://oauth2.googleapis.com/token":
            return FakeResponse(json.dumps({"access_token": "ya29.test"}).encode("utf-8"))
        if request.full_url == "https://gmail.googleapis.com/gmail/v1/users/me/messages/send":
            return FakeResponse()
        raise AssertionError(request.full_url)

    monkeypatch.setattr(report_email, "urlopen", fake_urlopen)

    message = EmailMessage()
    message["To"] = "owner@example.com"
    message["From"] = "Horizon AI Agents <old@example.com>"
    message["Subject"] = "Daily report"
    message.set_content("Report body")

    send_gmail_message(
        message,
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="refresh-token",
        sender="sean.fouz@gmail.com",
    )

    assert len(calls) == 2
    token_request, gmail_request = calls
    assert b"grant_type=refresh_token" in token_request.data
    assert gmail_request.headers["Authorization"] == "Bearer ya29.test"
    body = json.loads(gmail_request.data.decode("utf-8"))
    assert body["raw"]
    assert message["From"] == "Horizon AI Agents <sean.fouz@gmail.com>"


def test_gmail_oauth_credentials_loads_google_credentials_file(tmp_path):
    credentials_file = tmp_path / "client_secret_google.json"
    credentials_file.write_text(
        json.dumps({"web": {"client_id": "file-client-id", "client_secret": "file-client-secret"}}),
        encoding="utf-8",
    )

    credentials = report_email.gmail_oauth_credentials(credentials_file=credentials_file)

    assert credentials.client_id == "file-client-id"
    assert credentials.client_secret == "file-client-secret"


def test_gmail_oauth_credentials_file_takes_precedence_over_stale_env(monkeypatch, tmp_path):
    credentials_file = tmp_path / "client_secret_google.json"
    credentials_file.write_text(
        json.dumps({"web": {"client_id": "file-client-id", "client_secret": "file-client-secret"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("GMAIL_CLIENT_ID", "stale-client-id")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "stale-client-secret")

    credentials = report_email.gmail_oauth_credentials(credentials_file=credentials_file)

    assert credentials.client_id == "file-client-id"
    assert credentials.client_secret == "file-client-secret"


def test_gmail_oauth_credentials_uses_known_render_secret_file(monkeypatch, tmp_path):
    credentials_file = tmp_path / "client_secret_google.json"
    credentials_file.write_text(
        json.dumps({"web": {"client_id": "render-file-client-id", "client_secret": "render-file-client-secret"}}),
        encoding="utf-8",
    )
    monkeypatch.delenv("GMAIL_CLIENT_CREDENTIALS_FILE", raising=False)
    monkeypatch.setattr(report_email, "DEFAULT_RENDER_GMAIL_CREDENTIALS_FILE", credentials_file)

    def fail_discovery():
        raise AssertionError("discovery should not run when the known Render secret file exists")

    monkeypatch.setattr(report_email, "_discover_google_oauth_credentials_file", fail_discovery)

    credentials = report_email.gmail_oauth_credentials()

    assert credentials.client_id == "render-file-client-id"
    assert credentials.client_secret == "render-file-client-secret"


def test_gmail_access_token_includes_google_error_body(monkeypatch):
    def fake_urlopen(request, timeout=60):
        raise HTTPError(
            request.full_url,
            400,
            "Bad Request",
            {},
            BytesIO(
                json.dumps({"error": "invalid_grant", "error_description": "Token has been expired or revoked"}).encode(
                    "utf-8"
                )
            ),
        )

    monkeypatch.setattr(report_email, "urlopen", fake_urlopen)

    with pytest.raises(report_email.ReportEmailError, match="invalid_grant - Token has been expired or revoked"):
        report_email.gmail_access_token(
            client_id="client-id",
            client_secret="client-secret",
            refresh_token="refresh-token",
        )


def test_gmail_access_token_ignores_legacy_refresh_token_env(monkeypatch):
    requests = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return json.dumps({"access_token": "ya29.test"}).encode("utf-8")

    def fake_urlopen(request, timeout=60):
        requests.append(request)
        return FakeResponse()

    monkeypatch.setattr(report_email, "urlopen", fake_urlopen)
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "old-token")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN_CURRENT", "fresh-token")

    assert report_email.gmail_access_token(client_id="client-id", client_secret="client-secret") == "ya29.test"

    assert len(requests) == 1
    assert b"refresh_token=fresh-token" in requests[0].data
    assert b"old-token" not in requests[0].data


def test_gmail_access_token_accepts_copied_env_assignment(monkeypatch):
    requests = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return json.dumps({"access_token": "ya29.test"}).encode("utf-8")

    def fake_urlopen(request, timeout=60):
        requests.append(request)
        return FakeResponse()

    monkeypatch.setattr(report_email, "urlopen", fake_urlopen)
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN_CURRENT", "GMAIL_REFRESH_TOKEN_CURRENT=fresh-token")

    assert report_email.gmail_access_token(client_id="client-id", client_secret="client-secret") == "ya29.test"

    assert len(requests) == 1
    assert b"refresh_token=fresh-token" in requests[0].data
    assert b"GMAIL_REFRESH_TOKEN_CURRENT" not in requests[0].data


def test_send_gmail_message_includes_google_send_error_body(monkeypatch):
    calls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return json.dumps({"access_token": "ya29.test"}).encode("utf-8")

    def fake_urlopen(request, timeout=60):
        calls.append(request)
        if request.full_url == "https://oauth2.googleapis.com/token":
            return FakeResponse()
        raise HTTPError(
            request.full_url,
            403,
            "Forbidden",
            {},
            BytesIO(
                json.dumps({"error": {"status": "PERMISSION_DENIED", "message": "Gmail API has not been used"}}).encode(
                    "utf-8"
                )
            ),
        )

    monkeypatch.setattr(report_email, "urlopen", fake_urlopen)

    message = EmailMessage()
    message["To"] = "owner@example.com"
    message["From"] = "Horizon AI Agents <old@example.com>"
    message["Subject"] = "Daily report"
    message.set_content("Report body")

    with pytest.raises(report_email.ReportEmailError, match="PERMISSION_DENIED - Gmail API has not been used"):
        send_gmail_message(
            message,
            client_id="client-id",
            client_secret="client-secret",
            refresh_token="refresh-token",
            sender="sean.fouz@gmail.com",
        )

    assert len(calls) == 2
