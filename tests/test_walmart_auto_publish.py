import asyncio

import app.main as main_module
from app.inventory import InventoryRepository
from app.models import InventoryItem, WalmartAutoPublishRequest


class FakeWalmartClient:
    configured = True

    def __init__(self):
        self.offer_payloads = []
        self.inventory_payloads = []

    async def list_published_items(self, *, limit=1000):
        return []

    async def search_catalog(self, product_id_type, product_id, *, response_format="SPEC"):
        return {"status": "matched", "matched": True, "feed_type": "MP_ITEM_MATCH"}

    async def submit_offer_match_feed(self, payload):
        self.offer_payloads.append(payload)
        return {"status": "submitted", "feed_id": "OFFER-AUTO"}

    async def submit_inventory_feed(self, payload):
        self.inventory_payloads.append(payload)
        return {"status": "submitted", "feed_id": "INVENTORY-AUTO"}


def test_auto_publish_previews_submits_current_inventory_and_does_not_repeat(
    monkeypatch, tmp_path
):
    repository = InventoryRepository(tmp_path / "inventory.db")
    item = InventoryItem(
        sku="EBAY-AUTO-1",
        ebay_item_id="123",
        title="Samsung Galaxy S25 128GB Unlocked",
        condition="Open box",
        price=100,
        quantity=3,
        image_url="https://example.com/phone.jpg",
        category="Cell Phones & Smartphones",
        listing_status="ACTIVE",
        source="ebay-trading-api",
        item_specifics={
            "Brand": "Samsung",
            "Model": "Samsung Galaxy S25",
            "Storage": "128 GB",
            "Network": "Unlocked",
            "UPC": "887276900123",
            "Shipping Weight": "1 lb",
        },
    )
    repository.upsert_items([item])
    repository.upsert_walmart_drafts(
        [
            {
                "sku": item.sku,
                "ebay_item_id": item.ebay_item_id,
                "source_snapshot": item.model_dump(mode="json"),
                "prepared_listing": {
                    "product_identifier": {"type": "UPC", "value": "887276900123"},
                    "shipping_weight_lbs": 1.0,
                },
                "catalog_query": item.title,
                "catalog_candidates": [],
                "catalog_status": "candidates_found",
                "status": "draft_verified_match",
                "missing_fields": [],
            }
        ]
    )
    client = FakeWalmartClient()

    async def fake_generate(_request):
        return {"generated": 1}

    monkeypatch.setattr(main_module, "repository", repository)
    monkeypatch.setattr(main_module, "walmart_client", client)
    monkeypatch.setattr(main_module, "_generate_walmart_drafts", fake_generate)
    monkeypatch.setattr(main_module.settings, "walmart_auto_publish_excluded_terms", "don toliver,don oliver,otterbox")

    preview = asyncio.run(
        main_module._run_walmart_auto_publish_once(
            WalmartAutoPublishRequest(sync_ebay_first=False, confirm=False)
        )
    )
    submitted = asyncio.run(
        main_module._run_walmart_auto_publish_once(
            WalmartAutoPublishRequest(sync_ebay_first=False, confirm=True)
        )
    )
    repeated = asyncio.run(
        main_module._run_walmart_auto_publish_once(
            WalmartAutoPublishRequest(sync_ebay_first=False, confirm=True)
        )
    )

    assert preview["ready_skus"] == [item.sku]
    assert client.offer_payloads[0]["MPItem"][0]["Item"]["price"] == 110.0
    assert client.inventory_payloads[0]["Inventory"][0]["quantity"]["amount"] == 3
    assert submitted["status"] == "submitted"
    assert submitted["submitted_items"] == 1
    assert repeated["status"] == "no_ready_items"
    assert repeated["awaiting_walmart"] == [item.sku]
    assert len(client.offer_payloads) == 1
    assert len(client.inventory_payloads) == 1
    assert repository.walmart_drafts()[0]["publish_status"] == "submitted"


def test_auto_publish_excludes_user_blocked_products(monkeypatch):
    monkeypatch.setattr(main_module.settings, "walmart_auto_publish_excluded_terms", "don toliver,don oliver,otterbox")

    assert main_module._walmart_auto_publish_exclusion(
        InventoryItem(sku="VINYL", title="Don Toliver collectible vinyl")
    ) == "don toliver"
    assert main_module._walmart_auto_publish_exclusion(
        InventoryItem(sku="CASE", title="OtterBox Defender case")
    ) == "otterbox"
