import asyncio
import csv
import gzip
import hashlib
import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from base64 import b64encode
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings
from app.ebay_draft_batch import EbayDraftSpec
from app.models import InventoryItem


logger = logging.getLogger(__name__)
RETRY_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0)


class EbayClient:
    base_url = "https://api.ebay.com"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._configured_access_token = self._clean_access_token(settings.ebay_access_token)
        self._access_token = self._configured_access_token
        self._application_access_token: str | None = None
        self._catalog_access_denied = False
        if not self._access_token and not self._has_refresh_credentials() and not self._has_client_credentials():
            raise RuntimeError(
                "EBAY_ACCESS_TOKEN, eBay OAuth refresh credentials, or EBAY_CLIENT_ID/EBAY_CLIENT_SECRET "
                "are required for live eBay sync."
            )

    async def fetch_inventory_items(self, limit: int = 200) -> list[InventoryItem]:
        sell_inventory_items: list[InventoryItem] = []
        try:
            sell_inventory_items = await self._fetch_sell_inventory_items(limit=limit)
        except httpx.HTTPError as exc:
            logger.warning("eBay Sell Inventory API sync failed: %s", exc.__class__.__name__)

        if sell_inventory_items:
            return sell_inventory_items

        seller_username = getattr(self.settings, "ebay_seller_username", None)
        if not seller_username:
            return []

        browse_items = await self._fetch_browse_seller_items(seller_username, limit=limit)
        if browse_items:
            logger.info("Imported %s active eBay listings through Browse API.", len(browse_items))
        if browse_items and self._has_trading_user_token():
            try:
                trading_items = await self._enrich_with_trading_api(browse_items)
            except (httpx.HTTPError, RuntimeError) as exc:
                logger.warning(
                    "eBay Trading API enrichment could not start; using Browse data: %s",
                    exc,
                )
            else:
                logger.info(
                    "Expanded %s eBay listings into %s active seller SKU rows through Trading API.",
                    len(browse_items),
                    len(trading_items),
                )
                return trading_items
        return browse_items

    async def prepare_unpublished_drafts(
        self,
        drafts: list[EbayDraftSpec],
        *,
        confirm: bool = False,
        catalog_candidates_per_item: int = 10,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        async with httpx.AsyncClient(base_url=self.base_url, timeout=45) as client:
            await self._ensure_access_token(client)
            for draft in drafts:
                result: dict[str, Any] = {
                    "sheet_row": draft.sheet_row,
                    "sku": draft.sku,
                    "title": draft.title,
                    "quantity": draft.quantity,
                    "price": draft.price,
                    "category_id": draft.category_id,
                    "condition": draft.condition,
                    "status": "preview",
                    "inventory_item_created": False,
                    "offer_created": False,
                    "published": False,
                }
                try:
                    if confirm:
                        offers_response = await self._get(
                            client,
                            "/sell/inventory/v1/offer",
                            params={"sku": draft.sku, "limit": 10},
                            headers=self._headers(),
                        )
                        if offers_response.status_code not in {200, 404}:
                            result.update(
                                self._response_error(
                                    offers_response,
                                    "offer_lookup_failed",
                                )
                            )
                            results.append(result)
                            continue
                        existing_offers = (
                            offers_response.json().get("offers", [])
                            if offers_response.status_code == 200
                            else []
                        )
                        unpublished_offer = next(
                            (
                                offer
                                for offer in existing_offers
                                if not (offer.get("listing") or {}).get("listingId")
                                and not offer.get("listingId")
                            ),
                            None,
                        )
                        if unpublished_offer:
                            inventory_response = await self._get(
                                client,
                                (
                                    "/sell/inventory/v1/inventory_item/"
                                    f"{quote(draft.sku, safe='')}"
                                ),
                                headers=self._headers(),
                            )
                            saved_image_urls: list[str] = []
                            if inventory_response.status_code == 200:
                                inventory_payload = inventory_response.json()
                                raw_image_urls = (
                                    (inventory_payload.get("product") or {}).get("imageUrls")
                                    or []
                                )
                                if isinstance(raw_image_urls, list):
                                    saved_image_urls = [
                                        str(url)
                                        for url in self._dedupe_urls(raw_image_urls)
                                        if str(url).lower().startswith("https://")
                                    ]
                            result["offer_id"] = unpublished_offer.get("offerId")
                            result["offer_created"] = True
                            result["image_urls"] = saved_image_urls
                            result["image_count"] = len(saved_image_urls)
                            result["inventory_item_verified"] = (
                                inventory_response.status_code == 200
                            )
                            result["status"] = "existing_unpublished"
                            result["message"] = "An unpublished offer already exists for this SKU."
                            results.append(result)
                            continue

                    if draft.manual_image_urls:
                        candidates: list[dict[str, Any]] = []
                        selected: dict[str, Any] = {}
                        image_urls = [
                            url
                            for url in self._dedupe_urls(list(draft.manual_image_urls))
                            if url.lower().startswith("https://")
                        ][:12]
                        result["catalog_candidates"] = []
                        result["selected_catalog_product"] = None
                        result["image_source"] = draft.manual_image_source
                        result["catalog_match"] = {
                            "exact": True,
                            "manual_verified_image": True,
                        }
                        result["image_urls"] = image_urls
                        if not image_urls:
                            result["status"] = "blocked_no_catalog_image"
                            result["message"] = "The verified image override was not a secure URL."
                            results.append(result)
                            continue
                    else:
                        candidates = await self._search_catalog_products(
                            client,
                            draft.catalog_query or draft.title,
                            draft.category_id,
                            limit=catalog_candidates_per_item,
                        )
                        selected = self._select_catalog_product(draft, candidates)
                        result["catalog_candidates"] = [
                            self._catalog_candidate_summary(candidate, draft)
                            for candidate in candidates[:catalog_candidates_per_item]
                        ]
                        result["selected_catalog_product"] = (
                            self._catalog_candidate_summary(selected, draft) if selected else None
                        )
                        if not selected:
                            result["status"] = "blocked_no_catalog_image"
                            result["message"] = "No eBay catalog product with a stock image was found."
                            results.append(result)
                            continue

                        image_urls = self._catalog_image_urls(selected)
                        if not image_urls:
                            result["status"] = "blocked_no_catalog_image"
                            result["message"] = (
                                "The selected eBay catalog product did not include a stock image."
                            )
                            results.append(result)
                            continue

                        match = self._catalog_match(draft, selected)
                        result["catalog_match"] = match
                        result["image_urls"] = image_urls
                        if not match["exact"]:
                            result["status"] = "blocked_catalog_mismatch"
                            result["message"] = (
                                "The best eBay catalog result did not match the model, storage, and "
                                "color closely enough for automatic draft creation."
                            )
                            results.append(result)
                            continue
                    if not confirm:
                        result["status"] = "ready"
                        result["message"] = "Ready to create an unpublished eBay offer."
                        results.append(result)
                        continue

                    inventory_payload = self._inventory_item_payload(draft, selected, image_urls)
                    inventory_response = await self._put_json(
                        client,
                        f"/sell/inventory/v1/inventory_item/{quote(draft.sku, safe='')}",
                        json_data=inventory_payload,
                        headers=self._headers(),
                    )
                    if inventory_response.status_code not in {200, 204}:
                        result.update(self._response_error(inventory_response, "inventory_item_failed"))
                        results.append(result)
                        continue
                    result["inventory_item_created"] = True

                    offer_response = await self._post_json(
                        client,
                        "/sell/inventory/v1/offer",
                        json_data=self._offer_payload(draft),
                        headers=self._headers(),
                    )
                    if offer_response.status_code not in {200, 201}:
                        result.update(self._response_error(offer_response, "offer_create_failed"))
                        results.append(result)
                        continue
                    offer_payload = offer_response.json() if offer_response.content else {}
                    result["offer_id"] = offer_payload.get("offerId")
                    result["offer_created"] = True
                    result["status"] = "created_unpublished"
                    result["message"] = (
                        "Created an unpublished eBay offer with verified stock images. "
                        "No publish call was made."
                    )
                except httpx.HTTPStatusError as exc:
                    result.update(self._response_error(exc.response, "api_error"))
                except httpx.HTTPError as exc:
                    result["status"] = "api_error"
                    result["message"] = self._safe_http_error(exc)
                results.append(result)
        return results

    async def submit_seller_hub_draft_feed(
        self,
        drafts: list[EbayDraftSpec],
        *,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if not drafts:
            raise ValueError("At least one eBay draft is required.")

        image_urls_by_sku: dict[str, list[str]] = {}
        async with httpx.AsyncClient(base_url=self.base_url, timeout=60) as client:
            await self._ensure_access_token(client)
            for draft in drafts:
                response = await self._get(
                    client,
                    f"/sell/inventory/v1/inventory_item/{quote(draft.sku, safe='')}",
                    headers=self._headers(),
                )
                saved_urls: list[str] = []
                if response.status_code == 200:
                    raw_urls = ((response.json().get("product") or {}).get("imageUrls") or [])
                    if isinstance(raw_urls, list):
                        saved_urls = [
                            str(url).replace(" ", "%20")
                            for url in self._dedupe_urls(raw_urls)
                            if str(url).lower().startswith("https://")
                        ][:24]
                if not saved_urls:
                    saved_urls = [
                        str(url).replace(" ", "%20")
                        for url in self._dedupe_urls(draft.manual_image_urls)
                        if str(url).lower().startswith("https://")
                    ][:24]
                image_urls_by_sku[draft.sku] = saved_urls

            csv_text = self._seller_hub_draft_csv(drafts, image_urls_by_sku)
            missing_images = [
                draft.sku for draft in drafts if not image_urls_by_sku.get(draft.sku)
            ]
            preview = {
                "status": "preview",
                "requested_count": len(drafts),
                "rows_with_images": len(drafts) - len(missing_images),
                "missing_image_skus": missing_images,
                "published": False,
                "feed_type": "FX_LISTING",
                "action": "Draft",
            }
            if not confirm:
                return preview
            if missing_images:
                return {
                    **preview,
                    "status": "blocked_missing_images",
                    "message": "The Seller Hub draft feed was not submitted because images are missing.",
                }

            task_response = await self._post_json(
                client,
                "/sell/feed/v1/task",
                json_data={"feedType": "FX_LISTING", "schemaVersion": "1.0"},
                headers=self._headers(),
            )
            if task_response.status_code != 202:
                return {
                    **preview,
                    **self._response_error(task_response, "task_create_failed"),
                }
            location = str(task_response.headers.get("Location") or "").rstrip("/")
            task_id = location.rsplit("/", 1)[-1] if location else ""
            if not task_id:
                return {
                    **preview,
                    "status": "task_create_failed",
                    "http_status": task_response.status_code,
                    "message": "eBay accepted the task but did not return a task ID.",
                }

            upload_headers = self._headers()
            upload_headers.pop("Content-Type", None)
            upload_response = await client.post(
                f"/sell/feed/v1/task/{quote(task_id, safe='')}/upload_file",
                files={
                    "file": (
                        "eBay-draft-listings-template-Horizon-Wireless.csv",
                        csv_text.encode("utf-8"),
                        "text/csv",
                    )
                },
                headers=upload_headers,
            )
            if upload_response.status_code != 200:
                return {
                    **preview,
                    "task_id": task_id,
                    **self._response_error(upload_response, "file_upload_failed"),
                }
            return {
                **preview,
                "status": "submitted",
                "task_id": task_id,
                "message": "Submitted a Seller Hub Draft feed. No listing publish action was used.",
            }

    async def seller_hub_draft_task_status(self, task_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=45) as client:
            await self._ensure_access_token(client)
            response = await self._get(
                client,
                f"/sell/feed/v1/task/{quote(str(task_id), safe='')}",
                headers=self._headers(),
            )
            response.raise_for_status()
            return response.json()

    async def seller_hub_draft_result_excerpt(
        self,
        task_id: str,
        *,
        max_characters: int = 5000,
    ) -> str:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=60) as client:
            await self._ensure_access_token(client)
            result_headers = self._headers()
            result_headers["Accept"] = "application/octet-stream"
            result_headers.pop("Content-Type", None)
            response = await self._get(
                client,
                f"/sell/feed/v1/task/{quote(str(task_id), safe='')}/download_result_file",
                headers=result_headers,
            )
            response.raise_for_status()
            content = response.content
        try:
            if content.startswith(b"\x1f\x8b"):
                content = gzip.decompress(content)
            elif content.startswith(b"PK"):
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    names = [name for name in archive.namelist() if not name.endswith("/")]
                    if names:
                        content = archive.read(names[0])
        except (OSError, EOFError, zipfile.BadZipFile):
            pass
        text = content.decode("utf-8-sig", errors="replace")
        return text[: max(0, int(max_characters))]

    @staticmethod
    def _seller_hub_draft_csv(
        drafts: list[EbayDraftSpec],
        image_urls_by_sku: dict[str, list[str]],
    ) -> str:
        headers = [
            "Action",
            "Custom label (SKU)",
            "Category ID",
            "Title",
            "UPC",
            "Price",
            "Quantity",
            "Item photo URL",
            "Condition ID",
            "Description",
            "Format",
        ]
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(
            ["#INFO", "Version=0.0.2", "Template= eBay-draft-listings-template_US"]
            + [""] * (len(headers) - 3)
        )
        writer.writerow(
            [
                "#INFO Action and Category ID are required fields. "
                "1) Set Action to Draft "
                "2) Please find the category ID for your listings here: "
                "https://pages.ebay.com/sellerinformation/news/categorychanges.html"
            ]
            + [""] * (len(headers) - 1)
        )
        writer.writerow(
            [
                "#INFO After you've successfully uploaded your draft from the "
                "Seller Hub Reports tab, complete your drafts to active listings "
                "here: https://www.ebay.com/sh/lst/drafts"
            ]
            + [""] * (len(headers) - 1)
        )
        writer.writerow(
            ["#INFO"] + [""] * (len(headers) - 1)
        )
        writer.writerow(
            [
                "Action(SiteID=US|Country=US|Currency=USD|Version=1193|CC=UTF-8)",
                *headers[1:],
            ]
        )
        for draft in drafts:
            writer.writerow(
                [
                    "Draft",
                    f"SH-{draft.sku}",
                    draft.category_id,
                    draft.title,
                    "",
                    f"{draft.price:.2f}",
                    draft.quantity,
                    "|".join(image_urls_by_sku.get(draft.sku) or []),
                    "NEW" if draft.condition == "NEW" else "USED",
                    draft.description,
                    "FixedPrice",
                ]
            )
        return output.getvalue()

    async def _search_catalog_products(
        self,
        client: httpx.AsyncClient,
        query: str,
        category_id: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not self._catalog_access_denied:
            response = await self._get(
                client,
                "/commerce/catalog/v1_beta/product_summary/search",
                params={
                    "q": query,
                    "category_id": category_id,
                    "limit": max(1, min(limit, 50)),
                },
                headers=self._headers(),
            )
            if response.status_code not in {401, 403}:
                response.raise_for_status()
                if not response.content:
                    return []
                try:
                    payload = response.json()
                except ValueError:
                    logger.warning(
                        "eBay Catalog API returned a successful response without valid JSON "
                        "for query %r.",
                        query,
                    )
                    return []
                products = payload.get("productSummaries") or []
                return [product for product in products if isinstance(product, dict)]
            self._catalog_access_denied = True
            logger.info(
                "eBay Catalog API denied this seller token; using Browse PRODUCT stock images."
            )
        return await self._search_browse_catalog_products(
            client,
            query,
            category_id,
            limit=limit,
        )

    async def _search_browse_catalog_products(
        self,
        client: httpx.AsyncClient,
        query: str,
        category_id: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        seller_token = self._access_token
        if not self._application_access_token:
            self._application_access_token = await self._client_credentials_access_token(client)
        if not self._application_access_token:
            raise RuntimeError(
                "EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are required for eBay Browse product images."
            )

        self._access_token = self._application_access_token
        try:
            search_limit = max(50, min(limit * 5, 200))
            search_response = await self._get(
                client,
                "/buy/browse/v1/item_summary/search",
                params={
                    "q": query,
                    "category_ids": category_id,
                    "limit": search_limit,
                },
                headers=self._headers(),
            )
            search_response.raise_for_status()
            summaries = search_response.json().get("itemSummaries") or []
            candidates: list[dict[str, Any]] = []
            seen: set[str] = set()
            epid_summaries = 0
            detail_responses = 0
            product_containers = 0
            product_images = 0
            first_detail_keys: list[str] = []
            for summary in summaries[:search_limit]:
                if (
                    not isinstance(summary, dict)
                    or not summary.get("itemId")
                    or not summary.get("epid")
                ):
                    continue
                epid_summaries += 1
                detail_response = await self._get(
                    client,
                    f"/buy/browse/v1/item/{quote(str(summary['itemId']), safe='|')}",
                    params={"fieldgroups": "PRODUCT"},
                    headers=self._headers(),
                )
                if detail_response.status_code in {400, 404}:
                    continue
                detail_response.raise_for_status()
                detail = detail_response.json()
                detail_responses += 1
                if not first_detail_keys and isinstance(detail, dict):
                    first_detail_keys = sorted(str(key) for key in detail)[:40]
                if isinstance(detail.get("product"), dict):
                    product_containers += 1
                candidate = self._browse_product_catalog_candidate(summary, detail)
                if not candidate or not self._catalog_image_urls(candidate):
                    continue
                product_images += 1
                identity = "|".join(
                    [
                        str(candidate.get("epid") or ""),
                        str(candidate.get("title") or ""),
                        self._catalog_image_urls(candidate)[0],
                    ]
                )
                if identity in seen:
                    continue
                seen.add(identity)
                candidates.append(candidate)
                if len(candidates) >= max(1, min(limit, 50)):
                    break
            logger.info(
                "eBay Browse PRODUCT diagnostics: summaries=%s epid_summaries=%s "
                "detail_responses=%s product_containers=%s product_images=%s "
                "first_detail_keys=%s",
                len(summaries),
                epid_summaries,
                detail_responses,
                product_containers,
                product_images,
                first_detail_keys,
            )
            return candidates
        finally:
            self._access_token = seller_token

    @staticmethod
    def _browse_product_catalog_candidate(
        summary: dict[str, Any],
        detail: dict[str, Any],
    ) -> dict[str, Any] | None:
        product = detail.get("product")
        if not isinstance(product, dict):
            return None
        primary = product.get("image")
        if not isinstance(primary, dict) or not primary.get("imageUrl"):
            return None

        aspects: list[dict[str, Any]] = []
        for group in product.get("aspectGroups") or []:
            if not isinstance(group, dict):
                continue
            for aspect in group.get("aspects") or []:
                if not isinstance(aspect, dict):
                    continue
                name = str(aspect.get("name") or "").strip()
                values = aspect.get("values")
                if not isinstance(values, list):
                    value = aspect.get("value")
                    values = [value] if value else []
                clean_values = [str(value).strip() for value in values if str(value).strip()]
                if name and clean_values:
                    aspects.append(
                        {
                            "localizedName": name,
                            "localizedValues": clean_values,
                        }
                    )

        return {
            "epid": detail.get("epid") or summary.get("epid"),
            "title": product.get("title") or detail.get("title") or summary.get("title"),
            "brand": product.get("brand"),
            "image": primary,
            "additionalImages": product.get("additionalImages") or [],
            "aspects": aspects,
            "imageSource": "EBAY_BROWSE_PRODUCT",
        }

    async def _fetch_sell_inventory_items(self, limit: int = 200) -> list[InventoryItem]:
        items: list[InventoryItem] = []
        offset = 0
        page_size = max(1, min(limit, 200))

        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            await self._ensure_access_token(client)
            while True:
                response = await self._get(
                    client,
                    "/sell/inventory/v1/inventory_item",
                    params={"limit": page_size, "offset": offset},
                    headers=self._headers(),
                )
                response.raise_for_status()
                payload = response.json()
                raw_items = payload.get("inventoryItems", [])
                if not raw_items:
                    break

                for raw_item in raw_items:
                    item = self._normalize_inventory_item(raw_item)
                    offer = await self._fetch_offer(client, item.sku)
                    item = self._apply_offer(item, offer)
                    if self._is_active_available_listing(item):
                        items.append(item)
                    else:
                        self._log_skipped_listing(item, self._listing_skip_reason(item))

                offset += len(raw_items)
                if offset >= int(payload.get("total", offset)) or len(raw_items) < page_size:
                    break

        return items

    async def _fetch_browse_seller_items(self, seller_username: str, limit: int = 200) -> list[InventoryItem]:
        items: list[InventoryItem] = []
        seen_item_ids: set[str] = set()
        seen_skus: set[str] = set()
        offset = 0
        page_size = max(1, min(limit, 200))
        query = getattr(self.settings, "ebay_browse_search_query", None)
        if query is None or query == "":
            query = " "

        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            await self._ensure_access_token(client, prefer_application=True)
            while len(items) < limit:
                response = await self._get(
                    client,
                    "/buy/browse/v1/item_summary/search",
                    params={
                        "q": query,
                        "filter": f"sellers:{{{seller_username}}}",
                        "limit": min(page_size, limit - len(items)),
                        "offset": offset,
                    },
                    headers=self._headers(),
                )
                response.raise_for_status()
                payload = response.json()
                summaries = payload.get("itemSummaries", [])
                if not summaries:
                    break

                for summary in summaries:
                    item_id = str(summary.get("itemId") or "")
                    if not item_id or item_id in seen_item_ids:
                        continue
                    seen_item_ids.add(item_id)
                    try:
                        detail = await self._fetch_browse_item_detail(client, item_id)
                    except httpx.HTTPError as exc:
                        logger.warning(
                            "Skipping eBay Browse item %s because detail fetch failed after retries: %s",
                            item_id,
                            exc,
                        )
                        continue
                    combined = {**summary, **detail}
                    try:
                        group_rows = await self._fetch_browse_item_group(client, combined)
                    except httpx.HTTPError as exc:
                        logger.warning(
                            "Could not expand eBay Browse item group %s; using parent data: %s",
                            item_id,
                            exc,
                        )
                        group_rows = []
                    for raw_item in group_rows or [combined]:
                        item = self._normalize_browse_item(raw_item)
                        if item.sku in seen_skus:
                            continue
                        seen_skus.add(item.sku)
                        if self._is_active_available_listing(item):
                            items.append(item)
                        else:
                            self._log_skipped_listing(item, self._listing_skip_reason(item))
                        if len(items) >= limit:
                            break
                    if len(items) >= limit:
                        break

                offset += len(summaries)
                if offset >= int(payload.get("total", offset)) or len(summaries) < page_size:
                    break

        return items

    async def _fetch_browse_item_detail(self, client: httpx.AsyncClient, item_id: str) -> dict[str, Any]:
        response = await self._get(
            client,
            f"/buy/browse/v1/item/{item_id}",
            params={"fieldgroups": "PRODUCT"},
            headers=self._headers(),
        )
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    async def _fetch_browse_item_group(
        self,
        client: httpx.AsyncClient,
        raw_item: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if not bool(getattr(self.settings, "ebay_expand_item_groups", False)):
            return []
        primary_group = raw_item.get("primaryItemGroup")
        group_id: object = None
        if isinstance(primary_group, dict):
            group_id = primary_group.get("itemGroupId")
        group_id = group_id or raw_item.get("itemGroupId")
        group_id = group_id or self._legacy_item_id(raw_item)
        if not group_id:
            return []

        response = await self._get(
            client,
            "/buy/browse/v1/item/get_items_by_item_group",
            params={"item_group_id": str(group_id), "fieldgroups": "PRODUCT"},
            headers=self._headers(),
        )
        if response.status_code in {400, 404}:
            return []
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return []
        raw_group_items = payload.get("items")
        if not isinstance(raw_group_items, list):
            return []

        descriptions: dict[str, str] = {}
        for description_group in payload.get("commonDescriptions") or []:
            if not isinstance(description_group, dict):
                continue
            description = description_group.get("description")
            if not isinstance(description, str):
                continue
            for grouped_item_id in description_group.get("itemIds") or []:
                descriptions[str(grouped_item_id)] = description

        results: list[dict[str, Any]] = []
        for group_item in raw_group_items:
            if not isinstance(group_item, dict):
                continue
            item_id = str(group_item.get("itemId") or "")
            merged = {**raw_item, **group_item, "_browse_group_variant": True}
            if not merged.get("description") and item_id in descriptions:
                merged["description"] = descriptions[item_id]
            results.append(merged)
        if results:
            logger.info(
                "Expanded eBay Browse item group %s into %s purchasable variations.",
                group_id,
                len(results),
            )
        return results

    async def _enrich_with_trading_api(self, browse_items: list[InventoryItem]) -> list[InventoryItem]:
        enriched: list[InventoryItem] = []
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            if self._has_refresh_credentials():
                await self._ensure_access_token(client)
            elif self._configured_access_token:
                self._access_token = self._configured_access_token
            else:
                return browse_items
            for browse_item in browse_items:
                if not browse_item.ebay_item_id:
                    enriched.append(browse_item)
                    continue
                try:
                    response = await self._post_content(
                        client,
                        "/ws/api.dll",
                        content=self._trading_get_item_request(browse_item.ebay_item_id),
                        headers=self._trading_headers(),
                    )
                    response.raise_for_status()
                    trading_items = self._parse_trading_get_item(response.content, browse_item)
                except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
                    logger.warning(
                        "Could not enrich eBay listing %s through Trading API; using Browse data: %s",
                        browse_item.ebay_item_id,
                        exc,
                    )
                    enriched.append(browse_item)
                    continue

                active_items = [item for item in trading_items if self._is_active_available_listing(item)]
                if active_items:
                    enriched.extend(active_items)
                else:
                    self._log_skipped_listing(
                        browse_item,
                        "Trading API returned no active variation with available quantity",
                    )
        return enriched

    async def list_active_trading_item_ids(self, limit: int = 200) -> list[str]:
        """Return active seller listing IDs without expanding every Browse item."""
        seller_username = str(
            getattr(self.settings, "ebay_seller_username", "") or ""
        ).strip()
        if not seller_username:
            return []

        item_ids: list[str] = []
        seen_item_ids: set[str] = set()
        offset = 0
        page_size = max(1, min(limit, 200))
        query = getattr(self.settings, "ebay_browse_search_query", None)
        if query is None or query == "":
            query = " "

        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            await self._ensure_access_token(client, prefer_application=True)
            while len(item_ids) < limit:
                response = await self._get(
                    client,
                    "/buy/browse/v1/item_summary/search",
                    params={
                        "q": query,
                        "filter": f"sellers:{{{seller_username}}}",
                        "limit": min(page_size, limit - len(item_ids)),
                        "offset": offset,
                    },
                    headers=self._headers(),
                )
                response.raise_for_status()
                payload = response.json()
                summaries = payload.get("itemSummaries", [])
                if not summaries:
                    break

                for summary in summaries:
                    if not isinstance(summary, dict):
                        continue
                    item_id = self._legacy_item_id(summary)
                    if not item_id or item_id in seen_item_ids:
                        continue
                    seen_item_ids.add(item_id)
                    item_ids.append(item_id)
                    if len(item_ids) >= limit:
                        break

                offset += len(summaries)
                if offset >= int(payload.get("total", offset)) or len(summaries) < page_size:
                    break

        return item_ids

    async def fetch_trading_listing_snapshots(
        self,
        item_ids: list[str],
    ) -> list[dict[str, Any]]:
        snapshots: list[dict[str, Any]] = []
        async with httpx.AsyncClient(base_url=self.base_url, timeout=45) as client:
            await self._ensure_access_token(client)
            for item_id in self._dedupe_urls([str(value) for value in item_ids]):
                response = await self._post_content(
                    client,
                    "/ws/api.dll",
                    content=self._trading_get_item_request(item_id),
                    headers=self._trading_headers("GetItem"),
                )
                response.raise_for_status()
                snapshots.append(self._parse_trading_listing_snapshot(response.content))
        return snapshots

    async def fetch_active_inventory_quantities(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Fetch active seller SKU quantities in one lightweight Trading API call."""
        request_payload = self._trading_active_inventory_request(limit=limit)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=45) as client:
            await self._ensure_access_token(client)
            response = await self._post_content(
                client,
                "/ws/api.dll",
                content=request_payload,
                headers=self._trading_headers("GetMyeBaySelling"),
            )
            response.raise_for_status()
        return self._parse_trading_active_inventory(response.content)

    async def revise_inventory_quantities(
        self,
        updates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Update active Inventory API or legacy Trading API listings by seller SKU."""
        normalized = [
            {
                "sku": str(update.get("sku") or "").strip(),
                "item_id": str(update.get("item_id") or "").strip(),
                "quantity": max(0, int(update.get("quantity") or 0)),
                **(
                    {"inventory_tracking": str(update.get("inventory_tracking") or "").strip()}
                    if update.get("inventory_tracking")
                    else {}
                ),
                **(
                    {
                        "variation_specifics": {
                            str(name): str(value)
                            for name, value in update.get("variation_specifics", {}).items()
                            if str(name).strip() and str(value).strip()
                        }
                    }
                    if isinstance(update.get("variation_specifics"), dict)
                    else {}
                ),
                **(
                    {"start_price": float(update["start_price"])}
                    if update.get("start_price") is not None
                    else {}
                ),
                **(
                    {"currency": str(update.get("currency") or "USD").strip() or "USD"}
                    if update.get("start_price") is not None
                    else {}
                ),
            }
            for update in updates
            if str(update.get("sku") or "").strip()
        ]
        if not normalized:
            return []

        results: list[dict[str, Any]] = []
        inventory_api_updates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        trading_updates: list[dict[str, Any]] = []
        item_id_updates: list[dict[str, Any]] = []
        variation_specific_updates: list[dict[str, Any]] = []
        async with httpx.AsyncClient(base_url=self.base_url, timeout=45) as client:
            await self._ensure_access_token(client)
            for update in normalized:
                offer = await self._fetch_offer(client, update["sku"])
                if offer.get("offerId"):
                    inventory_api_updates.append((update, offer))
                elif update.get("inventory_tracking") == "variation_specifics":
                    variation_specific_updates.append(update)
                elif update.get("inventory_tracking") == "item_id":
                    item_id_updates.append(update)
                else:
                    trading_updates.append(update)

            for start in range(0, len(inventory_api_updates), 25):
                batch = inventory_api_updates[start : start + 25]
                payload = {
                    "requests": [
                        {
                            "sku": update["sku"],
                            "shipToLocationAvailability": {"quantity": update["quantity"]},
                            "offers": [
                                {
                                    "offerId": str(offer["offerId"]),
                                    "availableQuantity": update["quantity"],
                                }
                            ],
                        }
                        for update, offer in batch
                    ]
                }
                response = await self._post_json(
                    client,
                    "/sell/inventory/v1/bulk_update_price_quantity",
                    json_data=payload,
                    headers=self._headers(),
                )
                response.raise_for_status()
                response_payload = response.json() if response.content else {}
                response_rows = response_payload.get("responses") or []
                response_by_sku = {
                    str(row.get("sku") or ""): row
                    for row in response_rows
                    if isinstance(row, dict)
                }
                for update, _offer in batch:
                    row = response_by_sku.get(update["sku"], {})
                    status_code = int(row.get("statusCode") or 200)
                    results.append(
                        {
                            **update,
                            "status": "updated" if 200 <= status_code < 300 else "failed",
                            "api": "inventory",
                            "status_code": status_code,
                        }
                    )

            for start in range(0, len(trading_updates), 4):
                batch = trading_updates[start : start + 4]
                request_payload = self._trading_revise_inventory_request(batch)
                response = await self._post_content(
                    client,
                    "/ws/api.dll",
                    content=request_payload,
                    headers=self._trading_headers("ReviseInventoryStatus"),
                )
                response.raise_for_status()
                result = self._parse_trading_ack(response.content)
                status = "updated" if result["ack"] in {"Success", "Warning"} else "failed"
                for update in batch:
                    results.append(
                        {
                            **update,
                            "status": status,
                            "api": "trading",
                            "messages": result["messages"],
                        }
                    )

            for update in item_id_updates + variation_specific_updates:
                request_payload = self._trading_revise_fixed_price_quantity_request(update)
                response = await self._post_content(
                    client,
                    "/ws/api.dll",
                    content=request_payload,
                    headers=self._trading_headers("ReviseFixedPriceItem"),
                )
                response.raise_for_status()
                result = self._parse_trading_ack(response.content)
                results.append(
                    {
                        **update,
                        "status": (
                            "updated" if result["ack"] in {"Success", "Warning"} else "failed"
                        ),
                        "api": (
                            "trading_variation"
                            if update.get("inventory_tracking") == "variation_specifics"
                            else "trading_item"
                        ),
                        "messages": result["messages"],
                    }
                )
        return results

    async def revise_trading_listings(
        self,
        revisions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Revise listing metadata only; pricing is intentionally unsupported."""
        results: list[dict[str, Any]] = []
        if not revisions:
            return results

        async with httpx.AsyncClient(base_url=self.base_url, timeout=45) as client:
            await self._ensure_access_token(client)
            for revision in revisions:
                item_id = str(revision.get("item_id") or "").strip()
                listing_type = str(revision.get("listing_type") or "")
                has_variations = bool(revision.get("has_variations"))
                call_name = (
                    "ReviseFixedPriceItem"
                    if has_variations or "fixedprice" in listing_type.casefold()
                    else "ReviseItem"
                )
                request_payload = self._trading_revise_listing_request(
                    call_name=call_name,
                    item_id=item_id,
                    title=str(revision.get("title") or "").strip() or None,
                    item_specifics=revision.get("item_specifics"),
                )
                response = await self._post_content(
                    client,
                    "/ws/api.dll",
                    content=request_payload,
                    headers=self._trading_headers(call_name),
                )
                response.raise_for_status()
                result = self._parse_trading_ack(response.content)
                result.update({"item_id": item_id, "call_name": call_name})
                if result["ack"] not in {"Success", "Warning"}:
                    raise RuntimeError(
                        f"eBay {call_name} failed for {item_id}: "
                        f"{'; '.join(result['messages']) or result['ack']}"
                    )
                results.append(result)
        return results

    def _trading_headers(self, call_name: str = "GetItem") -> dict[str, str]:
        compatibility_level = str(
            getattr(self.settings, "ebay_trading_compatibility_level", "1455") or "1455"
        ).strip()
        return {
            "Content-Type": "text/xml;charset=UTF-8",
            "X-EBAY-API-IAF-TOKEN": str(self._access_token or ""),
            "X-EBAY-API-CALL-NAME": call_name,
            "X-EBAY-API-SITEID": "0",
            "X-EBAY-API-COMPATIBILITY-LEVEL": compatibility_level,
        }

    @staticmethod
    def _trading_get_item_request(item_id: str) -> bytes:
        root = ET.Element("GetItemRequest", xmlns="urn:ebay:apis:eBLBaseComponents")
        ET.SubElement(root, "ItemID").text = str(item_id)
        ET.SubElement(root, "DetailLevel").text = "ReturnAll"
        ET.SubElement(root, "IncludeItemSpecifics").text = "true"
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    @staticmethod
    def _trading_active_inventory_request(*, limit: int = 200) -> bytes:
        root = ET.Element("GetMyeBaySellingRequest", xmlns="urn:ebay:apis:eBLBaseComponents")
        active_list = ET.SubElement(root, "ActiveList")
        ET.SubElement(active_list, "Include").text = "true"
        pagination = ET.SubElement(active_list, "Pagination")
        ET.SubElement(pagination, "EntriesPerPage").text = str(max(1, min(limit, 200)))
        ET.SubElement(pagination, "PageNumber").text = "1"
        ET.SubElement(root, "HideVariations").text = "false"
        ET.SubElement(root, "DetailLevel").text = "ReturnAll"
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    @classmethod
    def _parse_trading_active_inventory(cls, payload: bytes | str) -> list[dict[str, Any]]:
        root = ET.fromstring(payload)
        ack = cls._xml_text(root, "Ack") or "Failure"
        if ack not in {"Success", "Warning"}:
            messages = [
                cls._xml_text(error, "LongMessage") or cls._xml_text(error, "ShortMessage")
                for error in cls._xml_children(root, "Errors")
            ]
            raise ValueError("; ".join(message for message in messages if message) or ack)

        item_array = cls._xml_nested_child(root, "ActiveList", "ItemArray")
        rows: list[dict[str, Any]] = []
        for item in cls._xml_children(item_array, "Item"):
            item_id = cls._xml_text(item, "ItemID") or ""
            variations = cls._xml_nested_child(item, "Variations")
            variation_rows = cls._xml_children(variations, "Variation")
            if variation_rows:
                for index, variation in enumerate(variation_rows, start=1):
                    seller_sku = cls._xml_text(variation, "SKU") or ""
                    variation_specifics = cls._xml_name_values(
                        cls._xml_child(variation, "VariationSpecifics")
                    )
                    sku = seller_sku or cls._generated_variation_sku(
                        item_id,
                        variation_specifics,
                        index,
                    )
                    total = cls._int_value(cls._xml_text(variation, "Quantity"))
                    sold = cls._int_value(
                        cls._xml_nested_text(variation, "SellingStatus", "QuantitySold")
                    )
                    row: dict[str, Any] = {
                        "sku": sku,
                        "item_id": item_id,
                        "quantity": max(0, total - sold),
                    }
                    if not seller_sku:
                        price_node = cls._xml_child(variation, "StartPrice")
                        price = cls._float_value(
                            price_node.text if price_node is not None else None
                        )
                        row.update(
                            {
                                "inventory_tracking": "variation_specifics",
                                "variation_specifics": variation_specifics,
                                "start_price": price,
                                "currency": (
                                    price_node.attrib.get("currencyID")
                                    if price_node is not None
                                    else None
                                )
                                or "USD",
                            }
                        )
                    rows.append(row)
                continue

            seller_sku = cls._xml_text(item, "SKU") or ""
            sku = seller_sku or (f"EBAY-{item_id}" if item_id else "")
            if not sku:
                continue
            total = cls._int_value(cls._xml_text(item, "Quantity"))
            sold = cls._int_value(cls._xml_nested_text(item, "SellingStatus", "QuantitySold"))
            row = {"sku": sku, "item_id": item_id, "quantity": max(0, total - sold)}
            if not seller_sku:
                row["inventory_tracking"] = "item_id"
            rows.append(row)
        return rows

    @staticmethod
    def _trading_revise_inventory_request(updates: list[dict[str, Any]]) -> bytes:
        if not 1 <= len(updates) <= 4:
            raise ValueError("ReviseInventoryStatus accepts between one and four updates.")
        root = ET.Element("ReviseInventoryStatusRequest", xmlns="urn:ebay:apis:eBLBaseComponents")
        for update in updates:
            sku = str(update.get("sku") or "").strip()
            item_id = str(update.get("item_id") or "").strip()
            if not sku:
                raise ValueError("An eBay seller SKU is required for quantity updates.")
            status = ET.SubElement(root, "InventoryStatus")
            if item_id:
                ET.SubElement(status, "ItemID").text = item_id
            ET.SubElement(status, "SKU").text = sku
            ET.SubElement(status, "Quantity").text = str(max(0, int(update.get("quantity") or 0)))
        ET.SubElement(root, "ErrorLanguage").text = "en_US"
        ET.SubElement(root, "WarningLevel").text = "High"
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    @staticmethod
    def _trading_revise_fixed_price_quantity_request(update: dict[str, Any]) -> bytes:
        item_id = str(update.get("item_id") or "").strip()
        if not item_id:
            raise ValueError("An eBay item ID is required for quantity updates without a seller SKU.")

        root = ET.Element(
            "ReviseFixedPriceItemRequest",
            xmlns="urn:ebay:apis:eBLBaseComponents",
        )
        item = ET.SubElement(root, "Item")
        ET.SubElement(item, "ItemID").text = item_id
        quantity = str(max(0, int(update.get("quantity") or 0)))
        if update.get("inventory_tracking") == "variation_specifics":
            specifics = update.get("variation_specifics")
            if not isinstance(specifics, dict) or not specifics:
                raise ValueError("Variation specifics are required to update an eBay variation without a SKU.")
            if update.get("start_price") is None:
                raise ValueError("Start price is required to update an eBay variation without a SKU.")
            variations = ET.SubElement(item, "Variations")
            variation = ET.SubElement(variations, "Variation")
            start_price = ET.SubElement(variation, "StartPrice")
            start_price.attrib["currencyID"] = str(update.get("currency") or "USD")
            start_price.text = f"{float(update['start_price']):.2f}"
            ET.SubElement(variation, "Quantity").text = quantity
            variation_specifics = ET.SubElement(variation, "VariationSpecifics")
            for name, value in specifics.items():
                pair = ET.SubElement(variation_specifics, "NameValueList")
                ET.SubElement(pair, "Name").text = str(name)
                ET.SubElement(pair, "Value").text = str(value)
        else:
            ET.SubElement(item, "Quantity").text = quantity
        ET.SubElement(root, "ErrorLanguage").text = "en_US"
        ET.SubElement(root, "WarningLevel").text = "High"
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    @classmethod
    def _parse_trading_listing_snapshot(cls, payload: bytes | str) -> dict[str, Any]:
        raw_xml = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        root = ET.fromstring(raw_xml)
        ack = cls._xml_text(root, "Ack") or "Failure"
        if ack not in {"Success", "Warning"}:
            messages = [
                cls._xml_text(error, "LongMessage") or cls._xml_text(error, "ShortMessage")
                for error in cls._xml_children(root, "Errors")
            ]
            raise ValueError("; ".join(message for message in messages if message) or ack)

        item = cls._xml_child(root, "Item")
        if item is None:
            raise ValueError("eBay Trading API response did not include an Item.")

        variations = cls._xml_child(item, "Variations")
        variation_rows: list[dict[str, Any]] = []
        for variation in cls._xml_children(variations, "Variation"):
            variation_rows.append(
                {
                    "sku": cls._xml_text(variation, "SKU"),
                    "specifics": cls._xml_name_value_lists(
                        cls._xml_child(variation, "VariationSpecifics")
                    ),
                    "start_price": cls._xml_money(cls._xml_child(variation, "StartPrice")),
                }
            )

        variation_names = [
            value
            for value in cls._xml_child_texts(
                cls._xml_nested_child(variations, "VariationSpecificsSet"),
                "Name",
            )
            if value
        ]
        picture_urls = cls._xml_child_texts(
            cls._xml_child(item, "PictureDetails"),
            "PictureURL",
        )
        for pictures in cls._xml_children(variations, "Pictures"):
            for picture_set in cls._xml_children(
                pictures,
                "VariationSpecificPictureSet",
            ):
                picture_urls.extend(
                    cls._xml_child_texts(picture_set, "PictureURL")
                )
        return {
            "item_id": cls._xml_text(item, "ItemID"),
            "title": cls._xml_text(item, "Title") or "",
            "listing_type": cls._xml_text(item, "ListingType") or "",
            "condition_id": cls._xml_text(item, "ConditionID"),
            "condition": cls._xml_text(item, "ConditionDisplayName"),
            "category_id": cls._xml_nested_text(item, "PrimaryCategory", "CategoryID"),
            "category_name": cls._xml_nested_text(item, "PrimaryCategory", "CategoryName"),
            "item_specifics": cls._xml_name_value_lists(
                cls._xml_child(item, "ItemSpecifics")
            ),
            "picture_urls": cls._dedupe_urls(picture_urls),
            "has_variations": bool(variation_rows),
            "variation_specific_names": variation_names,
            "prices": {
                "start_price": cls._xml_money(cls._xml_child(item, "StartPrice")),
                "current_price": cls._xml_money(
                    cls._xml_nested_child(item, "SellingStatus", "CurrentPrice")
                ),
                "variations": variation_rows,
            },
            "raw_xml": raw_xml,
        }

    @staticmethod
    def _trading_revise_listing_request(
        *,
        call_name: str,
        item_id: str,
        title: str | None,
        item_specifics: list[dict[str, Any]] | None,
    ) -> bytes:
        if call_name not in {"ReviseFixedPriceItem", "ReviseItem"}:
            raise ValueError(f"Unsupported eBay Trading API revision call: {call_name}")
        if not item_id:
            raise ValueError("An eBay item ID is required.")
        if not title and item_specifics is None:
            raise ValueError("A title or item-specific revision is required.")

        root = ET.Element(
            f"{call_name}Request",
            xmlns="urn:ebay:apis:eBLBaseComponents",
        )
        item = ET.SubElement(root, "Item")
        ET.SubElement(item, "ItemID").text = item_id
        if title:
            ET.SubElement(item, "Title").text = title[:80]
        if item_specifics is not None:
            specifics_node = ET.SubElement(item, "ItemSpecifics")
            for entry in item_specifics:
                name = str(entry.get("name") or "").strip()
                values = [
                    str(value).strip()
                    for value in entry.get("values") or []
                    if str(value).strip()
                ]
                if not name or not values:
                    continue
                name_value = ET.SubElement(specifics_node, "NameValueList")
                ET.SubElement(name_value, "Name").text = name
                for value in values:
                    ET.SubElement(name_value, "Value").text = value
        ET.SubElement(root, "ErrorLanguage").text = "en_US"
        ET.SubElement(root, "WarningLevel").text = "High"
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    @classmethod
    def _parse_trading_ack(cls, payload: bytes | str) -> dict[str, Any]:
        root = ET.fromstring(payload)
        messages = [
            cls._xml_text(error, "LongMessage") or cls._xml_text(error, "ShortMessage")
            for error in cls._xml_children(root, "Errors")
        ]
        return {
            "ack": cls._xml_text(root, "Ack") or "Failure",
            "messages": [message for message in messages if message],
        }

    @classmethod
    def _xml_name_value_lists(
        cls,
        parent: ET.Element | None,
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for name_value in cls._xml_children(parent, "NameValueList"):
            name = cls._xml_text(name_value, "Name")
            values = cls._xml_child_texts(name_value, "Value")
            if name and values:
                entries.append({"name": name, "values": values})
        return entries

    @staticmethod
    def _xml_money(node: ET.Element | None) -> dict[str, str] | None:
        if node is None or node.text is None:
            return None
        return {
            "value": node.text.strip(),
            "currency": str(node.attrib.get("currencyID") or ""),
        }

    def _parse_trading_get_item(
        self,
        payload: bytes | str,
        browse_item: InventoryItem,
    ) -> list[InventoryItem]:
        root = ET.fromstring(payload)
        ack = self._xml_text(root, "Ack") or "Failure"
        if ack not in {"Success", "Warning"}:
            messages = [
                self._xml_text(error, "LongMessage") or self._xml_text(error, "ShortMessage")
                for error in self._xml_children(root, "Errors")
            ]
            clean_messages = [message for message in messages if message]
            raise ValueError("; ".join(clean_messages) or f"eBay Trading API returned {ack}.")

        item_node = self._xml_child(root, "Item")
        if item_node is None:
            raise ValueError("eBay Trading API response did not include an Item.")

        item_id = self._xml_text(item_node, "ItemID") or browse_item.ebay_item_id
        title = self._xml_text(item_node, "Title") or browse_item.title
        description = (
            self._clean_xml_description(self._xml_text(item_node, "Description"))
            or browse_item.description
        )
        condition = self._xml_text(item_node, "ConditionDisplayName") or browse_item.condition
        listing_status = (
            self._xml_nested_text(item_node, "SellingStatus", "ListingStatus")
            or browse_item.listing_status
            or "ACTIVE"
        ).upper()
        category = (
            self._xml_nested_text(item_node, "PrimaryCategory", "CategoryName")
            or browse_item.category
        )
        parent_specifics = self._xml_name_values(self._xml_child(item_node, "ItemSpecifics"))
        category_id = self._xml_nested_text(item_node, "PrimaryCategory", "CategoryID")
        condition_id = self._xml_text(item_node, "ConditionID")
        if category_id:
            parent_specifics["categoryId"] = category_id
        if condition_id:
            parent_specifics["conditionId"] = condition_id
        parent_specifics.update(
            self._xml_product_identifiers(self._xml_child(item_node, "ProductListingDetails"))
        )
        shipping_weight = self._xml_shipping_weight(item_node)
        if shipping_weight:
            parent_specifics["Shipping Weight"] = shipping_weight

        parent_images = self._dedupe_urls(
            [
                value
                for value in self._xml_child_texts(
                    self._xml_child(item_node, "PictureDetails"),
                    "PictureURL",
                )
                if value
            ]
            + browse_item.image_urls
        )
        item_price_node = self._xml_nested_child(item_node, "SellingStatus", "CurrentPrice")
        if item_price_node is None:
            item_price_node = self._xml_child(item_node, "StartPrice")
        item_price = self._float_value(item_price_node.text if item_price_node is not None else None)
        item_currency = (
            item_price_node.attrib.get("currencyID") if item_price_node is not None else None
        ) or browse_item.currency
        variations_node = self._xml_child(item_node, "Variations")
        variation_nodes = self._xml_children(variations_node, "Variation")
        if variation_nodes:
            picture_map = self._xml_variation_picture_map(variations_node)
            results: list[InventoryItem] = []
            for index, variation in enumerate(variation_nodes, start=1):
                variation_specifics = self._xml_name_values(
                    self._xml_child(variation, "VariationSpecifics")
                )
                specifics = dict(parent_specifics)
                specifics.update(variation_specifics)
                for identifier_name in list(specifics):
                    normalized_name = re.sub(r"[^a-z0-9]", "", identifier_name.lower())
                    if normalized_name in {"gtin", "upc", "ean", "isbn"}:
                        specifics.pop(identifier_name, None)
                specifics.update(
                    self._xml_product_identifiers(
                        self._xml_child(variation, "VariationProductListingDetails")
                    )
                )
                variation_sku = self._xml_text(variation, "SKU") or self._generated_variation_sku(
                    item_id or browse_item.ebay_item_id or browse_item.sku,
                    variation_specifics,
                    index,
                )
                total_quantity = self._int_value(self._xml_text(variation, "Quantity"))
                sold_quantity = self._int_value(
                    self._xml_nested_text(variation, "SellingStatus", "QuantitySold")
                )
                price_node = self._xml_child(variation, "StartPrice")
                price = self._float_value(price_node.text if price_node is not None else None)
                currency = (
                    price_node.attrib.get("currencyID") if price_node is not None else None
                ) or item_currency
                matched_images = self._variation_images(variation_specifics, picture_map)
                image_urls = self._dedupe_urls(matched_images + parent_images)
                variation_label = " / ".join(variation_specifics.values())
                variation_title = f"{title} - {variation_label}" if variation_label else title
                results.append(
                    InventoryItem(
                        sku=variation_sku,
                        title=variation_title,
                        description=description,
                        condition=condition,
                        price=price if price is not None else item_price or browse_item.price,
                        currency=currency,
                        quantity=max(0, total_quantity - sold_quantity),
                        ebay_item_id=item_id,
                        ebay_url=browse_item.ebay_url
                        or (f"https://www.ebay.com/itm/{item_id}" if item_id else None),
                        image_url=self._primary_image_url(matched_images)
                        or self._primary_image_url(parent_images),
                        image_urls=image_urls,
                        category=category,
                        listing_status=listing_status,
                        item_specifics=specifics,
                        source="ebay-trading-api",
                        updated_at=datetime.now(timezone.utc),
                    )
                )
            return results

        total_quantity = self._int_value(self._xml_text(item_node, "Quantity"))
        sold_quantity = self._int_value(
            self._xml_nested_text(item_node, "SellingStatus", "QuantitySold")
        )
        item_sku = self._xml_text(item_node, "SKU") or browse_item.sku
        return [
            InventoryItem(
                sku=item_sku,
                title=title,
                description=description,
                condition=condition,
                price=item_price if item_price is not None else browse_item.price,
                currency=item_currency,
                quantity=max(0, total_quantity - sold_quantity),
                ebay_item_id=item_id,
                ebay_url=browse_item.ebay_url
                or (f"https://www.ebay.com/itm/{item_id}" if item_id else None),
                image_url=self._primary_image_url(parent_images) or browse_item.image_url,
                image_urls=parent_images,
                category=category,
                listing_status=listing_status,
                item_specifics=parent_specifics,
                source="ebay-trading-api",
                updated_at=datetime.now(timezone.utc),
            )
        ]

    @staticmethod
    def _xml_child(node: ET.Element | None, name: str) -> ET.Element | None:
        if node is None:
            return None
        for child in node:
            if str(child.tag).rsplit("}", 1)[-1] == name:
                return child
        return None

    @staticmethod
    def _xml_children(node: ET.Element | None, name: str) -> list[ET.Element]:
        if node is None:
            return []
        return [
            child
            for child in node
            if str(child.tag).rsplit("}", 1)[-1] == name
        ]

    @classmethod
    def _xml_nested_child(cls, node: ET.Element | None, *names: str) -> ET.Element | None:
        current = node
        for name in names:
            current = cls._xml_child(current, name)
            if current is None:
                return None
        return current

    @classmethod
    def _xml_text(cls, node: ET.Element | None, name: str) -> str | None:
        child = cls._xml_child(node, name)
        if child is None or child.text is None:
            return None
        value = child.text.strip()
        return value or None

    @classmethod
    def _xml_nested_text(cls, node: ET.Element | None, *names: str) -> str | None:
        child = cls._xml_nested_child(node, *names)
        if child is None or child.text is None:
            return None
        value = child.text.strip()
        return value or None

    @classmethod
    def _xml_child_texts(cls, node: ET.Element | None, name: str) -> list[str]:
        return [
            child.text.strip()
            for child in cls._xml_children(node, name)
            if child.text and child.text.strip()
        ]

    @classmethod
    def _xml_name_values(cls, container: ET.Element | None) -> dict[str, str]:
        values: dict[str, str] = {}
        for pair in cls._xml_children(container, "NameValueList"):
            name = cls._xml_text(pair, "Name")
            pair_values = cls._xml_child_texts(pair, "Value")
            if name and pair_values:
                values[name] = ", ".join(pair_values)
        return values

    @classmethod
    def _xml_product_identifiers(cls, container: ET.Element | None) -> dict[str, str]:
        identifiers: dict[str, str] = {}
        for name in ("GTIN", "UPC", "EAN", "ISBN"):
            value = cls._xml_text(container, name)
            if value:
                identifiers[name] = value
        return identifiers

    @classmethod
    def _xml_shipping_weight(cls, item_node: ET.Element) -> str | None:
        package = cls._xml_child(item_node, "ShippingPackageDetails")
        weight_lbs = 0.0
        found = False
        for name in ("WeightMajor", "WeightMinor"):
            measure = cls._xml_child(package, name)
            if measure is None or measure.text is None:
                continue
            try:
                amount = float(measure.text.strip())
            except (TypeError, ValueError):
                continue
            unit = str(measure.attrib.get("unit") or "lb").strip().lower()
            factors = {
                "lb": 1.0,
                "lbs": 1.0,
                "pound": 1.0,
                "oz": 1 / 16,
                "ounce": 1 / 16,
                "kg": 2.2046226218,
                "kilogram": 2.2046226218,
                "g": 0.0022046226218,
                "gr": 0.0022046226218,
                "gram": 0.0022046226218,
            }
            factor = factors.get(unit)
            if factor is None:
                continue
            weight_lbs += amount * factor
            found = True
        if not found or weight_lbs <= 0:
            return None
        formatted = f"{weight_lbs:.3f}".rstrip("0").rstrip(".")
        return f"{formatted} lb"

    @classmethod
    def _xml_variation_picture_map(
        cls,
        variations_node: ET.Element | None,
    ) -> dict[tuple[str, str], list[str]]:
        picture_map: dict[tuple[str, str], list[str]] = {}
        for pictures in cls._xml_children(variations_node, "Pictures"):
            name = cls._xml_text(pictures, "VariationSpecificName")
            if not name:
                continue
            for picture_set in cls._xml_children(pictures, "VariationSpecificPictureSet"):
                value = cls._xml_text(picture_set, "VariationSpecificValue")
                urls = cls._xml_child_texts(picture_set, "PictureURL")
                if value and urls:
                    picture_map[(name.casefold(), value.casefold())] = urls
        return picture_map

    @staticmethod
    def _variation_images(
        variation_specifics: dict[str, str],
        picture_map: dict[tuple[str, str], list[str]],
    ) -> list[str]:
        urls: list[str] = []
        for name, value in variation_specifics.items():
            urls.extend(picture_map.get((name.casefold(), value.casefold()), []))
        return EbayClient._dedupe_urls(urls)

    @staticmethod
    def _generated_variation_sku(
        item_id: str,
        variation_specifics: dict[str, str],
        index: int,
    ) -> str:
        identity = "|".join(
            f"{name}={value}" for name, value in sorted(variation_specifics.items())
        ) or str(index)
        suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
        base = re.sub(r"[^A-Za-z0-9._-]+", "-", f"EBAY-{item_id}").strip("-.")
        return f"{base[:39]}-{suffix}"

    @staticmethod
    def _clean_xml_description(value: str | None) -> str | None:
        if not value:
            return None
        cleaned = re.sub(r"<[^>]+>", " ", value)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned[:500] or None

    @staticmethod
    def _int_value(value: object) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Content-Language": "en-US",
            "X-EBAY-C-MARKETPLACE-ID": self.settings.ebay_marketplace_id,
        }

    def _has_refresh_credentials(self) -> bool:
        return all(
            str(getattr(self.settings, field, "") or "").strip()
            for field in ("ebay_client_id", "ebay_client_secret", "ebay_refresh_token")
        )

    def _has_client_credentials(self) -> bool:
        return all(
            str(getattr(self.settings, field, "") or "").strip()
            for field in ("ebay_client_id", "ebay_client_secret")
        )

    def _has_trading_user_token(self) -> bool:
        return self._has_refresh_credentials() or bool(
            self._configured_access_token
            and getattr(self.settings, "ebay_use_access_token_for_trading", False)
        )

    async def _ensure_access_token(
        self,
        client: httpx.AsyncClient,
        *,
        prefer_application: bool = False,
    ) -> None:
        if prefer_application and self._has_client_credentials():
            refreshed_token = await self._client_credentials_access_token(client)
        elif self._has_refresh_credentials():
            refreshed_token = await self._refresh_access_token(client)
        elif self._access_token:
            refreshed_token = None
        elif self._has_client_credentials():
            refreshed_token = await self._client_credentials_access_token(client)
        else:
            refreshed_token = None
        if refreshed_token:
            self._access_token = refreshed_token

    async def _refresh_access_token(self, client: httpx.AsyncClient) -> str | None:
        client_id = str(getattr(self.settings, "ebay_client_id", "") or "").strip()
        client_secret = str(getattr(self.settings, "ebay_client_secret", "") or "").strip()
        refresh_token = str(getattr(self.settings, "ebay_refresh_token", "") or "").strip()
        scopes = str(getattr(self.settings, "ebay_oauth_scopes", "") or "").strip()
        if not client_id or not client_secret or not refresh_token:
            return None

        credentials = b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        if scopes:
            data["scope"] = scopes
        response = await self._post(
            client,
            "/identity/v1/oauth2/token",
            data=data,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        response.raise_for_status()
        payload = response.json()
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise RuntimeError("eBay OAuth refresh response did not include an access token.")
        logger.info("Refreshed eBay access token for inventory sync.")
        return access_token.strip()

    async def _client_credentials_access_token(
        self,
        client: httpx.AsyncClient,
        *,
        scopes_override: str | None = None,
    ) -> str | None:
        client_id = str(getattr(self.settings, "ebay_client_id", "") or "").strip()
        client_secret = str(getattr(self.settings, "ebay_client_secret", "") or "").strip()
        scopes = (
            str(scopes_override).strip()
            if scopes_override is not None
            else str(
                getattr(
                    self.settings,
                    "ebay_application_oauth_scopes",
                    "https://api.ebay.com/oauth/api_scope",
                )
                or ""
            ).strip()
        )
        if not client_id or not client_secret:
            return None

        credentials = b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        data = {
            "grant_type": "client_credentials",
        }
        if scopes:
            data["scope"] = scopes
        response = await self._post(
            client,
            "/identity/v1/oauth2/token",
            data=data,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        response.raise_for_status()
        payload = response.json()
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise RuntimeError("eBay OAuth client credentials response did not include an access token.")
        logger.info("Minted eBay application access token for inventory sync.")
        return access_token.strip()

    async def _fetch_offer(self, client: httpx.AsyncClient, sku: str) -> dict[str, Any]:
        response = await self._get(
            client,
            "/sell/inventory/v1/offer",
            params={"sku": sku},
            headers=self._headers(),
        )
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        offers = response.json().get("offers", [])
        return offers[0] if offers else {}

    def _normalize_inventory_item(self, raw_item: dict[str, Any]) -> InventoryItem:
        product = raw_item.get("product", {})
        availability = raw_item.get("availability", {}).get("shipToLocationAvailability", {})
        aspects = product.get("aspects") or {}
        item_specifics = {
            str(key): ", ".join(value) if isinstance(value, list) else str(value)
            for key, value in aspects.items()
        }
        item_specifics.update(self._sell_product_identifiers(product))
        item_specifics.update(self._sell_package_specifics(raw_item))
        image_urls = self._image_urls_from_sell_product(product)
        item_id = str(raw_item.get("sku") or "")

        return InventoryItem(
            sku=item_id,
            title=str(product.get("title") or item_id or "Untitled eBay item"),
            description=product.get("description"),
            condition=raw_item.get("condition"),
            quantity=int(availability.get("quantity") or 0),
            image_url=self._primary_image_url(image_urls),
            image_urls=image_urls,
            item_specifics=item_specifics,
            source="ebay-api",
            updated_at=datetime.now(timezone.utc),
        )

    def _apply_offer(self, item: InventoryItem, offer: dict[str, Any]) -> InventoryItem:
        price = (offer.get("pricingSummary") or {}).get("price") or {}
        listing = offer.get("listing") or {}
        listing_id = listing.get("listingId") or offer.get("listingId")

        if price.get("value") is not None:
            item.price = float(price["value"])
            item.currency = price.get("currency") or item.currency
        if listing_id:
            item.ebay_item_id = str(listing_id)
            item.ebay_url = f"https://www.ebay.com/itm/{listing_id}"
        item.listing_status = str(offer.get("status") or listing.get("listingStatus") or "PUBLISHED")
        if offer.get("availableQuantity") is not None:
            item.quantity = int(offer.get("availableQuantity") or item.quantity)
        return item

    def _normalize_browse_item(self, raw_item: dict[str, Any]) -> InventoryItem:
        legacy_item_id = self._legacy_item_id(raw_item)
        restful_item_id = str(raw_item.get("itemId") or "")
        image_urls = self._image_urls_from_browse_item(raw_item)
        price = raw_item.get("price") or {}
        availability = self._browse_availability(raw_item)
        item_specifics = self._browse_item_specifics(raw_item)
        category = self._browse_category(raw_item)
        short_description = self._short_description(raw_item)

        return InventoryItem(
            sku=(
                self._generated_browse_variation_sku(legacy_item_id or restful_item_id, restful_item_id)
                if raw_item.get("_browse_group_variant")
                else f"EBAY-{legacy_item_id}" if legacy_item_id else restful_item_id
            ),
            title=str(raw_item.get("title") or f"eBay listing {legacy_item_id}"),
            description=short_description,
            condition=raw_item.get("condition"),
            price=self._float_value(price.get("value")),
            currency=str(price.get("currency") or "USD"),
            quantity=availability["quantity"],
            ebay_item_id=legacy_item_id,
            ebay_url=raw_item.get("itemWebUrl") or (f"https://www.ebay.com/itm/{legacy_item_id}" if legacy_item_id else None),
            image_url=self._primary_image_url(image_urls),
            image_urls=image_urls,
            category=category,
            listing_status=availability["status"],
            item_specifics=item_specifics,
            source="ebay-browse-api",
            updated_at=datetime.now(timezone.utc),
        )

    def _is_active_available_listing(self, item: InventoryItem) -> bool:
        return self._listing_skip_reason(item) is None

    @staticmethod
    def _listing_skip_reason(item: InventoryItem) -> str | None:
        if item.quantity <= 0:
            return "listing has no available quantity"
        if not item.image_url:
            return "listing has no valid primary eBay image"
        status = (item.listing_status or "ACTIVE").strip().upper()
        if status in {"SOLD", "ENDED", "INACTIVE", "OUT_OF_STOCK", "UNAVAILABLE"}:
            return f"listing status is {status}"
        if status in {"ACTIVE", "IN_STOCK", "PUBLISHED", "LIVE"} or not item.listing_status:
            return None
        return f"listing status is {status}"

    @staticmethod
    def _log_skipped_listing(item: InventoryItem, reason: str | None) -> None:
        logger.warning(
            "Skipping eBay listing for inventory social automation: ebay_item_id=%s sku=%s title=%r "
            "image_url=%s ebay_url=%s status=skipped error=%s",
            item.ebay_item_id,
            item.sku,
            item.title,
            item.image_url,
            item.ebay_url,
            reason or "not promotable",
        )

    @staticmethod
    def _legacy_item_id(raw_item: dict[str, Any]) -> str | None:
        value = raw_item.get("legacyItemId")
        if value:
            return str(value)
        item_id = str(raw_item.get("itemId") or "")
        match = re.search(r"\|(\d+)\|", item_id)
        return match.group(1) if match else None

    @staticmethod
    def _float_value(value: object) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _short_description(raw_item: dict[str, Any]) -> str | None:
        value = raw_item.get("shortDescription") or raw_item.get("conditionDescription") or raw_item.get("description")
        if not isinstance(value, str):
            return None
        cleaned = re.sub(r"<[^>]+>", " ", value)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned[:500] or None

    @staticmethod
    def _browse_category(raw_item: dict[str, Any]) -> str | None:
        category_path = raw_item.get("categoryPath")
        if isinstance(category_path, str) and category_path.strip():
            return category_path.strip()
        categories = raw_item.get("categories")
        if isinstance(categories, list) and categories:
            names = [str(category.get("categoryName")) for category in categories if isinstance(category, dict)]
            return " > ".join(name for name in names if name and name != "None") or None
        return None

    @staticmethod
    def _browse_availability(raw_item: dict[str, Any]) -> dict[str, int | str]:
        availabilities = raw_item.get("estimatedAvailabilities")
        if not isinstance(availabilities, list) or not availabilities:
            return {"quantity": 1, "status": "ACTIVE"}

        quantity = 0
        status = "OUT_OF_STOCK"
        for availability in availabilities:
            if not isinstance(availability, dict):
                continue
            availability_status = str(availability.get("estimatedAvailabilityStatus") or "").upper()
            available_quantity = availability.get("estimatedAvailableQuantity")
            remaining_quantity = availability.get("estimatedRemainingQuantity")
            candidate_quantity = available_quantity if available_quantity is not None else remaining_quantity
            if availability_status in {"IN_STOCK", "LIMITED_STOCK"}:
                status = "IN_STOCK"
                quantity = max(quantity, int(candidate_quantity or 1))
        return {"quantity": quantity, "status": status}

    @staticmethod
    def _browse_item_specifics(raw_item: dict[str, Any]) -> dict[str, str]:
        item_specifics: dict[str, str] = {}
        localized_aspects = raw_item.get("localizedAspects")
        if isinstance(localized_aspects, list):
            for aspect in localized_aspects:
                if not isinstance(aspect, dict):
                    continue
                name = str(aspect.get("name") or "").strip()
                value = str(aspect.get("value") or "").strip()
                if name and value:
                    item_specifics[name] = value
        direct_fields = {
            "brand": "Brand",
            "color": "Color",
            "gtin": "GTIN",
            "upc": "UPC",
            "ean": "EAN",
            "isbn": "ISBN",
            "mpn": "MPN",
            "conditionId": "conditionId",
            "categoryId": "categoryId",
            "listingMarketplaceId": "listingMarketplaceId",
        }
        for field, label in direct_fields.items():
            value = raw_item.get(field)
            if value:
                if isinstance(value, list):
                    clean_values = [str(part).strip() for part in value if str(part).strip()]
                    if clean_values:
                        item_specifics[label] = clean_values[0]
                else:
                    item_specifics[label] = str(value)
        product = raw_item.get("product")
        if isinstance(product, dict):
            gtins = product.get("gtins")
            if isinstance(gtins, list):
                clean_gtins = [str(value).strip() for value in gtins if str(value).strip()]
                if clean_gtins:
                    item_specifics["GTIN"] = clean_gtins[0]
            mpns = product.get("mpns")
            if isinstance(mpns, list):
                clean_mpns = [str(value).strip() for value in mpns if str(value).strip()]
                if clean_mpns:
                    item_specifics["MPN"] = clean_mpns[0]
            brand = product.get("brand")
            if brand:
                item_specifics["Brand"] = str(brand).strip()
        item_specifics.update(EbayClient._browse_shipping_specifics(raw_item))
        return item_specifics

    @staticmethod
    def _generated_browse_variation_sku(legacy_item_id: str, restful_item_id: str) -> str:
        suffix = hashlib.sha256(restful_item_id.encode("utf-8")).hexdigest()[:10]
        base = re.sub(r"[^A-Za-z0-9._-]+", "-", f"EBAY-{legacy_item_id}").strip("-.")
        return f"{base[:39]}-{suffix}"

    @staticmethod
    def _sell_product_identifiers(product: dict[str, Any]) -> dict[str, str]:
        identifiers: dict[str, str] = {}
        fields = {
            "brand": "Brand",
            "mpn": "MPN",
            "gtin": "GTIN",
            "upc": "UPC",
            "ean": "EAN",
            "isbn": "ISBN",
        }
        for field, label in fields.items():
            value = product.get(field)
            if isinstance(value, list):
                clean_values = [str(part).strip() for part in value if str(part).strip()]
                if clean_values:
                    identifiers[label] = clean_values[0]
            elif value is not None and str(value).strip():
                identifiers[label] = str(value).strip()
        return identifiers

    @staticmethod
    def _sell_package_specifics(raw_item: dict[str, Any]) -> dict[str, str]:
        package = raw_item.get("packageWeightAndSize")
        if not isinstance(package, dict):
            return {}
        weight = package.get("weight")
        if not isinstance(weight, dict) or weight.get("value") is None:
            return {}
        unit = str(weight.get("unit") or "POUND").strip().lower()
        unit_labels = {
            "pound": "lb",
            "ounce": "oz",
            "kilogram": "kg",
            "gram": "g",
        }
        return {"Shipping Weight": f"{weight['value']} {unit_labels.get(unit, unit)}"}

    @staticmethod
    def _browse_shipping_specifics(raw_item: dict[str, Any]) -> dict[str, str]:
        shipping_options = raw_item.get("shippingOptions")
        if not isinstance(shipping_options, list):
            return {}
        for option in shipping_options:
            if not isinstance(option, dict):
                continue
            shipping_cost = option.get("shippingCost") or option.get("shippingCostConverted")
            if not isinstance(shipping_cost, dict):
                continue
            value = EbayClient._float_value(shipping_cost.get("value"))
            currency = str(shipping_cost.get("currency") or "USD")
            if value is None:
                continue
            if value == 0:
                return {"Shipping": "Free Shipping", "Shipping Cost": f"0 {currency}"}
            return {"Shipping Cost": f"{value:g} {currency}"}
        return {}

    def _image_urls_from_sell_product(self, product: dict[str, Any]) -> list[str]:
        urls: list[str] = []
        for value in product.get("imageUrls") or []:
            if isinstance(value, str):
                urls.append(value)
        return self._dedupe_urls(urls)

    def _image_urls_from_browse_item(self, raw_item: dict[str, Any]) -> list[str]:
        urls: list[str] = []
        for field in ("image", "additionalImages", "thumbnailImages"):
            value = raw_item.get(field)
            if isinstance(value, dict):
                url = value.get("imageUrl")
                if isinstance(url, str):
                    urls.append(url)
            elif isinstance(value, list):
                for image in value:
                    if isinstance(image, dict) and isinstance(image.get("imageUrl"), str):
                        urls.append(str(image["imageUrl"]))
        return self._dedupe_urls(urls)

    def _primary_image_url(self, urls: list[str]) -> str | None:
        usable_urls = [url for url in self._dedupe_urls(urls) if self._usable_image_url(url)]
        if not usable_urls:
            return None
        return max(
            enumerate(usable_urls),
            key=lambda pair: (self._image_url_pixel_hint(pair[1]), -pair[0]),
        )[1]

    @staticmethod
    def _image_url_pixel_hint(url: str) -> int:
        match = re.search(r"/s-l(\d+)(?:[./?]|$)", url.lower())
        if not match:
            return 0
        return int(match.group(1))

    @staticmethod
    def _usable_image_url(url: str) -> bool:
        lowered = url.lower().split("?")[0]
        return lowered.startswith("https://") and any(
            marker in lowered
            for marker in (".jpg", ".jpeg", ".png", ".webp", "/images/", "i.ebayimg.com")
        )

    @staticmethod
    def _dedupe_urls(urls: list[str]) -> list[str]:
        seen: set[str] = set()
        deduped: list[str] = []
        for url in urls:
            clean_url = str(url).strip()
            if not clean_url or clean_url in seen:
                continue
            seen.add(clean_url)
            deduped.append(clean_url)
        return deduped

    @staticmethod
    def _catalog_candidate_text(candidate: dict[str, Any]) -> str:
        values: list[str] = [str(candidate.get("title") or "")]
        brand = candidate.get("brand")
        if brand:
            values.append(str(brand))
        aspects = candidate.get("aspects")
        if isinstance(aspects, list):
            for aspect in aspects:
                if not isinstance(aspect, dict):
                    continue
                values.append(str(aspect.get("localizedName") or ""))
                localized_values = aspect.get("localizedValues")
                if isinstance(localized_values, list):
                    values.extend(str(value) for value in localized_values)
        return EbayClient._normalized_match_text(" ".join(values))

    @staticmethod
    def _normalized_match_text(value: object) -> str:
        text = str(value or "").lower()
        text = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", text)
        return re.sub(r"[^a-z0-9]+", " ", text).strip()

    @staticmethod
    def _match_tokens(value: object) -> list[str]:
        return [token for token in EbayClient._normalized_match_text(value).split() if token]

    @staticmethod
    def _catalog_match(draft: EbayDraftSpec, candidate: dict[str, Any]) -> dict[str, Any]:
        candidate_text = EbayClient._catalog_candidate_text(candidate)
        candidate_tokens = set(candidate_text.split())
        candidate_title = str(candidate.get("title") or "")
        candidate_title_normalized = EbayClient._normalized_match_text(candidate_title)
        candidate_title_tokens = set(candidate_title_normalized.split())
        stop_tokens = {"apple", "samsung", "motorola", "jbl", "galaxy"}
        model_tokens = [
            token
            for token in EbayClient._match_tokens(draft.model)
            if token not in stop_tokens
        ]
        storage_tokens = EbayClient._match_tokens(draft.storage)
        color_tokens = EbayClient._match_tokens(draft.color)
        network_tokens = EbayClient._match_tokens(draft.network)

        missing_model = [token for token in model_tokens if token not in candidate_title_tokens]
        full_model_tokens = EbayClient._match_tokens(draft.model)
        if full_model_tokens:
            model_pattern = (
                r"\b"
                + r"[^a-z0-9]*".join(re.escape(token) for token in full_model_tokens)
                + r"\b"
            )
            if not re.search(model_pattern, candidate_title.lower()):
                missing_model.append("ordered_model_phrase")
        model_qualifiers = {"mini", "pro", "max", "ultra", "plus"}
        draft_qualifiers = set(full_model_tokens) & model_qualifiers
        candidate_qualifiers = candidate_title_tokens & model_qualifiers
        missing_model.extend(
            f"unexpected_{qualifier}"
            for qualifier in sorted(candidate_qualifiers - draft_qualifiers)
        )
        missing_model = list(dict.fromkeys(missing_model))
        missing_storage = [token for token in storage_tokens if token not in candidate_tokens]
        missing_color = [token for token in color_tokens if token not in candidate_tokens]
        missing_network = [token for token in network_tokens if token not in candidate_tokens]
        exact = not (missing_model or missing_storage or missing_color or missing_network)
        score = (
            sum(6 for token in model_tokens if token in candidate_tokens)
            + sum(4 for token in storage_tokens if token in candidate_tokens)
            + sum(3 for token in color_tokens if token in candidate_tokens)
            + sum(5 for token in network_tokens if token in candidate_tokens)
        )
        return {
            "exact": exact,
            "score": score,
            "missing_model_tokens": missing_model,
            "missing_storage_tokens": missing_storage,
            "missing_color_tokens": missing_color,
            "missing_network_tokens": missing_network,
        }

    @staticmethod
    def _select_catalog_product(
        draft: EbayDraftSpec,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        exact_candidates = [
            candidate
            for candidate in candidates
            if EbayClient._catalog_match(draft, candidate)["exact"]
        ]
        exact_with_images = [
            candidate
            for candidate in exact_candidates
            if EbayClient._catalog_image_urls(candidate)
        ]
        if exact_with_images:
            return max(
                exact_with_images,
                key=lambda candidate: int(EbayClient._catalog_match(draft, candidate)["score"]),
            )

        if exact_candidates:
            image_donors = [
                candidate
                for candidate in candidates
                if EbayClient._catalog_image_urls(candidate)
                and not EbayClient._catalog_match(draft, candidate)["missing_model_tokens"]
                and not EbayClient._catalog_match(draft, candidate)["missing_storage_tokens"]
                and not EbayClient._catalog_match(draft, candidate)["missing_color_tokens"]
            ]
            if image_donors:
                exact_candidate = max(
                    exact_candidates,
                    key=lambda candidate: int(
                        EbayClient._catalog_match(draft, candidate)["score"]
                    ),
                )
                image_donor = max(
                    image_donors,
                    key=lambda candidate: int(
                        EbayClient._catalog_match(draft, candidate)["score"]
                    ),
                )
                selected = dict(exact_candidate)
                selected["image"] = image_donor.get("image")
                selected["additionalImages"] = image_donor.get("additionalImages") or []
                selected["imageSourceEpid"] = image_donor.get("epid")
                return selected

        candidates_with_images = [
            candidate
            for candidate in candidates
            if EbayClient._catalog_image_urls(candidate)
        ]
        if not candidates_with_images:
            return None
        return max(
            candidates_with_images,
            key=lambda candidate: (
                bool(EbayClient._catalog_match(draft, candidate)["exact"]),
                int(EbayClient._catalog_match(draft, candidate)["score"]),
            ),
        )

    @staticmethod
    def _catalog_image_urls(candidate: dict[str, Any]) -> list[str]:
        urls: list[str] = []
        primary = candidate.get("image")
        if isinstance(primary, dict) and primary.get("imageUrl"):
            urls.append(str(primary["imageUrl"]))
        additional = candidate.get("additionalImages")
        if isinstance(additional, list):
            for image in additional:
                if isinstance(image, dict) and image.get("imageUrl"):
                    urls.append(str(image["imageUrl"]))
        return [
            url
            for url in EbayClient._dedupe_urls(urls)
            if url.lower().startswith("https://")
        ][:12]

    @staticmethod
    def _catalog_candidate_summary(
        candidate: dict[str, Any],
        draft: EbayDraftSpec,
    ) -> dict[str, Any]:
        return {
            "epid": candidate.get("epid"),
            "title": candidate.get("title"),
            "image_urls": EbayClient._catalog_image_urls(candidate),
            "image_source_epid": candidate.get("imageSourceEpid"),
            "match": EbayClient._catalog_match(draft, candidate),
        }

    @staticmethod
    def _inventory_item_payload(
        draft: EbayDraftSpec,
        catalog_product: dict[str, Any],
        image_urls: list[str],
    ) -> dict[str, Any]:
        product: dict[str, Any] = {
            "title": draft.title,
            "description": draft.description,
            "aspects": draft.aspects,
            "brand": draft.brand,
            "imageUrls": image_urls,
        }
        epid = str(catalog_product.get("epid") or "").strip()
        if epid:
            product["epid"] = epid
        payload: dict[str, Any] = {
            "availability": {
                "shipToLocationAvailability": {
                    "quantity": draft.quantity,
                }
            },
            "condition": draft.condition,
            "product": product,
        }
        if draft.condition_description:
            payload["conditionDescription"] = draft.condition_description
        return payload

    def _offer_payload(self, draft: EbayDraftSpec) -> dict[str, Any]:
        return {
            "sku": draft.sku,
            "marketplaceId": self.settings.ebay_marketplace_id,
            "format": "FIXED_PRICE",
            "availableQuantity": draft.quantity,
            "categoryId": draft.category_id,
            "listingDescription": draft.description,
            "listingDuration": "GTC",
            "pricingSummary": {
                "price": {
                    "value": f"{draft.price:.2f}",
                    "currency": "USD",
                }
            },
        }

    @staticmethod
    def _response_error(response: httpx.Response, status: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = {"message": response.text[:500]}
        return {
            "status": status,
            "http_status": response.status_code,
            "error": payload,
            "message": f"eBay returned HTTP {response.status_code}.",
        }

    @staticmethod
    def _safe_http_error(exc: httpx.HTTPError) -> str:
        response = getattr(exc, "response", None)
        if isinstance(response, httpx.Response):
            return f"eBay returned HTTP {response.status_code}."
        return f"eBay request failed: {exc.__class__.__name__}."

    async def _get(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return await self._request_with_retry(client, "GET", path, params=params, headers=headers)

    async def _post(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        data: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return await self._request_with_retry(client, "POST", path, data=data, headers=headers)

    async def _post_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        json_data: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return await self._request_with_retry(
            client,
            "POST",
            path,
            json_data=json_data,
            headers=headers,
        )

    async def _put_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        json_data: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return await self._request_with_retry(
            client,
            "PUT",
            path,
            json_data=json_data,
            headers=headers,
        )

    async def _post_content(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        content: bytes | str,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return await self._request_with_retry(
            client,
            "POST",
            path,
            content=content,
            headers=headers,
        )

    async def _request_with_retry(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        data: dict[str, object] | None = None,
        json_data: dict[str, object] | None = None,
        content: bytes | str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        last_exc: httpx.HTTPError | None = None
        for attempt, delay in enumerate((*RETRY_DELAYS_SECONDS, 0.0), start=1):
            try:
                if method == "POST":
                    if content is not None:
                        response = await client.post(path, content=content, headers=headers)
                    elif json_data is not None:
                        response = await client.post(path, json=json_data, headers=headers)
                    else:
                        response = await client.post(path, data=data, headers=headers)
                elif method == "PUT":
                    response = await client.put(path, json=json_data, headers=headers)
                else:
                    response = await client.get(path, params=params, headers=headers)
            except httpx.HTTPError as exc:
                last_exc = exc
                if delay:
                    logger.warning(
                        "Temporary eBay %s %s failure on attempt %s; retrying in %.1fs: %s",
                        method,
                        path,
                        attempt,
                        delay,
                        exc.__class__.__name__,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

            if response.status_code in RETRY_STATUS_CODES and delay:
                logger.warning(
                    "Temporary eBay %s %s HTTP %s on attempt %s; retrying in %.1fs.",
                    method,
                    path,
                    response.status_code,
                    attempt,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            if response.status_code == 401 and await self._recover_access_token(client, path):
                headers = self._with_current_bearer_token(headers)
                logger.warning(
                    "eBay %s %s returned 401 Unauthorized; minted a fresh OAuth token and retrying once.",
                    method,
                    path,
                )
                if method == "POST":
                    if content is not None:
                        response = await client.post(path, content=content, headers=headers)
                    elif json_data is not None:
                        response = await client.post(path, json=json_data, headers=headers)
                    else:
                        response = await client.post(path, data=data, headers=headers)
                elif method == "PUT":
                    response = await client.put(path, json=json_data, headers=headers)
                else:
                    response = await client.get(path, params=params, headers=headers)
            return response

        if last_exc:
            raise last_exc
        raise RuntimeError(f"eBay {method} {path} failed before returning a response.")

    async def _recover_access_token(self, client: httpx.AsyncClient, path: str) -> bool:
        if path == "/identity/v1/oauth2/token":
            return False
        try:
            if path.startswith("/buy/browse/") and self._has_client_credentials():
                token = await self._client_credentials_access_token(client)
            elif path.startswith("/commerce/catalog/") and self._has_client_credentials():
                token = await self._client_credentials_access_token(client)
            elif self._has_refresh_credentials():
                token = await self._refresh_access_token(client)
            elif self._configured_access_token:
                return False
            elif self._has_client_credentials():
                token = await self._client_credentials_access_token(client)
            else:
                return False
        except httpx.HTTPError as exc:
            logger.warning("Could not recover eBay OAuth token after 401: %s", exc)
            return False
        if not token:
            return False
        self._access_token = token
        return True

    def _with_current_bearer_token(self, headers: dict[str, str] | None) -> dict[str, str] | None:
        if headers is None:
            return None
        updated = dict(headers)
        if "X-EBAY-API-IAF-TOKEN" in updated:
            updated["X-EBAY-API-IAF-TOKEN"] = str(self._access_token or "")
        else:
            updated["Authorization"] = f"Bearer {self._access_token}"
        return updated

    @staticmethod
    def _clean_access_token(value: object) -> str | None:
        token = str(value or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        return token or None
