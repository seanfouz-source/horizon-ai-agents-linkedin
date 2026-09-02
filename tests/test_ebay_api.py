import asyncio
from types import SimpleNamespace

import httpx

import app.ebay as ebay_module
from app.ebay import EbayClient
from app.models import InventoryItem


def test_ebay_client_preserves_walmart_identifiers_and_package_weight():
    product_specifics = EbayClient._sell_product_identifiers(
        {
            "brand": "Samsung",
            "mpn": "SM-S931U",
            "upc": ["887276900123"],
        }
    )
    package_specifics = EbayClient._sell_package_specifics(
        {"packageWeightAndSize": {"weight": {"value": 24, "unit": "OUNCE"}}}
    )

    assert product_specifics == {
        "Brand": "Samsung",
        "MPN": "SM-S931U",
        "UPC": "887276900123",
    }
    assert package_specifics == {"Shipping Weight": "24 oz"}


class FakeAsyncClient:
    def __init__(self, handler, *args, **kwargs):
        self.handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def get(self, path, params=None, headers=None):
        return self._call_handler(path, params or {}, headers or {}, "GET")

    async def post(self, path, data=None, content=None, json=None, headers=None):
        payload = content if content is not None else json if json is not None else data or {}
        return self._call_handler(path, payload, headers or {}, "POST")

    def _call_handler(self, path, payload, headers, method):
        arg_count = getattr(getattr(self.handler, "__code__", None), "co_argcount", 2)
        if arg_count >= 4:
            return self.handler(path, payload, headers, method)
        return self.handler(path, payload)


def test_ebay_client_falls_back_to_browse_api_and_selects_primary_image(monkeypatch):
    def handler(path, params):
        request = httpx.Request("GET", f"https://api.ebay.com{path}")
        if path == "/sell/inventory/v1/inventory_item":
            return httpx.Response(200, json={"inventoryItems": [], "total": 0}, request=request)
        if path == "/buy/browse/v1/item_summary/search":
            assert params["filter"] == "sellers:{exactspec-electronics}"
            return httpx.Response(
                200,
                json={
                    "itemSummaries": [
                        {
                            "itemId": "v1|123456789012|0",
                            "legacyItemId": "123456789012",
                            "title": "Samsung Galaxy S25 Blue",
                            "itemWebUrl": "https://www.ebay.com/itm/123456789012",
                            "image": {"imageUrl": "https://i.ebayimg.com/images/g/demo/s-l300.jpg"},
                        }
                    ],
                    "total": 1,
                    "limit": 1,
                    "offset": 0,
                },
                request=request,
            )
        if path == "/buy/browse/v1/item/v1|123456789012|0":
            return httpx.Response(
                200,
                json={
                    "itemId": "v1|123456789012|0",
                    "legacyItemId": "123456789012",
                    "title": "Samsung Galaxy S25 Blue",
                    "shortDescription": "Open box phone with box.",
                    "condition": "Open box",
                    "price": {"value": "525.00", "currency": "USD"},
                    "itemWebUrl": "https://www.ebay.com/itm/123456789012",
                    "image": {"imageUrl": "https://i.ebayimg.com/images/g/demo/s-l300.jpg"},
                    "additionalImages": [
                        {"imageUrl": "https://i.ebayimg.com/images/g/demo/s-l1600.jpg"},
                    ],
                    "categoryPath": "Cell Phones & Smartphones",
                    "localizedAspects": [{"name": "Storage Capacity", "value": "128 GB"}],
                    "shippingOptions": [
                        {
                            "shippingCost": {"value": "0.00", "currency": "USD"},
                        }
                    ],
                    "estimatedAvailabilities": [
                        {
                            "estimatedAvailabilityStatus": "IN_STOCK",
                            "estimatedAvailableQuantity": 2,
                        }
                    ],
                },
                request=request,
            )
        raise AssertionError(f"Unexpected eBay path: {path}")

    monkeypatch.setattr(ebay_module.httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(handler))
    settings = SimpleNamespace(
        ebay_access_token="token",
        ebay_marketplace_id="EBAY_US",
        ebay_seller_username="exactspec-electronics",
        ebay_browse_search_query=" ",
    )

    items = asyncio.run(EbayClient(settings).fetch_inventory_items())

    assert len(items) == 1
    assert items[0].sku == "EBAY-123456789012"
    assert items[0].quantity == 2
    assert items[0].listing_status == "IN_STOCK"
    assert items[0].image_url == "https://i.ebayimg.com/images/g/demo/s-l1600.jpg"
    assert items[0].image_urls == [
        "https://i.ebayimg.com/images/g/demo/s-l300.jpg",
        "https://i.ebayimg.com/images/g/demo/s-l1600.jpg",
    ]
    assert items[0].item_specifics["Storage Capacity"] == "128 GB"
    assert items[0].item_specifics["Shipping"] == "Free Shipping"


