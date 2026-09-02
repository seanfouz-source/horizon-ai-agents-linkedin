from datetime import datetime, timedelta, timezone

from app.inventory import InventoryRepository
from app.marketplace_inventory_sync import choose_inventory_sync_plan


def _state(quantity: int, **overrides):
    state = {
        "synced_quantity": quantity,
        "pending_walmart_quantity": None,
        "pending_walmart_at": None,
    }
    state.update(overrides)
    return state


def test_first_sync_treats_ebay_as_source_of_truth():
    plan = choose_inventory_sync_plan(5, 2, None)

    assert plan.target_quantity == 5
    assert plan.source == "ebay_initial"
    assert plan.update_ebay is False
    assert plan.update_walmart is True


def test_ebay_change_flows_to_walmart():
    plan = choose_inventory_sync_plan(3, 5, _state(5))

    assert plan.target_quantity == 3
    assert plan.source == "ebay"
    assert plan.update_walmart is True
    assert plan.update_ebay is False


def test_walmart_change_flows_to_ebay():
    plan = choose_inventory_sync_plan(5, 4, _state(5))

    assert plan.target_quantity == 4
    assert plan.source == "walmart"
    assert plan.update_ebay is True
    assert plan.update_walmart is False


def test_conflicting_changes_choose_lower_quantity_to_avoid_overselling():
    plan = choose_inventory_sync_plan(4, 3, _state(5))

    assert plan.target_quantity == 3
    assert plan.source == "safety_minimum"
    assert plan.update_ebay is True
    assert plan.update_walmart is False


def test_pending_walmart_feed_is_not_mistaken_for_a_manual_change():
    now = datetime.now(timezone.utc)
    plan = choose_inventory_sync_plan(
        3,
        5,
        _state(
            3,
            pending_walmart_quantity=3,
            pending_walmart_at=(now - timedelta(minutes=1)).isoformat(),
        ),
        now=now,
    )

    assert plan.source == "walmart_feed_pending"
    assert plan.update_ebay is False
    assert plan.update_walmart is False
    assert plan.pending_walmart_quantity == 3


def test_inventory_sync_state_is_persistent(tmp_path):
    repository = InventoryRepository(tmp_path / "inventory.db")

    stored = repository.upsert_marketplace_inventory_sync_state(
        sku="PHONE-1",
        ebay_item_id="123",
        ebay_quantity=3,
        walmart_quantity=5,
        synced_quantity=3,
        pending_walmart_quantity=3,
        pending_walmart_at="2026-09-02T12:00:00+00:00",
        last_source="ebay",
        status="pending",
    )

    assert stored["sku"] == "PHONE-1"
    assert stored["pending_walmart_quantity"] == 3
    assert repository.marketplace_inventory_sync_summary()["by_status"] == {"pending": 1}
