import xml.etree.ElementTree as ET

from app.ebay import EbayClient
from app.ebay_listing_optimizer import (
    count_distinct_ebay_photos,
    listing_price_signature,
    propose_listing_optimization,
)
from scripts.optimize_ebay_listings import decode_backup_chunks, emit_backup


def _phone_snapshot(**overrides):
    snapshot = {
        "item_id": "366563870530",
        "title": "Apple iPhone 13 256 GB Factory Unlocked - Midnight",
        "listing_type": "FixedPriceItem",
        "condition": "Open box",
        "category_id": "9355",
        "category_name": "Cell Phones & Smartphones",
        "has_variations": False,
        "item_specifics": [
            {"name": "Brand", "values": ["Apple"]},
            {"name": "Model", "values": ["Apple iPhone 13"]},
            {"name": "Storage Capacity", "values": ["256 GB"]},
            {"name": "Color", "values": ["Midnight"]},
            {"name": "Lock Status", "values": ["Factory Unlocked"]},
        ],
        "picture_urls": [
            "https://i.ebayimg.com/00/s/MTIwMFgxNjAw/z/example/$_1.JPG",
            "https://i.ebayimg.com/images/g/example/s-l1600.jpg",
        ],
        "prices": {
            "start_price": {"value": "385.00", "currency": "USD"},
            "current_price": {"value": "385.00", "currency": "USD"},
            "variations": [],
        },
    }
    snapshot.update(overrides)
    return snapshot


def test_phone_title_uses_saved_facts_and_preserves_price_outside_proposal():
    proposal = propose_listing_optimization(_phone_snapshot())

    assert proposal["proposed_title"] == (
        "Apple iPhone 13 256GB Midnight Factory Unlocked Open Box"
    )
    assert proposal["item_specifics_changed"] is False
    assert proposal["requires_photo_capture"] is True


def test_optimizer_adds_only_title_supported_missing_specifics():
    snapshot = _phone_snapshot(
        item_specifics=[
            {"name": "Model", "values": ["Samsung Galaxy A36 5G"]},
            {"name": "Color", "values": ["Black"]},
        ],
        title="Samsung Galaxy A36 5G 128GB Factory Unlocked",
    )

    proposal = propose_listing_optimization(snapshot)

    assert proposal["item_specifics_changed"] is True
    assert proposal["item_specific_additions"] == [
        {"name": "Brand", "value": "Samsung"},
        {"name": "Lock Status", "value": "Factory Unlocked"},
        {"name": "Storage Capacity", "value": "128 GB"},
    ]
    assert proposal["proposed_title"] == (
        "Samsung Galaxy A36 5G 128GB Black Factory Unlocked Open Box"
    )


def test_optimizer_corrects_new_open_box_title_contradiction():
    proposal = propose_listing_optimization(
        {
            **_phone_snapshot(),
            "title": "Motorola One 5G Ace 64GB Factory Unlocked - New",
            "item_specifics": [
                {"name": "Model", "values": ["Motorola One 5G Ace"]},
                {"name": "Storage Capacity", "values": ["64 GB"]},
                {"name": "Color", "values": ["Gray"]},
                {"name": "Lock Status", "values": ["Factory Unlocked"]},
            ],
        }
    )

    assert "New" not in proposal["proposed_title"]
    assert proposal["proposed_title"].endswith("Open Box")


def test_optimizer_preserves_5g_keyword_when_saved_model_omits_it():
    proposal = propose_listing_optimization(
        {
            **_phone_snapshot(),
            "title": "Motorola Moto G 5G 128GB Factory Unlocked (2026)",
            "item_specifics": [
                {"name": "Model", "values": ["Motorola Moto G (2026)"]},
                {"name": "Storage Capacity", "values": ["128 GB"]},
                {"name": "Color", "values": ["Slipstream"]},
                {"name": "Lock Status", "values": ["Factory Unlocked"]},
            ],
        }
    )

    assert "5G" in proposal["proposed_title"]
    assert proposal["proposed_title"].endswith("Open Box")


def test_generic_title_cleanup_preserves_wifi_hyphen():
    proposal = propose_listing_optimization(
        {
            **_phone_snapshot(),
            "title": "Apple iPad 11th Gen A16 128 GB Wi-Fi + Cellular - Blue",
            "category_id": "171485",
            "item_specifics": [],
        }
    )

    assert "Wi-Fi" in proposal["proposed_title"]
    assert proposal["proposed_title"].endswith("Open Box")


def test_phone_title_preserves_title_color_when_specific_conflicts():
    proposal = propose_listing_optimization(
        {
            **_phone_snapshot(),
            "title": "Galaxy S25 Blue 128gb Open Box-Unlocked",
            "item_specifics": [
                {"name": "Brand", "values": ["Samsung"]},
                {"name": "Model", "values": ["Samsung Galaxy S25"]},
                {"name": "Storage Capacity", "values": ["128 GB"]},
                {"name": "Color", "values": ["Black"]},
                {"name": "Network", "values": ["Unlocked"]},
            ],
        }
    )

    assert proposal["proposed_title"] == (
        "Samsung Galaxy S25 128GB Blue Unlocked Open Box"
    )


