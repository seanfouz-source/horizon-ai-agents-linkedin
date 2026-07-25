from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient


def test_ebay_oauth_start_redirects_with_production_keyset(monkeypatch):
    import app.main as main_module

    monkeypatch.setattr(main_module.settings, "ebay_client_id", "client-id")
    monkeypatch.setattr(main_module.settings, "ebay_client_secret", "client-secret")
    monkeypatch.setattr(main_module.settings, "ebay_oauth_redirect_name", "redirect-name")
    monkeypatch.setattr(
        main_module.settings,
        "ebay_oauth_scopes",
        "https://api.ebay.com/oauth/api_scope https://api.ebay.com/oauth/api_scope/sell.inventory",
    )

    response = TestClient(main_module.app).get("/ebay/oauth/start", follow_redirects=False)

    assert response.status_code == 302
    redirect_url = urlparse(response.headers["location"])
    query = parse_qs(redirect_url.query)
    assert redirect_url.netloc == "auth.ebay.com"
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == ["redirect-name"]
    assert query["response_type"] == ["code"]
    assert query["scope"] == [
        "https://api.ebay.com/oauth/api_scope https://api.ebay.com/oauth/api_scope/sell.inventory"
    ]
    assert "%20" in response.headers["location"]
    assert "+" not in response.headers["location"]
    assert main_module._verify_ebay_oauth_state(query["state"][0]) is True


def test_ebay_oauth_callback_exchanges_code_and_returns_refresh_token(monkeypatch):
    import app.main as main_module

    monkeypatch.setattr(main_module.settings, "ebay_client_id", "client-id")
    monkeypatch.setattr(main_module.settings, "ebay_client_secret", "client-secret")
    monkeypatch.setattr(main_module.settings, "ebay_oauth_redirect_name", "redirect-name")
    monkeypatch.setattr(
        main_module,
        "_exchange_ebay_authorization_code",
        lambda code: {"refresh_token": f"refresh-for-{code}"},
    )

    state = main_module._sign_ebay_oauth_state()
    response = TestClient(main_module.app).get(
        "/ebay/oauth/callback",
        params={"code": "auth-code", "state": state},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "EBAY_REFRESH_TOKEN=refresh-for-auth-code" in response.text


def test_ebay_oauth_callback_rejects_invalid_state(monkeypatch):
    import app.main as main_module

    monkeypatch.setattr(main_module.settings, "ebay_client_id", "client-id")
    monkeypatch.setattr(main_module.settings, "ebay_client_secret", "client-secret")
    monkeypatch.setattr(main_module.settings, "ebay_oauth_redirect_name", "redirect-name")

    response = TestClient(main_module.app).get(
        "/ebay/oauth/callback",
        params={"code": "auth-code", "state": "invalid"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid or expired eBay OAuth state."
