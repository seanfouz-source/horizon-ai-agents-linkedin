import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.inventory import InventoryRepository
from app.marketplace_inventory_sync import (
    MarketplaceInventorySyncer,
    QUANTITY_POLICY_VERSION,
    choose_ended_listing_sync_plan,
    choose_inventory_sync_plan,
)
from app.models import InventoryItem


def _state(quantity: int, **overrides):
    state = {
        "synced_quantity": quantity,
        "pending_walmart_quantity": None,
        "pending_walmart_at": None,
        "quantity_policy_version": QUANTITY_POLICY_VERSION,
    }
    state.update(overrides)
    return state


def test_first_sync_treats_ebay_as_source_of_truth():
    plan = choose_inventory_sync_plan(5, 2, None)

    assert plan.target_quantity == 5
    assert plan.source == "ebay_initial"
    assert plan.update_ebay is False
    assert plan.update_walmart is True


def test_policy_upgrade_baselines_from_ebay_without_altering_it():
    plan = choose_inventory_sync_plan(
        8,
        3,
        {
            "synced_quantity": 5,
            "pending_walmart_quantity": None,
            "pending_walmart_at": None,
            "quantity_policy_version": QUANTITY_POLICY_VERSION - 1,
        },
    )

    assert plan.target_quantity == 8
    assert plan.source == "ebay_policy_baseline"
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
    assert plan.source == "walmart_sale"
    assert plan.update_ebay is True
    assert plan.update_walmart is False


def test_walmart_increase_is_overwritten_from_ebay():
    plan = choose_inventory_sync_plan(5, 7, _state(5))

    assert plan.target_quantity == 5
    assert plan.source == "ebay_authoritative"
    assert plan.update_ebay is False
    assert plan.update_walmart is True


def test_conflicting_changes_choose_lower_quantity_to_avoid_overselling():
    plan = choose_inventory_sync_plan(4, 3, _state(5))

    assert plan.target_quantity == 3
    assert plan.source == "safety_minimum"
    assert plan.update_ebay is True
    assert plan.update_walmart is False


def test_ebay_decrease_wins_when_walmart_was_increased():
    plan = choose_inventory_sync_plan(4, 6, _state(5))

    assert plan.target_quantity == 4
    assert plan.source == "ebay"
    assert plan.update_ebay is False
    assert plan.update_walmart is True


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


def test_walmart_sale_during_pending_feed_still_reduces_ebay():
    now = datetime.now(timezone.utc)
    plan = choose_inventory_sync_plan(
        5,
        4,
        _state(
            5,
            pending_walmart_quantity=6,
            pending_walmart_at=(now - timedelta(minutes=1)).isoformat(),
        ),
        now=now,
    )

    assert plan.target_quantity == 4
    assert plan.source == "walmart_sale"
    assert plan.update_ebay is True
    assert plan.update_walmart is False


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
        ebay_price=100.0,
        synced_walmart_price=110.0,
        price_currency="USD",
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
    assert stored["ebay_price"] == 100.0
    assert stored["synced_walmart_price"] == 110.0
    assert stored["price_currency"] == "USD"
    assert stored["pending_walmart_image_signature"] == "new-images"
    assert stored["last_image_feed_id"] == "IMAGE@123"
    assert stored["quantity_policy_version"] == 2
    assert repository.marketplace_inventory_sync_summary()["by_status"] == {"pending": 1}
    assert repository.marketplace_inventory_sync_summary()["pending_quantity"] == 1
    assert repository.marketplace_inventory_sync_summary()["pending_images"] == 1


