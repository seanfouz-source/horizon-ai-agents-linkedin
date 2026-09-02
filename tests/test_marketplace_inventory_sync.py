import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.inventory import InventoryRepository
from app.marketplace_inventory_sync import (
    MarketplaceInventorySyncer,
    choose_ended_listing_sync_plan,
    choose_inventory_sync_plan,
)
from app.models import InventoryItem


def _state(quantity: int, **overrides):
    state = {
        "synced_quantity": quantity,
        "pending_walmart_quantity": None,
        "pending_walmart_at": None,
    }
    state.update(overrides)
    return state


def test_first_sync_treats_ebay_as_source_of_truth():
    plan = choose_inventory_sync_plan(5, 2, None)

    assert plan.target_quantity == 5
    assert plan.source == "ebay_initial"
    assert plan.update_ebay is False
    assert plan.update_walmart is True


def test_ebay_change_flows_to_walmart():
    plan = choose_inventory_sync_plan(3, 5, _state(5))

    assert plan.target_quantity == 3
    assert plan.source == "ebay"
    assert plan.update_walmart is True
    assert plan.update_ebay is False


def test_walmart_change_flows_to_ebay():
    plan = choose_inventory_sync_plan(5, 4, _state(5))

    assert plan.target_quantity == 4
    assert plan.source == "walmart"
    assert plan.update_ebay is True
    assert plan.update_walmart is False


def test_conflicting_changes_choose_lower_quantity_to_avoid_overselling():
    plan = choose_inventory_sync_plan(4, 3, _state(5))

    assert plan.target_quantity == 3
    assert plan.source == "safety_minimum"
    assert plan.update_ebay is True
    assert plan.update_walmart is False


def test_pending_walmart_feed_is_not_mistaken_for_a_manual_change():
    now = datetime.now(timezone.utc)
    plan = choose_inventory_sync_plan(
        3,
        5,
        _state(
            3,
            pending_walmart_quantity=3,
            pending_walmart_at=(now - timedelta(minutes=1)).isoformat(),
        ),
        now=now,
    )

    assert plan.source == "walmart_feed_pending"
    assert plan.update_ebay is False
    assert plan.update_walmart is False
    assert plan.pending_walmart_quantity == 3


def test_ended_ebay_listing_drives_walmart_to_zero():
    plan = choose_ended_listing_sync_plan(4, _state(4))

    assert plan.target_quantity == 0
    assert plan.source == "ebay_listing_ended"
    assert plan.update_ebay is False
    assert plan.update_walmart is True


def test_inventory_sync_state_is_persistent(tmp_path):
    repository = InventoryRepository(tmp_path / "inventory.db")

    stored = repository.upsert_marketplace_inventory_sync_state(
        sku="PHONE-1",
        ebay_item_id="123",
        ebay_quantity=3,
        walmart_quantity=5,
        synced_quantity=3,
        pending_walmart_quantity=3,
        pending_walmart_at="2026-09-02T12:00:00+00:00",
        ebay_image_signature="new-images",
        synced_image_signature="old-images",
        pending_walmart_image_signature="new-images",
        pending_walmart_image_at="2026-09-02T12:00:00+00:00",
        last_image_feed_id="IMAGE@123",
        last_source="ebay",
        status="pending",
    )

    assert stored["sku"] == "PHONE-1"
    assert stored["pending_walmart_quantity"] == 3
    assert stored["pending_walmart_image_signature"] == "new-images"
    assert stored["last_image_feed_id"] == "IMAGE@123"
    assert repository.marketplace_inventory_sync_summary()["by_status"] == {"pending": 1}


class FakeEbayClient:
    def __init__(self):
        self.rows = [
            {
                "sku": "PHONE-LIVE",
                "item_id": "100",
                "quantity": 2,
                "image_urls": [
                    "https://i.ebayimg.com/images/g/new-main/s-l1600.jpg",
                    "https://i.ebayimg.com/images/g/new-back/s-l1600.jpg",
                ],
            }
        ]
        self.revisions = []

    async def fetch_active_inventory_quantities(self, *, limit):
        assert limit == 200
        return self.rows

    async def revise_inventory_quantities(self, updates):
        self.revisions.extend(updates)
        return [{**update, "status": "updated"} for update in updates]


