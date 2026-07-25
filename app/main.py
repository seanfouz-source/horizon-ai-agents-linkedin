import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.agents import (
    answer_customer_question,
    create_group_outreach_plan,
    create_slow_mover_outreach,
    create_social_drafts,
    draft_group_reply,
)
from app.campaigns import campaign_video_catalog, campaign_video_path
from app.config import get_settings
from app.ebay import EbayClient
from app.ebay_draft_batch import (
    EBAY_INVENTORY_SHEET_BATCH_ID,
    inventory_sheet_missing_drafts,
)
from app.integrations import extract_customer_message, manychat_dynamic_response, normalize_channel, zapier_social_drafts_response
from app.inventory import InventoryRepository
from app.inventory_seed import seed_inventory_if_empty
from app.media import product_card_for_item, product_card_jpeg_for_item, tiktok_ebay_photo_jpeg_for_item
from app.metricool import MetricoolPublishError, schedule_metricool_payloads
from app.models import (
    CustomerQuestion,
    CustomerAnswer,
    EbayDraftBatchRequest,
    EbayStoreImportRequest,
    GroupOutreachRequest,
    GroupReplyRequest,
    InventoryItem,
    InventorySearchResult,
    SlowMoverOutreachPlan,
    SlowMoverOutreachRequest,
    SocialDraftBatch,
    SocialDraftRequest,
    WalmartImportRequest,
    WalmartDraftGenerateRequest,
    WalmartInventorySyncRequest,
    WalmartItemOverride,
)
from app.reports import (
    MetricoolReportError,
    REPORT_TIMEZONE,
    build_daily_metricool_report,
    flatten_report_for_zapier,
    format_daily_report_markdown,
    format_daily_report_pdf,
    report_attachment_filename,
)
from app.report_email import (
    ReportEmailError,
    build_message_from_settings,
    exchange_gmail_authorization_code,
    gmail_access_token,
    gmail_oauth_credentials,
    send_message_from_settings,
)
from app.store_sync import StorePageSyncer
from app.walmart import (
    WalmartApiError,
    WalmartMarketplaceClient,
    build_walmart_catalog_query,
    build_walmart_draft,
    build_inventory_feed,
    build_offer_match_preview,
)
from app.walmart_public_data import PUBLIC_CATALOG_IDENTIFIERS


GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_OAUTH_STATE_MAX_AGE_SECONDS = 15 * 60
EBAY_OAUTH_STATE_MAX_AGE_SECONDS = 15 * 60
settings = get_settings()
repository = InventoryRepository(settings.resolved_database_path)
store_syncer = StorePageSyncer(settings, repository)
walmart_client = WalmartMarketplaceClient(settings)
ebay_sync_status: dict[str, Any] = {
    "source": "ebay-api",
    "status": "not_run",
    "imported": 0,
    "message": "eBay API sync has not run yet.",
    "last_attempt_at": None,
}
ebay_draft_status: dict[str, Any] = {
    "status": "not_run",
    "batch_id": EBAY_INVENTORY_SHEET_BATCH_ID,
    "created_unpublished": 0,
    "published": 0,
    "message": "The inventory-sheet eBay draft batch has not run yet.",
    "last_attempt_at": None,
}
ebay_seller_hub_draft_status: dict[str, Any] = (
    repository.latest_ebay_seller_hub_draft_job()
    or {
        "status": "not_run",
        "batch_id": None,
        "requested_count": 0,
        "success_count": 0,
        "failure_count": 0,
        "message": "The visible Seller Hub draft feed has not run yet.",
    }
)
walmart_sync_status: dict[str, Any] = {
    "status": "not_run",
    "configured": walmart_client.configured,
    "last_submission": None,
}
walmart_draft_status: dict[str, Any] = {
    "status": "not_run",
    "generated": 0,
    "message": "Walmart API draft staging has not run yet.",
    "last_attempt_at": None,
}
walmart_unpublished_status: dict[str, Any] = repository.latest_walmart_unpublished_job() or {
    "status": "not_authorized",
    "message": "No one-time zero-inventory Walmart offer batch has been authorized.",
}
app = FastAPI(title=settings.app_name)
logger = logging.getLogger(__name__)
LISTING_PHOTO_DIRECTORY = Path(__file__).with_name("listing_photos")
LISTING_PHOTO_FILENAMES = {
    "PHOTO-2026-07-24-13-09-52.jpg",
    "PHOTO-2026-07-24-13-10-27.jpg",
    "PHOTO-2026-07-24-13-10-59.jpg",
    "PHOTO-2026-07-24-13-11-44.jpg",
    "PHOTO-2026-07-24-13-17-29.jpg",
    "PHOTO-2026-07-24-13-17-54.jpg",
    "PHOTO-2026-07-24-13-18-22.jpg",
    "PHOTO-2026-07-24-13-18-46.jpg",
    "PHOTO-2026-07-24-13-19-13.jpg",
    "PHOTO-2026-07-24-13-19-38.jpg",
}


def verify_secret(x_horizon_secret: str | None, query_secret: str | None = None) -> None:
    expected = settings.webhook_shared_secret
    if not expected:
        return
    if x_horizon_secret != expected and query_secret != expected:
        raise HTTPException(status_code=401, detail="Invalid webhook secret.")


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": settings.app_name,
        "ebay_sync": ebay_sync_status,
        "ebay_drafts": ebay_draft_status,
        "ebay_seller_hub_drafts": ebay_seller_hub_draft_status,
        "store_sync": store_syncer.last_status,
        "walmart_sync": walmart_sync_status,
        "walmart_drafts": {
            **walmart_draft_status,
            "stored": repository.walmart_draft_summary(),
        },
        "walmart_unpublished": walmart_unpublished_status,
    }


@app.get("/gmail/oauth/start")
def gmail_oauth_start(
    secret: str | None = None,
    x_horizon_secret: str | None = Header(default=None),
) -> RedirectResponse:
    verify_secret(x_horizon_secret, secret)
    try:
        credentials = gmail_oauth_credentials(settings=settings)
        state = _sign_gmail_oauth_state()
    except ReportEmailError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    redirect_uri = _gmail_oauth_redirect_uri()
    authorization_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
        {
            "client_id": credentials.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": GMAIL_SEND_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "login_hint": settings.gmail_sender or settings.report_email_from or "sean.fouz@gmail.com",
        }
    )
    return RedirectResponse(authorization_url, status_code=302)


@app.get("/gmail/oauth/status")
def gmail_oauth_status(
    secret: str | None = None,
    test_refresh: bool = False,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, secret)
    try:
        credentials = gmail_oauth_credentials(settings=settings)
    except ReportEmailError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    raw_refresh_token = settings.gmail_refresh_token_current or ""
    clean_refresh_token = _diagnostic_clean_gmail_refresh_token(raw_refresh_token)
    status: dict[str, Any] = {
        "report_email_provider": settings.report_email_provider,
        "gmail_sender": settings.gmail_sender or settings.report_email_from,
        "public_base_url": settings.public_base_url,
        "redirect_uri": _gmail_oauth_redirect_uri(),
        "gmail_client_credentials_file": str(settings.gmail_client_credentials_file or "auto/default"),
        "gmail_client_id_hint": _diagnostic_hint(credentials.client_id),
        "gmail_client_id_sha256": _diagnostic_sha256(credentials.client_id),
        "gmail_refresh_token_current_present": bool(clean_refresh_token),
        "gmail_refresh_token_current_length": len(clean_refresh_token),
        "gmail_refresh_token_current_sha256": _diagnostic_sha256(clean_refresh_token) if clean_refresh_token else None,
        "gmail_refresh_token_current_has_assignment_prefix": raw_refresh_token.strip().startswith(
            "GMAIL_REFRESH_TOKEN_CURRENT="
        ),
    }

    if test_refresh:
        try:
            gmail_access_token(
                client_id=credentials.client_id,
                client_secret=credentials.client_secret,
                refresh_token=raw_refresh_token,
            )
        except ReportEmailError as exc:
            status["refresh_test"] = {"status": "failed", "error": str(exc)}
        else:
            status["refresh_test"] = {"status": "ok"}

    return status


@app.get("/ebay/oauth/start")
def ebay_oauth_start() -> RedirectResponse:
    client_id, _, redirect_name = _ebay_oauth_credentials()
    authorization_url = "https://auth.ebay.com/oauth2/authorize?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_name,
            "response_type": "code",
            "scope": settings.ebay_oauth_scopes,
            "state": _sign_ebay_oauth_state(),
        },
        quote_via=quote,
    )
    return RedirectResponse(authorization_url, status_code=302)