class FakeEbayClient:
    def __init__(self):
        self.rows = [
            {
                "sku": "PHONE-LIVE",
                "item_id": "100",
                "quantity": 2,
                "start_price": 100.0,
                "currency": "USD",
                "image_urls": [
                    "https://i.ebayimg.com/images/g/new-main/s-l1600.jpg",
                    "https://i.ebayimg.com/images/g/new-back/s-l1600.jpg",
                ],
            }
        ]
        self.revisions = []
        self.image_refreshes = []

    async def fetch_active_inventory_quantities(self, *, limit):
        assert limit == 200
        return self.rows

    async def revise_inventory_quantities(self, updates):
        self.revisions.extend(updates)
        return [{**update, "status": "updated"} for update in updates]

    async def fetch_trading_listing_images(self, item_ids):
        self.image_refreshes.append(item_ids)
        return [
            {
                "sku": row["sku"],
                "item_id": row["item_id"],
                "image_urls": row["image_urls"],
            }
            for row in self.rows
            if row["item_id"] in item_ids
        ]


class FakeWalmartClient:
    def __init__(self):
        self.settings = SimpleNamespace(
            walmart_maintenance_spec_version="5.0.20260608-18_15_07-api",
            walmart_price_markup_percent=10.0,
            walmart_auto_publish_excluded_terms="don toliver,don oliver,otterbox",
        )
        self.quantities = {"PHONE-LIVE": 2, "PHONE-ENDED": 1}
        self.published_items = [
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
        self.inventory_payloads = []
        self.image_payloads = []
        self.price_updates = []
        self.feed_status = {"feedStatus": "RECEIVED"}

    async def list_published_items(self, *, limit):
        assert limit == 1000
        return self.published_items

    async def get_inventory_quantity(self, sku):
        return self.quantities[sku]

    async def submit_inventory_feed(self, payload):
        self.inventory_payloads.append(payload)
        return {"status": "submitted", "feed_id": "INVENTORY@123"}

    async def submit_item_maintenance_feed(self, payload):
        self.image_payloads.append(payload)
        return {"status": "submitted", "feed_id": "IMAGE@123"}

    async def update_price(self, sku, amount, *, currency="USD"):
        self.price_updates.append(
            {"sku": sku, "amount": amount, "currency": currency}
        )
        return {"status": "updated", "sku": sku, "price": amount}

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
    assert first["updated_prices"] == 1
    assert first["price_markup_percent"] == 10.0
    assert walmart.price_updates == [
        {"sku": "PHONE-LIVE", "amount": 110.0, "currency": "USD"}
    ]
    assert first["image_updates_submitted"] == 1
    assert ebay.image_refreshes == [["100"]]
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
    assert second["updated_prices"] == 0
    assert len(walmart.price_updates) == 1
    assert len(walmart.image_payloads) == 1
    live_state = repository.marketplace_inventory_sync_state("PHONE-LIVE")
    assert live_state["pending_walmart_image_signature"] is None
    assert live_state["synced_image_signature"] == live_state["ebay_image_signature"]
    assert live_state["ebay_price"] == 100.0
    assert live_state["synced_walmart_price"] == 110.0

    ebay.rows[0]["start_price"] = 120.0
    third = asyncio.run(syncer.sync_once())

    assert third["updated_prices"] == 1
    assert walmart.price_updates[-1] == {
        "sku": "PHONE-LIVE",
        "amount": 132.0,
        "currency": "USD",
    }
    live_state = repository.marketplace_inventory_sync_state("PHONE-LIVE")
    assert live_state["ebay_price"] == 120.0
    assert live_state["synced_walmart_price"] == 132.0


def test_syncer_zeroes_excluded_walmart_item_without_reducing_ebay(tmp_path):
    repository = InventoryRepository(tmp_path / "inventory.db")
    repository.upsert_items(
        [
            InventoryItem(
                sku="PHONE-LIVE",
                title="OtterBox phone case",
                quantity=2,
                source="ebay-api",
            )
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
            }
        ]
    )
    ebay = FakeEbayClient()
    walmart = FakeWalmartClient()
    walmart.quantities = {"PHONE-LIVE": 2}

    result = asyncio.run(MarketplaceInventorySyncer(repository, ebay, walmart).sync_once())

    assert result["excluded_walmart_skus"] == 1
    assert result["zeroed_walmart"] == 1
    assert ebay.revisions == []
    assert walmart.price_updates == []
    assert walmart.image_payloads == []
    assert walmart.inventory_payloads[0]["Inventory"][0]["quantity"]["amount"] == 0
    assert repository.marketplace_inventory_sync_state("PHONE-LIVE")["last_source"] == (
        "excluded:otterbox"
    )

    second = asyncio.run(MarketplaceInventorySyncer(repository, ebay, walmart).sync_once())

    assert second["updated_walmart"] == 0
    assert len(walmart.inventory_payloads) == 1


