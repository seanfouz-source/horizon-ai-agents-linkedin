import asyncio
import copy
import json
import re
import uuid
from base64 import b64encode
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import unescape
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import quote

import httpx

from app.config import Settings
from app.models import InventoryItem, WalmartItemOverride


RETRY_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0)
PRODUCT_ID_TYPES = ("GTIN", "UPC", "EAN", "ISBN")
CONDITION_IMAGE_REQUIRED = {
    "Remanufactured",
    "Pre-Owned: Like New",
    "Pre-Owned: Good",
    "Pre-Owned: Fair",
    "New with defects",
}
SUPPORTED_CONDITIONS = {
    "Pre-Owned: Fair",
    "Remanufactured",
    "New with defects",
    "Open Box",
    "Pre-Owned: Good",
    "New without box",
    "New",
    "New without tags",
    "Pre-Owned: Like New",
}


class WalmartApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class WalmartMarketplaceClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = str(settings.walmart_api_base_url).rstrip("/")
        self._access_token: str | None = None
        self._token_expires_in: int | None = None

    @property
    def configured(self) -> bool:
        return bool(
            str(getattr(self.settings, "walmart_client_id", "") or "").strip()
            and str(getattr(self.settings, "walmart_client_secret", "") or "").strip()
        )

    async def verify_credentials(self) -> dict[str, Any]:
        token = await self._get_access_token(force=True)
        return {
            "status": "ok",
            "configured": True,
            "access_token_received": bool(token),
            "expires_in": self._token_expires_in,
            "environment": "sandbox" if "sandbox" in self.base_url.lower() else "production",
        }

    async def search_catalog(
        self,
        product_id_type: str,
        product_id: str,
        *,
        response_format: str = "SPEC",
    ) -> dict[str, Any]:
        identifier_type = str(product_id_type).strip().upper()
        if identifier_type not in PRODUCT_ID_TYPES:
            return {
                "status": "not_checked",
                "matched": None,
                "reason": f"Walmart catalog search does not support {identifier_type} identifiers.",
            }

        response = await self._request(
            "GET",
            "/v3/items/walmart/search",
            params={identifier_type.lower(): product_id, "responseFormat": response_format},
        )
        payload = self._json_object(response)
        items = payload.get("items")
        results = items if isinstance(items, list) else []
        first = results[0] if results and isinstance(results[0], dict) else None
        feed_type = str(first.get("feedType") or "") if first else ""
        matched = feed_type == "MP_ITEM_MATCH"
        return {
            "status": (
                "matched"
                if matched
                else "full_item_required"
                if feed_type == "MP_ITEM"
                else "not_matched"
            ),
            "matched": matched,
            "feed_type": feed_type or None,
            "version": first.get("version") if first else None,
            "product_type": first.get("productType") if first else None,
            "item_spec_payload": first.get("itemSpecPayload") if first else None,
        }

    async def search_catalog_by_query(self, query: str, *, limit: int = 5) -> dict[str, Any]:
        clean_query = re.sub(r"\s+", " ", str(query or "")).strip()
        if not clean_query:
            return {
                "status": "not_checked",
                "query": clean_query,
                "total_candidates": 0,
                "candidates": [],
                "reason": "No catalog search query could be built from the eBay listing.",
            }

        response = await self._request(
            "GET",
            "/v3/items/walmart/search",
            params={"query": clean_query, "responseFormat": "DEFAULT"},
        )
        payload = self._json_object(response)
        raw_items = payload.get("items")
        items = raw_items if isinstance(raw_items, list) else []
        candidates = [
            candidate
            for candidate in (_catalog_candidate(item) for item in items[: max(1, min(limit, 10))])
            if candidate
        ]
        return {
            "status": "candidates_found" if candidates else "no_candidates",
            "query": clean_query,
            "total_candidates": len(items),
            "candidates": candidates,
        }

    async def enrich_catalog_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """Add the public product identifier and weight for one Walmart item candidate."""
        item_id = str(candidate.get("walmart_item_id") or "").strip()
        if not item_id or not re.fullmatch(r"\d+", item_id):
            raise WalmartApiError("The Walmart catalog candidate did not include a valid item ID.")

        url = f"https://www.walmart.com/ip/{quote(item_id, safe='')}"
        response: httpx.Response | None = None
        last_transport_error: httpx.TransportError | None = None
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
                try:
                    response = await client.get(
                        url,
                        headers={
                            "Accept": "text/html,application/xhtml+xml",
                            "User-Agent": (
                                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/140.0.0.0 Safari/537.36"
                            ),
                        },
                    )
                except httpx.TransportError as exc:
                    last_transport_error = exc
                    if attempt >= len(RETRY_DELAYS_SECONDS):
                        break
                    await asyncio.sleep(RETRY_DELAYS_SECONDS[attempt])
                    continue
                if response.status_code not in RETRY_STATUS_CODES or attempt >= len(RETRY_DELAYS_SECONDS):
                    break
                await asyncio.sleep(RETRY_DELAYS_SECONDS[attempt])

        if response is None:
            message = "The public Walmart product page could not be loaded."
            if last_transport_error:
                message = f"{message} {last_transport_error.__class__.__name__}."
            raise WalmartApiError(message)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise WalmartApiError(
                f"The public Walmart product page returned HTTP {response.status_code}.",
                status_code=response.status_code,
            ) from exc

        enrichment = parse_walmart_product_page(response.text)
        return {
            **candidate,
            **{key: value for key, value in enrichment.items() if value not in (None, "", {}, [])},
            "public_product_url": str(response.url),
        }

    async def submit_offer_match_feed(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/v3/feeds",
            params={"feedType": "MP_ITEM_MATCH"},
            json=payload,
        )
        result = self._json_object(response)
        feed_id = result.get("feedId")
        if not isinstance(feed_id, str) or not feed_id:
            raise WalmartApiError("Walmart accepted the request but did not return a feedId.")
        return {
            "status": "submitted",
            "feed_type": "MP_ITEM_MATCH",
            "feed_id": feed_id,
            "correlation_id": response.request.headers.get("WM_QOS.CORRELATION_ID"),
        }

    async def submit_full_item_feed(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        response = await self._request(
            "POST",
            "/v3/feeds",
            params={"feedType": "MP_ITEM"},
            files={"file": ("walmart-full-item.json", content, "application/json")},
        )
        result = self._json_object(response)
        feed_id = result.get("feedId")
        if not isinstance(feed_id, str) or not feed_id:
            raise WalmartApiError("Walmart accepted the request but did not return a feedId.")
        return {
            "status": "submitted",
            "feed_type": "MP_ITEM",
            "feed_id": feed_id,
            "correlation_id": response.request.headers.get("WM_QOS.CORRELATION_ID"),
        }

    async def submit_inventory_feed(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        response = await self._request(
            "POST",
            "/v3/feeds",
            params={"feedType": "inventory"},
            files={"file": ("walmart-inventory.json", content, "application/json")},
        )
        result = self._json_object(response)
        feed_id = result.get("feedId")
        if not isinstance(feed_id, str) or not feed_id:
            raise WalmartApiError("Walmart accepted the inventory request but did not return a feedId.")
        return {
            "status": "submitted",
            "feed_type": "inventory",
            "feed_id": feed_id,
            "correlation_id": response.request.headers.get("WM_QOS.CORRELATION_ID"),
        }

    async def submit_item_maintenance_feed(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/v3/feeds",
            params={"feedType": "MP_MAINTENANCE"},
            json=payload,
        )
        result = self._json_object(response)
        feed_id = result.get("feedId")
        if not isinstance(feed_id, str) or not feed_id:
            raise WalmartApiError(
                "Walmart accepted the item maintenance request but did not return a feedId."
            )
        return {
            "status": "submitted",
            "feed_type": "MP_MAINTENANCE",
            "feed_id": feed_id,
            "correlation_id": response.request.headers.get("WM_QOS.CORRELATION_ID"),
        }

    async def get_feed_status(
        self,
        feed_id: str,
        *,
        include_details: bool = True,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        clean_feed_id = str(feed_id or "").strip()
        if not clean_feed_id:
            raise WalmartApiError("A Walmart feedId is required.")
        response = await self._request(
            "GET",
            f"/v3/feeds/{quote(clean_feed_id, safe='')}",
            params={
                "includeDetails": str(bool(include_details)).lower(),
                "offset": max(0, offset),
                "limit": max(1, min(limit, 50)),
            },
        )
        return self._json_object(response)

    async def list_published_items(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        try:
            response = await self._request(
                "GET",
                "/v3/items",
                params={
                    "publishedStatus": "PUBLISHED",
                    "lifecycleStatus": "ACTIVE",
                    "limit": max(1, min(limit, 1000)),
                },
            )
        except WalmartApiError as exc:
            # Newly approved sellers can receive a 404 while their catalog is
            # still empty. Treat that response as an empty published catalog;
            # once Walmart has a published item, this endpoint returns its SKU.
            if exc.status_code == 404:
                return []
            raise
        payload = self._json_object(response)
        raw_items = payload.get("ItemResponse") or payload.get("items") or []
        if isinstance(raw_items, dict):
            raw_items = raw_items.get("Item") or raw_items.get("items") or []
        if not isinstance(raw_items, list):
            raw_items = []
        return [
            summary
            for item in raw_items
            if isinstance(item, dict)
            and (summary := _published_item_summary(item)) is not None
        ]

    async def get_item_details(self, sku: str) -> dict[str, Any]:
        clean_sku = str(sku or "").strip()
        if not clean_sku:
            raise WalmartApiError("A Walmart seller SKU is required.")
        response = await self._request(
            "GET",
            f"/v3/items/{quote(clean_sku, safe='')}",
            params={"productIdType": "SKU"},
        )
        payload = self._json_object(response)
        raw_item: object = payload.get("ItemResponse") or payload.get("itemResponse") or payload
        if isinstance(raw_item, list):
            raw_item = raw_item[0] if raw_item else None
        if not isinstance(raw_item, dict):
            raise WalmartApiError(
                f"Walmart item details returned an unexpected response for SKU {clean_sku}."
            )
        summary = _published_item_summary(raw_item)
        if summary is None:
            raise WalmartApiError(
                f"Walmart item details did not include a SKU for {clean_sku}."
            )
        return summary

    async def get_inventory_quantity(self, sku: str) -> int:
        clean_sku = str(sku or "").strip()
        if not clean_sku:
            raise WalmartApiError("A Walmart seller SKU is required.")
        response = await self._request(
            "GET",
            "/v3/inventory",
            params={"sku": clean_sku},
        )
        payload = self._json_object(response)
        quantity = payload.get("quantity")
        if isinstance(quantity, dict):
            quantity = quantity.get("amount")
        if quantity is None and isinstance(payload.get("inventory"), dict):
            quantity = payload["inventory"].get("quantity")
            if isinstance(quantity, dict):
                quantity = quantity.get("amount")
        try:
            return max(0, int(float(quantity)))
        except (TypeError, ValueError) as exc:
            raise WalmartApiError(
                f"Walmart inventory response did not include a quantity for SKU {clean_sku}."
            ) from exc

    async def update_price(
        self,
        sku: str,
        amount: float,
        *,
        currency: str = "USD",
    ) -> dict[str, Any]:
        clean_sku = str(sku or "").strip()
        if not clean_sku:
            raise WalmartApiError("A Walmart seller SKU is required.")
        clean_currency = str(currency or "USD").strip().upper()
        if clean_currency != "USD":
            raise WalmartApiError(
                f"Walmart US price updates require USD; received {clean_currency or 'an empty currency'}."
            )
        try:
            clean_amount = float(
                Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise WalmartApiError("Walmart price updates require a numeric amount.") from exc
        if clean_amount <= 0:
            raise WalmartApiError("Walmart price updates require a positive amount.")

        response = await self._request(
            "PUT",
            "/v3/price",
            headers={"Content-Type": "application/json"},
            json={
                "sku": clean_sku,
                "pricing": [
                    {
                        "currentPriceType": "BASE",
                        "currentPrice": {
                            "currency": clean_currency,
                            "amount": clean_amount,
                        },
                    }
                ],
            },
        )
        result = self._json_object(response)
        return {
            "status": "updated",
            "sku": clean_sku,
            "price": clean_amount,
            "currency": clean_currency,
            "response": result,
            "correlation_id": response.request.headers.get("WM_QOS.CORRELATION_ID"),
        }

    async def _get_access_token(self, *, force: bool = False) -> str:
        if self._access_token and not force:
            return self._access_token
        if not self.configured:
            raise WalmartApiError(
                "WALMART_CLIENT_ID and WALMART_CLIENT_SECRET are required for Walmart Marketplace API calls."
            )

        client_id = str(self.settings.walmart_client_id or "").strip()
        client_secret = str(self.settings.walmart_client_secret or "").strip()
        credentials = b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        correlation_id = str(uuid.uuid4())
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            response = await client.post(
                "/v3/token",
                data={"grant_type": "client_credentials"},
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "WM_QOS.CORRELATION_ID": correlation_id,
                    "WM_SVC.NAME": self.settings.walmart_service_name,
                },
            )
        self._raise_for_status(response, "Walmart OAuth token request")
        payload = self._json_object(response)
        token_payload = payload
        nested = payload.get("clientCredentialsRes")
        if isinstance(nested, dict) and isinstance(nested.get("value"), dict):
            token_payload = nested["value"]
        token = token_payload.get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise WalmartApiError("Walmart OAuth response did not include an access token.")
        self._access_token = token.strip()
        try:
            self._token_expires_in = int(token_payload.get("expires_in") or 900)
        except (TypeError, ValueError):
            self._token_expires_in = 900
        return self._access_token

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        token = await self._get_access_token()
        headers = {
            "Accept": "application/json",
            "WM_SEC.ACCESS_TOKEN": token,
            "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
            "WM_SVC.NAME": self.settings.walmart_service_name,
            "WM_MARKET": self.settings.walmart_market,
        }
        channel_type = str(getattr(self.settings, "walmart_channel_type", "") or "").strip()
        if channel_type:
            headers["WM_CONSUMER.CHANNEL.TYPE"] = channel_type
        headers.update(kwargs.pop("headers", {}))

        response: httpx.Response | None = None
        last_transport_error: httpx.TransportError | None = None
        async with httpx.AsyncClient(base_url=self.base_url, timeout=60) as client:
            for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
                headers["WM_QOS.CORRELATION_ID"] = str(uuid.uuid4())
                try:
                    response = await client.request(method, path, headers=headers, **kwargs)
                except httpx.TransportError as exc:
                    last_transport_error = exc
                    if attempt >= len(RETRY_DELAYS_SECONDS):
                        break
                    await asyncio.sleep(RETRY_DELAYS_SECONDS[attempt])
                    continue
                if response.status_code == 401 and attempt == 0:
                    headers["WM_SEC.ACCESS_TOKEN"] = await self._get_access_token(force=True)
                    continue
                if response.status_code not in RETRY_STATUS_CODES or attempt >= len(RETRY_DELAYS_SECONDS):
                    break
                await asyncio.sleep(RETRY_DELAYS_SECONDS[attempt])

        if response is None:
            if last_transport_error is not None:
                raise WalmartApiError(
                    f"Walmart {method} {path} failed after retries: {last_transport_error}."
                ) from last_transport_error
            raise WalmartApiError(f"Walmart {method} {path} did not return a response.")
        self._raise_for_status(response, f"Walmart {method} {path}")
        return response

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise WalmartApiError("Walmart returned a non-JSON response.", status_code=response.status_code) from exc
        if not isinstance(payload, dict):
            raise WalmartApiError("Walmart returned an unexpected response shape.", status_code=response.status_code)
        return payload

    @staticmethod
    def _raise_for_status(response: httpx.Response, operation: str) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise WalmartApiError(
                f"{operation} failed with HTTP {response.status_code}.",
                status_code=response.status_code,
            ) from exc


def build_offer_match_preview(
    items: Iterable[InventoryItem],
    overrides: dict[str, WalmartItemOverride] | None = None,
    *,
    default_shipping_weight_lbs: float | None = None,
    price_markup_percent: float = 10.0,
) -> dict[str, Any]:
    item_overrides = overrides or {}
    ready_entries: list[dict[str, Any]] = []
    item_results: list[dict[str, Any]] = []

    for item in items:
        override = item_overrides.get(item.sku, WalmartItemOverride())
        entry, errors, warnings, resolved = _build_offer_match_item(
            item,
            override,
            default_shipping_weight_lbs=default_shipping_weight_lbs,
            price_markup_percent=price_markup_percent,
        )
        ready = not errors
        if entry is not None and ready:
            ready_entries.append({"Item": entry})
        item_results.append(
            {
                "sku": item.sku,
                "ebay_item_id": item.ebay_item_id,
                "title": item.title,
                "ebay_url": item.ebay_url,
                "ready": ready,
                "errors": errors,
                "warnings": warnings,
                "resolved": resolved,
            }
        )

    payload = {
        "MPItemFeedHeader": {
            "processMode": "REPLACE",
            "subset": "EXTERNAL",
            "locale": "en",
            "sellingChannel": "mpsetupbymatch",
            "version": "4.2",
        },
        "MPItem": ready_entries,
    }
    return {
        "feed_type": "MP_ITEM_MATCH",
        "total": len(item_results),
        "ready": len(ready_entries),
        "blocked": len(item_results) - len(ready_entries),
        "items": item_results,
        "payload": payload,
    }


def build_full_item_from_catalog_template(
    item: InventoryItem,
    catalog: dict[str, Any],
    resolved: dict[str, Any],
) -> dict[str, Any]:
    """Overlay seller-owned offer data on Walmart's current SPEC response template."""
    raw_template = catalog.get("item_spec_payload")
    if not isinstance(raw_template, dict):
        raise ValueError("Walmart did not return an itemSpecPayload template for full item setup.")
    payload = copy.deepcopy(raw_template)
    header = payload.get("MPItemFeedHeader")
    entries = payload.get("MPItem")
    if not isinstance(header, dict) or not str(header.get("version") or "").strip():
        raise ValueError("The Walmart item template is missing its required specification version.")
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise ValueError("The Walmart item template did not contain exactly one MPItem record.")

    catalog_version = str(catalog.get("version") or "").strip()
    if catalog_version and str(header.get("version") or "").strip() != catalog_version:
        raise ValueError("The Walmart item template version does not match its catalog response.")

    entry = entries[0]
    orderable = entry.get("Orderable")
    visible = entry.get("Visible")
    if not isinstance(orderable, dict) or not isinstance(visible, dict):
        raise ValueError("The Walmart item template is missing Orderable or Visible content.")

    product_type = str(
        catalog.get("product_type") or orderable.get("specProductType") or ""
    ).strip()
    if not product_type and len(visible) == 1:
        product_type = str(next(iter(visible))).strip()
    product_content = visible.get(product_type)
    if not product_type or not isinstance(product_content, dict):
        raise ValueError("The Walmart item template does not contain its product-type content block.")

    product_id_type, product_id = normalize_product_identifier(
        resolved.get("product_id_type"), resolved.get("product_id")
    )
    if not product_id_type or not product_id:
        raise ValueError("Full item setup requires a valid Walmart product identifier.")
    template_identifiers = orderable.get("productIdentifiers")
    if isinstance(template_identifiers, dict):
        template_type, template_id = normalize_product_identifier(
            template_identifiers.get("productIdType"), template_identifiers.get("productId")
        )
        if (
            template_type
            and template_id
            and _canonical_product_id(template_type, template_id)
            != _canonical_product_id(product_id_type, product_id)
        ):
            raise ValueError("The Walmart template identifier does not match the eBay item identifier.")
        if not template_type or not template_id:
            orderable["productIdentifiers"] = {
                "productIdType": product_id_type,
                "productId": product_id,
            }
    else:
        orderable["productIdentifiers"] = {
            "productIdType": product_id_type,
            "productId": product_id,
        }

    price = resolved.get("price")
    shipping_weight = resolved.get("shipping_weight_lbs")
    condition = str(resolved.get("condition") or "").strip()
    if not isinstance(price, (int, float)) or price <= 0:
        raise ValueError("Full item setup requires a positive Walmart price.")
    if not isinstance(shipping_weight, (int, float)) or shipping_weight <= 0:
        raise ValueError("Full item setup requires a positive shipping weight.")
    if condition not in SUPPORTED_CONDITIONS:
        raise ValueError("Full item setup requires a supported Walmart condition.")

    orderable["sku"] = item.sku
    orderable["price"] = round(float(price), 2)
    orderable["ShippingWeight"] = round(float(shipping_weight), 3)
    orderable.setdefault("specProductType", product_type)
    product_content["condition"] = condition

    ebay_images = _maintenance_image_urls([item.image_url, *item.image_urls])
    raw_secondary_images = product_content.get("productSecondaryImageURL")
    if not isinstance(raw_secondary_images, (list, tuple)):
        raw_secondary_images = [raw_secondary_images]
    template_images = _maintenance_image_urls(
        [product_content.get("mainImageUrl"), *raw_secondary_images]
    )
    images = list(dict.fromkeys([*ebay_images, *template_images]))
    if images:
        product_content["mainImageUrl"] = images[0]
        if len(images) > 1:
            product_content["productSecondaryImageURL"] = images[1:20]
        else:
            product_content.pop("productSecondaryImageURL", None)
    if not str(product_content.get("productName") or "").strip():
        product_content["productName"] = item.title
    if not str(product_content.get("shortDescription") or "").strip() and item.description:
        product_content["shortDescription"] = re.sub(
            r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", item.description))
        ).strip()
    brand = next(
        (
            str(value).strip()
            for key, value in item.item_specifics.items()
            if _normalize_key(key) == "brand" and str(value).strip()
        ),
        "",
    )
    if brand and not str(product_content.get("brand") or "").strip():
        product_content["brand"] = brand

    return payload


def _canonical_product_id(product_id_type: str, product_id: str) -> str:
    return product_id if product_id_type == "ISBN" else product_id.zfill(14)


def build_inventory_feed(items: Iterable[InventoryItem]) -> dict[str, Any]:
    today = date.today().isoformat()
    inventory = [
        {
            "sku": item.sku,
            "quantity": {"unit": "EACH", "amount": max(0, int(item.quantity))},
            "inventoryAvailableDate": today,
        }
        for item in items
    ]
    return {"InventoryHeader": {"version": "1.4"}, "Inventory": inventory}


def build_item_image_maintenance_feed(
    items: Iterable[dict[str, Any]],
    *,
    version: str = "5.0.20260608-18_15_07-api",
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for item in items:
        sku = str(item.get("sku") or "").strip()
        product_type = str(item.get("product_type") or "").strip()
        product_id_type = str(item.get("product_id_type") or "GTIN").strip().upper()
        product_id = _digits(item.get("product_id"))
        image_urls = _maintenance_image_urls(item.get("image_urls"))
        if not sku:
            raise ValueError("Walmart item maintenance requires a seller SKU.")
        if not product_type:
            raise ValueError(f"Walmart item maintenance requires a product type for SKU {sku}.")
        if product_id_type not in PRODUCT_ID_TYPES or not product_id:
            raise ValueError(f"Walmart item maintenance requires a product identifier for SKU {sku}.")
        if not image_urls:
            raise ValueError(f"Walmart item maintenance requires at least one image for SKU {sku}.")
        entries.append(
            {
                "Orderable": {
                    "sku": sku,
                    "productIdentifiers": {
                        "productIdType": product_id_type,
                        "productId": product_id,
                    },
                },
                "Visible": {
                    product_type: {
                        "mainImageUrl": image_urls[0],
                        **(
                            {"productSecondaryImageURL": image_urls[1:]}
                            if len(image_urls) > 1
                            else {}
                        ),
                    }
                },
            }
        )
    if not entries:
        raise ValueError("Walmart item maintenance requires at least one item.")
    return {
        "MPItemFeedHeader": {
            "businessUnit": "WALMART_US",
            "locale": "en",
            "version": str(version).strip(),
        },
        "MPItem": entries,
    }


def _published_item_summary(item: dict[str, Any]) -> dict[str, Any] | None:
    sku = str(item.get("sku") or "").strip()
    if not sku:
        return None
    summary: dict[str, Any] = {
        "sku": sku,
        "published_status": str(
            item.get("publishedStatus") or item.get("published_status") or ""
        ).upper(),
        "lifecycle_status": str(
            item.get("lifecycleStatus") or item.get("lifecycle_status") or ""
        ).upper(),
    }
    product_type = str(item.get("productType") or item.get("product_type") or "").strip()
    if product_type:
        summary["product_type"] = product_type

    identifiers: dict[str, object] = {}
    raw_identifiers = item.get("productIdentifiers")
    if isinstance(raw_identifiers, dict):
        raw_type = str(raw_identifiers.get("productIdType") or "").upper()
        raw_value = raw_identifiers.get("productId")
        if raw_type in PRODUCT_ID_TYPES and raw_value:
            identifiers[raw_type] = raw_value
    for identifier_type in PRODUCT_ID_TYPES:
        raw_value = item.get(identifier_type.lower()) or item.get(identifier_type)
        if raw_value:
            identifiers[identifier_type] = raw_value
    for identifier_type in PRODUCT_ID_TYPES:
        value = _digits(identifiers.get(identifier_type))
        if value:
            summary["product_id_type"] = identifier_type
            summary["product_id"] = value
            break

    item_id = str(item.get("itemId") or item.get("wpid") or "").strip()
    if item_id:
        summary["item_id"] = item_id
    return summary


def _maintenance_image_urls(value: object) -> list[str]:
    raw_values = value if isinstance(value, (list, tuple)) else [value]
    urls: list[str] = []
    for raw_value in raw_values:
        url = str(raw_value or "").strip()
        if not url.startswith("https://") or len(url) > 2500 or url in urls:
            continue
        urls.append(url)
        if len(urls) >= 20:
            break
    return urls


def build_walmart_catalog_query(item: InventoryItem) -> str:
    specifics = {_normalize_key(key): str(value or "").strip() for key, value in item.item_specifics.items()}
    values: list[str] = []
    for key_group in (
        ("brand",),
        ("model",),
        ("storage", "storagecapacity"),
        ("devicecolor", "manufacturercolor", "color", "colors"),
        ("size", "casesize"),
        ("bandcolor",),
    ):
        value = next((specifics.get(key) for key in key_group if specifics.get(key)), None)
        if value and value.lower() not in {entry.lower() for entry in values}:
            values.append(value)

    if not specifics.get("model"):
        title = re.sub(r"\s+", " ", item.title).strip()
        if title:
            values.insert(0, title[:140])
    query = re.sub(r"\s+", " ", " ".join(values)).strip()
    return query[:200]


def build_walmart_draft(
    item: InventoryItem,
    catalog_result: dict[str, Any] | None = None,
    *,
    lookup_error: str | None = None,
    price_markup_percent: float = 10.0,
) -> dict[str, Any]:
    specifics = {_normalize_key(key): str(value or "").strip() for key, value in item.item_specifics.items()}
    identifier_type, identifier = _product_identifier(item, WalmartItemOverride())
    shipping_weight_lbs = _shipping_weight_lbs(item.item_specifics)
    mapped_condition = _walmart_condition_for_item(item)
    images = [url for url in [item.image_url, *item.image_urls] if str(url or "").strip()]
    images = list(dict.fromkeys(images))
    catalog = catalog_result or {
        "status": "lookup_failed" if lookup_error else "not_requested",
        "query": build_walmart_catalog_query(item),
        "candidates": [],
    }
    candidates = catalog.get("candidates") if isinstance(catalog.get("candidates"), list) else []
    verified_match, match_reason = select_verified_catalog_match(item, candidates)
    if not identifier_type and verified_match:
        identifier_type = str(verified_match["product_id_type"])
        identifier = str(verified_match["product_id"])

    missing_fields: list[str] = []
    if not identifier_type or not identifier:
        missing_fields.append("product_identifier")
    if shipping_weight_lbs is None:
        missing_fields.append("shipping_weight_lbs")
    if not mapped_condition:
        missing_fields.append("walmart_condition")
    if not specifics.get("brand"):
        missing_fields.append("brand")
    if not images:
        missing_fields.append("images")

    prepared_listing = {
        "sku": item.sku,
        "product_name": item.title,
        "site_description": item.description,
        "brand": specifics.get("brand"),
        "model": specifics.get("model"),
        "category": item.category,
        "condition": mapped_condition,
        "source_condition": item.condition,
        "price": walmart_price(item.price, price_markup_percent),
        "source_price": item.price,
        "price_markup_percent": price_markup_percent,
        "currency": item.currency,
        "quantity": item.quantity,
        "shipping_weight_lbs": shipping_weight_lbs,
        "product_identifier": (
            {"type": identifier_type, "value": identifier}
            if identifier_type and identifier
            else None
        ),
        "images": images,
        "item_specifics": item.item_specifics,
    }
    return {
        "sku": item.sku,
        "ebay_item_id": item.ebay_item_id,
        "source_snapshot": item.model_dump(mode="json"),
        "prepared_listing": prepared_listing,
        "catalog_query": str(catalog.get("query") or build_walmart_catalog_query(item)),
        "catalog_candidates": candidates,
        "verified_match": verified_match,
        "match_reason": match_reason,
        "catalog_status": str(catalog.get("status") or "not_requested"),
        "status": "draft_verified_match" if verified_match else "draft_needs_review",
        "missing_fields": missing_fields,
        "lookup_error": lookup_error,
    }


def select_verified_catalog_match(
    item: InventoryItem,
    candidates: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    plausible, reason = plausible_catalog_candidates(item, candidates)
    if not plausible:
        return None, reason

    identified: list[tuple[dict[str, Any], str, str, str]] = []
    for candidate in plausible:
        identifiers = candidate.get("identifiers")
        if not isinstance(identifiers, dict):
            continue
        for identifier_type in PRODUCT_ID_TYPES:
            normalized_type, normalized_value = normalize_product_identifier(
                identifier_type,
                identifiers.get(identifier_type),
            )
            if normalized_type and normalized_value:
                canonical = (
                    f"ISBN:{normalized_value}"
                    if normalized_type == "ISBN"
                    else f"GTIN:{normalized_value.zfill(14)}"
                )
                identified.append((candidate, normalized_type, normalized_value, canonical))
                break

    canonical_identifiers = {entry[3] for entry in identified}
    if len(plausible) == 1 and identified:
        candidate, selected_type, selected_value, _ = identified[0]
    elif len(canonical_identifiers) == 1 and identified:
        candidate, selected_type, selected_value, _ = identified[0]
    else:
        return None, reason

    identifiers = candidate.get("identifiers")
    if not isinstance(identifiers, dict):
        return None, "The exact catalog candidate does not expose a product identifier."
    if not selected_type or not selected_value:
        return None, "The exact catalog candidate does not expose a valid product identifier."
    return {
        "confidence": "exact_brand_model_variant",
        "product_id_type": selected_type,
        "product_id": selected_value,
        "walmart_item_id": candidate.get("walmart_item_id"),
        "title": str(candidate.get("title") or ""),
        "brand": candidate.get("brand"),
        "shipping_weight_lbs": candidate.get("shipping_weight_lbs"),
        "public_product_url": candidate.get("public_product_url"),
    }, (
        "Duplicate Walmart catalog records resolved to one verified product identifier."
        if len(plausible) > 1
        else "Exactly one Walmart catalog candidate passed all verification checks."
    )


def plausible_catalog_candidates(
    item: InventoryItem,
    candidates: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """Return only candidates whose brand, model, variant, and carrier match exactly."""
    specifics = {_normalize_key(key): str(value or "").strip() for key, value in item.item_specifics.items()}
    source_brand = specifics.get("brand")
    source_model = specifics.get("model")
    if not source_brand or not source_model:
        return [], "The eBay listing does not contain both a brand and model."

    storage = specifics.get("storage") or specifics.get("storagecapacity")
    size = specifics.get("size") or specifics.get("casesize")
    color = (
        specifics.get("devicecolor")
        or specifics.get("manufacturercolor")
        or specifics.get("color")
        or specifics.get("colors")
    )
    category = _match_text(item.category)
    if ("smartphone" in category or "tablet" in category) and not storage:
        return [], "Storage is required to verify phone and tablet catalog variants."
    if "smartwatch" in category and not size:
        return [], "Case size is required to verify smartwatch catalog variants."

    required_values = [value for value in (storage, size, color) if value]
    source_brand_key = _match_text(source_brand)
    source_model_key = _match_text(source_model)
    raw_source_network_text = " ".join(
        str(value or "")
        for value in (
            item.title,
            specifics.get("network"),
            specifics.get("carrier"),
            specifics.get("networklocked"),
            specifics.get("lockstatus"),
        )
    )
    source_network_text = _match_text(raw_source_network_text)
    phone_category = "smartphone" in category or "cellphone" in category
    required_carriers = _carrier_keys(raw_source_network_text)
    verified: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        title = str(candidate.get("title") or "")
        title_key = _match_text(title)
        candidate_brand_key = _match_text(candidate.get("brand"))
        if not title_key or source_model_key not in title_key:
            continue
        if candidate_brand_key:
            if candidate_brand_key != source_brand_key:
                continue
        elif source_brand_key not in title_key:
            continue
        if any(_match_text(value) not in title_key for value in required_values):
            continue
        if phone_category and "unlocked" in source_network_text and "unlocked" not in title_key:
            continue
        if phone_category and any(carrier not in _carrier_keys(title) for carrier in required_carriers):
            continue
        verified.append(candidate)

    source_condition = _walmart_condition_for_item(item)
    if source_condition == "Open Box":
        open_box = [
            candidate
            for candidate in verified
            if re.search(r"\bopen[ -]?box\b", str(candidate.get("title") or ""), re.IGNORECASE)
        ]
        if open_box:
            verified = open_box

    if len(verified) != 1:
        if verified:
            return verified, f"{len(verified)} catalog candidates passed; exactly one is required."
        return [], "No catalog candidate passed the exact brand, model, variation, and carrier checks."
    return verified, "Exactly one Walmart catalog candidate passed the text verification checks."


def estimated_shipping_weight_lbs(item: InventoryItem) -> float:
    """Return a conservative packaged-weight estimate when neither marketplace supplies one."""
    text = _match_text(f"{item.category or ''} {item.title}")
    for needles, pounds in (
        (("laptop", "notebook", "macbook"), 10.0),
        (("tablet", "ipad"), 4.0),
        (("speaker", "boombox"), 5.0),
        (("headphone", "headset"), 2.0),
        (("smartphone", "cellphone", "iphone", "galaxy"), 2.0),
        (("smartwatch", "applewatch", "galaxywatch", "earbud", "airpod"), 1.0),
    ):
        if any(needle in text for needle in needles):
            return pounds
    return 5.0


class _ProductSchemaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._capturing = False
        self._buffer: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attributes = {str(key).lower(): str(value or "").lower() for key, value in attrs}
        self._capturing = attributes.get("type") == "application/ld+json"
        self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capturing:
            self.scripts.append("".join(self._buffer))
            self._capturing = False
            self._buffer = []


def parse_walmart_product_page(html_text: str) -> dict[str, Any]:
    """Extract public Product JSON-LD without retaining Walmart page internals."""
    parser = _ProductSchemaParser()
    parser.feed(str(html_text or ""))
    products: list[dict[str, Any]] = []

    def collect(value: object) -> None:
        if isinstance(value, list):
            for entry in value:
                collect(entry)
            return
        if not isinstance(value, dict):
            return
        schema_type = value.get("@type")
        if schema_type == "Product" or (
            isinstance(schema_type, list) and "Product" in schema_type
        ):
            products.append(value)
        graph = value.get("@graph")
        if isinstance(graph, list):
            collect(graph)

    for script in parser.scripts:
        try:
            collect(json.loads(script))
        except (TypeError, ValueError):
            continue

    product = products[0] if products else {}
    identifiers: dict[str, str] = {}
    for key in ("gtin", "gtin8", "gtin12", "gtin13", "gtin14"):
        identifier_type, value = normalize_product_identifier(key, product.get(key))
        if identifier_type and value:
            identifiers.setdefault(identifier_type, value)

    decoded = unescape(str(html_text or "")).replace('\\"', '"')
    for match in re.finditer(
        r'"(?:gtin|gtin8|gtin12|gtin13|gtin14|upc|ean)"\s*:\s*"?(\d{8,14})"?',
        decoded,
        flags=re.IGNORECASE,
    ):
        identifier_type, value = normalize_product_identifier("GTIN", match.group(1))
        if identifier_type and value:
            identifiers.setdefault(identifier_type, value)

    brand_value = product.get("brand")
    if isinstance(brand_value, dict):
        brand_value = brand_value.get("name")
    raw_weight = product.get("weight")
    if isinstance(raw_weight, dict):
        raw_weight = raw_weight.get("value") or raw_weight.get("name")
    weight = _parse_weight_lbs(raw_weight)
    if weight is None:
        weight_match = re.search(
            r'"name"\s*:\s*"(?:Assembled product weight|Shipping weight|Product weight)"'
            r'.{0,240}?"value"\s*:\s*"([^"]+)"',
            decoded,
            flags=re.IGNORECASE,
        )
        if weight_match:
            weight = _parse_weight_lbs(weight_match.group(1))

    manufacturer_number = product.get("mpn") or product.get("sku")
    if not manufacturer_number:
        manufacturer_match = re.search(
            r'"(?:manufactureNumber|manufacturerNumber|manufacturerPartNumber|mpn)"'
            r'\s*:\s*"([^"\\]{2,80})"',
            decoded,
            flags=re.IGNORECASE,
        )
        if manufacturer_match:
            manufacturer_number = manufacturer_match.group(1).strip()

    return {
        "title": product.get("name"),
        "brand": brand_value,
        "model": product.get("model"),
        "manufacturer_number": manufacturer_number,
        "identifiers": identifiers,
        "shipping_weight_lbs": weight,
    }


def _build_offer_match_item(
    item: InventoryItem,
    override: WalmartItemOverride,
    *,
    default_shipping_weight_lbs: float | None,
    price_markup_percent: float,
) -> tuple[dict[str, Any] | None, list[str], list[str], dict[str, Any]]:
    errors: list[str] = []
    warnings: list[str] = []
    product_id_type, product_id = _product_identifier(item, override)
    shipping_weight_lbs = (
        override.shipping_weight_lbs
        if override.shipping_weight_lbs is not None
        else _shipping_weight_lbs(item.item_specifics)
    )
    if shipping_weight_lbs is None:
        shipping_weight_lbs = default_shipping_weight_lbs
        if shipping_weight_lbs is not None:
            warnings.append("Used WALMART_DEFAULT_SHIPPING_WEIGHT_LBS; verify the packaged weight before submission.")
    condition = _walmart_condition_for_item(item, override.condition)
    price = (
        override.price
        if override.price is not None
        else walmart_price(item.price, price_markup_percent)
    )
    quantity = override.quantity if override.quantity is not None else item.quantity
    main_image_url = override.main_image_url or item.image_url

    if not item.sku or len(item.sku) > 50:
        errors.append("Walmart requires a seller SKU between 1 and 50 characters.")
    if not product_id_type or not product_id:
        errors.append("Missing a UPC, GTIN, EAN, or ISBN product identifier.")
    elif not _valid_product_identifier(product_id_type, product_id):
        errors.append(f"{product_id_type} value has an invalid format or length.")
    if shipping_weight_lbs is None or shipping_weight_lbs <= 0:
        errors.append("Missing Shipping Weight in pounds.")
    if not condition:
        errors.append("eBay condition could not be mapped to a supported Walmart condition.")
    elif condition not in SUPPORTED_CONDITIONS:
        errors.append(f"Walmart condition {condition!r} is not supported by MP_ITEM_MATCH v4.2.")
    if price is None or price <= 0:
        errors.append("Missing a positive Walmart selling price.")
    if quantity is None or quantity <= 0:
        errors.append("The eBay listing is not currently in stock.")
    if main_image_url and len(main_image_url) > 200:
        message = "Main image URL exceeds the MP_ITEM_MATCH v4.2 limit of 200 characters."
        if condition in CONDITION_IMAGE_REQUIRED:
            errors.append(message)
        else:
            warnings.append(f"{message} The optional image was omitted.")
        main_image_url = None
    if condition in CONDITION_IMAGE_REQUIRED and not main_image_url and not any(
        "Main image URL exceeds" in error for error in errors
    ):
        errors.append(f"Walmart requires a main image for condition {condition!r}.")

    resolved = {
        "product_id_type": product_id_type,
        "product_id": product_id,
        "shipping_weight_lbs": shipping_weight_lbs,
        "condition": condition,
        "price": price,
        "quantity": quantity,
        "main_image_url": main_image_url,
    }
    if errors:
        return None, errors, warnings, resolved

    entry: dict[str, Any] = {
        "sku": item.sku,
        "productIdentifiers": {
            "productIdType": product_id_type,
            "productId": product_id,
        },
        "ShippingWeight": round(float(shipping_weight_lbs), 3),
        "price": round(float(price), 2),
        "condition": condition,
    }
    if main_image_url:
        entry["mainImageUrl"] = main_image_url
    return entry, errors, warnings, resolved


def _product_identifier(
    item: InventoryItem,
    override: WalmartItemOverride,
) -> tuple[str | None, str | None]:
    if override.product_id_type and override.product_id:
        return normalize_product_identifier(override.product_id_type, override.product_id)

    specifics = {_normalize_key(key): value for key, value in item.item_specifics.items()}
    for identifier_type in PRODUCT_ID_TYPES:
        value = specifics.get(_normalize_key(identifier_type))
        if value:
            normalized_type, normalized_value = normalize_product_identifier(identifier_type, value)
            if normalized_type and normalized_value:
                return normalized_type, normalized_value
    return None, None


def _shipping_weight_lbs(item_specifics: dict[str, str]) -> float | None:
    normalized = {_normalize_key(key): value for key, value in item_specifics.items()}
    for key in ("shippingweight", "packageweight", "shippingweightlbs", "weight"):
        value = normalized.get(key)
        parsed = _parse_weight_lbs(value)
        if parsed is not None:
            return parsed
    return None


def _parse_weight_lbs(value: object) -> float | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*(lb|lbs|pound|pounds|oz|ounce|ounces|kg|g)?", text)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2) or "lb"
    if unit in {"oz", "ounce", "ounces"}:
        amount /= 16
    elif unit == "kg":
        amount *= 2.2046226218
    elif unit == "g":
        amount *= 0.0022046226218
    return round(amount, 3) if amount > 0 else None


def walmart_price(price: float | None, markup_percent: float = 10.0) -> float | None:
    if price is None:
        return None
    multiplier = Decimal("1") + (Decimal(str(markup_percent)) / Decimal("100"))
    return float(
        (Decimal(str(price)) * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def _walmart_condition(value: str | None) -> str | None:
    clean = re.sub(r"\s+", " ", str(value or "").strip()).lower()
    exact = {
        "new": "New",
        "brand new": "New",
        "new with defects": "New with defects",
        "new without box": "New without box",
        "new without tags": "New without tags",
        # This seller is approved for Walmart's Open Box condition lane, so keep
        # eBay's Open box condition exact instead of changing its program grade.
        "open box": "Open Box",
        "new_other": "Open Box",
        "new other": "Open Box",
        "1500": "Open Box",
        "remanufactured": "Remanufactured",
        "pre-owned: fair": "Pre-Owned: Fair",
        "pre-owned: good": "Pre-Owned: Good",
        "pre-owned: like new": "Pre-Owned: Like New",
        "used - acceptable": "Pre-Owned: Fair",
        "used - good": "Pre-Owned: Good",
        "used - excellent": "Pre-Owned: Like New",
        "used - like new": "Pre-Owned: Like New",
    }
    return exact.get(clean)


def _walmart_condition_for_item(
    item: InventoryItem,
    override_condition: str | None = None,
) -> str | None:
    mapped = _walmart_condition(override_condition or item.condition)
    if mapped:
        return mapped
    specifics = {
        _normalize_key(key): str(value or "").strip().lower()
        for key, value in item.item_specifics.items()
    }
    if specifics.get("conditionid") == "1500":
        return "Open Box"
    listing_text = f"{item.title or ''} {item.description or ''}"
    if re.search(r"\bopen[ -]?box\b", listing_text, re.IGNORECASE):
        return "Open Box"
    return None


def normalize_product_identifier(
    identifier_type: object,
    value: object,
) -> tuple[str | None, str | None]:
    """Return a Walmart identifier type/value only when its check digit is valid."""
    declared_type = str(identifier_type or "").strip().upper()
    clean = _digits(value)
    if not clean:
        return None, None
    if declared_type == "ISBN" and len(clean) in {10, 13}:
        normalized_type = "ISBN"
    else:
        normalized_type = {12: "UPC", 13: "EAN", 14: "GTIN"}.get(len(clean))
    if not normalized_type or not _valid_product_identifier(normalized_type, clean):
        return None, None
    return normalized_type, clean


def _valid_product_identifier(identifier_type: str, value: str) -> bool:
    clean_type = str(identifier_type or "").strip().upper()
    if not value.isdigit():
        return False
    lengths = {
        "GTIN": {14},
        "UPC": {12},
        "EAN": {13},
        "ISBN": {10, 13},
    }
    if len(value) not in lengths.get(clean_type, set()):
        return False
    if clean_type == "ISBN" and len(value) == 10:
        return sum((10 - index) * int(digit) for index, digit in enumerate(value)) % 11 == 0
    digits = [int(digit) for digit in value]
    body = digits[:-1]
    weighted = sum(
        digit * (3 if (len(body) - index) % 2 else 1)
        for index, digit in enumerate(body)
    )
    return (10 - weighted % 10) % 10 == digits[-1]


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _normalize_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _match_text(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _carrier_keys(value: object) -> set[str]:
    text = str(value or "").lower()
    patterns = {
        "att": r"\b(?:at\s*&\s*t|at\s+and\s+t|att)\b",
        "verizon": r"\bverizon\b",
        "tmobile": r"\bt[ -]?mobile\b",
        "tracfone": r"\btracfone\b",
        "straighttalk": r"\bstraight[ -]?talk\b",
        "boostmobile": r"\bboost(?:[ -]?mobile)?\b",
        "cricket": r"\bcricket\b",
    }
    return {key for key, pattern in patterns.items() if re.search(pattern, text)}


def _catalog_candidate(raw_item: object) -> dict[str, Any] | None:
    if not isinstance(raw_item, dict):
        return None

    def first_value(*keys: str) -> Any:
        for key in keys:
            value = raw_item.get(key)
            if value not in (None, "", [], {}):
                return value
        return None

    identifiers: dict[str, str] = {}
    for identifier_type, keys in {
        "GTIN": ("gtin", "GTIN"),
        "UPC": ("upc", "UPC"),
        "EAN": ("ean", "EAN"),
        "ISBN": ("isbn", "ISBN"),
    }.items():
        value = first_value(*keys)
        if value is not None:
            clean = _digits(value)
            if clean:
                identifiers[identifier_type] = clean

    nested_identifiers = raw_item.get("productIdentifiers")
    if isinstance(nested_identifiers, dict):
        nested_type = str(nested_identifiers.get("productIdType") or "").upper()
        nested_value = _digits(nested_identifiers.get("productId"))
        if nested_type in PRODUCT_ID_TYPES and nested_value:
            identifiers.setdefault(nested_type, nested_value)

    candidate = {
        "walmart_item_id": first_value("itemId", "walmartItemId", "usItemId"),
        "title": first_value("productName", "title", "itemName", "name"),
        "brand": first_value("brand", "brandName"),
        "product_type": first_value("productType", "productTypeName"),
        "category_path": first_value("categoryPath", "category"),
        "image_url": first_value("primaryImageUrl", "imageUrl", "thumbnailUrl"),
        "identifiers": identifiers,
    }
    if not any(value not in (None, "", {}) for value in candidate.values()):
        return None
    return candidate
