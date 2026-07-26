import asyncio

import app.main as main_module
from fastapi.testclient import TestClient
from app.models import SocialDraftBatch, SocialDraftRequest


class FakeStoreSyncer:
    def __init__(self, status):
        self.status = status
        self.last_status = {
            "source": "ebay-store-page",
            "status": "not_run",
            "imported": 0,
            "message": "Store page sync has not run yet.",
            "last_attempt_at": None,
        }
        self.called = False

    async def sync(self):
        self.called = True
        self.last_status = self.status
        return self.status


def test_social_drafts_refreshes_ebay_api_before_generation(monkeypatch):
    order = []
    api_status = {
        "source": "ebay-api",
        "status": "ok",
        "imported": 3,
        "message": "Imported 3 active eBay API listings.",
        "last_attempt_at": "2026-07-04T12:00:00+00:00",
    }
    fake_store_syncer = FakeStoreSyncer(
        {
            "source": "ebay-store-page",
            "status": "not_run",
            "imported": 0,
            "message": "Store page sync has not run yet.",
            "last_attempt_at": None,
        }
    )

    async def fake_sync_ebay_api_inventory():
        order.append("sync")
        return api_status

    async def fake_create_social_drafts(request):
        order.append("drafts")
        return SocialDraftBatch(campaign_name="Daily posts", posts=[], notes="Generated posts.")

    monkeypatch.setattr(main_module.settings, "sync_inventory_before_social_posts", True)
    monkeypatch.setattr(main_module, "_sync_ebay_api_inventory", fake_sync_ebay_api_inventory)
    monkeypatch.setattr(main_module, "store_syncer", fake_store_syncer)
    monkeypatch.setattr(main_module, "create_social_drafts", fake_create_social_drafts)

    batch, inventory_refresh = asyncio.run(
        main_module._create_social_drafts_with_inventory_refresh(SocialDraftRequest(brand_name="Horizon Wireless"))
    )

    assert order == ["sync", "drafts"]
    assert fake_store_syncer.called is False
    assert inventory_refresh["status"] == "ok"
    assert inventory_refresh["ebay_sync"] == api_status
    assert "Inventory refreshed from the eBay API" in batch.notes


def test_promote_all_inventory_requires_fresh_ebay_api_sync(monkeypatch):
    order = []
    inventory_refresh = {
        "source": "pre-social-refresh",
        "status": "fallback_ok",
        "message": "eBay API refresh did not complete; inventory refreshed from fallback.",
        "ebay_sync": {
            "status": "failed",
            "message": "eBay API failed.",
            "imported": 0,
            "last_attempt_at": "2026-07-04T12:00:00+00:00",
        },
        "store_sync": {
            "status": "ok",
            "message": "Imported 2 public eBay listings.",
            "imported": 2,
            "last_attempt_at": "2026-07-04T12:01:00+00:00",
        },
    }

    async def fake_refresh_inventory_for_social_posts():
        order.append("sync")
        return inventory_refresh

    async def fake_create_social_drafts(request):
        order.append("drafts")
        return SocialDraftBatch(campaign_name="Daily posts", posts=[], notes="Generated posts.")

    monkeypatch.setattr(main_module, "_refresh_inventory_for_social_posts", fake_refresh_inventory_for_social_posts)
    monkeypatch.setattr(main_module, "create_social_drafts", fake_create_social_drafts)

    batch, returned_refresh = asyncio.run(
        main_module._create_social_drafts_with_inventory_refresh(
            SocialDraftRequest(promote_all_inventory=True, brand_name="Horizon Wireless")
        )
    )

    assert order == ["sync"]
    assert returned_refresh == inventory_refresh
    assert batch.posts == []
    assert batch.metricool_payloads == []
    assert "latest eBay API inventory was not confirmed" in batch.notes


def test_inventory_schedule_webhook_forces_one_history_aware_listing(monkeypatch):
    captured_requests = []
    inventory_refresh = {
        "source": "pre-social-refresh",
        "status": "ok",
        "message": "Inventory refreshed.",
        "ebay_sync": {"status": "ok", "imported": 75},
        "store_sync": {"status": "not_run", "imported": 0},
    }

    async def fake_create_social_drafts_with_inventory_refresh(request):
        captured_requests.append(request)
        return (
            SocialDraftBatch(campaign_name="Daily all-inventory promotion", posts=[], notes="No eligible items."),
            inventory_refresh,
        )

    async def fake_schedule_metricool_payloads(payloads, settings):
        assert payloads == []
        return []

    monkeypatch.setattr(main_module.settings, "webhook_shared_secret", None)
    monkeypatch.setattr(
        main_module,
        "_create_social_drafts_with_inventory_refresh",
        fake_create_social_drafts_with_inventory_refresh,
    )
    monkeypatch.setattr(main_module, "schedule_metricool_payloads", fake_schedule_metricool_payloads)

    response = TestClient(main_module.app).post(
        "/webhooks/metricool/schedule-inventory",
        json={"max_products_per_run": 75, "ignore_recent_history": True},
    )

    assert response.status_code == 200
    assert len(captured_requests) == 1
    assert captured_requests[0].max_products_per_run == 1
    assert captured_requests[0].ignore_recent_history is False


def test_social_drafts_falls_back_to_store_page_when_ebay_api_fails(monkeypatch):
    api_status = {
        "source": "ebay-api",
        "status": "failed",
        "imported": 0,
        "message": "eBay API sync failed with HTTPStatusError.",
        "last_attempt_at": "2026-07-04T12:00:00+00:00",
    }
    store_status = {
        "source": "ebay-store-page",
        "status": "ok",
        "imported": 2,
        "message": "Imported 2 public eBay listings.",
        "last_attempt_at": "2026-07-04T12:01:00+00:00",
    }
    fake_store_syncer = FakeStoreSyncer(store_status)

    async def fake_sync_ebay_api_inventory():
        return api_status

    monkeypatch.setattr(main_module.settings, "sync_inventory_before_social_posts", True)
    monkeypatch.setattr(main_module, "_sync_ebay_api_inventory", fake_sync_ebay_api_inventory)
    monkeypatch.setattr(main_module, "store_syncer", fake_store_syncer)

    inventory_refresh = asyncio.run(main_module._refresh_inventory_for_social_posts())

    assert fake_store_syncer.called is True
    assert inventory_refresh["status"] == "fallback_ok"
    assert inventory_refresh["ebay_sync"] == api_status
    assert inventory_refresh["store_sync"] == store_status


def test_inventory_refresh_zapier_fields_are_flattened():
    fields = main_module._inventory_refresh_zapier_fields(
        {
            "source": "pre-social-refresh",
            "status": "fallback_ok",
            "message": "Used fallback.",
            "ebay_sync": {
                "status": "failed",
                "message": "eBay API failed.",
                "imported": 0,
                "last_attempt_at": "2026-07-04T12:00:00+00:00",
            },
            "store_sync": {
                "status": "ok",
                "message": "Imported 2 public eBay listings.",
                "imported": 2,
                "last_attempt_at": "2026-07-04T12:01:00+00:00",
            },
        }
    )

    assert fields["inventory_refresh_status"] == "fallback_ok"
    assert fields["inventory_refresh_message"] == "Used fallback."
    assert fields["ebay_sync_status"] == "failed"
    assert fields["store_sync_status"] == "ok"
    assert fields["store_sync_imported"] == 2