def _live_listing_repository(tmp_path):
    repository = InventoryRepository(tmp_path / "inventory.db")
    repository.upsert_items(
        [
            InventoryItem(
                sku="PHONE-LIVE",
                title="Live phone",
                quantity=2,
                source="ebay-api",
            )
        ]
    )
    repository.upsert_walmart_drafts(
        [
            {
                "sku": "PHONE-LIVE",
                "ebay_item_id": "100",
                "prepared_listing": {
                    "images": [
                        "https://i.ebayimg.com/images/g/new-main/s-l1600.jpg",
                        "https://i.ebayimg.com/images/g/new-back/s-l1600.jpg",
                    ]
                },
            }
        ]
    )
    return repository


def test_unpublished_walmart_draft_cannot_change_ebay(tmp_path):
    repository = _live_listing_repository(tmp_path)
    ebay = FakeEbayClient()
    walmart = FakeWalmartClient()
    walmart.published_items = []
    walmart.quantities = {}

    result = asyncio.run(MarketplaceInventorySyncer(repository, ebay, walmart).sync_once())

    assert result["status"] == "no_published_matches"
    assert result["checked"] == 0
    assert ebay.revisions == []
    assert walmart.inventory_payloads == []
    assert walmart.price_updates == []
    assert walmart.image_payloads == []


def test_first_walmart_publication_uses_ebay_quantity_without_changing_ebay(tmp_path):
    repository = _live_listing_repository(tmp_path)
    ebay = FakeEbayClient()
    walmart = FakeWalmartClient()
    walmart.published_items = walmart.published_items[:1]
    walmart.quantities = {"PHONE-LIVE": 1}

    result = asyncio.run(MarketplaceInventorySyncer(repository, ebay, walmart).sync_once())

    assert result["updated_ebay"] == 0
    assert result["updated_walmart"] == 1
    assert ebay.revisions == []
    assert walmart.inventory_payloads[0]["Inventory"][0]["quantity"]["amount"] == 2
    assert repository.marketplace_inventory_sync_state("PHONE-LIVE")["last_source"] == (
        "ebay_initial"
    )


def test_later_walmart_sale_reduces_ebay_through_client_call(tmp_path):
    repository = _live_listing_repository(tmp_path)
    repository.upsert_marketplace_inventory_sync_state(
        sku="PHONE-LIVE",
        ebay_item_id="100",
        ebay_quantity=2,
        walmart_quantity=2,
        synced_quantity=2,
        quantity_policy_version=QUANTITY_POLICY_VERSION,
        last_source="already_equal",
        status="synced",
    )
    ebay = FakeEbayClient()
    walmart = FakeWalmartClient()
    walmart.published_items = walmart.published_items[:1]
    walmart.quantities = {"PHONE-LIVE": 1}

    result = asyncio.run(MarketplaceInventorySyncer(repository, ebay, walmart).sync_once())

    assert result["updated_ebay"] == 1
    assert result["updated_walmart"] == 0
    assert ebay.revisions[0]["sku"] == "PHONE-LIVE"
    assert ebay.revisions[0]["quantity"] == 1
    assert walmart.inventory_payloads == []
    assert repository.marketplace_inventory_sync_state("PHONE-LIVE")["last_source"] == (
        "walmart_sale"
    )


