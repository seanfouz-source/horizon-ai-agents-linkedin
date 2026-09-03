from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.ebay import EbayClient
from app.inventory import InventoryRepository
from app.models import InventoryItem
from app.walmart import (
    WalmartMarketplaceClient,
    build_inventory_feed,
    build_item_image_maintenance_feed,
    walmart_price,
)


WALMART_FEED_GRACE_SECONDS = 10 * 60
EBAY_FULL_IMAGE_SCAN_SECONDS = 60 * 60
QUANTITY_POLICY_VERSION = 3
EBAY_QUOTA_COOLDOWN_SECONDS = 15 * 60


_ebay_trading_retry_at: datetime | None = None
_ebay_browse_retry_at: datetime | None = None

logger = logging.getLogger(__name__)


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

    try:
        policy_version = int(state.get("quantity_policy_version") or 1)
    except (TypeError, ValueError):
        policy_version = 1
    if policy_version < QUANTITY_POLICY_VERSION:
        # Establish today's eBay quantities as the safe cutover baseline. This
        # prevents an old Walmart discrepancy from being misread as a new sale.
        return InventorySyncPlan(
            target_quantity=ebay_quantity,
            source="ebay_policy_baseline",
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
        elif walmart_quantity < min(pending_quantity, synced_quantity):
            # A Walmart sale can land while our own Walmart quantity feed is still
            # processing.  Do not hide that real inventory decrease behind the
            # normal feed grace period.
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
    walmart_decreased = walmart_quantity < synced_quantity
    if walmart_decreased and not ebay_changed:
        target = walmart_quantity
        source = "walmart_sale"
    elif walmart_decreased:
        target = min(ebay_quantity, walmart_quantity)
        source = "safety_minimum"
    else:
        # eBay owns the inventory ceiling. A Walmart increase is treated as a
        # manual/stale edit and is overwritten instead of inflating eBay stock.
        target = ebay_quantity
        source = "ebay" if ebay_changed else "ebay_authoritative"

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
        global _ebay_browse_retry_at, _ebay_trading_retry_at

        attempted_at = datetime.now(timezone.utc)
        now_iso = attempted_at.isoformat()
        ebay_quantity_source = "ebay_trading_api"
        ebay_quantity_warning: str | None = None
        ebay_rows: list[dict[str, Any]] = []
        trading_error: str | None = None
        if _retry_is_due(_ebay_trading_retry_at, attempted_at):
            try:
                ebay_rows = await self.ebay_client.fetch_active_inventory_quantities(
                    limit=200
                )
            except Exception as trading_exc:
                trading_error = str(trading_exc)
                if _is_ebay_quota_error(trading_exc):
                    _ebay_trading_retry_at = attempted_at + timedelta(
                        seconds=EBAY_QUOTA_COOLDOWN_SECONDS
                    )
            else:
                _ebay_trading_retry_at = None
        else:
            trading_error = (
                "Trading API quota cooldown is active until "
                f"{_ebay_trading_retry_at.isoformat()}."
            )

        if not ebay_rows:
            fallback = getattr(
                self.ebay_client,
                "fetch_active_browse_inventory_quantities",
                None,
            )
            if fallback is None:
                raise RuntimeError(trading_error or "eBay Trading API returned no rows.")
            browse_error: str | None = None
            browse_exc: Exception | None = None
            if _retry_is_due(_ebay_browse_retry_at, attempted_at):
                try:
                    ebay_rows = await fallback(limit=200)
                except Exception as exc:
                    browse_exc = exc
                    browse_error = str(exc)
                    if _is_ebay_quota_error(exc):
                        _ebay_browse_retry_at = attempted_at + timedelta(
                            seconds=EBAY_QUOTA_COOLDOWN_SECONDS
                        )
                else:
                    _ebay_browse_retry_at = None
            else:
                browse_error = (
                    "Browse API quota cooldown is active until "
                    f"{_ebay_browse_retry_at.isoformat()}."
                )

            if ebay_rows:
                ebay_quantity_source = "ebay_browse_api_fallback"
                ebay_quantity_warning = trading_error
            else:
                ebay_rows = self._cached_active_ebay_quantity_rows()
                if ebay_rows:
                    ebay_quantity_source = "ebay_repository_cache_fallback"
                    ebay_quantity_warning = "; ".join(
                        error for error in (trading_error, browse_error) if error
                    )
                else:
                    detail = "; ".join(
                        error for error in (trading_error, browse_error) if error
                    )
                    if browse_exc is not None:
                        raise RuntimeError(
                            "eBay quantity reads failed and no safe active-listing cache "
                            f"was available: {detail}"
                        ) from browse_exc
                    raise RuntimeError(
                        "eBay quantity reads failed and no safe active-listing cache "
                        f"was available: {detail}"
                    )
            if not ebay_rows:
                raise RuntimeError(
                    "eBay Trading API quantity read failed and the Browse API returned "
                    "no active listings; no marketplace quantities were changed."
                )
        ebay_by_sku = {
            str(row["sku"]): {
                "sku": str(row["sku"]),
                "item_id": str(row.get("item_id") or ""),
                "quantity": max(0, int(row.get("quantity") or 0)),
                "image_urls": _normalized_image_urls(row.get("image_urls")),
                "image_complete": bool(row.get("image_complete")),
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
        quantity_sync_paused = ebay_quantity_source != "ebay_trading_api"
        # Browse and public store-page snapshots prove that a listing exists,
        # but they do not reliably expose its exact available quantity. Missing
        # Browse availability is represented conservatively as one, and a
        # partial search result cannot prove that an omitted listing ended.
        # Never let estimated or cached rows overwrite either marketplace.
        if quantity_sync_paused:
            logger.warning(
                "Marketplace quantity writes paused because the eBay quantity "
                "source is not authoritative: source=%s active_rows=%s warning=%s",
                ebay_quantity_source,
                len(ebay_rows),
                ebay_quantity_warning or "none",
            )
        if not quantity_sync_paused:
            for row in ebay_by_sku.values():
                self.repository.update_inventory_quantity(row["sku"], row["quantity"])
        ebay_source_by_sku = ebay_by_sku

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
        walmart_skus_by_item_id: dict[str, set[str]] = {}
        inferred_item_ids = {
            walmart_sku: _ebay_item_id_from_walmart_sku(walmart_sku)
            for walmart_sku in walmart_skus
        }
        for walmart_sku in walmart_skus:
            draft = draft_by_sku.get(walmart_sku) or {}
            item_id = str(
                draft.get("ebay_item_id")
                or (draft.get("source_snapshot") or {}).get("ebay_item_id")
                or inferred_item_ids.get(walmart_sku)
                or ""
            ).strip()
            if item_id:
                walmart_skus_by_item_id.setdefault(item_id, set()).add(walmart_sku)
        ebay_rows_by_item_id: dict[str, list[dict[str, Any]]] = {}
        for row in ebay_source_by_sku.values():
            item_id = str(row.get("item_id") or "").strip()
            if item_id:
                ebay_rows_by_item_id.setdefault(item_id, []).append(row)

        ebay_by_sku = {}
        unresolved_alias_skus: set[str] = set()
        for walmart_sku in walmart_skus:
            source_row = ebay_source_by_sku.get(walmart_sku)
            draft = draft_by_sku.get(walmart_sku) or {}
            source_snapshot = draft.get("source_snapshot") or {}
            snapshot_sku = str(source_snapshot.get("sku") or "").strip()
            if source_row is None and snapshot_sku:
                source_row = ebay_source_by_sku.get(snapshot_sku)
            item_id = str(
                draft.get("ebay_item_id")
                or source_snapshot.get("ebay_item_id")
                or inferred_item_ids.get(walmart_sku)
                or ""
            ).strip()
            if source_row is None and item_id:
                ebay_candidates = ebay_rows_by_item_id.get(item_id) or []
                walmart_candidates = walmart_skus_by_item_id.get(item_id) or set()
                exact_source_sku = (
                    str(ebay_candidates[0].get("sku") or "").strip()
                    if len(ebay_candidates) == 1
                    else ""
                )
                if len(ebay_candidates) == 1 and (
                    len(walmart_candidates) == 1
                    or inferred_item_ids.get(walmart_sku) is not None
                    or exact_source_sku in walmart_candidates
                ):
                    source_row = ebay_candidates[0]
                elif ebay_candidates:
                    unresolved_alias_skus.add(walmart_sku)
            if source_row is not None:
                ebay_by_sku[walmart_sku] = source_row

        walmart_skus_by_source_sku: dict[str, set[str]] = {}
        for walmart_sku, source_row in ebay_by_sku.items():
            source_sku = str(source_row.get("sku") or "").strip()
            if source_sku:
                walmart_skus_by_source_sku.setdefault(source_sku, set()).add(walmart_sku)
        duplicate_alias_skus: dict[str, str] = {}
        for source_sku, mapped_walmart_skus in walmart_skus_by_source_sku.items():
            # Only suppress aliases when the exact eBay SKU is also present as
            # the unambiguous canonical Walmart SKU. Variation listings without
            # such a canonical SKU remain unresolved instead of being guessed.
            if source_sku not in mapped_walmart_skus:
                continue
            for walmart_sku in mapped_walmart_skus - {source_sku}:
                duplicate_alias_skus[walmart_sku] = source_sku

        source_skus = {
            str(row.get("sku") or "").strip()
            for row in ebay_by_sku.values()
            if str(row.get("sku") or "").strip()
        }
        ebay_items_by_source_sku = {
            item.sku: item
            for item in self.repository.ebay_items(
                skus=source_skus,
                limit=200,
                include_inactive=True,
            )
        }
        excluded_skus = {
            walmart_sku: term
            for walmart_sku, source_row in ebay_by_sku.items()
            if (
                source_item := ebay_items_by_source_sku.get(
                    str(source_row.get("sku") or "").strip()
                )
            ) is not None
            and (term := self._walmart_exclusion(source_item)) is not None
        }
        common_skus = sorted(ebay_by_sku)
        ended_skus = (
            []
            if quantity_sync_paused
            else sorted(
                (
                    (walmart_skus & set(draft_by_sku))
                    | {
                        sku
                        for sku, item_id in inferred_item_ids.items()
                        if item_id
                    }
                )
                - set(ebay_by_sku)
                - unresolved_alias_skus
            )
        )
        sync_skus = sorted(set(common_skus) | set(ended_skus))
        if not sync_skus:
            return {
                "status": "no_published_matches",
                "checked": 0,
                "ebay_active_skus": len(ebay_source_by_sku),
                "ebay_image_skus": sum(
                    1 for row in ebay_source_by_sku.values() if row["image_urls"]
                ),
                "walmart_published_skus": len(walmart_skus),
                "ebay_quantity_source": ebay_quantity_source,
                "ebay_quantity_warning": ebay_quantity_warning,
                "quantity_sync_paused": quantity_sync_paused,
                "unresolved_alias_skus": len(unresolved_alias_skus),
                "duplicate_alias_skus": len(duplicate_alias_skus),
                "ended_ebay_skus": 0,
                "updated_ebay": 0,
                "updated_walmart": 0,
                "zeroed_walmart": 0,
                "updated_prices": 0,
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
            if quantity_sync_paused:
                plans[sku] = InventorySyncPlan(
                    target_quantity=walmart_quantity_value,
                    source="ebay_quantity_unavailable",
                    update_ebay=False,
                    update_walmart=False,
                )
            elif sku in excluded_skus:
                zero_plan = choose_ended_listing_sync_plan(
                    walmart_quantity_value,
                    states[sku],
                    now=attempted_at,
                )
                plans[sku] = InventorySyncPlan(
                    target_quantity=zero_plan.target_quantity,
                    source=f"excluded:{excluded_skus[sku]}",
                    update_ebay=False,
                    update_walmart=zero_plan.update_walmart,
                    pending_walmart_quantity=zero_plan.pending_walmart_quantity,
                    pending_walmart_at=zero_plan.pending_walmart_at,
                )
            elif sku in duplicate_alias_skus:
                zero_plan = choose_ended_listing_sync_plan(
                    walmart_quantity_value,
                    states[sku],
                    now=attempted_at,
                )
                plans[sku] = InventorySyncPlan(
                    target_quantity=zero_plan.target_quantity,
                    source=f"duplicate_alias:{duplicate_alias_skus[sku]}",
                    update_ebay=False,
                    update_walmart=zero_plan.update_walmart,
                    pending_walmart_quantity=zero_plan.pending_walmart_quantity,
                    pending_walmart_at=zero_plan.pending_walmart_at,
                )
            elif sku in ebay_by_sku:
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

        price_markup_percent = float(
            getattr(self.walmart_client.settings, "walmart_price_markup_percent", 10.0)
        )
        price_targets: dict[str, float] = {}
        price_candidates: dict[str, tuple[float, float, str]] = {}
        price_errors: dict[str, str] = {}
        for sku in common_skus:
            if sku in excluded_skus or sku in duplicate_alias_skus:
                continue
            source_price = ebay_by_sku[sku].get("start_price")
            if source_price is None:
                continue
            currency = str(ebay_by_sku[sku].get("currency") or "USD").strip().upper()
            if currency != "USD":
                price_errors[sku] = (
                    f"eBay price uses {currency or 'an empty currency'}; Walmart US requires USD."
                )
                continue
            target_price = walmart_price(float(source_price), price_markup_percent)
            if target_price is None or target_price <= 0:
                price_errors[sku] = "eBay did not return a positive price for Walmart markup."
                continue
            price_targets[sku] = target_price
            previous = states.get(sku) or {}
            previous_target = previous.get("synced_walmart_price")
            previous_currency = str(previous.get("price_currency") or "USD").upper()
            if (
                previous_target is None
                or abs(float(previous_target) - target_price) >= 0.005
                or previous_currency != currency
            ):
                price_candidates[sku] = (float(source_price), target_price, currency)

        async def update_walmart_price(
            sku: str,
            target_price: float,
            currency: str,
        ) -> tuple[str, dict[str, Any] | Exception]:
            try:
                async with semaphore:
                    return sku, await self.walmart_client.update_price(
                        sku,
                        target_price,
                        currency=currency,
                    )
            except Exception as exc:
                return sku, exc

        price_results = dict(
            await asyncio.gather(
                *(
                    update_walmart_price(sku, values[1], values[2])
                    for sku, values in price_candidates.items()
                )
            )
        )
        updated_price_skus: set[str] = set()
        for sku, result in price_results.items():
            if isinstance(result, Exception):
                price_errors[sku] = f"Walmart price update failed: {result}"
                continue
            updated_price_skus.add(sku)
            source_price, target_price, currency = price_candidates[sku]
            self.repository.update_marketplace_price_state(
                sku,
                ebay_price=source_price,
                synced_walmart_price=target_price,
                price_currency=currency,
            )

        ebay_updates = [
            {
                "sku": ebay_by_sku[sku]["sku"],
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
        ebay_result_by_source_sku = {
            str(result["sku"]): result for result in ebay_update_results
        }
        failed_ebay_skus = {
            sku
            for sku, plan in plans.items()
            if plan.update_ebay
            and (
                ebay_result_by_source_sku.get(str(ebay_by_sku[sku]["sku"]), {}).get("status")
                != "updated"
            )
        }
        for sku in failed_ebay_skus:
            quantity_errors[sku] = "eBay rejected the quantity update."

        walmart_update_skus = [
            sku
            for sku, plan in plans.items()
            if plan.update_walmart and sku not in failed_ebay_skus
        ]
        logger.info(
            "Marketplace quantity reconciliation planned: source=%s paused=%s "
            "checked=%s ebay_updates=%s walmart_updates=%s ended=%s",
            ebay_quantity_source,
            quantity_sync_paused,
            len(sync_skus),
            len(ebay_updates),
            len(walmart_update_skus),
            len(ended_skus),
        )
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
                "ebay_primary_image_url": (states[sku] or {}).get(
                    "ebay_primary_image_url"
                ),
                "last_ebay_image_scan_at": (states[sku] or {}).get(
                    "last_ebay_image_scan_at"
                ),
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

        image_refresh_item_ids: set[str] = set()
        for sku in common_skus:
            if sku in excluded_skus or sku in duplicate_alias_skus:
                continue
            if sku not in image_states or sku not in plans:
                continue
            source_row = ebay_by_sku[sku]
            images = source_row["image_urls"]
            if not images:
                continue
            fields = image_states[sku]
            current_primary = images[0]
            draft_images = _draft_image_urls(draft_by_sku.get(sku))
            previous_primary = str(fields["ebay_primary_image_url"] or "")
            if not previous_primary and draft_images:
                previous_primary = draft_images[0]
            last_scan = _parse_datetime(fields["last_ebay_image_scan_at"])
            scan_is_stale = (
                last_scan is None
                or (attempted_at - last_scan).total_seconds()
                >= EBAY_FULL_IMAGE_SCAN_SECONDS
            )
            main_image_changed = bool(
                previous_primary and current_primary != previous_primary
            )
            fields["ebay_primary_image_url"] = current_primary
            if source_row["image_complete"]:
                fields["last_ebay_image_scan_at"] = now_iso
            elif (
                ebay_quantity_source == "ebay_trading_api"
                and source_row["item_id"]
                and (main_image_changed or scan_is_stale)
            ):
                image_refresh_item_ids.add(source_row["item_id"])

        refreshed_image_rows: list[dict[str, Any]] = []
        if image_refresh_item_ids:
            try:
                refreshed_image_rows = await self.ebay_client.fetch_trading_listing_images(
                    sorted(image_refresh_item_ids)
                )
            except Exception as exc:
                for sku in common_skus:
                    if ebay_by_sku[sku]["item_id"] in image_refresh_item_ids:
                        image_errors[sku] = f"eBay full image refresh failed: {exc}"
        refreshed_by_sku = {
            str(row.get("sku") or "").strip(): row
            for row in refreshed_image_rows
            if str(row.get("sku") or "").strip()
        }
        for sku in common_skus:
            if sku in excluded_skus or sku in duplicate_alias_skus:
                continue
            if sku not in image_states or sku not in plans:
                continue
            item_id = ebay_by_sku[sku]["item_id"]
            if item_id not in image_refresh_item_ids:
                continue
            refreshed = refreshed_by_sku.get(str(ebay_by_sku[sku].get("sku") or ""))
            full_images = _normalized_image_urls(
                refreshed.get("image_urls") if refreshed else None
            )
            if not full_images:
                image_errors.setdefault(
                    sku,
                    "eBay did not return the full image set for this active listing.",
                )
                continue
            ebay_by_sku[sku]["image_urls"] = full_images
            ebay_by_sku[sku]["image_complete"] = True
            image_states[sku]["ebay_primary_image_url"] = full_images[0]
            image_states[sku]["last_ebay_image_scan_at"] = now_iso

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
            if sku in excluded_skus or sku in duplicate_alias_skus:
                continue
            if sku not in image_states or sku not in plans:
                continue
            images = ebay_by_sku[sku]["image_urls"]
            if not images:
                continue
            fields = image_states[sku]
            images_are_complete = bool(ebay_by_sku[sku]["image_complete"])
            if images_are_complete:
                current_signature = _image_signature(images)
                fields["ebay_image_signature"] = current_signature
            else:
                current_signature = str(
                    fields["ebay_image_signature"]
                    or fields["pending_walmart_image_signature"]
                    or fields["synced_image_signature"]
                    or ""
                )
                if not current_signature:
                    continue
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
                and images_are_complete
                and not blocked_by_pending_feed
                and sku not in image_errors
                and not fields["pending_walmart_image_signature"]
            ):
                # Walmart catalog imagery is authoritative. eBay-origin images
                # must never be submitted to Walmart, even when they changed.
                continue

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
                max(0, int(previous.get("ebay_quantity") or 0))
                if quantity_sync_paused and previous is not None
                else (
                    plan.target_quantity
                    if plan.update_ebay and sku not in failed_ebay_skus
                    else ebay_quantity
                )
            )
            if plan.update_ebay and sku not in failed_ebay_skus:
                self.repository.update_inventory_quantity(sku, plan.target_quantity)

            image_fields = image_states[sku]
            ebay_price = (
                float(source_row["start_price"])
                if source_row and source_row.get("start_price") is not None
                else (previous or {}).get("ebay_price")
            )
            synced_walmart_price = (
                price_targets[sku]
                if sku in updated_price_skus
                else (previous or {}).get("synced_walmart_price")
            )
            price_currency = (
                str(source_row.get("currency") or "USD").upper()
                if source_row and source_row.get("start_price") is not None
                else (previous or {}).get("price_currency")
            )
            sku_errors = [
                message
                for message in (
                    quantity_errors.get(sku),
                    price_errors.get(sku),
                    image_errors.get(sku),
                )
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
                ebay_price=ebay_price,
                synced_walmart_price=synced_walmart_price,
                price_currency=price_currency,
                ebay_image_signature=image_fields["ebay_image_signature"],
                ebay_primary_image_url=image_fields["ebay_primary_image_url"],
                last_ebay_image_scan_at=image_fields["last_ebay_image_scan_at"],
                synced_image_signature=image_fields["synced_image_signature"],
                pending_walmart_image_signature=image_fields[
                    "pending_walmart_image_signature"
                ],
                pending_walmart_image_at=image_fields["pending_walmart_image_at"],
                last_image_feed_id=image_fields["last_image_feed_id"],
                quantity_policy_version=QUANTITY_POLICY_VERSION,
                last_source=plan.source,
                status="error" if sku_errors else ("pending" if is_pending else "synced"),
                error_message=" ".join(sku_errors) or None,
            )

        all_errors: dict[str, str] = {}
        for error_source in (quantity_errors, price_errors, image_errors):
            for sku, message in error_source.items():
                if sku in all_errors:
                    all_errors[sku] = f"{all_errors[sku]} {message}"
                else:
                    all_errors[sku] = message
        return {
            "status": "partial_error" if all_errors else "ok",
            "checked": len(sync_skus),
            "ebay_active_skus": len(ebay_source_by_sku),
            "ebay_image_skus": sum(
                1 for row in ebay_source_by_sku.values() if row["image_urls"]
            ),
            "walmart_published_skus": len(walmart_skus),
            "ebay_quantity_source": ebay_quantity_source,
            "ebay_quantity_warning": ebay_quantity_warning,
            "quantity_sync_paused": quantity_sync_paused,
            "ended_ebay_skus": len(ended_skus),
            "unresolved_alias_skus": len(unresolved_alias_skus),
            "duplicate_alias_skus": len(duplicate_alias_skus),
            "updated_ebay": len(ebay_updates) - len(failed_ebay_skus),
            "updated_walmart": len(walmart_update_skus) if walmart_submission else 0,
            "zeroed_walmart": (
                len(
                    set(walmart_update_skus)
                    & (
                        set(ended_skus)
                        | set(excluded_skus)
                        | set(duplicate_alias_skus)
                    )
                )
                if walmart_submission
                else 0
            ),
            "excluded_walmart_skus": len(set(sync_skus) & set(excluded_skus)),
            "updated_prices": len(updated_price_skus),
            "price_markup_percent": price_markup_percent,
            "image_updates_submitted": len(maintenance_skus) if image_submission else 0,
            "image_updates_confirmed": image_updates_confirmed,
            "walmart_image_source_policy": "walmart_catalog_only",
            "walmart_submission": walmart_submission,
            "image_submission": image_submission,
            "errors": all_errors,
            "last_attempt_at": now_iso,
        }

    def _walmart_exclusion(self, item: InventoryItem) -> str | None:
        terms = [
            term.strip().lower()
            for term in str(
                getattr(
                    self.walmart_client.settings,
                    "walmart_auto_publish_excluded_terms",
                    "don toliver,don oliver,otterbox",
                )
                or ""
            ).split(",")
            if term.strip()
        ]
        searchable = " ".join(
            (
                item.title or "",
                item.description or "",
                item.category or "",
                " ".join(str(value or "") for value in item.item_specifics.values()),
            )
        ).lower()
        return next((term for term in terms if term in searchable), None)

    def _cached_active_ebay_quantity_rows(self) -> list[dict[str, Any]]:
        cached_items = [
            item
            for item in self.repository.ebay_items(limit=200, include_inactive=False)
            if item.source in {"ebay-store-page", "ebay-browse-api"}
            and item.ebay_item_id
        ]
        if not cached_items:
            return []

        # Store-page rows from one successful snapshot are written within a few
        # seconds. Selecting the newest cluster avoids reviving older public rows
        # that disappeared from a later snapshot.
        preferred_source = (
            "ebay-store-page"
            if any(item.source == "ebay-store-page" for item in cached_items)
            else "ebay-browse-api"
        )
        source_items = [
            item for item in cached_items if item.source == preferred_source
        ]
        latest = max(item.updated_at for item in source_items)
        snapshot_cutoff = latest - timedelta(minutes=5)
        rows: list[dict[str, Any]] = []
        for item in source_items:
            if item.updated_at < snapshot_cutoff:
                continue
            image_urls = item.image_urls or ([item.image_url] if item.image_url else [])
            row: dict[str, Any] = {
                "sku": item.sku,
                "item_id": str(item.ebay_item_id),
                "quantity": max(0, int(item.quantity)),
                "inventory_tracking": "item_id",
                "image_urls": image_urls,
                "image_complete": False,
            }
            if item.price is not None:
                row["start_price"] = float(item.price)
                row["currency"] = item.currency or "USD"
            rows.append(row)
        return rows


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


def _ebay_item_id_from_walmart_sku(sku: str) -> str | None:
    match = re.match(r"^EBAY-(\d{9,15})(?:-|$)", str(sku or "").strip(), re.IGNORECASE)
    return match.group(1) if match else None


def _retry_is_due(retry_at: datetime | None, now: datetime) -> bool:
    return retry_at is None or now >= retry_at


def _is_ebay_quota_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "429",
            "exceeded usage limit",
            "too many requests",
            "usage limit on this call",
        )
    )


def _image_signature(urls: list[str]) -> str:
    encoded = json.dumps(urls, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _draft_image_signature(draft: dict[str, Any] | None) -> str:
    images = _draft_image_urls(draft)
    return _image_signature(images) if images else ""


def _draft_image_urls(draft: dict[str, Any] | None) -> list[str]:
    if not draft:
        return []
    prepared = draft.get("prepared_listing")
    if not isinstance(prepared, dict):
        return []
    return _normalized_image_urls(prepared.get("images"))


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