@app.get("/ebay/oauth/callback", response_class=HTMLResponse)
def ebay_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> HTMLResponse:
    if error:
        detail = error_description or error
        raise HTTPException(status_code=400, detail=f"eBay authorization failed: {detail}")
    if not code:
        raise HTTPException(status_code=400, detail="eBay authorization did not return a code.")
    if not state or not _verify_ebay_oauth_state(state):
        raise HTTPException(status_code=400, detail="Invalid or expired eBay OAuth state.")

    token_payload = _exchange_ebay_authorization_code(code)
    refresh_token = token_payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise HTTPException(
            status_code=503,
            detail="eBay did not return a refresh token. Restart the eBay OAuth flow and grant access again.",
        )

    return HTMLResponse(
        _ebay_oauth_success_html(refresh_token),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/oauth2callback", response_class=HTMLResponse)
def gmail_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    if error:
        raise HTTPException(status_code=400, detail=f"Google authorization failed: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Google authorization did not return a code.")
    if not state or not _verify_gmail_oauth_state(state):
        raise HTTPException(status_code=400, detail="Invalid or expired Google OAuth state.")

    try:
        token_payload = exchange_gmail_authorization_code(
            code=code,
            redirect_uri=_gmail_oauth_redirect_uri(),
            settings=settings,
        )
    except ReportEmailError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    refresh_token = token_payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise HTTPException(
            status_code=503,
            detail="Google did not return a refresh token. Remove the old app grant and retry the OAuth start URL.",
        )

    return HTMLResponse(
        _gmail_oauth_success_html(refresh_token),
        headers={"Cache-Control": "no-store"},
    )


@app.on_event("startup")
async def startup_inventory_sync() -> None:
    seed_inventory_if_empty(repository, settings.seed_inventory_csv)
    asyncio.create_task(_startup_inventory_refresh())
    asyncio.create_task(_startup_walmart_auth_check())


async def _startup_walmart_auth_check() -> None:
    global walmart_sync_status
    if not walmart_client.configured:
        return
    try:
        authentication = await walmart_client.verify_credentials()
    except WalmartApiError as exc:
        logger.warning("Walmart Marketplace authentication check failed: %s", exc)
        walmart_sync_status = {
            "status": "authentication_failed",
            "configured": True,
            "authentication": {
                "status": "failed",
                "http_status": exc.status_code,
            },
            "last_submission": None,
        }
        return
    walmart_sync_status = {
        "status": "authenticated",
        "configured": True,
        "authentication": authentication,
        "last_submission": None,
    }


async def _startup_inventory_refresh() -> None:
    api_status: dict[str, Any] | None = None
    if settings.sync_ebay_api_on_startup and _has_ebay_sync_credentials():
        api_status = await _sync_ebay_api_inventory()
    if api_status and api_status.get("status") == "ok":
        seller_hub_batch_id = str(
            settings.ebay_seller_hub_draft_batch_id or ""
        ).strip()
        if seller_hub_batch_id:
            try:
                await _submit_seller_hub_drafts_once(seller_hub_batch_id)
            except Exception as exc:
                logger.warning("eBay Seller Hub draft feed failed at startup: %s", exc)
        if settings.walmart_stage_drafts_on_startup and walmart_client.configured:
            try:
                await _generate_walmart_drafts(
                    WalmartDraftGenerateRequest(sync_ebay_first=False)
                )
                batch_id = str(settings.walmart_unpublished_batch_id or "").strip()
                if batch_id:
                    await _submit_unpublished_batch_once(batch_id)
            except Exception as exc:
                logger.warning("Walmart API draft staging failed at startup: %s", exc)
        return
    if settings.sync_store_page_on_startup:
        await store_syncer.sync()


async def _sync_ebay_api_inventory() -> dict[str, Any]:
    global ebay_sync_status
    attempted_at = datetime.now(timezone.utc).isoformat()
    if not _has_ebay_sync_credentials():
        ebay_sync_status = {
            "source": "ebay-api",
            "status": "skipped",
            "imported": 0,
            "message": "eBay API credentials are not configured.",
            "last_attempt_at": attempted_at,
        }
        return ebay_sync_status

    try:
        client = EbayClient(settings)
        items = await client.fetch_inventory_items()
        count = repository.replace_ebay_inventory_snapshot(items)
        ebay_sync_status = {
            "source": "ebay-api",
            "status": "ok" if count else "empty",
            "imported": count,
            "inventory_count": repository.count(),
            "message": f"Imported {count} active eBay API listings.",
            "last_attempt_at": attempted_at,
        }
    except Exception as exc:
        logger.warning("eBay API inventory sync failed: %s", exc)
        ebay_sync_status = {
            "source": "ebay-api",
            "status": "failed",
            "imported": 0,
            "inventory_count": repository.count(),
            "message": f"eBay API sync failed with {exc.__class__.__name__}.",
            "last_attempt_at": attempted_at,
        }
    return ebay_sync_status


async def _submit_seller_hub_drafts_once(batch_id: str) -> dict[str, Any]:
    global ebay_seller_hub_draft_status
    clean_batch_id = str(batch_id or "").strip()
    if not clean_batch_id:
        raise ValueError("A non-empty eBay Seller Hub draft batch ID is required.")

    existing = repository.ebay_seller_hub_draft_job(clean_batch_id)
    terminal_statuses = {"COMPLETED", "COMPLETED_WITH_ERROR"}
    if existing and str(existing.get("status") or "").upper() in terminal_statuses:
        ebay_seller_hub_draft_status = existing
        return existing

    client = EbayClient(settings)
    task_id = str((existing or {}).get("task_id") or "").strip()
    requested_count = int((existing or {}).get("requested_count") or 0)
    if not task_id:
        drafts = inventory_sheet_missing_drafts()
        submission = await client.submit_seller_hub_draft_feed(drafts, confirm=True)
        if submission.get("status") != "submitted":
            ebay_seller_hub_draft_status = (
                repository.upsert_ebay_seller_hub_draft_job(
                    clean_batch_id,
                    status=str(submission.get("status") or "submission_failed"),
                    task_id=submission.get("task_id"),
                    requested_count=len(drafts),
                    error_message=str(
                        submission.get("message")
                        or submission.get("error")
                        or "The Seller Hub draft feed was not submitted."
                    ),
                )
            )
            return ebay_seller_hub_draft_status
        task_id = str(submission["task_id"])
        requested_count = len(drafts)
        ebay_seller_hub_draft_status = (
            repository.upsert_ebay_seller_hub_draft_job(
                clean_batch_id,
                status="SUBMITTED",
                task_id=task_id,
                requested_count=requested_count,
            )
        )

    task_payload: dict[str, Any] = {}
    for _ in range(45):
        task_payload = await client.seller_hub_draft_task_status(task_id)
        status = str(task_payload.get("status") or "UNKNOWN").upper()
        upload_summary = task_payload.get("uploadSummary") or {}
        success_count = int(
            upload_summary.get("successCount")
            or upload_summary.get("success")
            or 0
        )
        failure_count = int(
            upload_summary.get("failureCount")
            or upload_summary.get("failure")
            or 0
        )
        ebay_seller_hub_draft_status = (
            repository.upsert_ebay_seller_hub_draft_job(
                clean_batch_id,
                status=status,
                task_id=task_id,
                requested_count=requested_count,
                success_count=success_count,
                failure_count=failure_count,
            )
        )
        if status in terminal_statuses:
            if status == "COMPLETED_WITH_ERROR" or failure_count:
                try:
                    result_excerpt = await client.seller_hub_draft_result_excerpt(
                        task_id
                    )
                except (RuntimeError, httpx.HTTPError) as exc:
                    result_excerpt = None
                    error_message = (
                        f"The result file could not be downloaded: {exc.__class__.__name__}."
                    )
                else:
                    error_message = (
                        f"eBay accepted {success_count} draft rows and rejected "
                        f"{failure_count}."
                    )
                ebay_seller_hub_draft_status = (
                    repository.upsert_ebay_seller_hub_draft_job(
                        clean_batch_id,
                        status=status,
                        task_id=task_id,
                        requested_count=requested_count,
                        success_count=success_count,
                        failure_count=failure_count,
                        result_excerpt=result_excerpt,
                        error_message=error_message,
                    )
                )
            return ebay_seller_hub_draft_status
        await asyncio.sleep(2)
    return ebay_seller_hub_draft_status


def _has_ebay_sync_credentials() -> bool:
    if (settings.ebay_access_token or "").strip():
        return True
    if all(
        str(getattr(settings, field, "") or "").strip()
        for field in ("ebay_client_id", "ebay_client_secret", "ebay_refresh_token")
    ):
        return True
    return all(
        str(getattr(settings, field, "") or "").strip()
        for field in ("ebay_client_id", "ebay_client_secret")
    )


async def _prepare_walmart_import(
    import_request: WalmartImportRequest,
    *,
    force_verify_catalog: bool = False,
) -> dict[str, Any]:
    ebay_refresh: dict[str, Any] | None = None
    if import_request.sync_ebay_first:
        ebay_refresh = await _sync_ebay_api_inventory()

    items = repository.ebay_items(
        import_request.skus,
        limit=import_request.max_items,
        include_inactive=False,
    )
    preview = build_offer_match_preview(
        items,
        import_request.overrides,
        default_shipping_weight_lbs=settings.walmart_default_shipping_weight_lbs,
    )
    preview["ebay_sync"] = ebay_refresh
    preview["walmart_configured"] = walmart_client.configured

    if not (force_verify_catalog or import_request.verify_catalog):
        preview["catalog_verification"] = "not_requested"
        return preview
    if not walmart_client.configured:
        raise HTTPException(
            status_code=503,
            detail="WALMART_CLIENT_ID and WALMART_CLIENT_SECRET are required for catalog verification.",
        )

    for item_result in preview["items"]:
        if not item_result["ready"]:
            continue
        resolved = item_result["resolved"]
        try:
            catalog = await walmart_client.search_catalog(
                resolved["product_id_type"],
                resolved["product_id"],
            )
        except WalmartApiError as exc:
            raise _walmart_http_error(exc) from exc
        item_result["catalog"] = catalog
        if catalog.get("matched") is False:
            item_result["ready"] = False
            item_result["errors"].append(
                "No published Walmart catalog match was found; this item needs a full MP_ITEM setup."
            )
        elif catalog.get("matched") is None:
            item_result["warnings"].append(str(catalog.get("reason") or "Catalog match was not checked."))

    ready_skus = {item["sku"] for item in preview["items"] if item["ready"]}
    preview["payload"]["MPItem"] = [
        entry
        for entry in preview["payload"]["MPItem"]
        if entry.get("Item", {}).get("sku") in ready_skus
    ]
    preview["ready"] = len(preview["payload"]["MPItem"])
    preview["blocked"] = preview["total"] - preview["ready"]
    preview["catalog_verification"] = "completed"
    return preview


async def _generate_walmart_drafts(
    draft_request: WalmartDraftGenerateRequest,
) -> dict[str, Any]:
    global walmart_draft_status
    attempted_at = datetime.now(timezone.utc).isoformat()
    ebay_refresh: dict[str, Any] | None = None
    if draft_request.sync_ebay_first:
        ebay_refresh = await _sync_ebay_api_inventory()
        if ebay_refresh.get("status") != "ok":
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "The eBay API refresh did not complete, so Walmart drafts were not changed.",
                    "ebay_sync": ebay_refresh,
                },
            )

    items = repository.ebay_items(
        draft_request.skus,
        limit=draft_request.max_items,
        include_inactive=False,
    )
    if not items:
        raise HTTPException(status_code=422, detail="No active eBay API listings were available to stage.")

    semaphore = asyncio.Semaphore(4)

    async def stage_item(item: InventoryItem) -> dict[str, Any]:
        query = build_walmart_catalog_query(item)
        catalog_result: dict[str, Any] = {
            "status": "not_requested",
            "query": query,
            "total_candidates": 0,
            "candidates": [],
        }
        lookup_error: str | None = None
        if draft_request.search_walmart_catalog:
            if not walmart_client.configured:
                lookup_error = "Walmart Marketplace API credentials are not configured."
                catalog_result["status"] = "lookup_failed"
            else:
                try:
                    async with semaphore:
                        catalog_result = await walmart_client.search_catalog_by_query(
                            query,
                            limit=draft_request.catalog_candidates_per_item,
                        )
                except WalmartApiError as exc:
                    lookup_error = str(exc)
                    catalog_result = {
                        "status": "lookup_failed",
                        "query": query,
                        "total_candidates": 0,
                        "candidates": [],
                    }
        return build_walmart_draft(item, catalog_result, lookup_error=lookup_error)

    drafts = await asyncio.gather(*(stage_item(item) for item in items))
    stored = repository.upsert_walmart_drafts(drafts)
    catalog_counts: dict[str, int] = {}
    missing_identifier = 0
    verified_matches = 0
    for draft in drafts:
        catalog_status = str(draft["catalog_status"])
        catalog_counts[catalog_status] = catalog_counts.get(catalog_status, 0) + 1
        if "product_identifier" in draft["missing_fields"]:
            missing_identifier += 1
        if draft["status"] == "draft_verified_match":
            verified_matches += 1

    walmart_draft_status = {
        "status": "staged",
        "generated": stored,
        "catalog_status": catalog_counts,
        "missing_identifier": missing_identifier,
        "verified_matches": verified_matches,
        "message": (
            "Stored API-enriched drafts in the Render database. "
            "No Walmart item or inventory feed was submitted."
        ),
        "last_attempt_at": attempted_at,
    }
    return {
        **walmart_draft_status,
        "ebay_sync": ebay_refresh,
        "storage": "render_database",
        "walmart_feed_submitted": False,
        "drafts": drafts,
    }


