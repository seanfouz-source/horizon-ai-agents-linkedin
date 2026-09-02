from typing import Any


def walmart_feed_item_results(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    details = payload.get("itemDetails")
    raw_items = details.get("itemIngestionStatus") if isinstance(details, dict) else []
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        raw_items = []

    results: dict[str, dict[str, Any]] = {}
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        sku = str(raw_item.get("sku") or "").strip()
        if not sku:
            continue
        raw_errors = raw_item.get("ingestionErrors")
        error_items = raw_errors.get("ingestionError") if isinstance(raw_errors, dict) else []
        if isinstance(error_items, dict):
            error_items = [error_items]
        errors = [
            {
                "type": str(error.get("type") or ""),
                "code": str(error.get("code") or ""),
                "field": str(error.get("field") or ""),
                "description": str(error.get("description") or ""),
            }
            for error in (error_items or [])
            if isinstance(error, dict)
        ]
        results[sku] = {
            "sku": sku,
            "status": str(raw_item.get("ingestionStatus") or "UNKNOWN").upper(),
            "errors": errors,
            "error_message": "; ".join(
                error["description"] or error["field"] or error["code"]
                for error in errors
            ),
        }
    return results


def classify_walmart_offer_result(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "").upper()
    if status == "SUCCESS":
        return "offer_processed_inventory_pending"
    if status == "SYSTEM_ERROR":
        return "retryable_offer_error"

    error_text = " ".join(
        " ".join(
            str(error.get(key) or "")
            for key in ("code", "field", "description")
        )
        for error in result.get("errors") or []
        if isinstance(error, dict)
    ).lower()
    if "err_ext_data_0101211" in error_text or "different product id" in error_text:
        return "blocked_product_id_conflict"
    if "compliance review" in error_text:
        return "compliance_review"
    return "blocked_offer_error"


def classify_walmart_inventory_result(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "").upper()
    if status == "SUCCESS":
        return "submitted"
    if status == "SYSTEM_ERROR":
        return "offer_processed_inventory_pending"
    error_text = " ".join(
        " ".join(
            str(error.get(key) or "")
            for key in ("code", "field", "description")
        )
        for error in result.get("errors") or []
        if isinstance(error, dict)
    ).lower()
    if "did not find an item" in error_text or "54055672686268" in error_text:
        return "offer_processed_inventory_pending"
    return "blocked_inventory_error"
