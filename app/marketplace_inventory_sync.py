from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.ebay import EbayClient
from app.inventory import InventoryRepository
from app.models import InventoryItem
from app.walmart import WalmartMarketplaceClient, build_inventory_feed


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
        ebay_rows = await self.ebay_client.fetch_active_inventory_quantities(limit=200)
        ebay_by_sku = {
            str(row["sku"]): {
                "sku": str(row["sku"]),
                "item_id": str(row.get("item_id") or ""),
                "quantity": max(0, int(row.get("quantity") or 0)),
            }
            for row in ebay_rows
            if str(row.get("sku") or "").strip()
        }
        for row in ebay_by_sku.values():
            self.repository.update_inventory_quantity(row["sku"], row["quantity"])

        walmart_items = await self.walmart_client.list_published_items(limit=1000)
        walmart_skus = {
            str(row.get("sku") or "").strip()
            for row in walmart_items
            if str(row.get("sku") or "").strip()
        }
        common_skus = sorted(set(ebay_by_sku) & walmart_skus)
        if not common_skus:
            return {
                "status": "no_published_matches",
                "checked": 0,
                "ebay_active_skus": len(ebay_by_sku),
                "walmart_published_skus": len(walmart_skus),
                "updated_ebay": 0,
                "updated_walmart": 0,
                "last_attempt_at": attempted_at.isoformat(),
            }

        semaphore = asyncio.Semaphore(6)

        async def walmart_quantity(sku: str) -> tuple[str, int | Exception]:
            try:
                async with semaphore:
                    return sku, await self.walmart_client.get_inventory_quantity(sku)
            except Exception as exc:  # the caller records SKU-scoped API failures
                return sku, exc

        walmart_results = await asyncio.gather(*(walmart_quantity(sku) for sku in common_skus))
        walmart_by_sku: dict[str, int] = {}
        errors: dict[str, str] = {}
        for sku, result in walmart_results:
            if isinstance(result, Exception):
                errors[sku] = f"Walmart inventory lookup failed: {result}"
            else:
                walmart_by_sku[sku] = result

        plans: dict[str, InventorySyncPlan] = {}
        for sku, walmart_quantity_value in walmart_by_sku.items():
            plans[sku] = choose_inventory_sync_plan(
                ebay_by_sku[sku]["quantity"],
                walmart_quantity_value,
                self.repository.marketplace_inventory_sync_state(sku),
                now=attempted_at,
            )

        ebay_updates = [
            {
                "sku": sku,
                "item_id": ebay_by_sku[sku]["item_id"],
                "quantity": plan.target_quantity,
            }
            for sku, plan in plans.items()
            if plan.update_ebay
        ]
        ebay_update_results = await self.ebay_client.revise_inventory_quantities(ebay_updates)
        ebay_result_by_sku = {str(result["sku"]): result for result in ebay_update_results}
        failed_ebay_skus = {
            sku
            for sku, result in ebay_result_by_sku.items()
            if result.get("status") != "updated"
        }
        for sku in failed_ebay_skus:
            errors[sku] = "eBay rejected the quantity update."

        walmart_update_skus = [
            sku
            for sku, plan in plans.items()
            if plan.update_walmart and sku not in failed_ebay_skus
        ]
        walmart_submission: dict[str, Any] | None = None
        walmart_submission_error: str | None = None
        if walmart_update_skus:
            walmart_payload = build_inventory_feed(
                [
                    InventoryItem(
                        sku=sku,
                        title=sku,
                        quantity=plans[sku].target_quantity,
                        listing_status="ACTIVE",
                        source="marketplace-inventory-sync",
                    )
                    for sku in walmart_update_skus
                ]
            )
            try:
                walmart_submission = await self.walmart_client.submit_inventory_feed(
                    walmart_payload
                )
            except Exception as exc:  # preserve successful eBay changes and retry Walmart next run
                walmart_submission_error = str(exc)
                for sku in walmart_update_skus:
                    errors[sku] = f"Walmart inventory feed failed: {exc}"

        now_iso = attempted_at.isoformat()
        for sku, plan in plans.items():
            previous = self.repository.marketplace_inventory_sync_state(sku)
            previous_synced = (
                max(0, int(previous.get("synced_quantity") or 0)) if previous else None
            )
            failed = sku in errors
            synced_quantity = (
                previous_synced
                if failed and previous_synced is not None
                else plan.target_quantity
            )
            pending_quantity = plan.pending_walmart_quantity
            pending_at = plan.pending_walmart_at
            if plan.update_walmart and walmart_submission is not None and not failed:
                pending_quantity = plan.target_quantity
                pending_at = now_iso
            elif plan.update_walmart and walmart_submission_error:
                pending_quantity = None
                pending_at = None
            elif walmart_by_sku[sku] == plan.target_quantity:
                pending_quantity = None
                pending_at = None

            effective_ebay_quantity = (
                plan.target_quantity
                if plan.update_ebay and sku not in failed_ebay_skus
                else ebay_by_sku[sku]["quantity"]
            )
            if plan.update_ebay and sku not in failed_ebay_skus:
                self.repository.update_inventory_quantity(sku, plan.target_quantity)
            self.repository.upsert_marketplace_inventory_sync_state(
                sku=sku,
                ebay_item_id=ebay_by_sku[sku]["item_id"] or None,
                ebay_quantity=effective_ebay_quantity,
                walmart_quantity=walmart_by_sku[sku],
                synced_quantity=synced_quantity,
                pending_walmart_quantity=pending_quantity,
                pending_walmart_at=pending_at,
                last_source=plan.source,
                status="error" if failed else ("pending" if pending_quantity is not None else "synced"),
                error_message=errors.get(sku),
            )

        return {
            "status": "partial_error" if errors else "ok",
            "checked": len(plans),
            "ebay_active_skus": len(ebay_by_sku),
            "walmart_published_skus": len(walmart_skus),
            "updated_ebay": len(ebay_updates) - len(failed_ebay_skus),
            "updated_walmart": len(walmart_update_skus) if walmart_submission else 0,
            "walmart_submission": walmart_submission,
            "errors": errors,
            "last_attempt_at": attempted_at.isoformat(),
        }


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