async def _submit_unpublished_batch_once(batch_id: str) -> dict[str, Any]:
    global walmart_unpublished_status
    clean_batch_id = str(batch_id or "").strip()
    if not clean_batch_id:
        raise ValueError("A non-empty Walmart unpublished batch ID is required.")

    existing = repository.walmart_unpublished_job(clean_batch_id)
    if existing and existing.get("status") in {
        "submitted",
        "processing",
        "completed",
        "no_verified_matches",
    }:
        walmart_unpublished_status = existing
        return existing
    if (
        existing
        and existing.get("status") == "offer_submitted_inventory_pending"
        and existing.get("offer_feed_id")
    ):
        matched_skus = [str(sku) for sku in existing.get("matched_skus") or []]
        matched_items = repository.ebay_items(
            matched_skus,
            limit=max(1, len(matched_skus)),
            include_inactive=False,
        )
        zero_inventory_items = [item.model_copy(update={"quantity": 0}) for item in matched_items]
        inventory_submission = await walmart_client.submit_inventory_feed(
            build_inventory_feed(zero_inventory_items)
        )
        walmart_unpublished_status = repository.upsert_walmart_unpublished_job(
            clean_batch_id,
            status="submitted",
            matched_skus=matched_skus,
            skipped_skus=existing.get("skipped_skus") or [],
            offer_feed_id=str(existing["offer_feed_id"]),
            inventory_feed_id=str(inventory_submission["feed_id"]),
        )
        repository.set_walmart_draft_status(matched_skus, "unpublished_offer_submitted")
        return walmart_unpublished_status

    drafts = repository.walmart_drafts(limit=200)
    verified_drafts = [
        draft
        for draft in drafts
        if draft.get("status") == "draft_verified_match"
        and isinstance(draft.get("prepared_listing"), dict)
        and isinstance(draft["prepared_listing"].get("product_identifier"), dict)
    ]
    draft_skus = {str(draft["sku"]) for draft in drafts}
    overrides: dict[str, WalmartItemOverride] = {}
    for draft in verified_drafts:
        identifier = draft["prepared_listing"]["product_identifier"]
        overrides[str(draft["sku"])] = WalmartItemOverride(
            product_id_type=identifier["type"],
            product_id=identifier["value"],
        )
    for sku, identifier in PUBLIC_CATALOG_IDENTIFIERS.items():
        if sku not in draft_skus:
            continue
        overrides.setdefault(
            sku,
            WalmartItemOverride(
                product_id_type=identifier["product_id_type"],
                product_id=identifier["product_id"],
            ),
        )

    candidate_skus = sorted(overrides)
    skipped_skus = sorted(draft_skus - set(candidate_skus))
    if not candidate_skus:
        walmart_unpublished_status = repository.upsert_walmart_unpublished_job(
            clean_batch_id,
            status="no_verified_matches",
            skipped_skus=skipped_skus,
            error_message="No draft had a verified public identifier; no Walmart feed was submitted.",
        )
        return walmart_unpublished_status

    preflight = await _prepare_walmart_import(
        WalmartImportRequest(
            skus=candidate_skus,
            overrides=overrides,
            max_items=len(candidate_skus),
            sync_ebay_first=False,
            verify_catalog=True,
        ),
        force_verify_catalog=True,
    )
    ready_skus = [str(item["sku"]) for item in preflight["items"] if item.get("ready")]
    skipped_skus = sorted(set(skipped_skus) | (set(candidate_skus) - set(ready_skus)))
    if not ready_skus:
        walmart_unpublished_status = repository.upsert_walmart_unpublished_job(
            clean_batch_id,
            status="no_verified_matches",
            skipped_skus=skipped_skus,
            error_message="Walmart SPEC verification rejected every candidate; no feed was submitted.",
        )
        return walmart_unpublished_status

    ready_entries = [
        entry
        for entry in preflight["payload"]["MPItem"]
        if entry.get("Item", {}).get("sku") in set(ready_skus)
    ]
    offer_payload = {**preflight["payload"], "MPItem": ready_entries}
    repository.upsert_walmart_unpublished_job(
        clean_batch_id,
        status="submitting_offer",
        matched_skus=ready_skus,
        skipped_skus=skipped_skus,
    )

    try:
        offer_submission = await walmart_client.submit_offer_match_feed(offer_payload)
    except WalmartApiError as exc:
        walmart_unpublished_status = repository.upsert_walmart_unpublished_job(
            clean_batch_id,
            status="offer_failed",
            matched_skus=ready_skus,
            skipped_skus=skipped_skus,
            error_message=str(exc),
        )
        raise

    offer_feed_id = str(offer_submission["feed_id"])
    repository.upsert_walmart_unpublished_job(
        clean_batch_id,
        status="offer_submitted_inventory_pending",
        matched_skus=ready_skus,
        skipped_skus=skipped_skus,
        offer_feed_id=offer_feed_id,
    )

    ready_items = repository.ebay_items(ready_skus, limit=len(ready_skus), include_inactive=False)
    zero_inventory_items = [item.model_copy(update={"quantity": 0}) for item in ready_items]
    try:
        inventory_submission = await walmart_client.submit_inventory_feed(
            build_inventory_feed(zero_inventory_items)
        )
    except WalmartApiError as exc:
        walmart_unpublished_status = repository.upsert_walmart_unpublished_job(
            clean_batch_id,
            status="offer_submitted_inventory_pending",
            matched_skus=ready_skus,
            skipped_skus=skipped_skus,
            offer_feed_id=offer_feed_id,
            error_message=str(exc),
        )
        raise

    walmart_unpublished_status = repository.upsert_walmart_unpublished_job(
        clean_batch_id,
        status="submitted",
        matched_skus=ready_skus,
        skipped_skus=skipped_skus,
        offer_feed_id=offer_feed_id,
        inventory_feed_id=str(inventory_submission["feed_id"]),
    )
    repository.set_walmart_draft_status(ready_skus, "unpublished_offer_submitted")
    return walmart_unpublished_status


def _walmart_http_error(exc: WalmartApiError) -> HTTPException:
    status_code = 503 if exc.status_code in {401, 403, 429, 500, 502, 503, 504} else 502
    return HTTPException(status_code=status_code, detail=str(exc))


@app.get("/inventory/search", response_model=InventorySearchResult)
def search_inventory(q: str = "", limit: int = 8) -> InventorySearchResult:
    items = repository.search(q, limit=limit)
    return InventorySearchResult(total=len(items), items=items)


@app.get("/media/products/{sku}.png")
def product_media(sku: str) -> Response:
    item = repository.get(sku)
    if item is None:
        raise HTTPException(status_code=404, detail="No inventory item found for that SKU.")
    return Response(
        content=product_card_for_item(item),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _listing_photo_path(filename: str) -> Path:
    if filename not in LISTING_PHOTO_FILENAMES:
        raise HTTPException(status_code=404, detail="Listing photo not found.")
    path = LISTING_PHOTO_DIRECTORY / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Listing photo not found.")
    return path


@app.get("/media/listing-photos/{filename}")
def listing_photo_media(filename: str) -> FileResponse:
    return FileResponse(
        _listing_photo_path(filename),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.head("/media/listing-photos/{filename}")
def listing_photo_media_head(filename: str) -> Response:
    path = _listing_photo_path(filename)
    return Response(
        media_type="image/jpeg",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Length": str(path.stat().st_size),
        },
    )


@app.head("/media/products/{sku}.png")
def product_media_head(sku: str) -> Response:
    item = repository.get(sku)
    if item is None:
        raise HTTPException(status_code=404, detail="No inventory item found for that SKU.")
    content = product_card_for_item(item)
    return Response(
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Content-Length": str(len(content)),
        },
    )


@app.get("/media/products/{sku}.tiktok.jpeg")
@app.get("/media/products/{sku}.tiktok.jpg")
def product_media_tiktok_jpeg(sku: str) -> Response:
    item = repository.get(sku)
    if item is None:
        raise HTTPException(status_code=404, detail="No inventory item found for that SKU.")
    try:
        content = tiktok_ebay_photo_jpeg_for_item(item)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("Could not prepare TikTok eBay photo for sku=%s: %s", sku, exc)
        raise HTTPException(status_code=502, detail="Could not load the eBay image for that SKU.") from exc
    return Response(
        content=content,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.head("/media/products/{sku}.tiktok.jpeg")
@app.head("/media/products/{sku}.tiktok.jpg")
def product_media_tiktok_jpeg_head(sku: str) -> Response:
    item = repository.get(sku)
    if item is None:
        raise HTTPException(status_code=404, detail="No inventory item found for that SKU.")
    try:
        content = tiktok_ebay_photo_jpeg_for_item(item)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("Could not prepare TikTok eBay photo for sku=%s: %s", sku, exc)
        raise HTTPException(status_code=502, detail="Could not load the eBay image for that SKU.") from exc
    return Response(
        media_type="image/jpeg",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Content-Length": str(len(content)),
        },
    )


