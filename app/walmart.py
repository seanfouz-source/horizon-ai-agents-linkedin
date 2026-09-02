import asyncio
import json
import re
import uuid
from base64 import b64encode
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable
from urllib.parse import quote

import httpx

from app.config import Settings
from app.models import InventoryItem, WalmartItemOverride


RETRY_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
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
        if identifier_type not in {"UPC", "GTIN"}:
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
        return {
            "status": "matched" if feed_type == "MP_ITEM_MATCH" else "not_matched",
            "matched": feed_type == "MP_ITEM_MATCH",
            "feed_type": feed_type or None,
            "version": first.get("version") if first else None,
            "product_type": first.get("productType") if first else None,
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
        response = await self._request(
            "GET",
            "/v3/items",
            params={
                "publishedStatus": "PUBLISHED",
                "lifecycleStatus": "ACTIVE",
                "limit": max(1, min(limit, 1000)),
            },
        )
        payload = self._json_object(response)
        raw_items = payload.get("ItemResponse") or payload.get("items") or []
        if isinstance(raw_items, dict):
            raw_items = raw_items.get("Item") or raw_items.get("items") or []
        if not isinstance(raw_items, list):
            raw_items = []
        return [
            {
                "sku": str(item.get("sku") or "").strip(),
                "published_status": str(
                    item.get("publishedStatus") or item.get("published_status") or ""
                ).upper(),
                "lifecycle_status": str(
                    item.get("lifecycleStatus") or item.get("lifecycle_status") or ""
                ).upper(),
            }
            for item in raw_items
            if isinstance(item, dict) and str(item.get("sku") or "").strip()
        ]

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
        correlation_id = str(uuid.uuid4())
        headers = {
            "Accept": "application/json",
            "WM_SEC.ACCESS_TOKEN": token,
            "WM_QOS.CORRELATION_ID": correlation_id,
            "WM_SVC.NAME": self.settings.walmart_service_name,
            "WM_MARKET": self.settings.walmart_market,
        }
        channel_type = str(getattr(self.settings, "walmart_channel_type", "") or "").strip()
        if channel_type:
            headers["WM_CONSUMER.CHANNEL.TYPE"] = channel_type
        headers.update(kwargs.pop("headers", {}))

        response: httpx.Response | None = None
        async with httpx.AsyncClient(base_url=self.base_url, timeout=60) as client:
            for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
                response = await client.request(method, path, headers=headers, **kwargs)
                if response.status_code == 401 and attempt == 0:
                    headers["WM_SEC.ACCESS_TOKEN"] = await self._get_access_token(force=True)
                    continue
                if response.status_code not in RETRY_STATUS_CODES or attempt >= len(RETRY_DELAYS_SECONDS):
                    break
                await asyncio.sleep(RETRY_DELAYS_SECONDS[attempt])

        if response is None:
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
    mapped_condition = _walmart_condition(item.condition)
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
    specifics = {_normalize_key(key): str(value or "").strip() for key, value in item.item_specifics.items()}
    source_brand = specifics.get("brand")
    source_model = specifics.get("model")
    if not source_brand or not source_model:
        return None, "The eBay listing does not contain both a brand and model."

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
        return None, "Storage is required to verify phone and tablet catalog variants."
    if "smartwatch" in category and not size:
        return None, "Case size is required to verify smartwatch catalog variants."

    required_values = [value for value in (storage, size, color) if value]
    source_brand_key = _match_text(source_brand)
    source_model_key = _match_text(source_model)
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

        identifiers = candidate.get("identifiers")
        if not isinstance(identifiers, dict):
            continue
        selected_type: str | None = None
        selected_value: str | None = None
        for identifier_type in PRODUCT_ID_TYPES:
            candidate_value = _digits(identifiers.get(identifier_type))
            if candidate_value and _valid_product_identifier(identifier_type, candidate_value):
                selected_type = identifier_type
                selected_value = candidate_value
                break
        if not selected_type or not selected_value:
            continue
        verified.append(
            {
                "confidence": "exact_brand_model_variant",
                "product_id_type": selected_type,
                "product_id": selected_value,
                "walmart_item_id": candidate.get("walmart_item_id"),
                "title": title,
                "brand": candidate.get("brand"),
            }
        )

    if len(verified) != 1:
        if verified:
            return None, f"{len(verified)} catalog candidates passed; exactly one is required."
        return None, "No catalog candidate passed the exact brand, model, variation, and identifier checks."
    return verified[0], "Exactly one Walmart catalog candidate passed all verification checks."


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
    condition = _walmart_condition(override.condition or item.condition)
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
        return override.product_id_type.upper(), _digits(override.product_id)

    specifics = {_normalize_key(key): value for key, value in item.item_specifics.items()}
    for identifier_type in PRODUCT_ID_TYPES:
        value = specifics.get(_normalize_key(identifier_type))
        if value:
            return identifier_type, _digits(value)
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
        "open box": "Open Box",
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


def _valid_product_identifier(identifier_type: str, value: str) -> bool:
    if not value.isdigit():
        return False
    lengths = {
        "GTIN": {14},
        "UPC": {12},
        "EAN": {13},
        "ISBN": {10, 13},
    }
    return len(value) in lengths.get(identifier_type, set())


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _normalize_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _match_text(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


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