def test_ebay_browse_api_expands_item_group_and_recovers_catalog_gtins(monkeypatch):
    def handler(path, params):
        request = httpx.Request("GET", f"https://api.ebay.com{path}")
        if path == "/sell/inventory/v1/inventory_item":
            return httpx.Response(200, json={"inventoryItems": [], "total": 0}, request=request)
        if path == "/buy/browse/v1/item_summary/search":
            return httpx.Response(
                200,
                json={
                    "itemSummaries": [
                        {
                            "itemId": "v1|123456789012|0",
                            "legacyItemId": "123456789012",
                            "title": "Apple iPhone 16 - Various Colors",
                        }
                    ],
                    "total": 1,
                },
                request=request,
            )
        if path == "/buy/browse/v1/item/v1|123456789012|0":
            assert params["fieldgroups"] == "PRODUCT"
            return httpx.Response(
                200,
                json={
                    "itemId": "v1|123456789012|0",
                    "legacyItemId": "123456789012",
                    "title": "Apple iPhone 16 - Various Colors",
                    "primaryItemGroup": {"itemGroupId": "123456789012"},
                },
                request=request,
            )
        if path == "/buy/browse/v1/item/get_items_by_item_group":
            assert params == {"item_group_id": "123456789012", "fieldgroups": "PRODUCT"}
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "itemId": "v1|123456789012|111",
                            "legacyItemId": "123456789012",
                            "title": "Apple iPhone 16 Blue 128 GB",
                            "condition": "Open box",
                            "price": {"value": "599.00", "currency": "USD"},
                            "image": {"imageUrl": "https://i.ebayimg.com/images/g/blue/s-l1600.jpg"},
                            "localizedAspects": [
                                {"name": "Color", "value": "Blue"},
                                {"name": "Storage Capacity", "value": "128 GB"},
                            ],
                            "product": {"gtins": ["0195949820093"], "brand": "Apple"},
                            "estimatedAvailabilities": [
                                {
                                    "estimatedAvailabilityStatus": "IN_STOCK",
                                    "estimatedRemainingQuantity": 2,
                                }
                            ],
                        },
                        {
                            "itemId": "v1|123456789012|222",
                            "legacyItemId": "123456789012",
                            "title": "Apple iPhone 16 Black 256 GB",
                            "condition": "Open box",
                            "price": {"value": "699.00", "currency": "USD"},
                            "image": {"imageUrl": "https://i.ebayimg.com/images/g/black/s-l1600.jpg"},
                            "localizedAspects": [
                                {"name": "Color", "value": "Black"},
                                {"name": "Storage Capacity", "value": "256 GB"},
                            ],
                            "product": {"gtins": ["0195949820109"], "brand": "Apple"},
                            "estimatedAvailabilities": [
                                {
                                    "estimatedAvailabilityStatus": "IN_STOCK",
                                    "estimatedRemainingQuantity": 1,
                                }
                            ],
                        },
                    ]
                },
                request=request,
            )
        raise AssertionError(f"Unexpected eBay path: {path}")

    monkeypatch.setattr(ebay_module.httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(handler))
    settings = SimpleNamespace(
        ebay_access_token="application-token",
        ebay_marketplace_id="EBAY_US",
        ebay_seller_username="exactspec-electronics",
        ebay_browse_search_query=" ",
        ebay_expand_item_groups=True,
    )

    items = asyncio.run(EbayClient(settings).fetch_inventory_items())

    assert len(items) == 2
    assert items[0].sku != items[1].sku
    assert all(item.sku.startswith("EBAY-123456789012-") for item in items)
    assert [item.quantity for item in items] == [2, 1]
    assert [item.item_specifics["GTIN"] for item in items] == [
        "0195949820093",
        "0195949820109",
    ]
    assert all(item.item_specifics["Brand"] == "Apple" for item in items)