def test_walmart_alias_sale_updates_original_ebay_sku_by_item_id(tmp_path):
    repository = InventoryRepository(tmp_path / "inventory.db")
    repository.upsert_items(
        [
            InventoryItem(
                sku="PHONE-LIVE",
                ebay_item_id="100",
                title="Live phone",
                quantity=2,
                source="ebay-api",
            )
        ]
    )
    repository.upsert_walmart_drafts(
        [
            {
                "sku": "WALMART-ALIAS",
                "ebay_item_id": "100",
                "source_snapshot": {
                    "sku": "OLD-EBAY-SKU",
                    "ebay_item_id": "100",
                    "title": "Live phone",
                    "quantity": 2,
                },
                "prepared_listing": {"images": []},
            }
        ]
    )
    repository.upsert_marketplace_inventory_sync_state(
        sku="WALMART-ALIAS",
        ebay_item_id="100",
        ebay_quantity=2,
        walmart_quantity=2,
        synced_quantity=2,
        quantity_policy_version=QUANTITY_POLICY_VERSION,
        last_source="already_equal",
        status="synced",
    )
    ebay = FakeEbayClient()
    walmart = FakeWalmartClient()
    walmart.published_items = [
        {
            "sku": "WALMART-ALIAS",
            "published_status": "PUBLISHED",
            "lifecycle_status": "ACTIVE",
            "product_type": "Cell Phones",
            "product_id_type": "GTIN",
            "product_id": "00123456789012",
        }
    ]
    walmart.quantities = {"WALMART-ALIAS": 1}

    result = asyncio.run(MarketplaceInventorySyncer(repository, ebay, walmart).sync_once())

    assert result["updated_ebay"] == 1
    assert result["ended_ebay_skus"] == 0
    assert result["unresolved_alias_skus"] == 0
    assert ebay.revisions[0]["sku"] == "PHONE-LIVE"
    assert ebay.revisions[0]["item_id"] == "100"
    assert ebay.revisions[0]["quantity"] == 1
    assert repository.marketplace_inventory_sync_state("WALMART-ALIAS")["last_source"] == (
        "walmart_sale"
    )


def test_ambiguous_walmart_aliases_are_not_zeroed_or_applied_to_ebay(tmp_path):
    repository = InventoryRepository(tmp_path / "inventory.db")
    repository.upsert_items(
        [
            InventoryItem(
                sku="PHONE-LIVE",
                ebay_item_id="100",
                title="Variation listing",
                quantity=2,
                source="ebay-api",
            )
        ]
    )
    repository.upsert_walmart_drafts(
        [
            {
                "sku": sku,
                "ebay_item_id": "100",
                "source_snapshot": {
                    "sku": "OLD-VARIATION-SKU",
                    "ebay_item_id": "100",
                    "title": sku,
                    "quantity": 1,
                },
            }
            for sku in ("WALMART-VARIANT-A", "WALMART-VARIANT-B")
        ]
    )
    ebay = FakeEbayClient()
    walmart = FakeWalmartClient()
    walmart.published_items = [
        {
            "sku": sku,
            "published_status": "PUBLISHED",
            "lifecycle_status": "ACTIVE",
            "product_type": "Cell Phones",
            "product_id_type": "GTIN",
            "product_id": product_id,
        }
        for sku, product_id in (
            ("WALMART-VARIANT-A", "00123456789012"),
            ("WALMART-VARIANT-B", "00123456789029"),
        )
    ]
    walmart.quantities = {"WALMART-VARIANT-A": 1, "WALMART-VARIANT-B": 1}

    result = asyncio.run(MarketplaceInventorySyncer(repository, ebay, walmart).sync_once())

    assert result["status"] == "no_published_matches"
    assert result["unresolved_alias_skus"] == 2
    assert result["ended_ebay_skus"] == 0
    assert ebay.revisions == []
    assert walmart.inventory_payloads == []