class FakeWalmartClient:
    def __init__(self):
        self.settings = SimpleNamespace(
            walmart_maintenance_spec_version="5.0.20260608-18_15_07-api"
        )
        self.quantities = {"PHONE-LIVE": 2, "PHONE-ENDED": 1}
        self.inventory_payloads = []
        self.image_payloads = []
        self.feed_status = {"feedStatus": "RECEIVED"}

    async def list_published_items(self, *, limit):
        assert limit == 1000
        return [
            {
                "sku": "PHONE-LIVE",
                "published_status": "PUBLISHED",
                "lifecycle_status": "ACTIVE",
                "product_type": "Cell Phones",
                "product_id_type": "GTIN",
                "product_id": "00123456789012",
            },
            {
                "sku": "PHONE-ENDED",
                "published_status": "PUBLISHED",
                "lifecycle_status": "ACTIVE",
                "product_type": "Cell Phones",
                "product_id_type": "GTIN",
                "product_id": "00123456789029",
            },
        ]

    async def get_inventory_quantity(self, sku):
        return self.quantities[sku]

    async def submit_inventory_feed(self, payload):
        self.inventory_payloads.append(payload)
        return {"status": "submitted", "feed_id": "INVENTORY@123"}

    async def submit_item_maintenance_feed(self, payload):
        self.image_payloads.append(payload)
        return {"status": "submitted", "feed_id": "IMAGE@123"}

    async def get_feed_status(self, feed_id):
        assert feed_id == "IMAGE@123"
        return self.feed_status

    async def get_item_details(self, sku):
        raise AssertionError(f"Unexpected detail lookup for {sku}")


def test_syncer_updates_changed_images_and_zeroes_ended_listing(tmp_path):
    repository = InventoryRepository(tmp_path / "inventory.db")
    repository.upsert_items(
        [
            InventoryItem(sku="PHONE-LIVE", title="Live phone", quantity=2, source="ebay-api"),
            InventoryItem(sku="PHONE-ENDED", title="Ended phone", quantity=1, source="ebay-api"),
        ]
    )
    repository.upsert_walmart_drafts(
        [
            {
                "sku": "PHONE-LIVE",
                "ebay_item_id": "100",
                "prepared_listing": {
                    "images": [
                        "https://i.ebayimg.com/images/g/old-main/s-l1600.jpg"
                    ]
                },
            },
            {
                "sku": "PHONE-ENDED",
                "ebay_item_id": "200",
                "prepared_listing": {
                    "images": [
                        "https://i.ebayimg.com/images/g/ended/s-l1600.jpg"
                    ]
                },
            },
        ]
    )
    ebay = FakeEbayClient()
    walmart = FakeWalmartClient()
    syncer = MarketplaceInventorySyncer(repository, ebay, walmart)

    first = asyncio.run(syncer.sync_once())

    assert first["zeroed_walmart"] == 1
    assert first["image_updates_submitted"] == 1
    inventory_rows = walmart.inventory_payloads[0]["Inventory"]
    assert inventory_rows == [
        {
            "sku": "PHONE-ENDED",
            "quantity": {"unit": "EACH", "amount": 0},
            "inventoryAvailableDate": inventory_rows[0]["inventoryAvailableDate"],
        }
    ]
    image_item = walmart.image_payloads[0]["MPItem"][0]
    assert image_item["Orderable"]["sku"] == "PHONE-LIVE"
    assert image_item["Visible"]["Cell Phones"]["mainImageUrl"].endswith(
        "new-main/s-l1600.jpg"
    )
    ended_state = repository.marketplace_inventory_sync_state("PHONE-ENDED")
    live_state = repository.marketplace_inventory_sync_state("PHONE-LIVE")
    assert ended_state["ebay_quantity"] == 0
    assert ended_state["pending_walmart_quantity"] == 0
    assert live_state["pending_walmart_image_signature"] is not None
    assert live_state["last_image_feed_id"] == "IMAGE@123"

    walmart.quantities["PHONE-ENDED"] = 0
    walmart.feed_status = {
        "feedStatus": "PROCESSED",
        "itemsReceived": 1,
        "itemsSucceeded": 1,
        "itemsFailed": 0,
    }
    second = asyncio.run(syncer.sync_once())

    assert second["image_updates_confirmed"] == 1
    assert second["image_updates_submitted"] == 0
    assert len(walmart.image_payloads) == 1
    live_state = repository.marketplace_inventory_sync_state("PHONE-LIVE")
    assert live_state["pending_walmart_image_signature"] is None
    assert live_state["synced_image_signature"] == live_state["ebay_image_signature"]
