import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import app.agents as agents_module
from app.integrations import default_metricool_publication_times
from app.inventory import InventoryRepository
from app.models import InventoryItem, SocialDraftRequest, SocialPost


@pytest.fixture(autouse=True)
def disable_live_metricool_lookup(monkeypatch):
    async def fake_scheduled_counts(*args, **kwargs):
        return {}

    monkeypatch.setattr(agents_module, "scheduled_post_counts_by_day", fake_scheduled_counts)


class FakeRepository:
    def __init__(self, items):
        self.items = items

    def all_promotable(self, limit=12):
        return self.items[:limit]

    def search(self, query, limit=8):
        query = query.lower()
        return [item for item in self.items if query in item.title.lower()][:limit]

    def get(self, sku):
        return next((item for item in self.items if item.sku == sku), None)


def inventory_two_hour_slots(count, start_at=None):
    base = datetime.fromisoformat(start_at or "2026-07-02 09:00:00")
    return [
        (base + timedelta(hours=2 * index)).strftime("%Y-%m-%d %H:%M:%S")
        for index in range(count)
    ]


def test_inventory_publication_times_schedule_every_two_hours_during_daytime():
    central = ZoneInfo("America/Chicago")

    publication_times = agents_module._inventory_metricool_publication_times(
        19,
        now=datetime(2026, 7, 2, 8, 0, tzinfo=central),
    )

    assert len(publication_times) == 19
    assert publication_times[:4] == [
        "2026-07-02 09:00:00",
        "2026-07-02 11:00:00",
        "2026-07-02 13:00:00",
        "2026-07-02 15:00:00",
    ]
    assert publication_times[6:9] == [
        "2026-07-02 21:00:00",
        "2026-07-03 09:00:00",
        "2026-07-03 11:00:00",
    ]
    assert publication_times[-1] == "2026-07-04 17:00:00"
    assert all(
        value[11:16] in {"09:00", "11:00", "13:00", "15:00", "17:00", "19:00", "21:00"}
        for value in publication_times
    )


def test_inventory_publication_times_roll_to_next_morning_after_last_slot():
    central = ZoneInfo("America/Chicago")

    publication_times = agents_module._inventory_metricool_publication_times(
        2,
        now=datetime(2026, 7, 2, 20, 45, tzinfo=central),
    )

    assert publication_times == [
        "2026-07-03 09:00:00",
        "2026-07-03 11:00:00",
    ]


def test_all_inventory_mode_creates_one_payload_per_item_cross_posted(monkeypatch):
    items = [
        InventoryItem(
            sku="EBAY-1",
            title="Apple iPhone 14 Pro Max - Gold 128GB",
            condition="Open box",
            price=565,
            quantity=1,
            ebay_url="https://www.ebay.com/itm/1",
            image_url="https://example.com/iphone.jpg",
        ),
        InventoryItem(
            sku="EBAY-2",
            title="Samsung Galaxy S25 - Blue 128GB",
            condition="Open box",
            price=650,
            quantity=1,
            ebay_url="https://www.ebay.com/itm/2",
            image_url="https://example.com/samsung.jpg",
        ),
    ]
    monkeypatch.setattr(agents_module, "get_repository", lambda: FakeRepository(items))
    monkeypatch.setattr(
        agents_module,
        "default_metricool_publication_times",
        lambda count, start_at=None: [f"2026-05-29 {hour:02d}:00:00" for hour in range(8, 8 + count)],
    )

    batch = asyncio.run(
        agents_module.create_social_drafts(
            SocialDraftRequest(
                promote_all_inventory=True,
                brand_name="Horizon Wireless",
                platforms=["facebook", "instagram", "tiktok", "linkedin"],
                as_draft=False,
                auto_publish=True,
            )
        )
    )

    assert len(batch.posts) == 2
    assert [post.product_sku for post in batch.posts] == ["EBAY-1", "EBAY-2"]
    assert len(batch.metricool_payloads) == 2
    expected_media_by_sku = {
        "EBAY-1": "https://example.com/iphone.jpg",
        "EBAY-2": "https://example.com/samsung.jpg",
    }
    for payload in batch.metricool_payloads:
        assert payload["facebook"] is True
        assert payload["instagram"] is True
        assert payload["tiktok"] is True
        assert payload["linkedin"] is True
        assert payload["as_draft"] is False
        assert payload["auto_publish"] is True
        assert payload["media_01"] == expected_media_by_sku[payload["product_sku"]]
        assert payload["publication_date_time"].startswith("2026-05-29")
        assert payload["post_content"].startswith("Horizon Wireless Summer Sale spotlight:")
        assert "Available while supplies last." in payload["post_content"]
        assert "\nShop Now - Buy direct on eBay: https://www.ebay.com/itm/" in payload["post_content"]
        assert payload["buy_url"] == payload["ebay_url"]
        assert payload["link_url"] == payload["ebay_url"]
        assert payload["facebook_link_url"] == payload["ebay_url"]


