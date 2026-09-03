import asyncio
from datetime import datetime, timedelta, timezone

import app.main as main_module
from app.inventory import InventoryRepository
from app.models import InventoryItem, WalmartAutoPublishRequest
from app.product_identifier_lookup import ProductIdentifierLookupResult
from app.walmart_feed_reconciliation import (
    classify_walmart_inventory_result,
    classify_walmart_offer_result,
    walmart_feed_item_results,
)


class FakeWalmartClient:
    configured = True

    def __init__(self):
        self.offer_payloads = []
        self.full_item_payloads = []
        self.inventory_payloads = []
        self.catalog_response = {
            "status": "matched",
            "matched": True,
            "feed_type": "MP_ITEM_MATCH",
        }

    async def list_published_items(self, *, limit=1000):
        return []

    async def search_catalog(self, product_id_type, product_id, *, response_format="SPEC"):
        return self.catalog_response

    async def submit_offer_match_feed(self, payload):
        self.offer_payloads.append(payload)
        return {"status": "submitted", "feed_id": "OFFER-AUTO"}

    async def submit_full_item_feed(self, payload):
        self.full_item_payloads.append(payload)
        return {"status": "submitted", "feed_id": "FULL-AUTO"}

    async def submit_inventory_feed(self, payload):
        self.inventory_payloads.append(payload)
        return {"status": "submitted", "feed_id": "INVENTORY-AUTO"}

    async def get_feed_status(self, feed_id, *, include_details=True):
        sku = "EBAY-AUTO-1"
        return {
            "feedId": feed_id,
            "feedStatus": "PROCESSED",
            "itemDetails": {
                "itemIngestionStatus": [
                    {"sku": sku, "ingestionStatus": "SUCCESS", "ingestionErrors": {}}
                ]
            },
        }


def test_startup_open_box_retry_runs_once_without_refreshing_ebay(
    monkeypatch, tmp_path
):
    repository = InventoryRepository(tmp_path / "inventory.db")
    requests = []

    async def fake_publish(request):
        requests.append(request)
        return {
            "status": "submitted",
            "submitted_items": 4,
            "offer_feed_id": "OFFER-OPEN-BOX",
            "inventory_feed_id": "INVENTORY-OPEN-BOX",
        }

    monkeypatch.setattr(main_module, "repository", repository)
    monkeypatch.setattr(main_module, "_run_walmart_auto_publish_once", fake_publish)
    monkeypatch.setattr(main_module, "WALMART_OPEN_BOX_RETRY_DELAY_SECONDS", 0)

    asyncio.run(main_module._startup_walmart_open_box_retry())
    asyncio.run(main_module._startup_walmart_open_box_retry())

    assert len(requests) == 1
    assert requests[0].confirm is True
    assert requests[0].force_retry is False
    assert requests[0].sync_ebay_first is False
    assert requests[0].gtin_lookup_max_items == 0
    marker = repository.service_run_marker(main_module.WALMART_OPEN_BOX_RETRY_MARKER)
    assert marker["status"] == "complete"
    assert marker["result"]["submitted_items"] == 4


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
            "UPC": "887276900124",
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
                    "product_identifier": {"type": "UPC", "value": "887276900124"},
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