@app.get("/media/products/{sku}.jpeg")
@app.get("/media/products/{sku}.jpg")
def product_media_jpeg(sku: str) -> Response:
    item = repository.get(sku)
    if item is None:
        raise HTTPException(status_code=404, detail="No inventory item found for that SKU.")
    return Response(
        content=product_card_jpeg_for_item(item),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.head("/media/products/{sku}.jpeg")
@app.head("/media/products/{sku}.jpg")
def product_media_jpeg_head(sku: str) -> Response:
    item = repository.get(sku)
    if item is None:
        raise HTTPException(status_code=404, detail="No inventory item found for that SKU.")
    content = product_card_jpeg_for_item(item)
    return Response(
        media_type="image/jpeg",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Content-Length": str(len(content)),
        },
    )


@app.get("/campaigns/videos")
def campaign_videos() -> dict[str, object]:
    return {"videos": campaign_video_catalog()}


@app.get("/reports/daily")
async def daily_report(
    request: Request,
    date: str | None = None,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    try:
        report = await _build_daily_report(_parse_report_date(date))
    except MetricoolReportError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return report


@app.get("/reports/daily.md")
async def daily_report_markdown(
    request: Request,
    date: str | None = None,
    x_horizon_secret: str | None = Header(default=None),
) -> Response:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    try:
        report = await _build_daily_report(_parse_report_date(date))
    except MetricoolReportError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(content=format_daily_report_markdown(report), media_type="text/markdown")


@app.get("/reports/daily.pdf")
async def daily_report_pdf(
    request: Request,
    date: str | None = None,
    x_horizon_secret: str | None = Header(default=None),
) -> Response:
    content, filename = await _daily_report_pdf_content(request, date, x_horizon_secret)
    return Response(
        content=content,
        media_type="application/pdf",
        headers=_daily_report_pdf_headers(filename),
    )


@app.head("/reports/daily.pdf")
async def daily_report_pdf_head(
    request: Request,
    date: str | None = None,
    x_horizon_secret: str | None = Header(default=None),
) -> Response:
    content, filename = await _daily_report_pdf_content(request, date, x_horizon_secret)
    return Response(
        media_type="application/pdf",
        headers={**_daily_report_pdf_headers(filename), "Content-Length": str(len(content))},
    )


async def _daily_report_pdf_content(
    request: Request,
    date: str | None,
    x_horizon_secret: str | None,
) -> tuple[bytes, str]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    try:
        report = await _build_daily_report(_parse_report_date(date))
        content = format_daily_report_pdf(report)
    except MetricoolReportError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    filename = report_attachment_filename(report)
    return content, filename


@app.post("/reports/daily/email")
async def daily_report_email(
    request: Request,
    date: str | None = None,
    dry_run: bool = False,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    try:
        report = await _build_daily_report(_parse_report_date(date))
        report_fields = flatten_report_for_zapier(report)
        pdf_bytes = format_daily_report_pdf(report)
        message = build_message_from_settings(report_fields, pdf_bytes, settings)
        if not dry_run:
            send_message_from_settings(message, settings)
    except MetricoolReportError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ReportEmailError as exc:
        logger.error("Daily report email failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {
        "status": "prepared" if dry_run else "sent",
        "dry_run": dry_run,
        "report_date": report_fields["report_date"],
        "subject": message["Subject"],
        "to": message["To"],
        "attachment_filename": report_fields["attachment_filename"],
    }


def _daily_report_pdf_headers(filename: str) -> dict[str, str]:
    return {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }


@app.api_route("/webhooks/zapier/daily-report", methods=["GET", "POST"])
async def zapier_daily_report(
    request: Request,
    date: str | None = None,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    body = await parse_zapier_body(request) if request.method == "POST" else {}
    report_date = _parse_report_date(date or body.get("date"))
    try:
        report = await _build_daily_report(report_date)
    except MetricoolReportError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return flatten_report_for_zapier(report)


@app.get("/media/campaigns/{slug}.mp4")
def campaign_video_media(slug: str) -> FileResponse:
    path = campaign_video_path(slug)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="No campaign video found for that slug.")
    return FileResponse(
        path,
        media_type="video/mp4",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.head("/media/campaigns/{slug}.mp4")
def campaign_video_media_head(slug: str) -> Response:
    path = campaign_video_path(slug)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="No campaign video found for that slug.")
    return Response(
        media_type="video/mp4",
        headers={
            "Cache-Control": "public, max-age=86400",
            "Accept-Ranges": "bytes",
            "Content-Length": str(path.stat().st_size),
        },
    )


@app.post("/inventory/import")
async def import_inventory(
    items: list[InventoryItem],
    request: Request,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, int]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    count = repository.upsert_items(items)
    return {"imported": count}


@app.post("/inventory/sync/ebay")
async def sync_ebay_inventory(
    request: Request,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    return await _sync_ebay_api_inventory()


@app.get("/ebay/drafts/inventory-sheet")
def ebay_inventory_sheet_draft_manifest(
    request: Request,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    drafts = inventory_sheet_missing_drafts()
    return {
        "batch_id": EBAY_INVENTORY_SHEET_BATCH_ID,
        "total": len(drafts),
        "published": False,
        "drafts": [draft.to_dict() for draft in drafts],
        "last_run": ebay_draft_status,
    }


@app.post("/ebay/drafts/inventory-sheet")
async def create_ebay_inventory_sheet_drafts(
    draft_request: EbayDraftBatchRequest,
    request: Request,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    global ebay_draft_status
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    if draft_request.batch_id != EBAY_INVENTORY_SHEET_BATCH_ID:
        raise HTTPException(status_code=403, detail="The eBay draft batch ID is not authorized.")

    all_drafts = inventory_sheet_missing_drafts()
    selected_drafts = all_drafts[
        draft_request.offset : draft_request.offset + draft_request.max_items
    ]
    if not selected_drafts:
        raise HTTPException(status_code=422, detail="The requested eBay draft batch slice is empty.")

    try:
        results = await EbayClient(settings).prepare_unpublished_drafts(
            selected_drafts,
            confirm=draft_request.confirm,
            catalog_candidates_per_item=draft_request.catalog_candidates_per_item,
        )
    except (RuntimeError, httpx.HTTPError) as exc:
        ebay_draft_status = {
            "status": "failed",
            "batch_id": draft_request.batch_id,
            "created_unpublished": 0,
            "published": 0,
            "message": f"eBay draft preparation failed: {exc.__class__.__name__}.",
            "last_attempt_at": datetime.now(timezone.utc).isoformat(),
        }
        raise HTTPException(status_code=503, detail=ebay_draft_status) from exc

    status_counts: dict[str, int] = {}
    for result in results:
        status = str(result.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    created = sum(
        1
        for result in results
        if result.get("status") in {"created_unpublished", "existing_unpublished"}
    )
    ebay_draft_status = {
        "status": "created_unpublished" if draft_request.confirm else "previewed",
        "batch_id": draft_request.batch_id,
        "offset": draft_request.offset,
        "requested": len(selected_drafts),
        "created_unpublished": created,
        "published": 0,
        "status_counts": status_counts,
        "message": (
            "No eBay publish endpoint was called. Offers remain unpublished."
            if draft_request.confirm
            else "Catalog preview completed. No eBay inventory item or offer was created."
        ),
        "last_attempt_at": datetime.now(timezone.utc).isoformat(),
    }
    return {
        **ebay_draft_status,
        "total_batch_items": len(all_drafts),
        "next_offset": draft_request.offset + len(selected_drafts),
        "results": results,
    }


@app.post("/inventory/sync/store-page")
async def sync_default_store_page(
    request: Request,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    return await store_syncer.sync()


@app.post("/inventory/import/ebay-store-page")
async def import_ebay_store_page(
    import_request: EbayStoreImportRequest,
    request: Request,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    return await store_syncer.sync(import_request.store_url, import_request.max_pages)


@app.get("/walmart/status")
async def walmart_status(
    request: Request,
    test_auth: bool = False,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    status: dict[str, Any] = {
        "configured": walmart_client.configured,
        "environment": "sandbox" if "sandbox" in walmart_client.base_url.lower() else "production",
        "market": settings.walmart_market,
        "offer_feed_type": "MP_ITEM_MATCH",
        "offer_spec_version": "4.2",
        "last_sync": walmart_sync_status,
    }
    if test_auth:
        try:
            status["authentication"] = await walmart_client.verify_credentials()
        except WalmartApiError as exc:
            raise _walmart_http_error(exc) from exc
    return status


@app.get("/walmart/drafts/summary")
def walmart_drafts_summary() -> dict[str, Any]:
    return {
        "status": walmart_draft_status,
        "stored": repository.walmart_draft_summary(),
        "storage": "render_database",
        "walmart_feed_submitted": False,
        "note": (
            "Walmart Marketplace APIs do not expose Seller Center draft creation. "
            "These API-enriched records remain staged until a separate confirmed publish request."
        ),
    }


@app.get("/walmart/unpublished/summary")
def walmart_unpublished_summary() -> dict[str, Any]:
    job = repository.latest_walmart_unpublished_job()
    return {
        "job": job,
        "authorized_batch_id": str(settings.walmart_unpublished_batch_id or "") or None,
        "target_inventory_quantity": 0,
        "seller_center_destination": "Unpublished",
        "safety": (
            "Only exact brand, model, variation, and identifier matches are submitted. "
            "Ambiguous candidates are skipped."
        ),
    }


@app.get("/walmart/unpublished/feeds")
async def walmart_unpublished_feeds() -> dict[str, Any]:
    job = repository.latest_walmart_unpublished_job()
    if not job:
        return {"job": None, "offer_feed": None, "inventory_feed": None}
    result: dict[str, Any] = {"job": job, "offer_feed": None, "inventory_feed": None}
    for result_key, job_key in (
        ("offer_feed", "offer_feed_id"),
        ("inventory_feed", "inventory_feed_id"),
    ):
        feed_id = job.get(job_key)
        if not feed_id:
            continue
        try:
            feed = await walmart_client.get_feed_status(str(feed_id), include_details=False)
            result[result_key] = {
                key: feed.get(key)
                for key in (
                    "feedId",
                    "feedType",
                    "feedStatus",
                    "itemsReceived",
                    "itemsSucceeded",
                    "itemsFailed",
                    "itemsProcessing",
                    "itemDataErrorCount",
                    "itemSystemErrorCount",
                    "itemTimeoutErrorCount",
                )
                if key in feed
            }
        except WalmartApiError as exc:
            result[result_key] = {"status": "lookup_failed", "http_status": exc.status_code}
    return result


@app.get("/walmart/drafts")
def walmart_drafts(
    request: Request,
    sku: str | None = None,
    limit: int = 200,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    drafts = repository.walmart_drafts([sku] if sku else None, limit=limit)
    return {
        "total": len(drafts),
        "storage": "render_database",
        "walmart_feed_submitted": False,
        "drafts": drafts,
    }


@app.post("/walmart/drafts/generate")
async def generate_walmart_drafts(
    draft_request: WalmartDraftGenerateRequest,
    request: Request,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    return await _generate_walmart_drafts(draft_request)


@app.post("/walmart/import/preview")
async def preview_walmart_import(
    import_request: WalmartImportRequest,
    request: Request,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    return await _prepare_walmart_import(import_request)


@app.post("/walmart/import/submit")
async def submit_walmart_import(
    import_request: WalmartImportRequest,
    request: Request,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    global walmart_sync_status
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    if not import_request.confirm:
        raise HTTPException(
            status_code=400,
            detail="Set confirm=true after reviewing /walmart/import/preview to submit a live Walmart offer feed.",
        )
    if not walmart_client.configured:
        raise HTTPException(
            status_code=503,
            detail="WALMART_CLIENT_ID and WALMART_CLIENT_SECRET are not configured.",
        )

    preview = await _prepare_walmart_import(import_request, force_verify_catalog=True)
    if import_request.sync_ebay_first and (preview.get("ebay_sync") or {}).get("status") != "ok":
        raise HTTPException(
            status_code=503,
            detail={
                "message": "The requested eBay refresh did not complete, so no Walmart offer feed was submitted.",
                "ebay_sync": preview.get("ebay_sync"),
            },
        )
    if not preview["ready"]:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "No eBay listings passed Walmart preflight.",
                "preflight": preview,
            },
        )
    try:
        submission = await walmart_client.submit_offer_match_feed(preview["payload"])
    except WalmartApiError as exc:
        walmart_sync_status = {
            "status": "failed",
            "configured": walmart_client.configured,
            "last_submission": None,
            "message": str(exc),
            "last_attempt_at": datetime.now(timezone.utc).isoformat(),
        }
        raise _walmart_http_error(exc) from exc

    walmart_sync_status = {
        "status": "submitted",
        "configured": True,
        "last_submission": submission,
        "submitted_items": preview["ready"],
        "last_attempt_at": datetime.now(timezone.utc).isoformat(),
    }
    return {
        **submission,
        "submitted_items": preview["ready"],
        "blocked_items": preview["blocked"],
        "items": preview["items"],
        "next_step": f"Check /walmart/feeds/{submission['feed_id']} until the feed is PROCESSED.",
    }


@app.get("/walmart/feeds/{feed_id:path}")
async def walmart_feed_status(
    feed_id: str,
    request: Request,
    include_details: bool = True,
    offset: int = 0,
    limit: int = 50,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    try:
        return await walmart_client.get_feed_status(
            feed_id,
            include_details=include_details,
            offset=offset,
            limit=limit,
        )
    except WalmartApiError as exc:
        raise _walmart_http_error(exc) from exc


@app.post("/walmart/inventory/sync")
async def sync_walmart_inventory(
    sync_request: WalmartInventorySyncRequest,
    request: Request,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    global walmart_sync_status
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    if not sync_request.confirm:
        raise HTTPException(
            status_code=400,
            detail="Set confirm=true to overwrite Walmart inventory quantities with the current eBay snapshot.",
        )
    if not walmart_client.configured:
        raise HTTPException(
            status_code=503,
            detail="WALMART_CLIENT_ID and WALMART_CLIENT_SECRET are not configured.",
        )

    ebay_refresh: dict[str, Any] | None = None
    if sync_request.sync_ebay_first:
        ebay_refresh = await _sync_ebay_api_inventory()
        if ebay_refresh.get("status") != "ok":
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "The eBay refresh did not complete, so Walmart quantities were not changed.",
                    "ebay_sync": ebay_refresh,
                },
            )

    items = repository.ebay_items(
        sync_request.skus,
        limit=sync_request.max_items,
        include_inactive=sync_request.include_zero_quantity,
    )
    if not items:
        raise HTTPException(status_code=422, detail="No eBay inventory rows matched the Walmart inventory sync request.")
    payload = build_inventory_feed(items)
    try:
        submission = await walmart_client.submit_inventory_feed(payload)
    except WalmartApiError as exc:
        raise _walmart_http_error(exc) from exc

    walmart_sync_status = {
        "status": "inventory_submitted",
        "configured": True,
        "last_submission": submission,
        "submitted_items": len(items),
        "last_attempt_at": datetime.now(timezone.utc).isoformat(),
    }
    return {
        **submission,
        "submitted_items": len(items),
        "ebay_sync": ebay_refresh,
        "inventory": payload["Inventory"],
    }


@app.post("/agent/customer-answer", response_model=dict[str, Any])
async def customer_answer(question: CustomerQuestion) -> dict[str, Any]:
    answer = await answer_customer_question(question)
    return answer.model_dump()


@app.post("/agent/social-drafts", response_model=dict[str, Any])
async def social_drafts(request: SocialDraftRequest) -> dict[str, Any]:
    batch, inventory_refresh = await _create_social_drafts_with_inventory_refresh(request)
    response = batch.model_dump()
    response["inventory_refresh"] = inventory_refresh
    return response


@app.post("/webhooks/metricool/schedule-inventory")
async def schedule_inventory_rotation(
    request: Request,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    if not isinstance(body, dict):
        body = {}

    draft_request = SocialDraftRequest(
        promote_all_inventory=True,
        ignore_recent_history=bool(body.get("ignore_recent_history", False)),
        query="all inventory",
        max_products_per_run=min(max(int(body.get("max_products_per_run", 200)), 1), 200),
        platforms=["facebook", "instagram", "tiktok", "linkedin"],
        cross_post_to_all_platforms=True,
        brand_name=settings.metricool_brand_label,
        store_url=settings.ebay_store_url,
        sale_media_url=settings.ebay_store_sale_media_url,
        publish_after=str(body.get("publish_after") or "").strip() or None,
        as_draft=False,
        auto_publish=True,
    )
    batch, inventory_refresh = await _create_social_drafts_with_inventory_refresh(draft_request)
    try:
        results = await schedule_metricool_payloads(batch.metricool_payloads, settings=settings)
    except MetricoolPublishError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    _update_metricool_history(batch.metricool_payloads, results)
    scheduled = sum(1 for result in results if result.get("status") == "scheduled")
    failed = sum(1 for result in results if result.get("status") == "failed")
    return {
        "status": "ok" if not failed else "partial",
        "campaign_name": batch.campaign_name,
        "generated": len(batch.metricool_payloads),
        "scheduled": scheduled,
        "failed": failed,
        "inventory_refresh": inventory_refresh,
        "results": results,
        "notes": batch.notes,
    }


@app.post("/agent/slow-mover-outreach", response_model=dict[str, Any])
async def slow_mover_outreach(request: SlowMoverOutreachRequest) -> dict[str, Any]:
    inventory_refresh = await _refresh_inventory_for_social_posts()
    plan = create_slow_mover_outreach(request)
    response = plan.model_dump()
    response["inventory_refresh"] = inventory_refresh
    return response


@app.post("/agent/group-outreach-plan", response_model=dict[str, Any])
async def group_outreach_plan(request: GroupOutreachRequest) -> dict[str, Any]:
    plan = await create_group_outreach_plan(request)
    return plan.model_dump()


@app.post("/agent/group-reply", response_model=dict[str, Any])
async def group_reply(request: GroupReplyRequest) -> dict[str, Any]:
    draft = await draft_group_reply(request)
    return draft.model_dump()


@app.post("/webhooks/manychat")
async def manychat_webhook(
    request: Request,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    payload = await parse_zapier_body(request)
    message = extract_customer_message(payload)
    if not message:
        raise HTTPException(status_code=400, detail="No customer message found in payload.")
    await _refresh_inventory_for_social_posts()
    question = _customer_question_from_payload(payload, message)
    answer = await answer_customer_question(question)
    _log_customer_inquiry("manychat", question, answer)
    return manychat_dynamic_response(answer)


@app.post("/webhooks/zapier/customer-question")
async def zapier_customer_question(
    request: Request,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    payload = await parse_zapier_body(request)
    message = extract_customer_message(payload)
    if not message:
        raise HTTPException(status_code=400, detail="No customer message found in payload.")
    await _refresh_inventory_for_social_posts()
    question = _customer_question_from_payload(payload, message)
    answer = await answer_customer_question(question)
    _log_customer_inquiry("zapier_customer_question", question, answer)
    return answer.model_dump()


@app.post("/webhooks/zapier/facebook-comment-auto-reply")
async def zapier_facebook_comment_auto_reply(
    request: Request,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    payload = await parse_zapier_body(request)
    message = extract_customer_message(payload)
    if not message:
        raise HTTPException(status_code=400, detail="No Facebook comment text found in payload.")

    comment_id = _facebook_comment_id_from_payload(payload)
    if not comment_id:
        raise HTTPException(status_code=400, detail="No Facebook comment_id found in payload.")

    if _is_facebook_page_self_comment(payload):
        return {
            "status": "skipped",
            "skipped": True,
            "reason": "Skipped Horizon Wireless page/admin comment.",
            "comment_id": comment_id,
            "reply": "",
            "facebook_comment_reply_status": "skipped",
        }

    await _refresh_inventory_for_social_posts()
    question = _customer_question_from_payload(payload, message)
    answer = await answer_customer_question(question)
    _log_customer_inquiry("zapier_facebook_comment_auto_reply", question, answer)
    facebook_reply = await _post_facebook_comment_reply(comment_id, answer.reply)
    response = answer.model_dump()
    response.update(
        {
            "status": "posted",
            "skipped": False,
            "comment_id": comment_id,
            "facebook_comment_reply_status": "posted",
            "facebook_comment_reply_id": facebook_reply.get("id"),
            "facebook_comment_id_used": facebook_reply.get("comment_id_used"),
            "facebook_comment_reply_endpoint": facebook_reply.get("graph_endpoint"),
            "facebook_graph_response": facebook_reply,
        }
    )
    return response


@app.get("/webhooks/meta/facebook")
async def meta_facebook_webhook_verify(request: Request) -> Response:
    mode = request.query_params.get("hub.mode")
    verify_token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    expected_token = settings.facebook_webhook_verify_token or settings.webhook_shared_secret
    if not expected_token:
        raise HTTPException(status_code=503, detail="FACEBOOK_WEBHOOK_VERIFY_TOKEN is not configured.")
    if mode == "subscribe" and verify_token == expected_token and challenge is not None:
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Invalid Facebook webhook verification token.")


@app.post("/webhooks/meta/facebook")
async def meta_facebook_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    raw_body = await request.body()
    _verify_facebook_webhook_signature(
        raw_body,
        request.headers.get("x-hub-signature-256") or request.headers.get("x-hub-signature"),
    )
    try:
        payload = json.loads(raw_body or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Facebook webhook payload must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Facebook webhook payload must be a JSON object.")

    comment_events = _facebook_comment_events_from_webhook(payload)
    messenger_events = _facebook_messenger_events_from_webhook(payload)
    queued = 0
    skipped = 0
    for event in comment_events:
        if _is_facebook_page_self_comment(event):
            skipped += 1
            logger.info("Meta Facebook webhook skipped page self-comment: comment_id=%s", event.get("comment_id"))
            continue
        background_tasks.add_task(_handle_meta_facebook_comment_event, event)
        queued += 1
    for event in messenger_events:
        if _is_facebook_page_self_message(event):
            skipped += 1
            logger.info("Meta Facebook webhook skipped page self-message: sender_id=%s", event.get("sender_id"))
            continue
        background_tasks.add_task(_handle_meta_facebook_messenger_event, event)
        queued += 1

    logger.info(
        "Meta Facebook webhook accepted: object=%s comment_events=%s messenger_events=%s queued=%s skipped=%s",
        payload.get("object"),
        len(comment_events),
        len(messenger_events),
        queued,
        skipped,
    )

    return {
        "status": "accepted",
        "object": payload.get("object"),
        "comment_events": len(comment_events),
        "messenger_events": len(messenger_events),
        "queued": queued,
        "skipped": skipped,
    }


@app.post("/webhooks/zapier/social-drafts")
async def zapier_social_drafts(
    request: Request,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    draft_request = SocialDraftRequest.model_validate(await parse_zapier_body(request))
    batch, inventory_refresh = await _create_social_drafts_with_inventory_refresh(draft_request)
    response = zapier_social_drafts_response(batch)
    response.update(_inventory_refresh_zapier_fields(inventory_refresh))
    return response


@app.post("/webhooks/zapier/slow-mover-outreach")
async def zapier_slow_mover_outreach(
    request: Request,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    outreach_request = SlowMoverOutreachRequest.model_validate(await parse_zapier_body(request))
    inventory_refresh = await _refresh_inventory_for_social_posts()
    plan = create_slow_mover_outreach(outreach_request)
    response = _zapier_slow_mover_outreach_response(plan)
    response.update(_inventory_refresh_zapier_fields(inventory_refresh))
    return response


@app.post("/webhooks/zapier/group-reply")
async def zapier_group_reply(
    request: Request,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    reply_request = GroupReplyRequest.model_validate(await parse_zapier_body(request))
    draft = await draft_group_reply(reply_request)
    return draft.model_dump()


@app.post("/webhooks/metricool/inbox")
async def metricool_inbox_webhook(
    request: Request,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    payload = await parse_zapier_body(request)
    message = extract_customer_message(payload)
    if not message:
        raise HTTPException(status_code=400, detail="No conversation text found in payload.")
    await _refresh_inventory_for_social_posts()
    question = _customer_question_from_payload(payload, message, channel_key="provider", user_key="recipient")
    answer = await answer_customer_question(question)
    _log_customer_inquiry("metricool_inbox", question, answer)
    return {
        "reply": answer.reply,
        "needs_human": answer.needs_human,
        "matched_items": [item.model_dump() for item in answer.matched_items],
    }


async def parse_zapier_body(request: Request) -> dict[str, Any]:
    raw_body = await request.body()
    if not raw_body:
        return {}

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        payload: Any = json.loads(raw_body)
    else:
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            form = await request.form()
            payload = dict(form)

    if isinstance(payload, str):
        payload = json.loads(payload)

    if isinstance(payload, dict) and isinstance(payload.get("data"), str):
        payload = json.loads(payload["data"])

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Zapier payload must be a JSON object.")

    return payload


def _customer_question_from_payload(
    payload: dict[str, Any],
    message: str,
    *,
    channel_key: str = "channel",
    user_key: str = "user_id",
) -> CustomerQuestion:
    metadata = _customer_metadata_from_payload(payload)
    user_id = (
        payload.get(user_key)
        or payload.get("subscriber_id")
        or payload.get("user_id")
        or payload.get("profile_id")
        or payload.get("sender_id")
        or ""
    )
    conversation_id = payload.get("conversation_id") or payload.get("conversation") or payload.get("thread_id")
    if conversation_id is not None:
        metadata["conversation_id"] = str(conversation_id)
    return CustomerQuestion(
        message=message,
        channel=normalize_channel(payload.get(channel_key) or payload.get("platform") or payload.get("channel")),
        user_id=str(user_id),
        first_name=str(payload.get("first_name") or payload.get("name") or ""),
        metadata=metadata,
    )


def _customer_metadata_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(value, (str, int, float, bool)):
            metadata[str(key)] = str(value)
    custom_fields = payload.get("custom_fields")
    if isinstance(custom_fields, dict):
        for key, value in custom_fields.items():
            if isinstance(value, (str, int, float, bool)):
                metadata[str(key)] = str(value)
    return metadata


def _facebook_comment_id_from_payload(payload: dict[str, Any]) -> str:
    direct_keys = (
        "comment_id",
        "facebook_comment_id",
        "commentId",
        "commentID",
        "comment id",
        "id",
    )
    for key in direct_keys:
        value = payload.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()

    custom_fields = payload.get("custom_fields")
    if isinstance(custom_fields, dict):
        for key in direct_keys:
            value = custom_fields.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
    return ""


def _facebook_comment_events_from_webhook(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("object") != "page":
        return []

    events: list[dict[str, Any]] = []
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return events

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        page_id = str(entry.get("id") or "")
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict) or change.get("field") != "feed":
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            if str(value.get("item") or "").lower() != "comment":
                continue
            if str(value.get("verb") or "").lower() not in {"add", "edited"}:
                continue

            comment_id = _facebook_comment_id_from_webhook_value(value)
            message = _facebook_comment_message_from_webhook_value(value)
            if not comment_id or not message:
                continue

            author = value.get("from")
            author_id = ""
            author_name = ""
            if isinstance(author, dict):
                author_id = str(author.get("id") or "")
                author_name = str(author.get("name") or "")

            post_id = str(value.get("post_id") or "")
            parent_id = str(value.get("parent_id") or "")
            event = {
                "message": message,
                "channel": "facebook",
                "page_id": page_id,
                "post_id": post_id,
                "comment_id": comment_id,
                "parent_id": parent_id,
                "commenter_id": author_id,
                "from_id": author_id,
                "from_name": author_name,
                "user_id": author_id,
                "subscriber_id": author_id,
                "first_name": author_name,
                "custom_fields": {
                    "facebook_page_id": page_id,
                    "facebook_post_id": post_id,
                    "facebook_comment_id": comment_id,
                    "facebook_parent_id": parent_id,
                },
            }
            events.append(event)
    return events


def _facebook_comment_id_from_webhook_value(value: dict[str, Any]) -> str:
    for key in ("comment_id", "id"):
        comment_id = value.get(key)
        if isinstance(comment_id, (str, int)) and str(comment_id).strip():
            return str(comment_id).strip()
    return ""


def _facebook_comment_message_from_webhook_value(value: dict[str, Any]) -> str:
    for key in ("message", "text"):
        message = value.get(key)
        if isinstance(message, str) and message.strip():
            return message.strip()
    return ""


def _facebook_messenger_events_from_webhook(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("object") != "page":
        return []

    events: list[dict[str, Any]] = []
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return events

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        page_id = str(entry.get("id") or "")
        messaging_events = entry.get("messaging")
        if not isinstance(messaging_events, list):
            continue

        for messaging_event in messaging_events:
            if not isinstance(messaging_event, dict):
                continue
            message_data = messaging_event.get("message")
            postback_data = messaging_event.get("postback")
            if isinstance(message_data, dict) and message_data.get("is_echo"):
                continue
            if not isinstance(message_data, dict) and not isinstance(postback_data, dict):
                continue

            message = _facebook_messenger_message_text(messaging_event)
            if not message:
                continue

            sender_id = _facebook_messenger_party_id(messaging_event.get("sender"))
            recipient_id = _facebook_messenger_party_id(messaging_event.get("recipient")) or page_id
            if not sender_id:
                continue

            mid = ""
            if isinstance(message_data, dict):
                mid = str(message_data.get("mid") or "")
            event = {
                "message": message,
                "channel": "messenger",
                "page_id": page_id or recipient_id,
                "recipient_id": recipient_id,
                "sender_id": sender_id,
                "user_id": sender_id,
                "subscriber_id": sender_id,
                "conversation_id": mid or sender_id,
                "messenger_mid": mid,
                "custom_fields": {
                    "facebook_page_id": page_id or recipient_id,
                    "messenger_sender_id": sender_id,
                    "messenger_recipient_id": recipient_id,
                    "messenger_mid": mid,
                },
            }
            events.append(event)
    return events


def _facebook_messenger_message_text(messaging_event: dict[str, Any]) -> str:
    message_data = messaging_event.get("message")
    if isinstance(message_data, dict):
        text = message_data.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()

    postback_data = messaging_event.get("postback")
    if isinstance(postback_data, dict):
        for key in ("title", "payload"):
            text = postback_data.get(key)
            if isinstance(text, str) and text.strip():
                return text.strip()
    return ""


def _facebook_messenger_party_id(value: Any) -> str:
    if isinstance(value, dict):
        party_id = value.get("id")
        if isinstance(party_id, (str, int)) and str(party_id).strip():
            return str(party_id).strip()
    return ""


def _is_facebook_page_self_comment(payload: dict[str, Any]) -> bool:
    configured_page_id = str(settings.facebook_page_id or "").strip()
    configured_page_name = settings.facebook_page_name.strip().casefold()
    id_keys = ("commenter_id", "from_id", "user_id", "subscriber_id")
    name_keys = ("commenter_name", "from_name", "first_name", "name", "author_name")

    if configured_page_id:
        for key in id_keys:
            value = payload.get(key)
            if isinstance(value, (str, int)) and str(value).strip() == configured_page_id:
                return True

    if configured_page_name:
        for key in name_keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip().casefold() == configured_page_name:
                return True

    return False


def _is_facebook_page_self_message(payload: dict[str, Any]) -> bool:
    configured_page_id = str(settings.facebook_page_id or "").strip()
    page_id = str(payload.get("page_id") or payload.get("recipient_id") or "").strip()
    sender_id = str(payload.get("sender_id") or payload.get("user_id") or payload.get("subscriber_id") or "").strip()
    if configured_page_id and sender_id == configured_page_id:
        return True
    if page_id and sender_id == page_id:
        return True
    return False


async def _handle_meta_facebook_comment_event(event: dict[str, Any]) -> None:
    comment_id = _facebook_comment_id_from_payload(event)
    message = extract_customer_message(event)
    try:
        if not comment_id or not message:
            logger.warning("Meta Facebook webhook skipped invalid comment event: %s", event)
            return
        await _refresh_inventory_for_social_posts()
        question = _customer_question_from_payload(event, message)
        answer = await answer_customer_question(question)
        _log_customer_inquiry("meta_facebook_webhook", question, answer)
        facebook_reply = await _post_facebook_comment_reply(comment_id, answer.reply)
        logger.info(
            "Meta Facebook comment reply posted: comment_id=%s reply_id=%s endpoint=%s",
            comment_id,
            facebook_reply.get("id"),
            facebook_reply.get("graph_endpoint"),
        )
    except Exception:
        logger.exception("Meta Facebook comment reply failed: comment_id=%s", comment_id)


async def _handle_meta_facebook_messenger_event(event: dict[str, Any]) -> None:
    sender_id = str(event.get("sender_id") or event.get("user_id") or event.get("subscriber_id") or "").strip()
    message = extract_customer_message(event)
    try:
        if not sender_id or not message:
            logger.warning("Meta Facebook webhook skipped invalid Messenger event: %s", event)
            return
        await _refresh_inventory_for_social_posts()
        question = _customer_question_from_payload(event, message)
        answer = await answer_customer_question(question)
        _log_customer_inquiry("meta_facebook_messenger", question, answer)
        messenger_reply = await _send_facebook_messenger_reply(sender_id, answer.reply)
        logger.info(
            "Meta Facebook Messenger reply sent: sender_id=%s message_id=%s endpoint=%s",
            sender_id,
            messenger_reply.get("message_id") or messenger_reply.get("id"),
            messenger_reply.get("graph_endpoint"),
        )
    except Exception:
        logger.exception("Meta Facebook Messenger reply failed: sender_id=%s", sender_id)


async def _post_facebook_comment_reply(comment_id: str, reply: str) -> dict[str, Any]:
    token = settings.facebook_page_access_token
    if not token:
        raise HTTPException(status_code=503, detail="FACEBOOK_PAGE_ACCESS_TOKEN is not configured.")

    api_version = settings.facebook_graph_api_version.strip().strip("/")
    message = reply[:1900]
    attempts: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=20) as client:
        for candidate_id in _facebook_comment_id_candidates(comment_id):
            url = f"https://graph.facebook.com/{api_version}/{candidate_id}/comments"
            for attempt_number in range(3):
                try:
                    response = await client.post(
                        url,
                        json={"message": message},
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    result = payload if isinstance(payload, dict) else {"response": payload}
                    result["comment_id_used"] = candidate_id
                    result["graph_endpoint"] = url
                    result["attempts"] = attempts
                    return result
                except httpx.HTTPStatusError as exc:
                    status_code = exc.response.status_code
                    detail = _facebook_error_detail(exc.response)
                    attempts.append(
                        {
                            "comment_id": candidate_id,
                            "graph_endpoint": url,
                            "status_code": status_code,
                            "error": detail,
                        }
                    )
                    if status_code in {408, 425, 429, 500, 502, 503, 504} and attempt_number < 2:
                        await asyncio.sleep(0.5 * (2**attempt_number))
                        continue
                    break
                except httpx.HTTPError as exc:
                    attempts.append(
                        {
                            "comment_id": candidate_id,
                            "graph_endpoint": url,
                            "status_code": None,
                            "error": str(exc),
                        }
                    )
                    if attempt_number < 2:
                        await asyncio.sleep(0.5 * (2**attempt_number))
                        continue
                    break

    raise HTTPException(
        status_code=502,
        detail={
            "message": "Facebook comment reply failed.",
            "attempts": attempts,
            "required_permissions": ["pages_read_engagement", "pages_manage_engagement"],
        },
    )


async def _send_facebook_messenger_reply(recipient_id: str, reply: str) -> dict[str, Any]:
    token = settings.facebook_page_access_token
    if not token:
        raise HTTPException(status_code=503, detail="FACEBOOK_PAGE_ACCESS_TOKEN is not configured.")

    api_version = settings.facebook_graph_api_version.strip().strip("/")
    url = f"https://graph.facebook.com/{api_version}/me/messages"
    message = reply[:1900]
    attempts: list[dict[str, Any]] = []
    body = {
        "recipient": {"id": recipient_id},
        "messaging_type": "RESPONSE",
        "message": {"text": message},
    }
    async with httpx.AsyncClient(timeout=20) as client:
        for attempt_number in range(3):
            try:
                response = await client.post(
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                )
                response.raise_for_status()
                payload = response.json()
                result = payload if isinstance(payload, dict) else {"response": payload}
                result["recipient_id"] = recipient_id
                result["graph_endpoint"] = url
                result["attempts"] = attempts
                return result
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                detail = _facebook_error_detail(exc.response)
                attempts.append({"graph_endpoint": url, "status_code": status_code, "error": detail})
                if status_code in {408, 425, 429, 500, 502, 503, 504} and attempt_number < 2:
                    await asyncio.sleep(0.5 * (2**attempt_number))
                    continue
                break
            except httpx.HTTPError as exc:
                attempts.append({"graph_endpoint": url, "status_code": None, "error": str(exc)})
                if attempt_number < 2:
                    await asyncio.sleep(0.5 * (2**attempt_number))
                    continue
                break

    raise HTTPException(
        status_code=502,
        detail={
            "message": "Facebook Messenger reply failed.",
            "attempts": attempts,
            "required_permissions": ["pages_messaging"],
        },
    )


def _facebook_comment_id_candidates(comment_id: str) -> list[str]:
    raw_id = str(comment_id).strip()
    candidates = [raw_id]
    parts = [part for part in raw_id.split("_") if part]
    if len(parts) > 1:
        candidates.append(parts[-1])
    if len(parts) > 2:
        candidates.append("_".join(parts[-2:]))

    unique_candidates: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in unique_candidates:
            unique_candidates.append(candidate)
    return unique_candidates


def _verify_facebook_webhook_signature(raw_body: bytes, signature_header: str | None) -> None:
    app_secret = settings.facebook_app_secret
    if not app_secret:
        return
    if not signature_header or "=" not in signature_header:
        logger.warning("Meta Facebook webhook rejected: missing signature header.")
        raise HTTPException(status_code=401, detail="Missing Facebook webhook signature.")

    algorithm_name, received_signature = signature_header.split("=", maxsplit=1)
    algorithm_name = algorithm_name.lower().strip()
    if algorithm_name == "sha256":
        digestmod = hashlib.sha256
    elif algorithm_name == "sha1":
        digestmod = hashlib.sha1
    else:
        logger.warning("Meta Facebook webhook rejected: unsupported signature algorithm=%s.", algorithm_name)
        raise HTTPException(status_code=401, detail="Unsupported Facebook webhook signature algorithm.")

    expected_signature = hmac.new(app_secret.encode("utf-8"), raw_body, digestmod).hexdigest()
    if not secrets.compare_digest(received_signature, expected_signature):
        logger.warning("Meta Facebook webhook rejected: invalid %s signature.", algorithm_name)
        raise HTTPException(status_code=401, detail="Invalid Facebook webhook signature.")


def _facebook_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            code = error.get("code")
            if message and code:
                return f"{message} (code {code})"
            if message:
                return str(message)
        return json.dumps(payload)[:500]
    return str(payload)[:500]


def _log_customer_inquiry(source: str, question: CustomerQuestion, answer: CustomerAnswer) -> None:
    matched_item = answer.matched_items[0] if answer.matched_items else None
    logger.info(
        "%s inquiry handled: customer_name=%r profile_id=%s post_id=%s conversation_id=%s incoming=%r "
        "matched_ebay_item_id=%s response=%r product_url=%s recommendations=%s stayed_in_messenger=%s "
        "redirected_to_ebay=%s needs_human=%s success=True",
        source,
        question.first_name,
        question.user_id,
        answer.social_post_id or question.metadata.get("post_id"),
        answer.messenger_conversation_id or question.metadata.get("conversation_id"),
        question.message[:500],
        answer.ebay_item_id or (matched_item.ebay_item_id if matched_item else None),
        answer.reply[:500],
        answer.ebay_listing_url or (matched_item.ebay_url if matched_item else None),
        [_item.ebay_item_id for _item in answer.recommended_items],
        answer.conversation_allowed,
        answer.redirect_to_ebay,
        answer.needs_human,
    )


async def _build_daily_report(report_date: date | None = None) -> dict[str, Any]:
    report = await build_daily_metricool_report(report_date)
    report["inventory"] = {"total_items": repository.count(), "store_sync": store_syncer.last_status}
    return report


async def _create_social_drafts_with_inventory_refresh(
    request: SocialDraftRequest,
) -> tuple[SocialDraftBatch, dict[str, Any]]:
    inventory_refresh = await _refresh_inventory_for_social_posts()
    if request.promote_all_inventory and inventory_refresh.get("status") != "ok":
        refresh_message = inventory_refresh.get("message")
        if not isinstance(refresh_message, str) or not refresh_message:
            refresh_message = "A fresh eBay API inventory sync did not complete."
        return (
            SocialDraftBatch(
                campaign_name="Daily all-inventory promotion",
                posts=[],
                notes=(
                    "Skipped automated inventory posts because the latest eBay API inventory "
                    f"was not confirmed. {refresh_message}"
                ),
            ),
            inventory_refresh,
        )
    batch = await create_social_drafts(request)
    _append_inventory_refresh_note(batch, inventory_refresh)
    return batch, inventory_refresh


def _update_metricool_history(
    payloads: list[dict[str, object]],
    results: list[dict[str, object]],
) -> None:
    for payload, result in zip(payloads, results, strict=False):
        scheduled_at = str(payload.get("publication_date_time") or payload.get("publicationDate") or "")
        if not scheduled_at:
            continue
        sku = str(payload.get("product_sku") or "") or None
        item = repository.get(sku) if sku else None
        platforms = ",".join(
            network
            for network in ("facebook", "instagram", "tiktok", "linkedin")
            if payload.get(network)
        ) or "unknown"
        repository.record_social_post(
            ebay_item_id=item.ebay_item_id if item else None,
            sku=sku,
            title=str(payload.get("product_title") or "Horizon Wireless eBay listing"),
            item_url=str(payload.get("ebay_url") or "") or None,
            image_url=str(payload.get("media_01") or "") or None,
            caption=str(payload.get("post_content") or ""),
            scheduled_at=scheduled_at,
            platform=platforms,
            metricool_post_id=str(result.get("metricool_post_id") or "") or None,
            status=str(result.get("status") or "failed"),
            error_message=str(result.get("error") or "") or None,
        )


async def _refresh_inventory_for_social_posts() -> dict[str, Any]:
    if not settings.sync_inventory_before_social_posts:
        return {
            "source": "pre-social-refresh",
            "status": "skipped",
            "message": "Automatic inventory refresh before social posts is disabled.",
            "ebay_sync": ebay_sync_status,
            "store_sync": store_syncer.last_status,
        }

    api_status = await _sync_ebay_api_inventory()
    store_status = store_syncer.last_status
    if api_status.get("status") == "ok":
        return {
            "source": "pre-social-refresh",
            "status": "ok",
            "message": "Inventory refreshed from the eBay API before social posts were generated.",
            "ebay_sync": api_status,
            "store_sync": store_status,
        }

    store_status = await store_syncer.sync()
    if store_status.get("status") == "ok":
        return {
            "source": "pre-social-refresh",
            "status": "fallback_ok",
            "message": "eBay API refresh did not complete; inventory refreshed from the public eBay store page fallback.",
            "ebay_sync": api_status,
            "store_sync": store_status,
        }

    if store_status.get("status") in {"cached", "fallback"}:
        return {
            "source": "pre-social-refresh",
            "status": str(store_status.get("status")),
            "message": "Inventory refresh did not complete; social posts used the best available cached inventory.",
            "ebay_sync": api_status,
            "store_sync": store_status,
        }

    return {
        "source": "pre-social-refresh",
        "status": "failed",
        "message": "Inventory refresh failed before social posts were generated; cached inventory was used if available.",
        "ebay_sync": api_status,
        "store_sync": store_status,
    }


def _append_inventory_refresh_note(batch: SocialDraftBatch, inventory_refresh: dict[str, Any]) -> None:
    message = inventory_refresh.get("message")
    if not isinstance(message, str) or not message:
        return
    separator = " " if batch.notes else ""
    batch.notes = f"{batch.notes}{separator}{message}"


def _inventory_refresh_zapier_fields(inventory_refresh: dict[str, Any]) -> dict[str, Any]:
    ebay_sync = inventory_refresh.get("ebay_sync")
    if not isinstance(ebay_sync, dict):
        ebay_sync = {}
    store_sync = inventory_refresh.get("store_sync")
    if not isinstance(store_sync, dict):
        store_sync = {}
    return {
        "inventory_refresh_status": inventory_refresh.get("status"),
        "inventory_refresh_message": inventory_refresh.get("message"),
        "inventory_refresh_source": inventory_refresh.get("source"),
        "ebay_sync_status": ebay_sync.get("status"),
        "ebay_sync_message": ebay_sync.get("message"),
        "ebay_sync_imported": ebay_sync.get("imported"),
        "ebay_sync_last_attempt_at": ebay_sync.get("last_attempt_at"),
        "store_sync_status": store_sync.get("status"),
        "store_sync_message": store_sync.get("message"),
        "store_sync_imported": store_sync.get("imported"),
        "store_sync_last_attempt_at": store_sync.get("last_attempt_at"),
    }


def _zapier_slow_mover_outreach_response(plan: SlowMoverOutreachPlan) -> dict[str, Any]:
    response = plan.model_dump()
    social_fields = zapier_social_drafts_response(
        SocialDraftBatch(
            campaign_name=plan.campaign_name,
            posts=plan.posts,
            metricool_payloads=plan.metricool_payloads,
            notes=plan.notes,
        )
    )
    response.update(
        {
            key: value
            for key, value in social_fields.items()
            if key not in {"campaign_name", "posts", "metricool_payloads", "notes"}
        }
    )
    response["slow_mover_count"] = len(plan.drafts)
    response["slow_mover_sku_items"] = [draft.sku for draft in plan.drafts]
    response["slow_mover_reason_items"] = [draft.reason for draft in plan.drafts]
    response["comment_keyword_items"] = [draft.comment_keyword for draft in plan.drafts]
    response["manychat_reply_items"] = [draft.manychat_reply for draft in plan.drafts]
    return response


def _gmail_oauth_redirect_uri() -> str:
    return f"{settings.public_base_url.rstrip('/')}/oauth2callback"


def _ebay_oauth_credentials() -> tuple[str, str, str]:
    client_id = str(settings.ebay_client_id or "").strip()
    client_secret = str(settings.ebay_client_secret or "").strip()
    redirect_name = str(settings.ebay_oauth_redirect_name or "").strip()
    if not client_id or not client_secret or not redirect_name:
        raise HTTPException(
            status_code=503,
            detail="EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, and EBAY_OAUTH_REDIRECT_NAME are required.",
        )
    return client_id, client_secret, redirect_name


def _exchange_ebay_authorization_code(code: str) -> dict[str, Any]:
    client_id, client_secret, redirect_name = _ebay_oauth_credentials()
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    try:
        response = httpx.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_name,
            },
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"eBay OAuth code exchange failed with status {exc.response.status_code}.",
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"eBay OAuth code exchange failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=503, detail="eBay OAuth code exchange returned an invalid response.")
    return payload


def _sign_ebay_oauth_state() -> str:
    payload = {
        "ts": int(time.time()),
        "nonce": secrets.token_urlsafe(18),
    }
    encoded_payload = _urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _ebay_oauth_state_signature(encoded_payload)
    return f"{encoded_payload}.{signature}"


def _verify_ebay_oauth_state(value: str) -> bool:
    try:
        encoded_payload, signature = value.rsplit(".", 1)
    except ValueError:
        return False
    expected_signature = _ebay_oauth_state_signature(encoded_payload)
    if not hmac.compare_digest(signature, expected_signature):
        return False
    try:
        payload = json.loads(_urlsafe_b64decode(encoded_payload).decode("utf-8"))
        issued_at = int(payload["ts"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False
    return 0 <= time.time() - issued_at <= EBAY_OAUTH_STATE_MAX_AGE_SECONDS


def _ebay_oauth_state_signature(encoded_payload: str) -> str:
    _, client_secret, _ = _ebay_oauth_credentials()
    digest = hmac.new(client_secret.encode("utf-8"), encoded_payload.encode("utf-8"), hashlib.sha256).digest()
    return _urlsafe_b64encode(digest)


def _sign_gmail_oauth_state() -> str:
    payload = {
        "ts": int(time.time()),
        "nonce": secrets.token_urlsafe(18),
    }
    encoded_payload = _urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _gmail_oauth_state_signature(encoded_payload)
    return f"{encoded_payload}.{signature}"


def _verify_gmail_oauth_state(value: str) -> bool:
    try:
        encoded_payload, signature = value.rsplit(".", 1)
    except ValueError:
        return False

    expected_signature = _gmail_oauth_state_signature(encoded_payload)
    if not hmac.compare_digest(signature, expected_signature):
        return False

    try:
        payload = json.loads(_urlsafe_b64decode(encoded_payload).decode("utf-8"))
        issued_at = int(payload["ts"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False

    return 0 <= time.time() - issued_at <= GMAIL_OAUTH_STATE_MAX_AGE_SECONDS


def _gmail_oauth_state_signature(encoded_payload: str) -> str:
    secret = _gmail_oauth_state_secret()
    digest = hmac.new(secret.encode("utf-8"), encoded_payload.encode("utf-8"), hashlib.sha256).digest()
    return _urlsafe_b64encode(digest)


def _gmail_oauth_state_secret() -> str:
    if settings.webhook_shared_secret:
        return settings.webhook_shared_secret
    return gmail_oauth_credentials(settings=settings).client_secret


def _diagnostic_clean_gmail_refresh_token(value: str) -> str:
    token = value.strip().strip("\"'")
    if "=" in token:
        key, candidate = token.split("=", 1)
        if key.strip() == "GMAIL_REFRESH_TOKEN_CURRENT":
            token = candidate.strip().strip("\"'")
    return token


def _diagnostic_hint(value: str) -> str:
    if len(value) <= 12:
        return "*" * len(value)
    return f"{value[:6]}...{value[-6:]}"


def _diagnostic_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _urlsafe_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_b64decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _gmail_oauth_success_html(refresh_token: str) -> str:
    escaped_token = escape(refresh_token)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="robots" content="noindex">
  <title>Gmail connected</title>
</head>
<body>
  <h1>Gmail connected</h1>
  <p>Copy this value into Render for the <code>horizon-ai-agents</code> web service.</p>
  <pre>GMAIL_REFRESH_TOKEN_CURRENT={escaped_token}</pre>
  <p>After saving the environment variable, trigger the daily report cron again.</p>
</body>
</html>"""


def _ebay_oauth_success_html(refresh_token: str) -> str:
    escaped_token = escape(refresh_token)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="robots" content="noindex">
  <title>eBay connected</title>
</head>
<body>
  <h1>eBay connected</h1>
  <p>Copy this value into Render for the <code>horizon-ai-agents</code> web service.</p>
  <pre>EBAY_REFRESH_TOKEN={escaped_token}</pre>
  <p>This page is not cached. Close it after saving the environment variable.</p>
</body>
</html>"""


def _parse_report_date(value: object) -> date | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="Report date must be a YYYY-MM-DD string.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Report date must use YYYY-MM-DD format.") from exc