def test_all_inventory_mode_caps_tiktok_without_stopping_other_platforms(monkeypatch):
    items = [
        InventoryItem(
            sku=f"EBAY-{index}",
            title=f"Apple iPhone 14 Pro Max - Gold {index}",
            condition="Open box",
            price=565,
            quantity=1,
            ebay_url=f"https://www.ebay.com/itm/{index}",
            image_url=f"https://example.com/iphone-{index}.jpg",
        )
        for index in range(1, 6)
    ]
    monkeypatch.setattr(agents_module, "get_repository", lambda: FakeRepository(items))
    monkeypatch.setattr(
        agents_module,
        "default_metricool_publication_times",
        lambda count, start_at=None: [f"2026-06-12 {hour:02d}:00:00" for hour in range(8, 8 + count)],
    )

    batch = asyncio.run(
        agents_module.create_social_drafts(
            SocialDraftRequest(
                promote_all_inventory=True,
                brand_name="Horizon Wireless",
                platforms=["facebook", "instagram", "tiktok", "linkedin"],
                tiktok_daily_post_cap=3,
                as_draft=False,
                auto_publish=True,
            )
        )
    )

    assert len(batch.metricool_payloads) == 5
    assert [payload["tiktok"] for payload in batch.metricool_payloads] == [True, True, True, False, False]
    assert all(payload["facebook"] for payload in batch.metricool_payloads)
    assert all(payload["instagram"] for payload in batch.metricool_payloads)
    assert all(payload["linkedin"] for payload in batch.metricool_payloads)
    assert "TikTok auto-publish was kept to 3 posts per scheduled day" in batch.notes


def test_all_inventory_mode_staggers_from_publish_after(monkeypatch):
    items = [
        InventoryItem(
            sku="EBAY-1",
            title="Apple iPhone 14 Pro Max - Gold 128GB",
            quantity=1,
            ebay_url="https://www.ebay.com/itm/1",
            image_url="https://example.com/iphone.jpg",
        ),
        InventoryItem(
            sku="EBAY-2",
            title="Samsung Galaxy S25 - Blue 128GB",
            quantity=1,
            ebay_url="https://www.ebay.com/itm/2",
            image_url="https://example.com/samsung.jpg",
        ),
    ]
    monkeypatch.setattr(agents_module, "get_repository", lambda: FakeRepository(items))
    central = ZoneInfo("America/Chicago")
    monkeypatch.setattr(
        agents_module,
        "default_metricool_publication_times",
        lambda count, start_at=None: default_metricool_publication_times(
            count,
            now=datetime(2026, 5, 29, 9, 0, tzinfo=central),
            start_at=start_at,
        ),
    )

    batch = asyncio.run(
        agents_module.create_social_drafts(
            SocialDraftRequest(
                promote_all_inventory=True,
                brand_name="Horizon Wireless",
                platforms=["facebook"],
                publish_after="2026-05-30 07:00:00",
                as_draft=False,
                auto_publish=True,
            )
        )
    )

    assert [payload["publication_date_time"] for payload in batch.metricool_payloads] == [
        "2026-05-30 09:00:00",
        "2026-05-30 18:00:00",
    ]


