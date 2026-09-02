from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.ebay import EbayClient
from app.inventory import InventoryRepository
from app.models import InventoryItem
from app.walmart import (
    WalmartMarketplaceClient,
    build_inventory_feed,
    build_item_image_maintenance_feed,
)


WALMART_FEED_GRACE_SECONDS = 10 * 60


@dataclass(frozen=True)
class InventorySyncPlan:
    target_quantity: int
    source: str
    update_ebay: bool
    update_walmart: bool
    pending_walmart_quantity: int | None = None
    pending_walmart_at: str | None = None


def choose_inventory_sync_plan(
    ebay_quantity: int,
    walmart_quantity: int,
    state: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> InventorySyncPlan:
    ebay_quantity = max(0, int(ebay_quantity))
    walmart_quantity = max(0, int(walmart_quantity))
    current_time = now or datetime.now(timezone.utc)

    if state is None:
        return InventorySyncPlan(
            target_quantity=ebay_quantity,
            source="ebay_initial",
            update_ebay=False,
            update_walmart=walmart_quantity != ebay_quantity,
        )

    synced_quantity = max(0, int(state.get("synced_quantity") or 0))
    pending_quantity = state.get("pending_walmart_quantity")
    pending_at = _parse_datetime(state.get("pending_walmart_at"))
    if pending_quantity is not None:
        pending_quantity = max(0, int(pending_quantity))
        if walmart_quantity == pending_quantity:
            pending_quantity = None
            pending_at = None
        elif ebay_quantity == synced_quantity and pending_at is not None:
            age_seconds = (current_time - pending_at).total_seconds()
            if age_seconds < WALMART_FEED_GRACE_SECONDS:
                return InventorySyncPlan(
                    target_quantity=synced_quantity,
                    source="walmart_feed_pending",
                    update_ebay=False,
                    update_walmart=False,
                    pending_walmart_quantity=pending_quantity,
                    pending_walmart_at=pending_at.isoformat(),
                )
            return InventorySyncPlan(
                target_quantity=synced_quantity,
                source="walmart_feed_retry",
                update_ebay=False,
                update_walmart=True,
            )

    if ebay_quantity == walmart_quantity:
        return InventorySyncPlan(
            target_quantity=ebay_quantity,
            source="already_equal",
            update_ebay=False,
            update_walmart=False,
        )

    ebay_changed = ebay_quantity != synced_quantity
    walmart_changed = walmart_quantity != synced_quantity
    if ebay_changed and not walmart_changed:
        target = ebay_quantity
        source = "ebay"
    elif walmart_changed and not ebay_changed:
        target = walmart_quantity
        source = "walmart"
    else:
        target = min(ebay_quantity, walmart_quantity)
        source = "safety_minimum"

    return InventorySyncPlan(
        target_quantity=target,
        source=source,
        update_ebay=ebay_quantity != target,
        update_walmart=walmart_quantity != target,
    )


def choose_ended_listing_sync_plan(
    walmart_quantity: int,
    state: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> InventorySyncPlan:
    walmart_quantity = max(0, int(walmart_quantity))
    current_time = now or datetime.now(timezone.utc)
    pending_quantity = state.get("pending_walmart_quantity") if state else None
    pending_at = _parse_datetime(state.get("pending_walmart_at")) if state else None
    if pending_quantity is not None and max(0, int(pending_quantity)) == 0:
        if walmart_quantity == 0:
            pending_quantity = None
            pending_at = None
        elif pending_at is not None:
            age_seconds = (current_time - pending_at).total_seconds()
            if age_seconds < WALMART_FEED_GRACE_SECONDS:
                return InventorySyncPlan(
                    target_quantity=0,
                    source="ebay_listing_ended_pending",
                    update_ebay=False,
                    update_walmart=False,
                    pending_walmart_quantity=0,
                    pending_walmart_at=pending_at.isoformat(),
                )
    return InventorySyncPlan(
        target_quantity=0,
        source="ebay_listing_ended",
        update_ebay=False,
        update_walmart=walmart_quantity != 0,
    )


class MarketplaceInventorySyncer:
    def __init__(
        self,
        repository: InventoryRepository,
        ebay_client: EbayClient,
        walmart_client: WalmartMarketplaceClient,
    ):
        self.repository = repository
        self.ebay_client = ebay_client
        self.walmart_client = walmart_client

    async def sync_once(self) -> dict[str, Any]:
        attempted_at = datetime.now(timezone.utc)
        now_iso = attempted_at.isoformat()
        ebay_rows = await self.ebay_client.fetch_active_inventory_quantities(limit=200)
        ebay_by_sku = {
            str(row["sku"]): {
                "sku": str(row["sku"]),
                "item_id": str(row.get("item_id") or ""),
                "quantity": max(0, int(row.get("quantity") or 0)),
                "image_urls": _normalized_image_urls(row.get("image_urls")),
                **(
                    {"inventory_tracking": row["inventory_tracking"]}
                    if row.get("inventory_tracking")
                    else {}
                ),
                **(
                    {"variation_specifics": row["variation_specifics"]}
                    if isinstance(row.get("variation_specifics"), dict)
                    else {}
                ),
                **(
                    {
                        "start_price": row["start_price"],
                        "currency": row.get("currency") or "USD",
                    }
                    if row.get("start_price") is not None
                    else {}
                ),
            }
            for row in ebay_rows
            if str(row.get("sku") or "").strip()
        }
        for row in ebay_by_sku.values():
            self.repository.update_inventory_quantity(row["sku"], row["quantity"])

        walmart_items = await self.walmart_client.list_published_items(limit=1000)
        walmart_by_item_sku = {
            str(row.get("sku") or "").strip(): row
            for row in walmart_items
            if str(row.get("sku") or "").strip()
        }
        walmart_skus = set(walmart_by_item_sku)
        draft_by_sku = {
            str(row.get("sku") or "").strip(): row
            for row in self.repository.walmart_drafts(skus=walmart_skus, limit=200)
            if str(row.get("sku") or "").strip()
        }
        common_skus = sorted(set(ebay_by_sku) & walmart_skus)
        ended_skus = sorted((walmart_skus & set(draft_by_sku)) - set(ebay_by_sku))
        sync_skus = sorted(set(common_skus) | set(ended_skus))
        if not sync_skus:
            return {
                "status": "no_published_matches",
                "checked": 0,
                "ebay_active_skus": len(ebay_by_sku),
                "ebay_image_skus": sum(
                    1 for row in ebay_by_sku.values() if row["image_urls"]
                ),
                "walmart_published_skus": len(walmart_skus),
                "ended_ebay_skus": 0,
                "updated_ebay": 0,
                "updated_walmart": 0,
                "zeroed_walmart": 0,
                "image_updates_submitted": 0,
                "image_updates_confirmed": 0,
                "last_attempt_at": now_iso,
            }

        semaphore = asyncio.Semaphore(6)

        async def walmart_quantity(sku: str) -> tuple[str, int | Exception]:
            try:
                async with semaphore:
                    return sku, await self.walmart_client.get_inventory_quantity(sku)
            except Exception as exc:
                return sku, exc

        walmart_results = await asyncio.gather(
            *(walmart_quantity(sku) for sku in sync_skus)
        )
        walmart_by_sku: dict[str, int] = {}
        quantity_errors: dict[str, str] = {}
        for sku, result in walmart_results:
            if isinstance(result, Exception):
                quantity_errors[sku] = f"Walmart inventory lookup failed: {result}"
            else:
                walmart_by_sku[sku] = result

        states = {
            sku: self.repository.marketplace_inventory_sync_state(sku)
            for sku in sync_skus
        }
        plans: dict[str, InventorySyncPlan] = {}
        for sku, walmart_quantity_value in walmart_by_sku.items():
            if sku in ebay_by_sku:
                plans[sku] = choose_inventory_sync_plan(
                    ebay_by_sku[sku]["quantity"],
                    walmart_quantity_value,
                    states[sku],
                    now=attempted_at,
                )
            else:
                plans[sku] = choose_ended_listing_sync_plan(
                    walmart_quantity_value,
                    states[sku],
                    now=attempted_at,
                )
                self.repository.update_inventory_quantity(sku, 0)

        ebay_updates = [
            {
                "sku": sku,
                "item_id": ebay_by_sku[sku]["item_id"],
                "quantity": plan.target_quantity,
                **(
                    {"inventory_tracking": ebay_by_sku[sku]["inventory_tracking"]}
                    if ebay_by_sku[sku].get("inventory_tracking")
                    else {}
                ),
                **(
                    {"variation_specifics": ebay_by_sku[sku]["variation_specifics"]}
                    if isinstance(ebay_by_sku[sku].get("variation_specifics"), dict)
                    else {}
                ),
                **(
                    {
                        "start_price": ebay_by_sku[sku]["start_price"],
                        "currency": ebay_by_sku[sku].get("currency") or "USD",
                    }
                    if ebay_by_sku[sku].get("start_price") is not None
                    else {}
                ),
            }
            for sku, plan in plans.items()
            if plan.update_ebay and sku in ebay_by_sku
        ]
        ebay_update_results = await self.ebay_client.revise_inventory_quantities(ebay_updates)
        ebay_result_by_sku = {
            str(result["sku"]): result for result in ebay_update_results
        }
        failed_ebay_skus = {
            sku
            for sku, result in ebay_result_by_sku.items()
            if result.get("status") != "updated"
        }
        for sku in failed_ebay_skus:
            quantity_errors[sku] = "eBay rejected the quantity update."

        walmart_update_skus = [
            sku
            for sku, plan in plans.items()
            if plan.update_walmart and sku not in failed_ebay_skus
        ]
        walmart_submission: dict[str, Any] | None = None
        if walmart_update_skus:
            walmart_payload = build_inventory_feed(
                [
                    InventoryItem(
                        sku=sku,
                        title=sku,
                        quantity=plans[sku].target_quantity,
                        listing_status="ENDED" if sku in ended_skus else "ACTIVE",
                        source="marketplace-inventory-sync",
                    )
                    for sku in walmart_update_skus
                ]
            )
            try:
                walmart_submission = await self.walmart_client.submit_inventory_feed(
                    walmart_payload
                )
            except Exception as exc:
                for sku in walmart_update_skus:
                    quantity_errors[sku] = f"Walmart inventory feed failed: {exc}"

        image_states = {
            sku: {
                "ebay_image_signature": (states[sku] or {}).get("ebay_image_signature"),
                "synced_image_signature": (states[sku] or {}).get("synced_image_signature"),
                "pending_walmart_image_signature": (states[sku] or {}).get(
                    "pending_walmart_image_signature"
                ),
                "pending_walmart_image_at": (states[sku] or {}).get(
                    "pending_walmart_image_at"
                ),
                "last_image_feed_id": (states[sku] or {}).get("last_image_feed_id"),
            }
            for sku in plans
        }
        image_errors: dict[str, str] = {}
        image_updates_confirmed = 0
        pending_feed_ids = {
            str(image_states[sku]["last_image_feed_id"])
            for sku in common_skus
            if sku in image_states
            and image_states[sku]["pending_walmart_image_signature"]
            and image_states[sku]["last_image_feed_id"]
        }

        async def walmart_feed_status(
            feed_id: str,
        ) -> tuple[str, dict[str, Any] | Exception]:
            try:
                async with semaphore:
                    return feed_id, await self.walmart_client.get_feed_status(feed_id)
            except Exception as exc:
                return feed_id, exc

        feed_statuses = dict(
            await asyncio.gather(
                *(walmart_feed_status(feed_id) for feed_id in pending_feed_ids)
            )
        )
        image_candidates: dict[str, list[str]] = {}
        for sku in common_skus:
            if sku not in image_states or sku not in plans:
                continue
            images = ebay_by_sku[sku]["image_urls"]
            if not images:
                continue
            current_signature = _image_signature(images)
            fields = image_states[sku]
            fields["ebay_image_signature"] = current_signature
            synced_signature = str(fields["synced_image_signature"] or "")
            if not synced_signature:
                synced_signature = _draft_image_signature(draft_by_sku.get(sku))
                if not synced_signature:
                    synced_signature = current_signature
                fields["synced_image_signature"] = synced_signature

            pending_signature = str(fields["pending_walmart_image_signature"] or "")
            pending_at = _parse_datetime(fields["pending_walmart_image_at"])
            pending_feed_id = str(fields["last_image_feed_id"] or "")
            blocked_by_pending_feed = False
            if pending_signature:
                if pending_signature != current_signature:
                    fields["pending_walmart_image_signature"] = None
                    fields["pending_walmart_image_at"] = None
                elif pending_feed_id and pending_feed_id in feed_statuses:
                    feed_status = feed_statuses[pending_feed_id]
                    if isinstance(feed_status, Exception):
                        age = (
                            (attempted_at - pending_at).total_seconds()
                            if pending_at is not None
                            else WALMART_FEED_GRACE_SECONDS
                        )
                        blocked_by_pending_feed = age < WALMART_FEED_GRACE_SECONDS
                        if not blocked_by_pending_feed:
                            fields["pending_walmart_image_signature"] = None
                            fields["pending_walmart_image_at"] = None
                    else:
                        outcome = _walmart_feed_outcome(feed_status)
                        if outcome == "succeeded":
                            fields["synced_image_signature"] = pending_signature
                            fields["pending_walmart_image_signature"] = None
                            fields["pending_walmart_image_at"] = None
                            synced_signature = pending_signature
                            image_updates_confirmed += 1
                        elif outcome == "failed":
                            fields["pending_walmart_image_signature"] = None
                            fields["pending_walmart_image_at"] = None
                            image_errors[sku] = (
                                f"Walmart rejected image maintenance feed {pending_feed_id}."
                            )
                        else:
                            blocked_by_pending_feed = True
                elif pending_at is not None:
                    blocked_by_pending_feed = (
                        attempted_at - pending_at
                    ).total_seconds() < WALMART_FEED_GRACE_SECONDS
                    if not blocked_by_pending_feed:
                        fields["pending_walmart_image_signature"] = None
                        fields["pending_walmart_image_at"] = None

            if (
                current_signature != synced_signature
                and not blocked_by_pending_feed
                and sku not in image_errors
                and not fields["pending_walmart_image_signature"]
            ):
                image_candidates[sku] = images

        detail_skus = [
            sku
            for sku in image_candidates
            if not walmart_by_item_sku[sku].get("product_type")
            or not walmart_by_item_sku[sku].get("product_id")
        ]

        async def walmart_item_details(
            sku: str,
        ) -> tuple[str, dict[str, Any] | Exception]:
            try:
                async with semaphore:
                    return sku, await self.walmart_client.get_item_details(sku)
            except Exception as exc:
                return sku, exc

        detail_results = dict(
            await asyncio.gather(
                *(walmart_item_details(sku) for sku in detail_skus)
            )
        )
        maintenance_items: list[dict[str, Any]] = []
        maintenance_skus: list[str] = []
        for sku, images in image_candidates.items():
            metadata = dict(walmart_by_item_sku[sku])
            details = detail_results.get(sku)
            if isinstance(details, Exception):
                image_errors[sku] = f"Walmart item detail lookup failed: {details}"
                continue
            if isinstance(details, dict):
                metadata.update(details)
            if not metadata.get("product_type") or not metadata.get("product_id"):
                image_errors[sku] = (
                    "Walmart item details are missing the product type or product identifier "
                    "required for image maintenance."
                )
                continue
            maintenance_items.append(
                {
                    "sku": sku,
                    "product_type": metadata["product_type"],
                    "product_id_type": metadata.get("product_id_type") or "GTIN",
                    "product_id": metadata["product_id"],
                    "image_urls": images,
                }
            )
            maintenance_skus.append(sku)

        image_submission: dict[str, Any] | None = None
        if maintenance_items:
            version = str(
                getattr(
                    self.walmart_client.settings,
                    "walmart_maintenance_spec_version",
                    "5.0.20260608-18_15_07-api",
                )
            )
            try:
                image_payload = build_item_image_maintenance_feed(
                    maintenance_items,
                    version=version,
                )
                image_submission = await self.walmart_client.submit_item_maintenance_feed(
                    image_payload
                )
            except Exception as exc:
                for sku in maintenance_skus:
                    image_errors[sku] = f"Walmart image maintenance feed failed: {exc}"
            else:
                feed_id = str(image_submission["feed_id"])
                for sku in maintenance_skus:
                    fields = image_states[sku]
                    fields["pending_walmart_image_signature"] = _image_signature(
                        image_candidates[sku]
                    )
                    fields["pending_walmart_image_at"] = now_iso
                    fields["last_image_feed_id"] = feed_id

        for sku, plan in plans.items():
            previous = states[sku]
            previous_synced = (
                max(0, int(previous.get("synced_quantity") or 0)) if previous else None
            )
            quantity_failed = sku in quantity_errors
            synced_quantity = (
                previous_synced
                if quantity_failed and previous_synced is not None
                else plan.target_quantity
            )
            pending_quantity = plan.pending_walmart_quantity
            pending_at = plan.pending_walmart_at
            if plan.update_walmart and walmart_submission is not None and not quantity_failed:
                pending_quantity = plan.target_quantity
                pending_at = now_iso
            elif plan.update_walmart and walmart_submission is None:
                pending_quantity = None
                pending_at = None
            elif walmart_by_sku[sku] == plan.target_quantity:
                pending_quantity = None
                pending_at = None

            source_row = ebay_by_sku.get(sku)
            ebay_quantity = int(source_row["quantity"]) if source_row else 0
            effective_ebay_quantity = (
                plan.target_quantity
                if plan.update_ebay and sku not in failed_ebay_skus
                else ebay_quantity
            )
            if plan.update_ebay and sku not in failed_ebay_skus:
                self.repository.update_inventory_quantity(sku, plan.target_quantity)

            image_fields = image_states[sku]
            sku_errors = [
                message
                for message in (quantity_errors.get(sku), image_errors.get(sku))
                if message
            ]
            is_pending = (
                pending_quantity is not None
                or image_fields["pending_walmart_image_signature"] is not None
            )
            self.repository.upsert_marketplace_inventory_sync_state(
                sku=sku,
                ebay_item_id=(
                    (str(source_row["item_id"] or "") or None)
                    if source_row
                    else (previous or {}).get("ebay_item_id")
                    or draft_by_sku.get(sku, {}).get("ebay_item_id")
                ),
                ebay_quantity=effective_ebay_quantity,
                walmart_quantity=walmart_by_sku[sku],
                synced_quantity=synced_quantity,
                pending_walmart_quantity=pending_quantity,
                pending_walmart_at=pending_at,
                ebay_image_signature=image_fields["ebay_image_signature"],
                synced_image_signature=image_fields["synced_image_signature"],
                pending_walmart_image_signature=image_fields[
                    "pending_walmart_image_signature"
                ],
                pending_walmart_image_at=image_fields["pending_walmart_image_at"],
                last_image_feed_id=image_fields["last_image_feed_id"],
                last_source=plan.source,
                status="error" if sku_errors else ("pending" if is_pending else "synced"),
                error_message=" ".join(sku_errors) or None,
            )

        all_errors = {**quantity_errors, **image_errors}
        return {
            "status": "partial_error" if all_errors else "ok",
            "checked": len(plans),
            "ebay_active_skus": len(ebay_by_sku),
            "ebay_image_skus": sum(
                1 for row in ebay_by_sku.values() if row["image_urls"]
            ),
            "walmart_published_skus": len(walmart_skus),
            "ended_ebay_skus": len(ended_skus),
            "updated_ebay": len(ebay_updates) - len(failed_ebay_skus),
            "updated_walmart": len(walmart_update_skus) if walmart_submission else 0,
            "zeroed_walmart": (
                len(set(walmart_update_skus) & set(ended_skus))
                if walmart_submission
                else 0
            ),
            "image_updates_submitted": len(maintenance_skus) if image_submission else 0,
            "image_updates_confirmed": image_updates_confirmed,
            "walmart_submission": walmart_submission,
            "image_submission": image_submission,
            "errors": all_errors,
            "last_attempt_at": now_iso,
        }


def _normalized_image_urls(value: object) -> list[str]:
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


def _image_signature(urls: list[str]) -> str:
    encoded = json.dumps(urls, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _draft_image_signature(draft: dict[str, Any] | None) -> str:
    if not draft:
        return ""
    prepared = draft.get("prepared_listing")
    if not isinstance(prepared, dict):
        return ""
    images = _normalized_image_urls(prepared.get("images"))
    return _image_signature(images) if images else ""


def _walmart_feed_outcome(payload: dict[str, Any]) -> str:
    nested_feed = payload.get("feed")
    nested_status = (
        nested_feed.get("feedStatus") if isinstance(nested_feed, dict) else None
    )
    feed_status = str(
        payload.get("feedStatus") or payload.get("status") or nested_status or ""
    ).upper()
    if feed_status in {"ERROR", "FAILED", "FAILURE"}:
        return "failed"
    failed_count = _recursive_failed_count(payload)
    if feed_status in {"PROCESSED", "COMPLETED", "DONE"}:
        has_failure = failed_count > 0 or _contains_failure_status(payload)
        return "failed" if has_failure else "succeeded"
    return "pending"


def _recursive_failed_count(value: object) -> int:
    if isinstance(value, dict):
        total = 0
        for key, nested in value.items():
            normalized_key = "".join(
                character for character in key.lower() if character.isalnum()
            )
            if normalized_key in {
                "itemsfailed",
                "faileditems",
                "itemserror",
                "errorcount",
                "failurecount",
            }:
                try:
                    total += max(0, int(nested))
                except (TypeError, ValueError):
                    pass
            else:
                total += _recursive_failed_count(nested)
        return total
    if isinstance(value, list):
        return sum(_recursive_failed_count(item) for item in value)
    return 0


def _contains_failure_status(value: object) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized_key = "".join(
                character for character in key.lower() if character.isalnum()
            )
            if normalized_key in {"status", "ingestionstatus", "itemstatus"} and str(
                nested or ""
            ).upper() in {
                "DATA_ERROR",
                "ERROR",
                "FAILED",
                "FAILURE",
                "SYSTEM_ERROR",
                "TIMEOUT_ERROR",
            }:
                return True
            if _contains_failure_status(nested):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_failure_status(item) for item in value)
    return False


def _parse_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