def test_ebay_trading_parser_expands_variations_with_upcs_and_shipping_weight():
    payload = b"""<?xml version="1.0" encoding="utf-8"?>
    <GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <Item>
        <ItemID>123456789012</ItemID>
        <Title>Samsung Galaxy S25</Title>
        <Description><![CDATA[<p>Open box phone.</p>]]></Description>
        <ConditionID>1500</ConditionID>
        <ConditionDisplayName>Open box</ConditionDisplayName>
        <PrimaryCategory><CategoryID>9355</CategoryID><CategoryName>Cell Phones</CategoryName></PrimaryCategory>
        <PictureDetails><PictureURL>https://i.ebayimg.com/images/g/base/s-l1600.jpg</PictureURL></PictureDetails>
        <ShippingPackageDetails>
          <WeightMajor unit="lbs">1</WeightMajor>
          <WeightMinor unit="oz">8</WeightMinor>
        </ShippingPackageDetails>
        <ItemSpecifics>
          <NameValueList><Name>Brand</Name><Value>Samsung</Value></NameValueList>
        </ItemSpecifics>
        <SellingStatus>
          <ListingStatus>Active</ListingStatus>
          <CurrentPrice currencyID="USD">525.00</CurrentPrice>
        </SellingStatus>
        <Variations>
          <Pictures>
            <VariationSpecificName>Color</VariationSpecificName>
            <VariationSpecificPictureSet>
              <VariationSpecificValue>Blue</VariationSpecificValue>
              <PictureURL>https://i.ebayimg.com/images/g/blue/s-l1600.jpg</PictureURL>
            </VariationSpecificPictureSet>
          </Pictures>
          <Variation>
            <SKU>S25-BLUE-128</SKU>
            <StartPrice currencyID="USD">520.00</StartPrice>
            <Quantity>3</Quantity>
            <SellingStatus><QuantitySold>1</QuantitySold></SellingStatus>
            <VariationSpecifics>
              <NameValueList><Name>Color</Name><Value>Blue</Value></NameValueList>
              <NameValueList><Name>Storage Capacity</Name><Value>128 GB</Value></NameValueList>
            </VariationSpecifics>
            <VariationProductListingDetails><UPC>887276900123</UPC></VariationProductListingDetails>
          </Variation>
          <Variation>
            <StartPrice currencyID="USD">540.00</StartPrice>
            <Quantity>1</Quantity>
            <SellingStatus><QuantitySold>0</QuantitySold></SellingStatus>
            <VariationSpecifics>
              <NameValueList><Name>Color</Name><Value>Black</Value></NameValueList>
              <NameValueList><Name>Storage Capacity</Name><Value>256 GB</Value></NameValueList>
            </VariationSpecifics>
            <VariationProductListingDetails><UPC>887276900130</UPC></VariationProductListingDetails>
          </Variation>
        </Variations>
      </Item>
    </GetItemResponse>"""
    settings = SimpleNamespace(
        ebay_access_token="seller-token",
        ebay_marketplace_id="EBAY_US",
    )
    browse_item = InventoryItem(
        sku="EBAY-123456789012",
        title="Samsung Galaxy S25",
        condition="Open box",
        quantity=3,
        ebay_item_id="123456789012",
        ebay_url="https://www.ebay.com/itm/123456789012",
        image_url="https://i.ebayimg.com/images/g/base/s-l1600.jpg",
        image_urls=["https://i.ebayimg.com/images/g/base/s-l1600.jpg"],
        listing_status="IN_STOCK",
        source="ebay-browse-api",
    )

    items = EbayClient(settings)._parse_trading_get_item(payload, browse_item)

    assert len(items) == 2
    assert items[0].sku == "S25-BLUE-128"
    assert items[0].quantity == 2
    assert items[0].price == 520.0
    assert items[0].item_specifics["UPC"] == "887276900123"
    assert items[0].item_specifics["Shipping Weight"] == "1.5 lb"
    assert items[0].item_specifics["Brand"] == "Samsung"
    assert items[0].image_url == "https://i.ebayimg.com/images/g/blue/s-l1600.jpg"
    assert items[0].source == "ebay-trading-api"
    assert items[1].quantity == 1
    assert items[1].item_specifics["UPC"] == "887276900130"
    assert items[1].sku.startswith("EBAY-123456789012-")
    assert len(items[1].sku) <= 50


def test_ebay_lightweight_inventory_parser_includes_variations():
    payload = b"""<?xml version="1.0" encoding="utf-8"?>
    <GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <ActiveList><ItemArray>
        <Item>
          <ItemID>123</ItemID><SKU>PHONE-1</SKU><Quantity>5</Quantity>
          <SellingStatus><QuantitySold>2</QuantitySold></SellingStatus>
        </Item>
        <Item>
          <ItemID>456</ItemID><Variations>
            <Variation><SKU>WATCH-BLACK</SKU><Quantity>3</Quantity>
              <SellingStatus><QuantitySold>1</QuantitySold></SellingStatus></Variation>
            <Variation><SKU>WATCH-WHITE</SKU><Quantity>1</Quantity>
              <SellingStatus><QuantitySold>0</QuantitySold></SellingStatus></Variation>
          </Variations></Item>
      </ItemArray></ActiveList>
    </GetMyeBaySellingResponse>"""

    rows = EbayClient._parse_trading_active_inventory(payload)

    assert rows == [
        {"sku": "PHONE-1", "item_id": "123", "quantity": 3},
        {"sku": "WATCH-BLACK", "item_id": "456", "quantity": 2},
        {"sku": "WATCH-WHITE", "item_id": "456", "quantity": 1},
    ]


def test_ebay_lightweight_inventory_parser_includes_prices_for_seller_skus():
    payload = b"""<?xml version="1.0" encoding="utf-8"?>
    <GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <ActiveList><ItemArray>
        <Item>
          <ItemID>123</ItemID><SKU>PHONE-1</SKU>
          <StartPrice currencyID="USD">499.99</StartPrice><Quantity>1</Quantity>
        </Item>
        <Item>
          <ItemID>456</ItemID><Variations>
            <Variation><SKU>WATCH-BLACK</SKU>
              <StartPrice currencyID="USD">299.95</StartPrice><Quantity>2</Quantity>
            </Variation>
          </Variations>
        </Item>
      </ItemArray></ActiveList>
    </GetMyeBaySellingResponse>"""

    rows = EbayClient._parse_trading_active_inventory(payload)

    assert rows == [
        {
            "sku": "PHONE-1",
            "item_id": "123",
            "quantity": 1,
            "start_price": 499.99,
            "currency": "USD",
        },
        {
            "sku": "WATCH-BLACK",
            "item_id": "456",
            "quantity": 2,
            "start_price": 299.95,
            "currency": "USD",
        },
    ]