def test_all_inventory_mode_can_filter_by_query(monkeypatch):
    items = [
        InventoryItem(sku="EBAY-1", title="Apple iPhone 14 Pro Max", quantity=1, image_url="https://example.com/iphone.jpg"),
        InventoryItem(sku="EBAY-2", title="Samsung Galaxy S25", quantity=1, image_url="https://example.com/samsung.jpg"),
    ]
    monkeypatch.setattr(agents_module, "get_repository", lambda: FakeRepository(items))
    monkeypatch.setattr(
        agents_module,
        "default_metricool_publication_times",
        lambda count, start_at=None: ["2026-05-29 08:00:00"],
    )

    batch = asyncio.run(
        agents_module.create_social_drafts(
            SocialDraftRequest(promote_all_inventory=True, query="Samsung", brand_name="Horizon Wireless")
        )
    )

    assert [post.product_sku for post in batch.posts] == ["EBAY-2"]


def test_all_inventory_mode_excludes_demo_rows(monkeypatch):
    items = [
        InventoryItem(
            sku="HZ-DEMO-001",
            title="Demo Vintage Camera Lens",
            quantity=1,
            ebay_item_id="123456789001",
            ebay_url="https://www.ebay.com/itm/123456789001",
            image_url="https://example.com/lens.jpg",
            source="csv",
        ),
        InventoryItem(
            sku="EBAY-1",
            title="Apple iPhone 14 Pro Max",
            quantity=1,
            ebay_item_id="1",
            ebay_url="https://www.ebay.com/itm/1",
            image_url="https://example.com/iphone.jpg",
            source="ebay-browse-api",
        ),
    ]
    monkeypatch.setattr(agents_module, "get_repository", lambda: FakeRepository(items))
    monkeypatch.setattr(
        agents_module,
        "default_metricool_publication_times",
        lambda count, start_at=None: ["2026-05-29 08:00:00"],
    )

    batch = asyncio.run(
        agents_module.create_social_drafts(
            SocialDraftRequest(promote_all_inventory=True, query="all inventory", brand_name="Horizon Wireless")
        )
    )

    assert [post.product_sku for post in batch.posts] == ["EBAY-1"]


def test_all_inventory_mode_keeps_listing_image_with_sale_and_store_page_overrides(monkeypatch):
    items = [
        InventoryItem(
            sku="EBAY-1",
            title="Apple iPhone 14 Pro Max",
            condition="Open box",
            price=565,
            quantity=1,
            ebay_url="https://www.ebay.com/itm/1",
            image_url="https://example.com/iphone.jpg",
        )
    ]
    monkeypatch.setattr(agents_module, "get_repository", lambda: FakeRepository(items))
    monkeypatch.setattr(
        agents_module,
        "default_metricool_publication_times",
        lambda count, start_at=None: ["2026-05-29 08:00:00"],
    )

    batch = asyncio.run(
        agents_module.create_social_drafts(
            SocialDraftRequest(
                promote_all_inventory=True,
                brand_name="Horizon Wireless",
                sale_name="Horizon Wireless Summer Sale",
                store_url="https://www.ebay.com/str/exactspec",
                sale_media_url="https://example.com/summer-sale.jpg",
            )
        )
    )

    assert batch.campaign_name == "Horizon Wireless Summer Sale inventory promotion"
    assert "Horizon Wireless Summer Sale spotlight: Apple iPhone 14 Pro Max" in batch.metricool_payloads[0][
        "post_content"
    ]
    assert "Shop Now - Buy direct on eBay: https://www.ebay.com/itm/1" in batch.metricool_payloads[0]["post_content"]
    assert batch.metricool_payloads[0]["media_01"] == "https://example.com/iphone.jpg"


def test_all_inventory_mode_uses_listing_image_even_with_explicit_media_url(monkeypatch):
    items = [
        InventoryItem(
            sku="EBAY-1",
            title="Apple iPhone 14 Pro Max",
            condition="Open box",
            price=565,
            quantity=1,
            ebay_url="https://www.ebay.com/itm/1",
            image_url="https://example.com/iphone.jpg",
        )
    ]
    monkeypatch.setattr(agents_module, "get_repository", lambda: FakeRepository(items))
    monkeypatch.setattr(
        agents_module,
        "default_metricool_publication_times",
        lambda count, start_at=None: ["2026-05-29 08:00:00"],
    )

    batch = asyncio.run(
        agents_module.create_social_drafts(
            SocialDraftRequest(
                promote_all_inventory=True,
                brand_name="Horizon Wireless",
                media_url="https://example.com/campaign-video.mp4",
            )
        )
    )

    assert batch.metricool_payloads[0]["media_01"] == "https://example.com/iphone.jpg"