def test_auto_publish_uses_walmart_full_item_template_when_search_requires_it(
    monkeypatch, tmp_path
):
    repository = InventoryRepository(tmp_path / "inventory.db")
    item = InventoryItem(
        sku="EBAY-AUTO-1",
        ebay_item_id="123",
        title="Apple iPhone 13 128GB Blue Open Box",
        description="Open box and tested.",
        condition="Open box",
        price=400,
        quantity=2,
        image_url="https://i.ebayimg.com/images/g/primary/s-l1600.jpg",
        image_urls=["https://i.ebayimg.com/images/g/secondary/s-l1600.jpg"],
        category="Cell Phones & Smartphones",
        listing_status="ACTIVE",
        source="ebay-trading-api",
        item_specifics={
            "Brand": "Apple",
            "Model": "Apple iPhone 13",
            "UPC": "194252053164",
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
                    "product_identifier": {"type": "UPC", "value": "194252053164"},
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
    client.catalog_response = {
        "status": "full_item_required",
        "matched": False,
        "feed_type": "MP_ITEM",
        "version": "5.0.20260205-21_38_48-api",
        "product_type": "Cell Phones",
        "item_spec_payload": {
            "MPItemFeedHeader": {
                "locale": "en",
                "version": "5.0.20260205-21_38_48-api",
                "businessUnit": "WALMART_US",
            },
            "MPItem": [
                {
                    "Orderable": {
                        "specProductType": "Cell Phones",
                        "productIdentifiers": {
                            "productIdType": "GTIN",
                            "productId": "00194252053164",
                        },
                        "electronicsIndicator": "Yes",
                    },
                    "Visible": {
                        "Cell Phones": {
                            "productName": "Apple iPhone 13 128GB Blue",
                            "brand": "Apple",
                            "has_written_warranty": "No",
                            "isProp65WarningRequired": "No",
                        }
                    },
                }
            ],
        },
    }

    async def fake_generate(_request):
        return {"generated": 1}

    monkeypatch.setattr(main_module, "repository", repository)
    monkeypatch.setattr(main_module, "walmart_client", client)
    monkeypatch.setattr(main_module, "_generate_walmart_drafts", fake_generate)
    monkeypatch.setattr(
        main_module.settings,
        "walmart_auto_publish_excluded_terms",
        "don toliver,don oliver,otterbox",
    )

    submitted = asyncio.run(
        main_module._run_walmart_auto_publish_once(
            WalmartAutoPublishRequest(sync_ebay_first=False, confirm=True)
        )
    )

    assert submitted["status"] == "submitted"
    assert submitted["submitted_skus"] == [item.sku]
    assert submitted["offer_feed_ids"] == ["FULL-AUTO"]
    assert client.offer_payloads == []
    assert len(client.full_item_payloads) == 1
    full_item = client.full_item_payloads[0]["MPItem"][0]
    assert full_item["Orderable"]["price"] == 440.0
    assert full_item["Visible"]["Cell Phones"]["condition"] == "Open Box"
    assert client.inventory_payloads[0]["Inventory"][0]["quantity"]["amount"] == 2


def test_auto_publish_excludes_user_blocked_products(monkeypatch):
    monkeypatch.setattr(main_module.settings, "walmart_auto_publish_excluded_terms", "don toliver,don oliver,otterbox")

    assert main_module._walmart_auto_publish_exclusion(
        InventoryItem(sku="VINYL", title="Don Toliver collectible vinyl")
    ) == "don toliver"
    assert main_module._walmart_auto_publish_exclusion(
        InventoryItem(sku="CASE", title="OtterBox Defender case")
    ) == "otterbox"


def test_feed_reconciliation_classifies_live_walmart_failure_modes():
    payload = {
        "itemDetails": {
            "itemIngestionStatus": [
                {
                    "sku": "CONFLICT",
                    "ingestionStatus": "DATA_ERROR",
                    "ingestionErrors": {
                        "ingestionError": [
                            {
                                "code": "ERR_EXT_DATA_0101211",
                                "description": "This SKU is already set up with a different Product ID.",
                            }
                        ]
                    },
                },
                {
                    "sku": "REVIEW",
                    "ingestionStatus": "DATA_ERROR",
                    "ingestionErrors": {
                        "ingestionError": {
                            "description": "This item is currently under compliance review.",
                        }
                    },
                },
                {
                    "sku": "RETRY",
                    "ingestionStatus": "SYSTEM_ERROR",
                    "ingestionErrors": {},
                },
            ]
        }
    }

    results = walmart_feed_item_results(payload)

    assert classify_walmart_offer_result(results["CONFLICT"]) == "blocked_product_id_conflict"
    assert classify_walmart_offer_result(results["REVIEW"]) == "compliance_review"
    assert classify_walmart_offer_result(results["RETRY"]) == "retryable_offer_error"


def test_inventory_not_found_remains_pending_for_a_safe_retry():
    result = {
        "status": "DATA_ERROR",
        "errors": [
            {
                "code": "EXT_DATA_ERROR_54055672686268",
                "description": "The system did not find an item with the SKU information provided.",
            }
        ],
    }

    assert classify_walmart_inventory_result(result) == "offer_processed_inventory_pending"


def test_retryable_offer_uses_exponential_backoff():
    now = datetime(2026, 9, 2, 22, 0, tzinfo=timezone.utc)
    first_failure = {
        "publish_status": "retryable_offer_error",
        "publish_attempts": 1,
        "last_publish_at": (now - timedelta(minutes=59)).isoformat(),
    }
    second_failure = {
        "publish_status": "retryable_offer_error",
        "publish_attempts": 2,
        "last_publish_at": (now - timedelta(hours=5)).isoformat(),
    }
    repeated_failure = {
        "publish_status": "retryable_offer_error",
        "publish_attempts": 3,
        "last_publish_at": (now - timedelta(hours=23)).isoformat(),
    }

    assert main_module._walmart_publish_retry_due(first_failure, now) is False
    first_failure["last_publish_at"] = (now - timedelta(hours=1, minutes=1)).isoformat()
    assert main_module._walmart_publish_retry_due(first_failure, now) is True

    assert main_module._walmart_publish_retry_due(second_failure, now) is False
    second_failure["last_publish_at"] = (now - timedelta(hours=6, minutes=1)).isoformat()
    assert main_module._walmart_publish_retry_due(second_failure, now) is True

    assert main_module._walmart_publish_retry_due(repeated_failure, now) is False
    repeated_failure["last_publish_at"] = (now - timedelta(hours=24, minutes=1)).isoformat()
    assert main_module._walmart_publish_retry_due(repeated_failure, now) is True


def test_compliance_review_waits_full_48_hours():
    now = datetime(2026, 9, 2, 22, 0, tzinfo=timezone.utc)
    draft = {
        "publish_status": "compliance_review",
        "last_publish_at": (now - timedelta(hours=47)).isoformat(),
    }

    assert main_module._walmart_publish_retry_due(draft, now) is False
    draft["last_publish_at"] = (now - timedelta(hours=49)).isoformat()
    assert main_module._walmart_publish_retry_due(draft, now) is True


def test_online_identifier_lookup_is_walmart_verified_and_cached(monkeypatch, tmp_path):
    repository = InventoryRepository(tmp_path / "inventory.db")
    item = InventoryItem(
        sku="EBAY-MISSING-ID",
        title="Samsung Galaxy S25 128GB Unlocked Navy",
        condition="Open box",
        price=500,
        quantity=1,
        image_url="https://example.com/phone.jpg",
        category="Cell Phones & Smartphones",
        listing_status="ACTIVE",
        source="ebay-trading-api",
        item_specifics={
            "Brand": "Samsung",
            "Model": "Samsung Galaxy S25",
            "Storage": "128 GB",
            "Device Color": "Navy",
            "Network": "Unlocked",
        },
    )
    draft = {
        "sku": item.sku,
        "ebay_item_id": "123",
        "source_snapshot": item.model_dump(mode="json"),
        "prepared_listing": {"product_identifier": None, "shipping_weight_lbs": None},
        "catalog_candidates": [],
        "catalog_status": "no_candidates",
        "status": "draft_needs_review",
        "missing_fields": ["product_identifier", "shipping_weight_lbs"],
    }
    repository.upsert_items([item])
    repository.upsert_walmart_drafts([draft])

    class FakeLookup:
        configured = True

        def __init__(self):
            self.calls = 0

        async def lookup(self, item, candidates):
            self.calls += 1
            return ProductIdentifierLookupResult(
                status="verified",
                product_id_type="UPC",
                product_id="887276900124",
                source_urls=["https://www.bestbuy.com/example"],
                matched_product="Samsung Galaxy S25 128GB Unlocked Navy",
                reason="Exact variant matched.",
            )

    lookup = FakeLookup()
    client = FakeWalmartClient()
    monkeypatch.setattr(main_module, "repository", repository)
    monkeypatch.setattr(main_module, "walmart_client", client)
    monkeypatch.setattr(main_module, "product_identifier_lookup", lookup)
    monkeypatch.setattr(main_module.settings, "walmart_gtin_lookup_enabled", True)
    monkeypatch.setattr(main_module.settings, "walmart_gtin_lookup_retry_seconds", 604800)

    first_budget = main_module._WalmartGtinLookupBudget(1)
    first = asyncio.run(
        main_module._resolve_walmart_auto_publish_draft(
            item, draft, gtin_lookup_budget=first_budget
        )
    )
    second_budget = main_module._WalmartGtinLookupBudget(1)
    second = asyncio.run(
        main_module._resolve_walmart_auto_publish_draft(
            item, draft, gtin_lookup_budget=second_budget
        )
    )

    assert first[0].product_id == "887276900124"
    assert first[2]["identifier_source"] == "verified_online_identifier"
    assert first_budget.summary()["verified"] == 1
    assert second[0].product_id == "887276900124"
    assert second[2]["online_lookup"]["status"] == "verified_cache"
    assert second_budget.summary()["cache_hits"] == 1
    assert lookup.calls == 1
    cached = repository.walmart_product_identifier_cache(item.sku)
    assert cached["verification_status"] == "verified"
    assert cached["lookup_attempts"] == 1


def test_cached_full_item_required_identifier_is_rechecked_without_waiting(
    monkeypatch, tmp_path
):
    repository = InventoryRepository(tmp_path / "inventory.db")
    item = InventoryItem(
        sku="EBAY-FULL-CACHE",
        title="JBL Go 4 Red Portable Speaker Open Box",
        condition="Open box",
        price=55,
        quantity=1,
        image_url="https://example.com/speaker.jpg",
        item_specifics={"Brand": "JBL", "Model": "Go 4"},
    )
    repository.upsert_walmart_product_identifier_cache(
        item.sku,
        source_fingerprint=main_module.product_identifier_fingerprint(item),
        verification_status="full_item_required",
        product_id_type="UPC",
        product_id="050036399296",
        next_lookup_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
    )

    class LookupMustNotRun:
        configured = True

        async def lookup(self, item, candidates):
            raise AssertionError("The cached identifier should be reused before online research.")

    monkeypatch.setattr(main_module, "repository", repository)
    monkeypatch.setattr(main_module, "product_identifier_lookup", LookupMustNotRun())

    product_id_type, product_id, result = asyncio.run(
        main_module._resolve_online_product_identifier(
            item, [], main_module._WalmartGtinLookupBudget(0)
        )
    )

    assert product_id_type == "UPC"
    assert product_id == "050036399296"
    assert result["status"] == "full_item_template_cache"


def test_condition_specific_walmart_upc_is_replaced_with_original_identifier(
    monkeypatch, tmp_path
):
    repository = InventoryRepository(tmp_path / "inventory.db")
    item = InventoryItem(
        sku="IPHONE-12-PRO-MAX",
        title="Apple iPhone 12 Pro Max 128GB Pacific Blue Factory Unlocked Open Box",
        condition="Open box",
        price=400,
        quantity=1,
        image_url="https://example.com/iphone.jpg",
        category="Cell Phones & Smartphones",
        listing_status="ACTIVE",
        item_specifics={
            "Brand": "Apple",
            "Model": "Apple iPhone 12 Pro Max",
            "Storage": "128 GB",
            "Device Color": "Pacific Blue",
            "Network": "Unlocked",
        },
    )
    draft = {
        "sku": item.sku,
        "source_snapshot": item.model_dump(mode="json"),
        "prepared_listing": {"product_identifier": None, "shipping_weight_lbs": None},
        "catalog_candidates": [
            {
                "title": "Pre-Owned Apple iPhone 12 Pro Max 128GB Unlocked Pacific Blue",
                "brand": "Apple",
                "identifiers": {"UPC": "683346583606"},
            }
        ],
        "catalog_status": "candidates_found",
        "status": "draft_needs_review",
        "missing_fields": ["product_identifier", "shipping_weight_lbs"],
    }

    class FakeLookup:
        configured = True

        async def lookup(self, item, candidates):
            return ProductIdentifierLookupResult(
                status="verified",
                product_id_type="UPC",
                product_id="194252020432",
                source_urls=["https://www.apple.com/example"],
                matched_product="Apple iPhone 12 Pro Max 128GB Pacific Blue",
                reason="Original retail identifier verified.",
            )

    monkeypatch.setattr(main_module, "repository", repository)
    monkeypatch.setattr(main_module, "walmart_client", FakeWalmartClient())
    monkeypatch.setattr(main_module, "product_identifier_lookup", FakeLookup())
    monkeypatch.setattr(main_module.settings, "walmart_gtin_lookup_enabled", True)
    monkeypatch.setattr(main_module.settings, "walmart_gtin_lookup_retry_seconds", 604800)

    override, _updated, result = asyncio.run(
        main_module._resolve_walmart_auto_publish_draft(
            item,
            draft,
            gtin_lookup_budget=main_module._WalmartGtinLookupBudget(1),
        )
    )

    assert override.product_id == "194252020432"
    assert override.product_id != "683346583606"
    assert result["identifier_source"] == "verified_online_identifier"


def test_original_product_identifier_walmart_error_is_eligible_for_research_retry():
    assert main_module._walmart_original_identifier_retry(
        {
            "publish_status": "blocked_offer_error",
            "publish_error": (
                "A Pre-Owned item can only be created when the associated original product "
                "is in New condition. The PCF is invalid/Not_eligible for processing"
            ),
        }
    )
    assert not main_module._walmart_original_identifier_retry(
        {"publish_status": "blocked_offer_error", "publish_error": "Generic data error"}
    )