def test_ebay_number_sku_maps_to_live_seller_sku_without_a_draft(tmp_path):
    repository = InventoryRepository(tmp_path / "inventory.db")
    repository.upsert_items(
        [
            InventoryItem(
                sku="PHONE-LIVE",
                ebay_item_id="123456789012",
                title="Samsung phone",
                quantity=3,
                source="ebay-api",
            )
        ]
    )
    ebay = FakeEbayClient()
    ebay.rows = [
        {
            "sku": "PHONE-LIVE",
            "item_id": "123456789012",
            "quantity": 3,
            "image_urls": [],
        }
    ]
    walmart = FakeWalmartClient()
    walmart.published_items = [
        {
            "sku": "EBAY-123456789012",
            "published_status": "PUBLISHED",
            "lifecycle_status": "ACTIVE",
            "product_type": "Cell Phones",
            "product_id_type": "GTIN",
            "product_id": "00123456789012",
        }
    ]
    walmart.quantities = {"EBAY-123456789012": 0}

    result = asyncio.run(MarketplaceInventorySyncer(repository, ebay, walmart).sync_once())

    assert result["updated_ebay"] == 0
    assert result["updated_walmart"] == 1
    assert result["unresolved_alias_skus"] == 0
    assert result["ended_ebay_skus"] == 0
    assert ebay.revisions == []
    assert walmart.inventory_payloads[0]["Inventory"][0]["sku"] == "EBAY-123456789012"
    assert walmart.inventory_payloads[0]["Inventory"][0]["quantity"]["amount"] == 3


def test_trading_quota_failure_uses_browse_quantities_and_rebaselines_from_ebay(tmp_path):
    class BrowseFallbackEbayClient(FakeEbayClient):
        async def fetch_active_inventory_quantities(self, *, limit):
            raise ValueError("Trading API usage limit exceeded")

        async def fetch_active_browse_inventory_quantities(self, *, limit):
            assert limit == 200
            return [
                {
                    "sku": "EBAY-123456789012",
                    "item_id": "123456789012",
                    "quantity": 2,
                    "inventory_tracking": "item_id",
                    "image_urls": [],
                    "image_complete": False,
                }
            ]

    repository = InventoryRepository(tmp_path / "inventory.db")
    repository.upsert_items(
        [
            InventoryItem(
                sku="EBAY-123456789012",
                ebay_item_id="123456789012",
                title="Samsung phone",
                quantity=2,
                source="ebay-browse-api",
            )
        ]
    )
    repository.upsert_marketplace_inventory_sync_state(
        sku="EBAY-123456789012",
        ebay_item_id="123456789012",
        ebay_quantity=2,
        walmart_quantity=0,
        synced_quantity=2,
        quantity_policy_version=QUANTITY_POLICY_VERSION - 1,
        last_source="walmart_sale",
        status="synced",
    )
    ebay = BrowseFallbackEbayClient()
    walmart = FakeWalmartClient()
    walmart.published_items = [
        {
            "sku": "EBAY-123456789012",
            "published_status": "PUBLISHED",
            "lifecycle_status": "ACTIVE",
            "product_type": "Cell Phones",
            "product_id_type": "GTIN",
            "product_id": "00123456789012",
        }
    ]
    walmart.quantities = {"EBAY-123456789012": 0}

    result = asyncio.run(MarketplaceInventorySyncer(repository, ebay, walmart).sync_once())

    assert result["status"] == "ok"
    assert result["ebay_quantity_source"] == "ebay_browse_api_fallback"
    assert result["updated_ebay"] == 0
    assert result["updated_walmart"] == 1
    assert ebay.revisions == []
    state = repository.marketplace_inventory_sync_state("EBAY-123456789012")
    assert state["last_source"] == "ebay_policy_baseline"
    assert state["quantity_policy_version"] == QUANTITY_POLICY_VERSION