def test_all_inventory_mode_skips_missing_image_instead_of_using_banner(monkeypatch, caplog):
    items = [
        InventoryItem(
            sku="EBAY-1",
            ebay_item_id="1",
            title="Apple iPhone 14 Pro Max",
            condition="Open box",
            price=565,
            quantity=1,
            ebay_url="https://www.ebay.com/itm/1",
            image_url=None,
            listing_status="ACTIVE",
        )
    ]
    monkeypatch.setattr(agents_module, "get_repository", lambda: FakeRepository(items))

    with caplog.at_level(logging.WARNING, logger="app.agents"):
        batch = asyncio.run(
            agents_module.create_social_drafts(
                SocialDraftRequest(
                    promote_all_inventory=True,
                    brand_name="Horizon Wireless",
                    sale_media_url="https://example.com/banner.jpg",
                    media_url="https://example.com/banner.jpg",
                )
            )
        )

    assert batch.posts == []
    assert batch.metricool_payloads == []
    assert "listing has no valid primary eBay image" in caplog.text
    assert "https://example.com/banner.jpg" not in caplog.text


def test_all_inventory_mode_adds_free_shipping_cta_when_ebay_marks_it_free(monkeypatch):
    items = [
        InventoryItem(
            sku="EBAY-1",
            title="Samsung Galaxy S25",
            condition="Open box",
            price=500,
            quantity=1,
            ebay_url="https://www.ebay.com/itm/1",
            image_url="https://example.com/samsung.jpg",
            item_specifics={"Shipping": "Free Shipping", "Shipping Cost": "0 USD"},
        )
    ]
    monkeypatch.setattr(agents_module, "get_repository", lambda: FakeRepository(items))
    monkeypatch.setattr(
        agents_module,
        "default_metricool_publication_times",
        lambda count, start_at=None: ["2026-05-29 08:00:00"],
    )

    batch = asyncio.run(
        agents_module.create_social_drafts(
            SocialDraftRequest(promote_all_inventory=True, brand_name="Horizon Wireless")
        )
    )

    assert "Free Shipping available." in batch.metricool_payloads[0]["post_content"]


def test_all_inventory_mode_uses_latest_synced_listing_data(tmp_path, monkeypatch):
    repository = InventoryRepository(tmp_path / "inventory.db")
    repository.upsert_items(
        [
            InventoryItem(
                sku="EBAY-123",
                ebay_item_id="123",
                title="Old iPhone Listing",
                condition="Open box",
                price=399,
                quantity=1,
                ebay_url="https://www.ebay.com/itm/123",
                image_url="https://example.com/old-image.jpg",
                listing_status="ACTIVE",
                source="ebay-browse-api",
            )
        ]
    )
    repository.replace_ebay_inventory_snapshot(
        [
            InventoryItem(
                sku="EBAY-123",
                ebay_item_id="123",
                title="Updated iPhone Listing",
                condition="Open box",
                price=429,
                quantity=1,
                ebay_url="https://www.ebay.com/itm/123",
                image_url="https://i.ebayimg.com/images/g/new/s-l1600.jpg",
                listing_status="IN_STOCK",
                source="ebay-browse-api",
            )
        ]
    )
    monkeypatch.setattr(agents_module, "get_repository", lambda: repository)
    monkeypatch.setattr(
        agents_module,
        "default_metricool_publication_times",
        lambda count, start_at=None: ["2026-05-29 08:00:00"],
    )

    batch = asyncio.run(
        agents_module.create_social_drafts(
            SocialDraftRequest(promote_all_inventory=True, max_products_per_run=1, brand_name="Horizon Wireless")
        )
    )

    payload = batch.metricool_payloads[0]
    assert "Updated iPhone Listing" in payload["post_content"]
    assert "Old iPhone Listing" not in payload["post_content"]
    assert "Price: $429.00" in payload["post_content"]
    assert payload["media_01"] == "https://horizon-ai-agents.onrender.com/media/products/EBAY-123.tiktok.jpg"
    assert payload["facebook_media_01"] == "https://i.ebayimg.com/images/g/new/s-l1600.jpg"
    assert payload["tiktok_media_01"] == "https://horizon-ai-agents.onrender.com/media/products/EBAY-123.tiktok.jpg"