def test_generic_title_cleanup_normalizes_attached_wifi_separator():
    proposal = propose_listing_optimization(
        {
            **_phone_snapshot(),
            "title": "Samsung Galaxy Tab A11+ -Wifi + Cellular 5G",
            "category_id": "171485",
            "item_specifics": [],
        }
    )

    assert proposal["proposed_title"] == (
        "Samsung Galaxy Tab A11+ Wi-Fi + Cellular 5G Open Box"
    )


def test_distinct_ebay_photo_count_collapses_size_variants():
    urls = [
        "https://i.ebayimg.com/00/s/MTIwMFgxNjAw/z/example/$_1.JPG?set_id=123",
        "https://i.ebayimg.com/images/g/example/s-l1600.jpg",
        "https://i.ebayimg.com/images/g/second/s-l500.jpg",
    ]

    assert count_distinct_ebay_photos(urls) == 2


def test_price_signature_ignores_current_price_but_detects_listing_price():
    before = _phone_snapshot()
    after_current_price_change = _phone_snapshot(
        prices={
            **before["prices"],
            "current_price": {"value": "390.00", "currency": "USD"},
        }
    )
    after_start_price_change = _phone_snapshot(
        prices={
            **before["prices"],
            "start_price": {"value": "390.00", "currency": "USD"},
        }
    )

    assert listing_price_signature(before) == listing_price_signature(
        after_current_price_change
    )
    assert listing_price_signature(before) != listing_price_signature(
        after_start_price_change
    )


def test_revise_request_never_contains_price_fields():
    payload = EbayClient._trading_revise_listing_request(
        call_name="ReviseFixedPriceItem",
        item_id="366563870530",
        title="Apple iPhone 13 256GB Midnight Factory Unlocked Open Box",
        item_specifics=[
            {"name": "Brand", "values": ["Apple"]},
            {"name": "Color", "values": ["Midnight"]},
        ],
    )

    assert b"StartPrice" not in payload
    assert b"CurrentPrice" not in payload
    assert b"BuyItNowPrice" not in payload
    root = ET.fromstring(payload)
    assert EbayClient._xml_nested_text(root, "Item", "ItemID") == "366563870530"
    assert (
        EbayClient._xml_nested_text(root, "Item", "Title")
        == "Apple iPhone 13 256GB Midnight Factory Unlocked Open Box"
    )


def test_trading_snapshot_preserves_raw_xml_prices_and_specifics():
    payload = b"""<?xml version="1.0" encoding="utf-8"?>
    <GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <Item>
        <ItemID>366563870530</ItemID>
        <Title>Apple iPhone 13 256 GB Factory Unlocked - Midnight</Title>
        <ListingType>FixedPriceItem</ListingType>
        <ConditionID>1500</ConditionID>
        <ConditionDisplayName>Open box</ConditionDisplayName>
        <PrimaryCategory>
          <CategoryID>9355</CategoryID>
          <CategoryName>Cell Phones &amp; Smartphones</CategoryName>
        </PrimaryCategory>
        <StartPrice currencyID="USD">385.00</StartPrice>
        <SellingStatus><CurrentPrice currencyID="USD">385.00</CurrentPrice></SellingStatus>
        <ItemSpecifics>
          <NameValueList><Name>Brand</Name><Value>Apple</Value></NameValueList>
          <NameValueList><Name>Color</Name><Value>Midnight</Value></NameValueList>
        </ItemSpecifics>
        <PictureDetails>
          <PictureURL>https://i.ebayimg.com/images/g/example/s-l1600.jpg</PictureURL>
        </PictureDetails>
      </Item>
    </GetItemResponse>"""

    snapshot = EbayClient._parse_trading_listing_snapshot(payload)

    assert snapshot["item_id"] == "366563870530"
    assert snapshot["prices"]["start_price"] == {
        "value": "385.00",
        "currency": "USD",
    }
    assert snapshot["item_specifics"][0] == {
        "name": "Brand",
        "values": ["Apple"],
    }
    assert "<GetItemResponse" in snapshot["raw_xml"]


def test_backup_chunks_round_trip(capsys):
    backup = {"listing_count": 1, "listings": [{"item_id": "123", "title": "Phone"}]}

    emit_backup(backup, chunk_size=20)
    lines = capsys.readouterr().out.splitlines()
    manifest_line = next(
        line for line in lines if line.startswith("EBAY_OPTIMIZATION_BACKUP_MANIFEST=")
    )
    chunk_lines = [
        line for line in lines if line.startswith("EBAY_OPTIMIZATION_BACKUP_CHUNK=")
    ]
    manifest = __import__("json").loads(manifest_line.split("=", 1)[1])
    chunks = [
        line.split(":", 1)[1]
        for line in sorted(chunk_lines, key=lambda value: value.split("=", 1)[1])
    ]

    assert decode_backup_chunks(chunks, manifest["sha256"]) == backup