def test_ebay_lightweight_inventory_parser_includes_current_images():
    payload = b"""<?xml version="1.0" encoding="utf-8"?>
    <GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <ActiveList><ItemArray>
        <Item>
          <ItemID>456</ItemID>
          <PictureDetails>
            <PictureURL>https://i.ebayimg.com/images/g/base/s-l1600.jpg</PictureURL>
          </PictureDetails>
          <Variations>
            <Variation><SKU>WATCH-BLACK</SKU><Quantity>2</Quantity>
              <VariationSpecifics>
                <NameValueList><Name>Color</Name><Value>Black</Value></NameValueList>
              </VariationSpecifics>
            </Variation>
            <Pictures>
              <VariationSpecificName>Color</VariationSpecificName>
              <VariationSpecificPictureSet>
                <VariationSpecificValue>Black</VariationSpecificValue>
                <PictureURL>https://i.ebayimg.com/images/g/black/s-l1600.jpg</PictureURL>
              </VariationSpecificPictureSet>
            </Pictures>
          </Variations>
        </Item>
      </ItemArray></ActiveList>
    </GetMyeBaySellingResponse>"""

    rows = EbayClient._parse_trading_active_inventory(payload)

    assert rows[0]["image_urls"] == [
        "https://i.ebayimg.com/images/g/black/s-l1600.jpg",
        "https://i.ebayimg.com/images/g/base/s-l1600.jpg",
    ]
    assert rows[0]["image_complete"] is True


def test_ebay_lightweight_inventory_parser_uses_gallery_image_fallback():
    payload = b"""<?xml version="1.0" encoding="utf-8"?>
    <GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <ActiveList><ItemArray><Item>
        <ItemID>123</ItemID><SKU>PHONE-1</SKU><Quantity>1</Quantity>
        <PictureDetails>
          <GalleryURL>https://i.ebayimg.com/images/g/gallery/s-l1600.jpg</GalleryURL>
        </PictureDetails>
      </Item></ItemArray></ActiveList>
    </GetMyeBaySellingResponse>"""

    rows = EbayClient._parse_trading_active_inventory(payload)

    assert rows[0]["image_urls"] == [
        "https://i.ebayimg.com/images/g/gallery/s-l1600.jpg"
    ]
    assert rows[0]["image_complete"] is False


def test_ebay_lightweight_inventory_parser_generates_missing_skus():
    payload = b"""<?xml version="1.0" encoding="utf-8"?>
    <GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <ActiveList><ItemArray>
        <Item>
          <ItemID>123</ItemID><Quantity>5</Quantity>
          <SellingStatus><QuantitySold>2</QuantitySold></SellingStatus>
        </Item>
        <Item>
          <ItemID>456</ItemID><Variations>
            <Variation><StartPrice currencyID="USD">299.95</StartPrice><Quantity>3</Quantity>
              <VariationSpecifics>
                <NameValueList><Name>Color</Name><Value>Black</Value></NameValueList>
                <NameValueList><Name>Size</Name><Value>44 mm</Value></NameValueList>
              </VariationSpecifics>
              <SellingStatus><QuantitySold>1</QuantitySold></SellingStatus>
            </Variation>
          </Variations>
        </Item>
      </ItemArray></ActiveList>
    </GetMyeBaySellingResponse>"""

    rows = EbayClient._parse_trading_active_inventory(payload)

    assert rows[0] == {
        "sku": "EBAY-123",
        "item_id": "123",
        "quantity": 3,
        "inventory_tracking": "item_id",
    }
    assert rows[1] == {
        "sku": EbayClient._generated_variation_sku(
            "456", {"Color": "Black", "Size": "44 mm"}, 1
        ),
        "item_id": "456",
        "quantity": 2,
        "inventory_tracking": "variation_specifics",
        "variation_specifics": {"Color": "Black", "Size": "44 mm"},
        "start_price": 299.95,
        "currency": "USD",
    }


def test_ebay_revises_legacy_listing_quantity(monkeypatch):
    requests = []

    def handler(path, payload, headers, method):
        requests.append((method, path, payload, headers))
        request = httpx.Request(method, f"https://api.ebay.com{path}")
        if method == "GET" and path == "/sell/inventory/v1/offer":
            return httpx.Response(404, json={}, request=request)
        if method == "POST" and path == "/ws/api.dll":
            assert headers["X-EBAY-API-CALL-NAME"] == "ReviseInventoryStatus"
            assert b"<ItemID>123</ItemID>" in payload
            assert b"<SKU>PHONE-1</SKU>" in payload
            assert b"<Quantity>2</Quantity>" in payload
            return httpx.Response(
                200,
                content=b'<ReviseInventoryStatusResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack></ReviseInventoryStatusResponse>',
                request=request,
            )
        raise AssertionError(f"Unexpected eBay request: {method} {path}")

    monkeypatch.setattr(ebay_module.httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(handler))
    settings = SimpleNamespace(
        ebay_access_token="seller-token",
        ebay_marketplace_id="EBAY_US",
        ebay_trading_compatibility_level="1455",
    )

    result = asyncio.run(
        EbayClient(settings).revise_inventory_quantities(
            [{"sku": "PHONE-1", "item_id": "123", "quantity": 2}]
        )
    )

    assert result[0]["status"] == "updated"
    assert result[0]["api"] == "trading"