def test_all_phones_query_excludes_non_phone_inventory(monkeypatch):
    items = [
        InventoryItem(sku="EBAY-1", title="Apple iPhone 14 Pro Max", quantity=1, image_url="https://example.com/iphone.jpg"),
        InventoryItem(sku="HZ-DEMO-001", title="Demo Vintage Camera Lens", quantity=1, image_url="https://example.com/lens.jpg"),
    ]
    monkeypatch.setattr(agents_module, "get_repository", lambda: FakeRepository(items))
    monkeypatch.setattr(
        agents_module,
        "default_metricool_publication_times",
        lambda count, start_at=None: ["2026-05-29 08:00:00"],
    )

    batch = asyncio.run(
        agents_module.create_social_drafts(
            SocialDraftRequest(promote_all_inventory=True, query="all phones", brand_name="Horizon Wireless")
        )
    )

    assert [post.product_sku for post in batch.posts] == ["EBAY-1"]


def test_all_phones_query_looks_past_non_phone_first_items(monkeypatch):
    items = [
        InventoryItem(sku="HZ-DEMO-001", title="Demo Vintage Camera Lens", quantity=1, image_url="https://example.com/lens.jpg"),
        InventoryItem(sku="EBAY-1", title="Apple iPhone 14 Pro Max", quantity=1, image_url="https://example.com/iphone.jpg"),
        InventoryItem(sku="EBAY-2", title="Samsung Galaxy S25", quantity=1, image_url="https://example.com/samsung.jpg"),
    ]
    monkeypatch.setattr(agents_module, "get_repository", lambda: FakeRepository(items))
    monkeypatch.setattr(
        agents_module,
        "default_metricool_publication_times",
        lambda count, start_at=None: [f"2026-05-29 {hour:02d}:00:00" for hour in range(8, 8 + count)],
    )

    batch = asyncio.run(
        agents_module.create_social_drafts(
            SocialDraftRequest(
                promote_all_inventory=True,
                query="all phones",
                max_products_per_run=2,
                brand_name="Horizon Wireless",
            )
        )
    )

    assert [post.product_sku for post in batch.posts] == ["EBAY-1", "EBAY-2"]


def test_all_inventory_mode_records_history_without_same_day_duplicates(tmp_path, monkeypatch):
    repository = InventoryRepository(tmp_path / "inventory.db")
    repository.upsert_items(
        [
            InventoryItem(
                sku=f"EBAY-{index}",
                ebay_item_id=str(index),
                title=f"Apple iPhone 14 Pro Max - Gold {index}",
                condition="Open box",
                price=565,
                quantity=1,
                ebay_url=f"https://www.ebay.com/itm/{index}",
                image_url=f"https://example.com/iphone-{index}.jpg",
                listing_status="ACTIVE",
            )
            for index in range(1, 4)
        ]
    )
    monkeypatch.setattr(agents_module, "get_repository", lambda: repository)
    monkeypatch.setattr(
        agents_module,
        "_inventory_metricool_publication_times",
        inventory_two_hour_slots,
    )

    request = SocialDraftRequest(
        promote_all_inventory=True,
        max_products_per_run=3,
        brand_name="Horizon Wireless",
        platforms=["facebook", "instagram"],
        as_draft=False,
        auto_publish=True,
    )

    first_batch = asyncio.run(agents_module.create_social_drafts(request))

    assert [payload["publication_date_time"] for payload in first_batch.metricool_payloads] == [
        "2026-07-02 09:00:00",
        "2026-07-02 11:00:00",
        "2026-07-02 13:00:00",
    ]
    assert repository.social_post_count_for_day("2026-07-02") == 3
    assert repository.social_post_count_for_hour("2026-07-02 09") == 1
    assert repository.social_post_count_for_hour("2026-07-02 11") == 1
    assert repository.social_post_count_for_hour("2026-07-02 13") == 1

    second_batch = asyncio.run(agents_module.create_social_drafts(request))

    assert second_batch.posts == []
    assert second_batch.metricool_payloads == []


def test_all_inventory_mode_rotates_through_full_store_inventory(tmp_path, monkeypatch):
    repository = InventoryRepository(tmp_path / "inventory.db")
    base_updated_at = datetime.fromisoformat("2026-07-01T12:00:00+00:00")
    repository.upsert_items(
        [
            InventoryItem(
                sku=f"EBAY-{index:02d}",
                ebay_item_id=str(index),
                title=f"Store Listing {index:02d}",
                quantity=1,
                ebay_url=f"https://www.ebay.com/itm/{index}",
                image_url=f"https://i.ebayimg.com/images/g/{index}/s-l1600.jpg",
                listing_status="ACTIVE",
                source="ebay-browse-api",
                updated_at=base_updated_at - timedelta(minutes=index),
            )
            for index in range(1, 19)
        ]
    )
    for index in range(1, 13):
        repository.record_social_post(
            ebay_item_id=str(index),
            sku=f"EBAY-{index:02d}",
            title=f"Store Listing {index:02d}",
            item_url=f"https://www.ebay.com/itm/{index}",
            image_url=f"https://i.ebayimg.com/images/g/{index}/s-l1600.jpg",
            caption="Queued earlier",
            scheduled_at=f"2026-07-{index:02d} 09:00:00",
            platform="facebook,instagram,tiktok,linkedin",
        )

    monkeypatch.setattr(agents_module, "get_repository", lambda: repository)
    monkeypatch.setattr(
        agents_module,
        "default_metricool_publication_times",
        lambda count, start_at=None: [
            "2026-07-20 09:00:00",
            "2026-07-20 18:00:00",
        ][:count],
    )

    batch = asyncio.run(
        agents_module.create_social_drafts(
            SocialDraftRequest(
                promote_all_inventory=True,
                max_products_per_run=2,
                brand_name="Horizon Wireless",
                platforms=["facebook", "instagram", "tiktok", "linkedin"],
            )
        )
    )

    assert [post.product_sku for post in batch.posts] == ["EBAY-13", "EBAY-14"]
    assert [payload["facebook"] for payload in batch.metricool_payloads] == [True, True]
    assert [payload["instagram"] for payload in batch.metricool_payloads] == [True, True]
    assert [payload["tiktok"] for payload in batch.metricool_payloads] == [True, True]
    assert [payload["linkedin"] for payload in batch.metricool_payloads] == [True, True]


def test_all_inventory_mode_matches_composite_ebay_ids_to_history(tmp_path, monkeypatch):
    repository = InventoryRepository(tmp_path / "inventory.db")
    repository.upsert_items(
        [
            InventoryItem(
                sku=f"EBAY-36600000000{index}",
                ebay_item_id=f"v1|36600000000{index}|0",
                title=f"Store Listing {index}",
                quantity=1,
                ebay_url=f"https://www.ebay.com/itm/36600000000{index}",
                image_url=f"https://i.ebayimg.com/images/g/{index}/s-l1600.jpg",
                listing_status="ACTIVE",
                source="ebay-browse-api",
            )
            for index in range(1, 5)
        ]
    )
    for index in range(1, 3):
        repository.record_social_post(
            ebay_item_id=f"v1|36600000000{index}|0",
            sku=f"EBAY-36600000000{index}",
            title=f"Store Listing {index}",
            item_url=f"https://www.ebay.com/itm/36600000000{index}",
            image_url=f"https://i.ebayimg.com/images/g/{index}/s-l1600.jpg",
            caption="Queued earlier",
            scheduled_at=f"2026-07-02 {9 + ((index - 1) * 2):02d}:00:00",
            platform="facebook,instagram,tiktok,linkedin",
        )

    monkeypatch.setattr(agents_module, "get_repository", lambda: repository)
    monkeypatch.setattr(
        agents_module,
        "_inventory_metricool_publication_times",
        inventory_two_hour_slots,
    )

    batch = asyncio.run(
        agents_module.create_social_drafts(
            SocialDraftRequest(
                promote_all_inventory=True,
                max_products_per_run=2,
                brand_name="Horizon Wireless",
                platforms=["facebook", "instagram", "tiktok", "linkedin"],
            )
        )
    )

    assert {post.product_sku for post in batch.posts} == {"EBAY-366000000003", "EBAY-366000000004"}