def test_ebay_revises_quantities_without_seller_skus(monkeypatch):
    revise_payloads = []

    def handler(path, payload, headers, method):
        request = httpx.Request(method, f"https://api.ebay.com{path}")
        if method == "GET" and path == "/sell/inventory/v1/offer":
            return httpx.Response(404, json={}, request=request)
        if method == "POST" and path == "/ws/api.dll":
            assert headers["X-EBAY-API-CALL-NAME"] == "ReviseFixedPriceItem"
            revise_payloads.append(payload)
            return httpx.Response(
                200,
                content=b'<ReviseFixedPriceItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack></ReviseFixedPriceItemResponse>',
                request=request,
            )
        raise AssertionError(f"Unexpected eBay request: {method} {path}")

    monkeypatch.setattr(ebay_module.httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(handler))
    settings = SimpleNamespace(
        ebay_access_token="seller-token",
        ebay_marketplace_id="EBAY_US",
        ebay_trading_compatibility_level="1455",
    )

    results = asyncio.run(
        EbayClient(settings).revise_inventory_quantities(
            [
                {
                    "sku": "EBAY-123",
                    "item_id": "123",
                    "quantity": 2,
                    "inventory_tracking": "item_id",
                },
                {
                    "sku": "EBAY-456-generated",
                    "item_id": "456",
                    "quantity": 4,
                    "inventory_tracking": "variation_specifics",
                    "variation_specifics": {"Color": "Black", "Size": "44 mm"},
                    "start_price": 299.95,
                    "currency": "USD",
                },
            ]
        )
    )

    assert len(revise_payloads) == 2
    assert b"<ItemID>123</ItemID>" in revise_payloads[0]
    assert b"<Quantity>2</Quantity>" in revise_payloads[0]
    assert b"<Variations>" not in revise_payloads[0]
    assert b"<ItemID>456</ItemID>" in revise_payloads[1]
    assert b'<StartPrice currencyID="USD">299.95</StartPrice>' in revise_payloads[1]
    assert b"<Quantity>4</Quantity>" in revise_payloads[1]
    assert b"<Name>Color</Name>" in revise_payloads[1]
    assert b"<Value>44 mm</Value>" in revise_payloads[1]
    assert [row["api"] for row in results] == ["trading_item", "trading_variation"]
    assert all(row["status"] == "updated" for row in results)


def test_ebay_trading_enrichment_uses_refreshed_seller_token(monkeypatch):
    payload = b"""<?xml version="1.0" encoding="utf-8"?>
    <GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <Item>
        <ItemID>123456789012</ItemID><SKU>PHONE-1</SKU><Title>Phone</Title>
        <ConditionDisplayName>Open box</ConditionDisplayName><Quantity>2</Quantity>
        <PictureDetails><PictureURL>https://i.ebayimg.com/images/g/demo/s-l1600.jpg</PictureURL></PictureDetails>
        <ProductListingDetails><UPC>887276900123</UPC></ProductListingDetails>
        <SellingStatus><ListingStatus>Active</ListingStatus><QuantitySold>1</QuantitySold>
          <CurrentPrice currencyID="USD">500.00</CurrentPrice></SellingStatus>
      </Item>
    </GetItemResponse>"""

    def handler(path, request_payload, headers, method):
        request = httpx.Request(method, f"https://api.ebay.com{path}")
        if path == "/identity/v1/oauth2/token":
            assert request_payload["grant_type"] == "refresh_token"
            return httpx.Response(200, json={"access_token": "fresh-seller-token"}, request=request)
        if path == "/ws/api.dll":
            assert headers["X-EBAY-API-IAF-TOKEN"] == "fresh-seller-token"
            assert headers["X-EBAY-API-CALL-NAME"] == "GetItem"
            assert b"<ItemID>123456789012</ItemID>" in request_payload
            return httpx.Response(200, content=payload, request=request)
        raise AssertionError(f"Unexpected eBay request: {method} {path}")

    monkeypatch.setattr(ebay_module.httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(handler))
    settings = SimpleNamespace(
        ebay_access_token=None,
        ebay_client_id="client-id",
        ebay_client_secret="client-secret",
        ebay_refresh_token="refresh-token",
        ebay_oauth_scopes="https://api.ebay.com/oauth/api_scope",
        ebay_marketplace_id="EBAY_US",
        ebay_trading_compatibility_level="1455",
    )
    browse_item = InventoryItem(
        sku="EBAY-123456789012",
        title="Phone",
        condition="Open box",
        quantity=1,
        ebay_item_id="123456789012",
        image_url="https://i.ebayimg.com/images/g/demo/s-l1600.jpg",
        image_urls=["https://i.ebayimg.com/images/g/demo/s-l1600.jpg"],
        listing_status="IN_STOCK",
        source="ebay-browse-api",
    )

    items = asyncio.run(EbayClient(settings)._enrich_with_trading_api([browse_item]))

    assert [item.sku for item in items] == ["PHONE-1"]
    assert items[0].quantity == 1
    assert items[0].item_specifics["UPC"] == "887276900123"