def test_inventory_scheduler_skips_items_already_scheduled_that_day(tmp_path):
    repository = InventoryRepository(tmp_path / "inventory.db")
    for index in range(1, 3):
        repository.record_social_post(
            ebay_item_id=f"36600000000{index}",
            sku=f"EBAY-36600000000{index}",
            title=f"Store Listing {index}",
            item_url=f"https://www.ebay.com/itm/36600000000{index}",
            image_url=f"https://i.ebayimg.com/images/g/{index}/s-l1600.jpg",
            caption="Queued earlier",
            scheduled_at=f"2026-07-02 {9 + ((index - 1) * 2):02d}:00:00",
            platform="facebook,instagram,tiktok,linkedin",
        )
    posts = [
        SocialPost(
            platform="facebook",
            text=f"Store Listing {index}",
            product_sku=f"EBAY-36600000000{index}",
            product_title=f"Store Listing {index}",
            ebay_url=f"https://www.ebay.com/itm/36600000000{index}",
            media_url=f"https://i.ebayimg.com/images/g/{index}/s-l1600.jpg",
        )
        for index in range(1, 5)
    ]

    scheduled_posts = agents_module._assign_inventory_posts_to_daily_unique_slots(
        repository,
        posts,
        ["2026-07-02 13:00:00", "2026-07-02 15:00:00"],
        2,
    )

    assert [post.product_sku for post in scheduled_posts] == [
        "EBAY-366000000003",
        "EBAY-366000000004",
    ]
    assert [post.suggested_schedule for post in scheduled_posts] == [
        "2026-07-02 13:00:00",
        "2026-07-02 15:00:00",
    ]


def test_all_inventory_mode_respects_existing_hourly_history(tmp_path, monkeypatch):
    repository = InventoryRepository(tmp_path / "inventory.db")
    repository.record_social_post(
        ebay_item_id="old-1",
        sku="EBAY-OLD-1",
        title="Existing Metricool post 1",
        item_url="https://www.ebay.com/itm/old1",
        image_url="https://example.com/old1.jpg",
        caption="Existing post",
        scheduled_at="2026-07-02 09:00:00",
        platform="facebook,instagram",
    )
    repository.record_social_post(
        ebay_item_id="old-2",
        sku="EBAY-OLD-2",
        title="Existing Metricool post 2",
        item_url="https://www.ebay.com/itm/old2",
        image_url="https://example.com/old2.jpg",
        caption="Existing post",
        scheduled_at="2026-07-02 09:30:00",
        platform="facebook,instagram",
    )
    repository.upsert_items(
        [
            InventoryItem(
                sku="EBAY-3",
                ebay_item_id="3",
                title="Samsung Galaxy S25 Blue 128GB",
                quantity=1,
                ebay_url="https://www.ebay.com/itm/3",
                image_url="https://example.com/samsung.jpg",
                listing_status="ACTIVE",
            )
        ]
    )
    monkeypatch.setattr(agents_module, "get_repository", lambda: repository)
    monkeypatch.setattr(
        agents_module,
        "_inventory_metricool_publication_times",
        inventory_two_hour_slots,
    )

    batch = asyncio.run(
        agents_module.create_social_drafts(
            SocialDraftRequest(
                promote_all_inventory=True,
                max_products_per_run=1,
                brand_name="Horizon Wireless",
                platforms=["facebook"],
            )
        )
    )

    assert [payload["publication_date_time"] for payload in batch.metricool_payloads] == ["2026-07-02 11:00:00"]