def test_ebay_client_retries_temporary_browse_api_failures(monkeypatch):
    calls = {"search": 0}

    async def fake_sleep(delay):
        return None

    def handler(path, params):
        request = httpx.Request("GET", f"https://api.ebay.com{path}")
        if path == "/sell/inventory/v1/inventory_item":
            return httpx.Response(200, json={"inventoryItems": [], "total": 0}, request=request)
        if path == "/buy/browse/v1/item_summary/search":
            calls["search"] += 1
            if calls["search"] == 1:
                return httpx.Response(503, json={"error": "temporary"}, request=request)
            return httpx.Response(
                200,
                json={
                    "itemSummaries": [
                        {
                            "itemId": "v1|123456789012|0",
                            "legacyItemId": "123456789012",
                            "title": "Samsung Galaxy S25 Blue",
                            "itemWebUrl": "https://www.ebay.com/itm/123456789012",
                            "image": {"imageUrl": "https://i.ebayimg.com/images/g/demo/s-l1600.jpg"},
                            "estimatedAvailabilities": [
                                {
                                    "estimatedAvailabilityStatus": "IN_STOCK",
                                    "estimatedAvailableQuantity": 1,
                                }
                            ],
                        }
                    ],
                    "total": 1,
                },
                request=request,
            )
        if path == "/buy/browse/v1/item/v1|123456789012|0":
            return httpx.Response(
                404,
                json={},
                request=request,
            )
        raise AssertionError(f"Unexpected eBay path: {path}")

    monkeypatch.setattr(ebay_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(ebay_module.httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(handler))
    settings = SimpleNamespace(
        ebay_access_token="token",
        ebay_marketplace_id="EBAY_US",
        ebay_seller_username="exactspec-electronics",
        ebay_browse_search_query=" ",
    )

    items = asyncio.run(EbayClient(settings).fetch_inventory_items())

    assert calls["search"] == 2
    assert [item.ebay_item_id for item in items] == ["123456789012"]


def test_ebay_client_skips_browse_listing_without_primary_image(monkeypatch, caplog):
    def handler(path, params):
        request = httpx.Request("GET", f"https://api.ebay.com{path}")
        if path == "/sell/inventory/v1/inventory_item":
            return httpx.Response(200, json={"inventoryItems": [], "total": 0}, request=request)
        if path == "/buy/browse/v1/item_summary/search":
            return httpx.Response(
                200,
                json={
                    "itemSummaries": [
                        {
                            "itemId": "v1|123456789012|0",
                            "legacyItemId": "123456789012",
                            "title": "Samsung Galaxy S25 Blue",
                            "itemWebUrl": "https://www.ebay.com/itm/123456789012",
                            "estimatedAvailabilities": [
                                {
                                    "estimatedAvailabilityStatus": "IN_STOCK",
                                    "estimatedAvailableQuantity": 1,
                                }
                            ],
                        }
                    ],
                    "total": 1,
                },
                request=request,
            )
        if path == "/buy/browse/v1/item/v1|123456789012|0":
            return httpx.Response(404, json={}, request=request)
        raise AssertionError(f"Unexpected eBay path: {path}")

    monkeypatch.setattr(ebay_module.httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(handler))
    settings = SimpleNamespace(
        ebay_access_token="token",
        ebay_marketplace_id="EBAY_US",
        ebay_seller_username="exactspec-electronics",
        ebay_browse_search_query=" ",
    )

    with caplog.at_level("WARNING", logger="app.ebay"):
        items = asyncio.run(EbayClient(settings).fetch_inventory_items())

    assert items == []
    assert "listing has no valid primary eBay image" in caplog.text


def test_ebay_client_refreshes_access_token_before_sync(monkeypatch):
    requests = []

    def handler(path, payload, headers, method):
        requests.append((method, path, payload, headers.get("Authorization", "")))
        request = httpx.Request(method, f"https://api.ebay.com{path}")
        if method == "POST" and path == "/identity/v1/oauth2/token":
            assert payload["scope"] == "https://api.ebay.com/oauth/api_scope"
            assert headers["Authorization"].startswith("Basic ")
            if payload["grant_type"] == "refresh_token":
                assert payload["refresh_token"] == "refresh-token"
                return httpx.Response(200, json={"access_token": "fresh-access-token", "expires_in": 7200}, request=request)
            if payload["grant_type"] == "client_credentials":
                return httpx.Response(200, json={"access_token": "application-access-token", "expires_in": 7200}, request=request)
        if path == "/sell/inventory/v1/inventory_item":
            assert headers["Authorization"] == "Bearer fresh-access-token"
            return httpx.Response(200, json={"inventoryItems": [], "total": 0}, request=request)
        if path == "/buy/browse/v1/item_summary/search":
            assert headers["Authorization"] == "Bearer application-access-token"
            return httpx.Response(200, json={"itemSummaries": [], "total": 0}, request=request)
        raise AssertionError(f"Unexpected eBay request: {method} {path}")

    monkeypatch.setattr(ebay_module.httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(handler))
    settings = SimpleNamespace(
        ebay_access_token=None,
        ebay_client_id="client-id",
        ebay_client_secret="client-secret",
        ebay_refresh_token="refresh-token",
        ebay_oauth_scopes="https://api.ebay.com/oauth/api_scope",
        ebay_marketplace_id="EBAY_US",
        ebay_seller_username="exactspec-electronics",
        ebay_browse_search_query=" ",
    )

    items = asyncio.run(EbayClient(settings).fetch_inventory_items())

    assert items == []
    assert requests[0][0:2] == ("POST", "/identity/v1/oauth2/token")
    assert requests[1][0:2] == ("GET", "/sell/inventory/v1/inventory_item")


def test_ebay_client_mints_application_token_with_client_credentials(monkeypatch):
    requests = []

    def handler(path, payload, headers, method):
        requests.append((method, path, payload, headers.get("Authorization", "")))
        request = httpx.Request(method, f"https://api.ebay.com{path}")
        if method == "POST" and path == "/identity/v1/oauth2/token":
            assert payload["grant_type"] == "client_credentials"
            assert payload["scope"] == "https://api.ebay.com/oauth/api_scope"
            assert headers["Authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "application-access-token", "expires_in": 7200}, request=request)
        if path == "/sell/inventory/v1/inventory_item":
            assert headers["Authorization"] == "Bearer application-access-token"
            return httpx.Response(200, json={"inventoryItems": [], "total": 0}, request=request)
        if path == "/buy/browse/v1/item_summary/search":
            assert headers["Authorization"] == "Bearer application-access-token"
            return httpx.Response(200, json={"itemSummaries": [], "total": 0}, request=request)
        raise AssertionError(f"Unexpected eBay request: {method} {path}")

    monkeypatch.setattr(ebay_module.httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(handler))
    settings = SimpleNamespace(
        ebay_access_token=None,
        ebay_client_id="client-id",
        ebay_client_secret="client-secret",
        ebay_refresh_token=None,
        ebay_oauth_scopes="https://api.ebay.com/oauth/api_scope",
        ebay_marketplace_id="EBAY_US",
        ebay_seller_username="exactspec-electronics",
        ebay_browse_search_query=" ",
    )

    items = asyncio.run(EbayClient(settings).fetch_inventory_items())

    assert items == []
    assert requests[0][0:2] == ("POST", "/identity/v1/oauth2/token")
    assert requests[1][0:2] == ("GET", "/sell/inventory/v1/inventory_item")


def test_ebay_client_uses_application_token_for_browse_after_refresh_token_sell_sync(monkeypatch):
    requests = []

    def handler(path, payload, headers, method):
        requests.append((method, path, payload, headers.get("Authorization", "")))
        request = httpx.Request(method, f"https://api.ebay.com{path}")
        if method == "POST" and path == "/identity/v1/oauth2/token":
            if payload["grant_type"] == "refresh_token":
                return httpx.Response(200, json={"access_token": "seller-user-token"}, request=request)
            if payload["grant_type"] == "client_credentials":
                return httpx.Response(200, json={"access_token": "browse-application-token"}, request=request)
        if path == "/sell/inventory/v1/inventory_item":
            assert headers["Authorization"] == "Bearer seller-user-token"
            return httpx.Response(200, json={"inventoryItems": [], "total": 0}, request=request)
        if path == "/buy/browse/v1/item_summary/search":
            assert headers["Authorization"] == "Bearer browse-application-token"
            return httpx.Response(200, json={"itemSummaries": [], "total": 0}, request=request)
        raise AssertionError(f"Unexpected eBay request: {method} {path}")

    monkeypatch.setattr(ebay_module.httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(handler))
    settings = SimpleNamespace(
        ebay_access_token=None,
        ebay_client_id="client-id",
        ebay_client_secret="client-secret",
        ebay_refresh_token="refresh-token",
        ebay_oauth_scopes="https://api.ebay.com/oauth/api_scope",
        ebay_marketplace_id="EBAY_US",
        ebay_seller_username="exactspec-electronics",
        ebay_browse_search_query=" ",
    )

    items = asyncio.run(EbayClient(settings).fetch_inventory_items())

    assert items == []
    assert requests[0][2]["grant_type"] == "refresh_token"
    assert requests[2][2]["grant_type"] == "client_credentials"


def test_ebay_client_restores_static_user_token_for_trading_after_browse(monkeypatch):
    requests = []
    trading_payload = b"""<?xml version="1.0" encoding="utf-8"?>
    <GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <Item>
        <ItemID>123456789012</ItemID><SKU>PHONE-1</SKU><Title>Phone</Title>
        <ConditionDisplayName>Open box</ConditionDisplayName><Quantity>1</Quantity>
        <PictureDetails><PictureURL>https://i.ebayimg.com/images/g/demo/s-l1600.jpg</PictureURL></PictureDetails>
        <ShippingPackageDetails><WeightMajor unit="lbs">1</WeightMajor></ShippingPackageDetails>
        <ProductListingDetails><UPC>887276900123</UPC></ProductListingDetails>
        <SellingStatus><ListingStatus>Active</ListingStatus><QuantitySold>0</QuantitySold>
          <CurrentPrice currencyID="USD">500.00</CurrentPrice></SellingStatus>
      </Item>
    </GetItemResponse>"""

    def handler(path, payload, headers, method):
        requests.append((method, path, headers))
        request = httpx.Request(method, f"https://api.ebay.com{path}")
        if path == "/sell/inventory/v1/inventory_item":
            assert headers["Authorization"] == "Bearer seller-user-token"
            return httpx.Response(403, json={"error": "missing sell scope"}, request=request)
        if path == "/identity/v1/oauth2/token":
            assert payload["grant_type"] == "client_credentials"
            return httpx.Response(200, json={"access_token": "browse-application-token"}, request=request)
        if path == "/buy/browse/v1/item_summary/search":
            assert headers["Authorization"] == "Bearer browse-application-token"
            return httpx.Response(
                200,
                json={
                    "itemSummaries": [
                        {
                            "itemId": "v1|123456789012|0",
                            "legacyItemId": "123456789012",
                            "title": "Phone",
                            "image": {"imageUrl": "https://i.ebayimg.com/images/g/demo/s-l1600.jpg"},
                            "estimatedAvailabilities": [
                                {"estimatedAvailabilityStatus": "IN_STOCK", "estimatedAvailableQuantity": 1}
                            ],
                        }
                    ],
                    "total": 1,
                },
                request=request,
            )
        if path == "/buy/browse/v1/item/v1|123456789012|0":
            return httpx.Response(404, json={}, request=request)
        if path == "/ws/api.dll":
            assert headers["X-EBAY-API-IAF-TOKEN"] == "seller-user-token"
            return httpx.Response(200, content=trading_payload, request=request)
        raise AssertionError(f"Unexpected eBay request: {method} {path}")

    monkeypatch.setattr(ebay_module.httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(handler))
    settings = SimpleNamespace(
        ebay_access_token="seller-user-token",
        ebay_client_id="client-id",
        ebay_client_secret="client-secret",
        ebay_refresh_token=None,
        ebay_oauth_scopes="https://api.ebay.com/oauth/api_scope",
        ebay_marketplace_id="EBAY_US",
        ebay_seller_username="exactspec-electronics",
        ebay_browse_search_query=" ",
        ebay_expand_item_groups=False,
        ebay_use_access_token_for_trading=True,
        ebay_trading_compatibility_level="1455",
    )

    items = asyncio.run(EbayClient(settings).fetch_inventory_items())

    assert [item.sku for item in items] == ["PHONE-1"]
    assert items[0].source == "ebay-trading-api"
    assert items[0].item_specifics["UPC"] == "887276900123"
    assert items[0].item_specifics["Shipping Weight"] == "1 lb"


def test_ebay_client_renews_application_token_once_after_browse_401(monkeypatch):
    token_calls = 0
    browse_authorizations = []

    def handler(path, payload, headers, method):
        nonlocal token_calls
        request = httpx.Request(method, f"https://api.ebay.com{path}")
        if method == "POST" and path == "/identity/v1/oauth2/token":
            token_calls += 1
            assert payload["grant_type"] == "client_credentials"
            return httpx.Response(200, json={"access_token": f"app-token-{token_calls}"}, request=request)
        if path == "/buy/browse/v1/item_summary/search":
            browse_authorizations.append(headers["Authorization"])
            if len(browse_authorizations) == 1:
                return httpx.Response(401, json={"error": "unauthorized"}, request=request)
            return httpx.Response(
                200,
                json={
                    "itemSummaries": [
                        {
                            "itemId": "v1|123456789012|0",
                            "legacyItemId": "123456789012",
                            "title": "Samsung Galaxy S25 Blue",
                            "itemWebUrl": "https://www.ebay.com/itm/123456789012",
                            "image": {"imageUrl": "https://i.ebayimg.com/images/g/demo/s-l1600.jpg"},
                            "estimatedAvailabilities": [
                                {
                                    "estimatedAvailabilityStatus": "IN_STOCK",
                                    "estimatedAvailableQuantity": 1,
                                }
                            ],
                        }
                    ],
                    "total": 1,
                },
                request=request,
            )
        if path == "/buy/browse/v1/item/v1|123456789012|0":
            return httpx.Response(404, json={}, request=request)
        raise AssertionError(f"Unexpected eBay request: {method} {path}")

    monkeypatch.setattr(ebay_module.httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(handler))
    settings = SimpleNamespace(
        ebay_access_token="Bearer stale-token",
        ebay_client_id="client-id",
        ebay_client_secret="client-secret",
        ebay_refresh_token=None,
        ebay_oauth_scopes="https://api.ebay.com/oauth/api_scope",
        ebay_marketplace_id="EBAY_US",
        ebay_seller_username="exactspec-electronics",
        ebay_browse_search_query=" ",
    )

    items = asyncio.run(EbayClient(settings)._fetch_browse_seller_items("exactspec-electronics"))

    assert token_calls == 2
    assert browse_authorizations == ["Bearer app-token-1", "Bearer app-token-2"]
    assert [item.ebay_item_id for item in items] == ["123456789012"]
