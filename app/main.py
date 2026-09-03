import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import date, datetime, timedelta, timezone
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
from app.marketplace_inventory_sync import MarketplaceInventorySyncer
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
    WalmartAutoPublishRequest,
    WalmartCatalogRepairRequest,
    WalmartImportRequest,
    WalmartDraftGenerateRequest,
    WalmartInventorySyncRequest,
    WalmartItemOverride,
    WalmartOfferFeedReconcileRequest,
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
from app.product_identifier_lookup import (
    OpenAIProductIdentifierLookup,
    ProductIdentifierLookupError,
    product_identifier_fingerprint,
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
    build_full_item_from_catalog_template,
    build_inventory_feed,
    build_offer_match_from_catalog_template,
    build_offer_match_preview,
    estimated_shipping_weight_lbs,
    normalize_product_identifier,
    plausible_catalog_candidates,
    select_verified_catalog_match,
    walmart_price,
)
from app.walmart_public_data import PUBLIC_CATALOG_IDENTIFIERS
from app.walmart_feed_reconciliation import (
    classify_walmart_inventory_result,
    classify_walmart_offer_result,
    walmart_feed_item_results,
)


GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_OAUTH_STATE_MAX_AGE_SECONDS = 15 * 60
EBAY_OAUTH_STATE_MAX_AGE_SECONDS = 15 * 60
settings = get_settings()
repository = InventoryRepository(settings.resolved_database_path)
store_syncer = StorePageSyncer(settings, repository)
walmart_client = WalmartMarketplaceClient(settings)
product_identifier_lookup = OpenAIProductIdentifierLookup(
    settings.openai_api_key,
    model=settings.walmart_gtin_lookup_model,
)
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
walmart_auto_publish_status: dict[str, Any] = {
    "status": "not_run",
    "enabled": settings.walmart_auto_publish_enabled,
    "interval_seconds": settings.walmart_auto_publish_interval_seconds,
    "catalog_limit": settings.walmart_auto_publish_catalog_limit,
    "last_attempt_at": None,
}
marketplace_inventory_sync_status: dict[str, Any] = {
    "status": "not_run",
    "enabled": settings.marketplace_inventory_sync_enabled,
    "interval_seconds": settings.marketplace_inventory_sync_interval_seconds,
    "last_attempt_at": None,
}
marketplace_inventory_sync_lock = asyncio.Lock()
walmart_auto_publish_lock = asyncio.Lock()
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
WALMART_OPEN_BOX_RETRY_MARKER = "walmart-open-box-fallback-2026-09-02-v4"
WALMART_OPEN_BOX_RETRY_DELAY_SECONDS = 60
WALMART_OPEN_BOX_RETRY_GTIN_LIMIT = 0


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
        "walmart_auto_publish": {
            **walmart_auto_publish_status,
            "stored": repository.walmart_draft_summary(),
        },
        "marketplace_inventory_sync": {
            **marketplace_inventory_sync_status,
            "stored": repository.marketplace_inventory_sync_summary(),
        },
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
    if settings.marketplace_inventory_sync_enabled:
        asyncio.create_task(_marketplace_inventory_sync_loop())
    if settings.walmart_auto_publish_enabled and walmart_client.configured:
        asyncio.create_task(_startup_walmart_open_box_retry())
        asyncio.create_task(_walmart_auto_publish_loop())


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


async def _marketplace_inventory_sync_loop() -> None:
    interval = max(30, int(settings.marketplace_inventory_sync_interval_seconds))
    await asyncio.sleep(interval)
    while True:
        try:
            await _run_marketplace_inventory_sync_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Marketplace inventory sync failed: %s", exc)
        await asyncio.sleep(interval)


async def _walmart_auto_publish_loop() -> None:
    initial_delay = max(60, int(settings.walmart_auto_publish_initial_delay_seconds))
    interval = max(900, int(settings.walmart_auto_publish_interval_seconds))
    await asyncio.sleep(initial_delay)
    while True:
        try:
            await _run_walmart_auto_publish_once(
                WalmartAutoPublishRequest(
                    max_items=max(1, min(int(settings.walmart_auto_publish_batch_size), 200)),
                    sync_ebay_first=True,
                    confirm=True,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Walmart automatic draft publishing failed: %s", exc)
        await asyncio.sleep(interval)


async def _startup_walmart_open_box_retry() -> None:
    if WALMART_OPEN_BOX_RETRY_DELAY_SECONDS:
        await asyncio.sleep(WALMART_OPEN_BOX_RETRY_DELAY_SECONDS)
    previous = repository.service_run_marker(WALMART_OPEN_BOX_RETRY_MARKER)
    if previous and previous.get("status") == "complete":
        return
    repository.upsert_service_run_marker(
        WALMART_OPEN_BOX_RETRY_MARKER,
        status="running",
    )
    try:
        result = await _run_walmart_auto_publish_once(
            WalmartAutoPublishRequest(
                max_items=max(1, min(int(settings.walmart_auto_publish_batch_size), 200)),
                gtin_lookup_max_items=WALMART_OPEN_BOX_RETRY_GTIN_LIMIT,
                sync_ebay_first=False,
                confirm=True,
                force_retry=False,
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        repository.upsert_service_run_marker(
            WALMART_OPEN_BOX_RETRY_MARKER,
            status="failed",
            result={"message": str(exc)},
        )
        logger.warning("One-time Walmart Open Box publishing retry failed: %s", exc)
        return
    repository.upsert_service_run_marker(
        WALMART_OPEN_BOX_RETRY_MARKER,
        status="complete",
        result={
            key: result.get(key)
            for key in (
                "status",
                "submitted_items",
                "offer_feed_id",
                "inventory_feed_id",
                "message",
            )
        },
    )


async def _run_marketplace_inventory_sync_once() -> dict[str, Any]:
    global marketplace_inventory_sync_status
    if marketplace_inventory_sync_lock.locked():
        return {
            **marketplace_inventory_sync_status,
            "status": "already_running",
        }
    async with marketplace_inventory_sync_lock:
        try:
            syncer = MarketplaceInventorySyncer(
                repository,
                EbayClient(settings),
                walmart_client,
            )
            marketplace_inventory_sync_status = await syncer.sync_once()
        except Exception as exc:
            marketplace_inventory_sync_status = {
                "status": "error",
                "enabled": settings.marketplace_inventory_sync_enabled,
                "interval_seconds": settings.marketplace_inventory_sync_interval_seconds,
                "message": str(exc),
                "last_attempt_at": datetime.now(timezone.utc).isoformat(),
            }
            raise
    return marketplace_inventory_sync_status


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
        price_markup_percent=settings.walmart_price_markup_percent,
    )
    preview["ebay_sync"] = ebay_refresh
    preview["walmart_configured"] = walmart_client.configured
    preview["match_item_payloads"] = []
    preview["full_item_payloads"] = []

    if not (force_verify_catalog or import_request.verify_catalog):
        preview["catalog_verification"] = "not_requested"
        return preview
    if not walmart_client.configured:
        raise HTTPException(
            status_code=503,
            detail="WALMART_CLIENT_ID and WALMART_CLIENT_SECRET are required for catalog verification.",
        )

    items_by_sku = {item.sku: item for item in items}
    match_item_payloads: list[dict[str, Any]] = []
    full_item_payloads: list[dict[str, Any]] = []
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
        if catalog.get("matched") is True:
            try:
                match_payload = build_offer_match_from_catalog_template(
                    items_by_sku[str(item_result["sku"])],
                    catalog,
                    resolved,
                )
            except (KeyError, TypeError, ValueError) as exc:
                item_result["ready"] = False
                item_result["errors"].append(str(exc))
            else:
                item_result["submission_route"] = "MP_ITEM_MATCH"
                match_item_payloads.append(
                    {"sku": str(item_result["sku"]), "payload": match_payload}
                )
        elif (
            catalog.get("status") == "full_item_required"
            and isinstance(catalog.get("item_spec_payload"), dict)
        ):
            try:
                full_payload = build_full_item_from_catalog_template(
                    items_by_sku[str(item_result["sku"])],
                    catalog,
                    resolved,
                )
            except (KeyError, TypeError, ValueError) as exc:
                item_result["ready"] = False
                item_result["errors"].append(str(exc))
            else:
                item_result["submission_route"] = "MP_ITEM"
                full_item_payloads.append(
                    {"sku": str(item_result["sku"]), "payload": full_payload}
                )
        elif catalog.get("matched") is False:
            item_result["ready"] = False
            item_result["errors"].append(
                "Walmart did not return either an existing catalog match or a current full-item template."
            )
        elif catalog.get("matched") is None:
            item_result["warnings"].append(str(catalog.get("reason") or "Catalog match was not checked."))

    match_ready_skus = {row["sku"] for row in match_item_payloads}
    ready_skus = {item["sku"] for item in preview["items"] if item["ready"]}
    preview["payload"]["MPItem"] = [
        entry
        for entry in preview["payload"]["MPItem"]
        if entry.get("Item", {}).get("sku") in match_ready_skus
    ]
    preview["match_item_payloads"] = match_item_payloads
    preview["full_item_payloads"] = full_item_payloads
    preview["match_ready_skus"] = sorted(match_ready_skus)
    preview["full_item_ready_skus"] = sorted(
        row["sku"] for row in full_item_payloads
    )
    preview["ready"] = len(ready_skus)
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
        return build_walmart_draft(
            item,
            catalog_result,
            lookup_error=lookup_error,
            price_markup_percent=settings.walmart_price_markup_percent,
        )

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


def _walmart_auto_publish_exclusion(item: InventoryItem) -> str | None:
    terms = [
        term.strip().lower()
        for term in str(settings.walmart_auto_publish_excluded_terms or "").split(",")
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


def _condition_specific_catalog_candidate(candidate: dict[str, Any]) -> bool:
    title = str(candidate.get("title") or "").strip().lower()
    return any(
        marker in title
        for marker in (
            "open box",
            "open-box",
            "pre-owned",
            "preowned",
            "refurbished",
            "renewed",
            "restored",
            "used ",
        )
    )


def _walmart_full_item_retry(draft: dict[str, Any]) -> bool:
    if str(draft.get("publish_status") or "") != "blocked_offer_error":
        return False
    error = str(draft.get("publish_error") or "").lower()
    return "submit a full setup" in error or "requires a full setup" in error


class _WalmartGtinLookupBudget:
    def __init__(self, maximum: int) -> None:
        self.maximum = max(0, int(maximum))
        self.remaining = self.maximum
        self.attempted = 0
        self.cache_hits = 0
        self.verified = 0
        self.unresolved = 0
        self._lock = asyncio.Lock()

    async def claim(self) -> bool:
        async with self._lock:
            if self.remaining <= 0:
                return False
            self.remaining -= 1
            self.attempted += 1
            return True

    async def record(self, outcome: str) -> None:
        async with self._lock:
            if outcome == "cache_hit":
                self.cache_hits += 1
            elif outcome == "verified":
                self.verified += 1
            elif outcome == "unresolved":
                self.unresolved += 1

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": bool(settings.walmart_gtin_lookup_enabled),
            "configured": product_identifier_lookup.configured,
            "model": settings.walmart_gtin_lookup_model,
            "max_per_run": self.maximum,
            "attempted": self.attempted,
            "cache_hits": self.cache_hits,
            "verified": self.verified,
            "unresolved": self.unresolved,
            "remaining": self.remaining,
        }


def _future_iso_timestamp(seconds: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=max(60, int(seconds)))
    ).isoformat()


def _cached_lookup_is_deferred(cached: dict[str, Any]) -> bool:
    raw_next = str(cached.get("next_lookup_at") or "").strip()
    if not raw_next:
        return False
    try:
        next_lookup = datetime.fromisoformat(raw_next)
    except ValueError:
        return False
    if next_lookup.tzinfo is None:
        next_lookup = next_lookup.replace(tzinfo=timezone.utc)
    return next_lookup > datetime.now(timezone.utc)


async def _resolve_online_product_identifier(
    item: InventoryItem,
    candidates: list[dict[str, Any]],
    budget: _WalmartGtinLookupBudget,
) -> tuple[str | None, str | None, dict[str, Any]]:
    fingerprint = product_identifier_fingerprint(item)
    cached = repository.walmart_product_identifier_cache(item.sku)
    if cached and cached.get("source_fingerprint") == fingerprint:
        product_id_type, product_id = normalize_product_identifier(
            cached.get("product_id_type"), cached.get("product_id")
        )
        cached_status = str(cached.get("verification_status") or "")
        if (
            cached_status in {"verified", "full_item_required", "full_item_template"}
            and product_id_type
            and product_id
        ):
            await budget.record("cache_hit")
            return product_id_type, product_id, {
                "status": (
                    "full_item_template_cache"
                    if cached_status in {"full_item_required", "full_item_template"}
                    else "verified_cache"
                ),
                "source_urls": cached.get("source_urls") or [],
                "matched_product": cached.get("matched_product"),
            }
        if _cached_lookup_is_deferred(cached):
            return None, None, {
                "status": "cached_unresolved",
                "reason": cached.get("reason") or "The previous online lookup was unresolved.",
                "next_lookup_at": cached.get("next_lookup_at"),
            }

    if not settings.walmart_gtin_lookup_enabled:
        return None, None, {
            "status": "disabled",
            "reason": "Automatic online GTIN lookup is disabled.",
        }
    if not product_identifier_lookup.configured:
        return None, None, {
            "status": "not_configured",
            "reason": "OPENAI_API_KEY is not configured for automatic GTIN lookup.",
        }
    if not await budget.claim():
        return None, None, {
            "status": "run_limit_reached",
            "reason": "This run reached its online GTIN lookup limit; the next hourly run will continue.",
        }

    retry_at = _future_iso_timestamp(settings.walmart_gtin_lookup_retry_seconds)
    try:
        researched = await product_identifier_lookup.lookup(item, candidates)
    except ProductIdentifierLookupError as exc:
        await budget.record("unresolved")
        repository.upsert_walmart_product_identifier_cache(
            item.sku,
            source_fingerprint=fingerprint,
            verification_status="lookup_error",
            reason=str(exc),
            next_lookup_at=_future_iso_timestamp(3600),
        )
        return None, None, {"status": "lookup_error", "reason": str(exc)}

    if (
        researched.status != "verified"
        or not researched.product_id_type
        or not researched.product_id
    ):
        await budget.record("unresolved")
        repository.upsert_walmart_product_identifier_cache(
            item.sku,
            source_fingerprint=fingerprint,
            verification_status=researched.status,
            source_urls=researched.source_urls,
            matched_product=researched.matched_product,
            reason=researched.reason,
            next_lookup_at=retry_at,
        )
        return None, None, {
            "status": researched.status,
            "reason": researched.reason or "No single verified identifier was found online.",
            "source_urls": researched.source_urls,
        }

    try:
        catalog = await walmart_client.search_catalog(
            researched.product_id_type,
            researched.product_id,
            response_format="SPEC",
        )
    except WalmartApiError as exc:
        await budget.record("unresolved")
        repository.upsert_walmart_product_identifier_cache(
            item.sku,
            source_fingerprint=fingerprint,
            verification_status="walmart_verification_error",
            product_id_type=researched.product_id_type,
            product_id=researched.product_id,
            source_urls=researched.source_urls,
            matched_product=researched.matched_product,
            reason=str(exc),
            next_lookup_at=_future_iso_timestamp(3600),
        )
        return None, None, {
            "status": "walmart_verification_error",
            "reason": f"The online identifier could not be checked against Walmart: {exc}",
        }

    full_item_template = (
        catalog.get("status") == "full_item_required"
        and isinstance(catalog.get("item_spec_payload"), dict)
    )
    if catalog.get("matched") is not True and not full_item_template:
        await budget.record("unresolved")
        catalog_status = str(catalog.get("status") or "not_matched")
        reason = (
            "The researched identifier requires a full Walmart item setup."
            if catalog_status == "full_item_required"
            else "The researched identifier did not match a published Walmart catalog item."
        )
        repository.upsert_walmart_product_identifier_cache(
            item.sku,
            source_fingerprint=fingerprint,
            verification_status=catalog_status,
            product_id_type=researched.product_id_type,
            product_id=researched.product_id,
            source_urls=researched.source_urls,
            matched_product=researched.matched_product,
            reason=reason,
            next_lookup_at=retry_at,
        )
        return None, None, {
            "status": catalog_status,
            "reason": reason,
            "source_urls": researched.source_urls,
        }

    await budget.record("verified")
    verification_status = "full_item_template" if full_item_template else "verified"
    repository.upsert_walmart_product_identifier_cache(
        item.sku,
        source_fingerprint=fingerprint,
        verification_status=verification_status,
        product_id_type=researched.product_id_type,
        product_id=researched.product_id,
        source_urls=researched.source_urls,
        matched_product=researched.matched_product,
        reason=researched.reason,
        next_lookup_at=None,
    )
    return researched.product_id_type, researched.product_id, {
        "status": verification_status,
        "source_urls": researched.source_urls,
        "matched_product": researched.matched_product,
        "submission_route": "MP_ITEM" if full_item_template else "MP_ITEM_MATCH",
    }


async def _resolve_walmart_auto_publish_draft(
    item: InventoryItem,
    draft: dict[str, Any],
    *,
    gtin_lookup_budget: _WalmartGtinLookupBudget | None = None,
) -> tuple[WalmartItemOverride | None, dict[str, Any], dict[str, Any]]:
    prepared = dict(draft.get("prepared_listing") or {})
    candidates = [
        dict(candidate)
        for candidate in (draft.get("catalog_candidates") or [])
        if isinstance(candidate, dict)
    ]
    identifier = prepared.get("product_identifier")
    identifier_source: str | None = None
    product_id_type: str | None = None
    product_id: str | None = None
    matched_candidate: dict[str, Any] | None = None

    if isinstance(identifier, dict):
        product_id_type, product_id = normalize_product_identifier(
            identifier.get("type"), identifier.get("value")
        )
        if product_id_type and product_id:
            identifier_source = "ebay_or_stored_draft"

    public_identifier = PUBLIC_CATALOG_IDENTIFIERS.get(item.sku)
    if not product_id and public_identifier:
        product_id_type, product_id = normalize_product_identifier(
            public_identifier.get("product_id_type"),
            public_identifier.get("product_id"),
        )
        if product_id_type and product_id:
            identifier_source = "reviewed_public_identifier"

    catalog_resolution_reason = "No verified Walmart catalog identifier was available."
    online_lookup: dict[str, Any] | None = None
    if not product_id:
        verified_match, catalog_resolution_reason = select_verified_catalog_match(item, candidates)
        if verified_match:
            matched_candidate = verified_match
        else:
            plausible, plausible_reason = plausible_catalog_candidates(item, candidates)
            catalog_resolution_reason = plausible_reason
            if len(plausible) == 1 and plausible[0].get("walmart_item_id"):
                try:
                    enriched = await walmart_client.enrich_catalog_candidate(plausible[0])
                except WalmartApiError as exc:
                    catalog_resolution_reason = f"Could not enrich the exact Walmart candidate: {exc}"
                else:
                    candidates = [
                        enriched if candidate is plausible[0] else candidate
                        for candidate in candidates
                    ]
                    matched_candidate, catalog_resolution_reason = select_verified_catalog_match(
                        item, candidates
                    )
            elif len(plausible) == 1:
                catalog_resolution_reason = (
                    "The exact catalog candidate did not include a Walmart item ID."
                )
        if matched_candidate and _condition_specific_catalog_candidate(matched_candidate):
            catalog_resolution_reason = (
                "The exact Walmart result is a condition-specific seller listing, so its "
                "reseller UPC cannot be used as the original product identifier."
            )
        elif matched_candidate:
            product_id_type = str(matched_candidate["product_id_type"])
            product_id = str(matched_candidate["product_id"])
            identifier_source = "exact_walmart_catalog_candidate"

    if not product_id and gtin_lookup_budget is not None:
        product_id_type, product_id, online_lookup = await _resolve_online_product_identifier(
            item, candidates, gtin_lookup_budget
        )
        if product_id_type and product_id:
            identifier_source = "verified_online_identifier"

    if not product_id:
        reason = catalog_resolution_reason
        if online_lookup and online_lookup.get("reason"):
            reason = str(online_lookup["reason"])
        return None, {**draft, "catalog_candidates": candidates}, {
            "sku": item.sku,
            "ready": False,
            "reason": reason,
            "catalog_reason": catalog_resolution_reason,
            "online_lookup": online_lookup,
        }

    shipping_weight = prepared.get("shipping_weight_lbs")
    weight_source = "ebay_or_stored_draft"
    if shipping_weight is None and matched_candidate:
        shipping_weight = matched_candidate.get("shipping_weight_lbs")
        if shipping_weight is not None:
            weight_source = "walmart_public_product_page"
    if shipping_weight is None:
        shipping_weight = estimated_shipping_weight_lbs(item)
        weight_source = "conservative_category_estimate"

    prepared["product_identifier"] = {
        "type": product_id_type,
        "value": product_id,
    }
    if product_id_type not in {"GTIN", "UPC", "EAN", "ISBN"}:
        return None, draft, {
            "sku": item.sku,
            "ready": False,
            "reason": "The resolved product identifier type is not supported by Walmart.",
        }
    try:
        resolved_shipping_weight = float(shipping_weight)
    except (TypeError, ValueError):
        return None, draft, {
            "sku": item.sku,
            "ready": False,
            "reason": "The resolved shipping weight was not numeric.",
        }
    prepared["shipping_weight_lbs"] = resolved_shipping_weight
    missing_fields = [
        field
        for field in (draft.get("missing_fields") or [])
        if field not in {"product_identifier", "shipping_weight_lbs"}
    ]
    updated_draft = {
        **draft,
        "prepared_listing": prepared,
        "catalog_candidates": candidates,
        "status": "draft_ready_to_publish",
        "missing_fields": missing_fields,
        "lookup_error": None,
    }
    override = WalmartItemOverride(
        product_id_type=product_id_type,
        product_id=product_id,
        shipping_weight_lbs=resolved_shipping_weight,
    )
    return override, updated_draft, {
        "sku": item.sku,
        "ready": True,
        "identifier_source": identifier_source,
        "online_lookup": online_lookup,
        "weight_source": weight_source,
        "estimated_shipping_weight": weight_source == "conservative_category_estimate",
    }


async def _wait_for_walmart_feed(
    feed_id: str,
    *,
    attempts: int = 20,
    delay_seconds: float = 2.0,
) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for attempt in range(max(1, attempts)):
        latest = await walmart_client.get_feed_status(feed_id, include_details=True)
        if str(latest.get("feedStatus") or "").upper() in {
            "PROCESSED",
            "ERROR",
        }:
            return latest
        if attempt < attempts - 1:
            await asyncio.sleep(delay_seconds)
    return latest


def _apply_walmart_offer_feed_results(
    feed_payload: dict[str, Any],
    expected_skus: set[str],
) -> dict[str, str]:
    states: dict[str, str] = {}
    for sku, result in walmart_feed_item_results(feed_payload).items():
        if sku not in expected_skus:
            continue
        state = classify_walmart_offer_result(result)
        states[sku] = state
        repository.update_walmart_draft_publish_state(
            [sku],
            state,
            error_message=result.get("error_message") or None,
        )
    return states


def _apply_walmart_inventory_feed_results(
    feed_payload: dict[str, Any],
    eligible_skus: set[str],
) -> dict[str, str]:
    states: dict[str, str] = {}
    for sku, result in walmart_feed_item_results(feed_payload).items():
        if sku not in eligible_skus:
            continue
        state = classify_walmart_inventory_result(result)
        states[sku] = state
        repository.update_walmart_draft_publish_state(
            [sku],
            state,
            error_message=result.get("error_message") or None,
        )
    return states


def _group_walmart_full_item_payloads(
    payload_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for row in payload_rows:
        sku = str(row.get("sku") or "").strip()
        payload = row.get("payload")
        if not sku or not isinstance(payload, dict):
            continue
        header = payload.get("MPItemFeedHeader")
        entries = payload.get("MPItem")
        if not isinstance(header, dict) or not isinstance(entries, list) or len(entries) != 1:
            continue
        key = json.dumps(header, sort_keys=True, separators=(",", ":"))
        group = groups.setdefault(
            key,
            {
                "feed_type": "MP_ITEM",
                "skus": [],
                "payload": {"MPItemFeedHeader": header, "MPItem": []},
            },
        )
        group["skus"].append(sku)
        group["payload"]["MPItem"].append(entries[0])
    return list(groups.values())


def _group_walmart_match_item_payloads(
    payload_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for row in payload_rows:
        sku = str(row.get("sku") or "").strip()
        payload = row.get("payload")
        if not sku or not isinstance(payload, dict):
            continue
        header = payload.get("MPItemFeedHeader")
        entries = payload.get("MPItem")
        if not isinstance(header, dict) or not isinstance(entries, list) or len(entries) != 1:
            continue
        key = json.dumps(header, sort_keys=True, separators=(",", ":"))
        group = groups.setdefault(
            key,
            {
                "feed_type": "MP_ITEM_MATCH",
                "skus": [],
                "payload": {"MPItemFeedHeader": header, "MPItem": []},
            },
        )
        group["skus"].append(sku)
        group["payload"]["MPItem"].append(entries[0])
    return list(groups.values())


def _walmart_offer_groups_from_preflight(
    preflight: dict[str, Any],
) -> list[dict[str, Any]]:
    groups = _group_walmart_match_item_payloads(
        preflight.get("match_item_payloads") or []
    )
    groups.extend(
        _group_walmart_full_item_payloads(preflight.get("full_item_payloads") or [])
    )
    return groups


async def _reconcile_walmart_auto_publish_feeds(
    *,
    confirm_inventory_actions: bool,
) -> dict[str, Any]:
    drafts = repository.walmart_drafts(limit=200)
    existing_states = {
        str(draft.get("sku") or "").strip(): str(draft.get("publish_status") or "")
        for draft in drafts
    }
    offer_groups: dict[str, set[str]] = {}
    inventory_groups: dict[str, set[str]] = {}
    for draft in drafts:
        sku = str(draft.get("sku") or "").strip()
        offer_feed_id = str(draft.get("offer_feed_id") or "").strip()
        inventory_feed_id = str(draft.get("inventory_feed_id") or "").strip()
        if sku and offer_feed_id:
            offer_groups.setdefault(offer_feed_id, set()).add(sku)
        if sku and inventory_feed_id:
            inventory_groups.setdefault(inventory_feed_id, set()).add(sku)

    offer_states: dict[str, str] = {}
    inventory_states: dict[str, str] = {}
    feed_errors: dict[str, str] = {}
    for feed_id, skus in offer_groups.items():
        try:
            payload = await walmart_client.get_feed_status(feed_id, include_details=True)
        except WalmartApiError as exc:
            feed_errors[feed_id] = str(exc)
            continue
        if str(payload.get("feedStatus") or "").upper() == "PROCESSED":
            active_skus = {
                sku
                for sku in skus
                if existing_states.get(sku)
                not in {
                    "published",
                    "excluded",
                    "submitted",
                    "retryable_offer_error",
                    "compliance_review",
                    "blocked_product_id_conflict",
                    "blocked_product_id_conflict_remediated",
                    "blocked_offer_error",
                    "blocked_inventory_error",
                }
            }
            offer_states.update(_apply_walmart_offer_feed_results(payload, active_skus))

    conflict_inventory_success: set[str] = set()
    for feed_id, skus in inventory_groups.items():
        try:
            payload = await walmart_client.get_feed_status(feed_id, include_details=True)
        except WalmartApiError as exc:
            feed_errors[feed_id] = str(exc)
            continue
        if str(payload.get("feedStatus") or "").upper() != "PROCESSED":
            continue
        results = walmart_feed_item_results(payload)
        eligible = {
            sku
            for sku in skus
            if offer_states.get(sku) == "offer_processed_inventory_pending"
        }
        inventory_states.update(_apply_walmart_inventory_feed_results(payload, eligible))
        conflict_inventory_success.update(
            sku
            for sku, result in results.items()
            if sku in skus
            and offer_states.get(sku) == "blocked_product_id_conflict"
            and str(result.get("status") or "").upper() == "SUCCESS"
        )

    retry_inventory_skus = {
        sku
        for sku, state in offer_states.items()
        if state == "offer_processed_inventory_pending"
        and inventory_states.get(sku) != "submitted"
    }
    remediation_feed_id: str | None = None
    retry_inventory_feed_id: str | None = None
    retry_inventory_states: dict[str, str] = {}
    remediation_states: dict[str, str] = {}
    if confirm_inventory_actions and conflict_inventory_success:
        conflict_items = repository.ebay_items(
            conflict_inventory_success,
            limit=len(conflict_inventory_success),
            include_inactive=True,
        )
        zero_items = [item.model_copy(update={"quantity": 0}) for item in conflict_items]
        if zero_items:
            submission = await walmart_client.submit_inventory_feed(
                build_inventory_feed(zero_items)
            )
            remediation_feed_id = str(submission["feed_id"])
            repository.update_walmart_draft_publish_state(
                [item.sku for item in zero_items],
                "blocked_product_id_conflict",
                inventory_feed_id=remediation_feed_id,
                error_message=(
                    "Walmart retained a different product under this SKU; inventory was reset to zero."
                ),
            )
            remediation_payload = await _wait_for_walmart_feed(remediation_feed_id)
            remediation_states = {
                sku: str(result.get("status") or "UNKNOWN")
                for sku, result in walmart_feed_item_results(remediation_payload).items()
            }
            remediated_skus = [
                sku for sku, status in remediation_states.items() if status == "SUCCESS"
            ]
            repository.update_walmart_draft_publish_state(
                remediated_skus,
                "blocked_product_id_conflict_remediated",
                inventory_feed_id=remediation_feed_id,
                error_message=(
                    "Walmart retained a different product under this SKU; inventory was reset to zero."
                ),
            )

    if confirm_inventory_actions and retry_inventory_skus:
        retry_items = repository.ebay_items(
            retry_inventory_skus,
            limit=len(retry_inventory_skus),
            include_inactive=False,
        )
        if retry_items:
            submission = await walmart_client.submit_inventory_feed(
                build_inventory_feed(retry_items)
            )
            retry_inventory_feed_id = str(submission["feed_id"])
            repository.update_walmart_draft_publish_state(
                [item.sku for item in retry_items],
                "offer_processed_inventory_pending",
                inventory_feed_id=retry_inventory_feed_id,
            )
            retry_payload = await _wait_for_walmart_feed(retry_inventory_feed_id)
            retry_inventory_states = _apply_walmart_inventory_feed_results(
                retry_payload,
                {item.sku for item in retry_items},
            )

    return {
        "offer_states": offer_states,
        "inventory_states": inventory_states,
        "feed_errors": feed_errors,
        "inventory_retry_skus": sorted(retry_inventory_skus),
        "inventory_retry_feed_id": retry_inventory_feed_id,
        "inventory_retry_states": retry_inventory_states,
        "inventory_remediation_skus": sorted(conflict_inventory_success),
        "inventory_remediation_feed_id": remediation_feed_id,
        "inventory_remediation_states": remediation_states,
    }


def _walmart_publish_retry_due(draft: dict[str, Any], now: datetime | None = None) -> bool:
    state = str(draft.get("publish_status") or "")
    if state == "retryable_offer_error":
        try:
            attempts = max(1, int(draft.get("publish_attempts") or 1))
        except (TypeError, ValueError):
            attempts = 1
        retry_delays = (60 * 60, 6 * 60 * 60, 24 * 60 * 60)
        wait_seconds = retry_delays[min(attempts - 1, len(retry_delays) - 1)]
    elif state == "compliance_review":
        wait_seconds = 48 * 60 * 60
    else:
        wait_seconds = None
    if wait_seconds is None:
        return True
    raw_last_attempt = str(draft.get("last_publish_at") or "").strip()
    if not raw_last_attempt:
        return True
    try:
        last_attempt = datetime.fromisoformat(raw_last_attempt.replace("Z", "+00:00"))
    except ValueError:
        return True
    if last_attempt.tzinfo is None:
        last_attempt = last_attempt.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return (current - last_attempt).total_seconds() >= wait_seconds


async def _run_walmart_auto_publish_once(
    auto_request: WalmartAutoPublishRequest,
) -> dict[str, Any]:
    global walmart_auto_publish_status, walmart_sync_status
    attempted_at = datetime.now(timezone.utc).isoformat()
    if walmart_auto_publish_lock.locked():
        return {**walmart_auto_publish_status, "status": "already_running"}
    if not walmart_client.configured:
        raise HTTPException(
            status_code=503,
            detail="WALMART_CLIENT_ID and WALMART_CLIENT_SECRET are not configured.",
        )

    async with walmart_auto_publish_lock:
        ebay_refresh: dict[str, Any] | None = None
        if auto_request.sync_ebay_first:
            ebay_refresh = await _sync_ebay_api_inventory()
            if ebay_refresh.get("status") != "ok":
                raise HTTPException(
                    status_code=503,
                    detail={
                        "message": "The eBay refresh failed, so no Walmart drafts were submitted.",
                        "ebay_sync": ebay_refresh,
                    },
                )

        reconciliation = await _reconcile_walmart_auto_publish_feeds(
            confirm_inventory_actions=auto_request.confirm
        )

        generation = await _generate_walmart_drafts(
            WalmartDraftGenerateRequest(
                max_items=auto_request.max_items,
                sync_ebay_first=False,
                search_walmart_catalog=True,
                catalog_candidates_per_item=5,
            )
        )
        active_items = repository.ebay_items(
            limit=auto_request.max_items,
            include_inactive=False,
        )
        drafts_by_sku = {
            str(draft["sku"]): draft
            for draft in repository.walmart_drafts(
                [item.sku for item in active_items],
                limit=auto_request.max_items,
            )
        }
        try:
            published_items = await walmart_client.list_published_items(
                limit=max(1, min(int(settings.walmart_auto_publish_catalog_limit), 1000))
            )
        except WalmartApiError as exc:
            raise _walmart_http_error(exc) from exc
        published_skus = {
            str(item.get("sku") or "").strip()
            for item in published_items
            if str(item.get("sku") or "").strip()
        }
        repository.update_walmart_draft_publish_state(published_skus, "published")

        excluded: list[dict[str, Any]] = []
        already_published: list[str] = []
        awaiting_walmart: list[str] = []
        candidates_to_resolve: list[tuple[InventoryItem, dict[str, Any]]] = []
        nonrepeatable_states = {
            "submitting_offer",
            "offer_processed_inventory_pending",
            "offer_submitted_inventory_pending",
            "submitted",
            "processing",
            "blocked_product_id_conflict",
            "blocked_product_id_conflict_remediated",
            "blocked_offer_error",
            "blocked_inventory_error",
        }
        for item in active_items:
            exclusion = _walmart_auto_publish_exclusion(item)
            if exclusion:
                excluded.append({"sku": item.sku, "matched_term": exclusion})
                repository.update_walmart_draft_publish_state([item.sku], "excluded")
                continue
            if item.sku in published_skus:
                already_published.append(item.sku)
                continue
            draft = drafts_by_sku.get(item.sku)
            if not draft:
                continue
            publish_status = str(draft.get("publish_status") or "")
            if publish_status in {"retryable_offer_error", "compliance_review"}:
                if not auto_request.force_retry and not _walmart_publish_retry_due(draft):
                    awaiting_walmart.append(item.sku)
                    continue
            if (
                publish_status in nonrepeatable_states
                and not _walmart_full_item_retry(draft)
            ):
                awaiting_walmart.append(item.sku)
                continue
            candidates_to_resolve.append((item, draft))

        submitted_not_published = {
            sku
            for sku in awaiting_walmart
            if sku not in published_skus
        }
        available_slots = max(
            0,
            int(settings.walmart_auto_publish_catalog_limit)
            - len(published_skus | submitted_not_published),
        )

        enrichment_semaphore = asyncio.Semaphore(4)
        gtin_lookup_limit = (
            auto_request.gtin_lookup_max_items
            if auto_request.gtin_lookup_max_items is not None
            else settings.walmart_gtin_lookup_max_per_run
        )
        gtin_lookup_budget = _WalmartGtinLookupBudget(gtin_lookup_limit)

        async def resolve_one(
            item: InventoryItem,
            draft: dict[str, Any],
        ) -> tuple[InventoryItem, WalmartItemOverride | None, dict[str, Any], dict[str, Any]]:
            async with enrichment_semaphore:
                override, updated_draft, result = await _resolve_walmart_auto_publish_draft(
                    item,
                    draft,
                    gtin_lookup_budget=gtin_lookup_budget,
                )
            return item, override, updated_draft, result

        resolved_rows = await asyncio.gather(
            *(resolve_one(item, draft) for item, draft in candidates_to_resolve)
        )
        if resolved_rows:
            repository.upsert_walmart_drafts(row[2] for row in resolved_rows)

        resolution_results: list[dict[str, Any]] = []
        overrides: dict[str, WalmartItemOverride] = {}
        for item, override, _draft, result in resolved_rows:
            if override is None:
                repository.update_walmart_draft_publish_state(
                    [item.sku], "blocked_missing_info", error_message=str(result.get("reason") or "")
                )
            elif len(overrides) < available_slots:
                overrides[item.sku] = override
            else:
                result = {
                    **result,
                    "ready": False,
                    "reason": "The configured 250-item Walmart catalog limit has been reached.",
                }
                repository.update_walmart_draft_publish_state(
                    [item.sku], "blocked_catalog_limit", error_message=result["reason"]
                )
            resolution_results.append(result)

        candidate_skus = list(overrides)
        preflight: dict[str, Any] | None = None
        ready_skus: list[str] = []
        if candidate_skus:
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
            ready_skus = [
                str(item["sku"])
                for item in preflight["items"]
                if item.get("ready")
            ]
            blocked_preflight = [
                item
                for item in preflight["items"]
                if not item.get("ready")
            ]
            for item in blocked_preflight:
                repository.update_walmart_draft_publish_state(
                    [str(item["sku"])],
                    "blocked_preflight",
                    error_message="; ".join(str(error) for error in item.get("errors") or []),
                )
            repository.update_walmart_draft_publish_state(ready_skus, "ready")

        base_result: dict[str, Any] = {
            "status": "previewed" if not auto_request.confirm else "no_ready_items",
            "enabled": settings.walmart_auto_publish_enabled,
            "confirm": auto_request.confirm,
            "force_retry": auto_request.force_retry,
            "generated_drafts": int(generation.get("generated") or 0),
            "active_ebay_items": len(active_items),
            "published_walmart_items": len(published_skus),
            "catalog_limit": int(settings.walmart_auto_publish_catalog_limit),
            "available_catalog_slots": available_slots,
            "already_published": already_published,
            "awaiting_walmart": awaiting_walmart,
            "excluded": excluded,
            "resolved": resolution_results,
            "gtin_lookup": gtin_lookup_budget.summary(),
            "ready_skus": ready_skus,
            "match_ready_skus": (preflight or {}).get("match_ready_skus", []),
            "full_item_ready_skus": (preflight or {}).get("full_item_ready_skus", []),
            "blocked_items": (preflight or {}).get("blocked", 0),
            "ebay_sync": ebay_refresh,
            "reconciliation": reconciliation,
            "offer_feed_id": None,
            "inventory_feed_id": None,
            "last_attempt_at": attempted_at,
        }

        if not auto_request.confirm or not ready_skus:
            walmart_auto_publish_status = base_result
            return base_result

        offer_groups = _walmart_offer_groups_from_preflight(preflight)
        repository.update_walmart_draft_publish_state(
            ready_skus,
            "submitting_offer",
            increment_attempts=True,
        )
        submitted_offer_groups: list[dict[str, Any]] = []
        offer_submission_errors: dict[str, str] = {}
        for group in offer_groups:
            group_skus = [str(sku) for sku in group["skus"]]
            try:
                if group["feed_type"] == "MP_ITEM":
                    submission = await walmart_client.submit_full_item_feed(group["payload"])
                else:
                    submission = await walmart_client.submit_offer_match_feed(group["payload"])
            except WalmartApiError as exc:
                repository.update_walmart_draft_publish_state(
                    group_skus, "offer_failed", error_message=str(exc)
                )
                offer_submission_errors[",".join(group_skus)] = str(exc)
                continue
            feed_id = str(submission["feed_id"])
            repository.update_walmart_draft_publish_state(
                group_skus,
                "offer_submitted_inventory_pending",
                offer_feed_id=feed_id,
            )
            submitted_offer_groups.append({**group, "feed_id": feed_id})

        if not submitted_offer_groups:
            walmart_auto_publish_status = {
                **base_result,
                "status": "offer_failed",
                "offer_submission_errors": offer_submission_errors,
                "message": "Walmart rejected every item-setup feed submission.",
            }
            return walmart_auto_publish_status

        async def wait_for_offer_group(group: dict[str, Any]) -> dict[str, Any]:
            try:
                feed = await _wait_for_walmart_feed(str(group["feed_id"]))
            except WalmartApiError as exc:
                return {**group, "wait_error": str(exc), "feed": None}
            return {**group, "wait_error": None, "feed": feed}

        completed_offer_groups = await asyncio.gather(
            *(wait_for_offer_group(group) for group in submitted_offer_groups)
        )
        offer_states: dict[str, str] = {}
        processing_offer_skus: list[str] = []
        offer_wait_errors: dict[str, str] = {}
        for group in completed_offer_groups:
            group_skus = {str(sku) for sku in group["skus"]}
            if group.get("wait_error"):
                processing_offer_skus.extend(sorted(group_skus))
                offer_wait_errors[str(group["feed_id"])] = str(group["wait_error"])
                continue
            offer_feed = group.get("feed") or {}
            if str(offer_feed.get("feedStatus") or "").upper() != "PROCESSED":
                processing_offer_skus.extend(sorted(group_skus))
                repository.update_walmart_draft_publish_state(
                    group_skus,
                    "processing",
                    offer_feed_id=str(group["feed_id"]),
                )
                continue
            offer_states.update(_apply_walmart_offer_feed_results(offer_feed, group_skus))

        offer_feed_ids = [str(group["feed_id"]) for group in submitted_offer_groups]
        offer_feed_id = offer_feed_ids[0] if offer_feed_ids else None
        accepted_offer_skus = [
            sku
            for sku in ready_skus
            if offer_states.get(sku) == "offer_processed_inventory_pending"
        ]
        if not accepted_offer_skus:
            walmart_auto_publish_status = {
                **base_result,
                "status": "processing" if processing_offer_skus else "offer_processed_no_success",
                "offer_feed_id": offer_feed_id,
                "offer_feed_ids": offer_feed_ids,
                "offer_states": offer_states,
                "processing_offer_skus": processing_offer_skus,
                "offer_submission_errors": offer_submission_errors,
                "offer_wait_errors": offer_wait_errors,
                "message": (
                    "Walmart is still processing item setup; inventory was not sent yet."
                    if processing_offer_skus
                    else "Walmart did not accept any offer, so no inventory feed was sent."
                ),
            }
            return walmart_auto_publish_status

        ready_items = repository.ebay_items(
            accepted_offer_skus,
            limit=len(accepted_offer_skus),
            include_inactive=False,
        )
        try:
            inventory_submission = await walmart_client.submit_inventory_feed(
                build_inventory_feed(ready_items)
            )
        except WalmartApiError as exc:
            repository.update_walmart_draft_publish_state(
                accepted_offer_skus,
                "offer_processed_inventory_pending",
                error_message=str(exc),
            )
            walmart_auto_publish_status = {
                **base_result,
                "status": "offer_submitted_inventory_pending",
                "offer_feed_id": offer_feed_id,
                "offer_feed_ids": offer_feed_ids,
                "message": str(exc),
            }
            raise _walmart_http_error(exc) from exc

        inventory_feed_id = str(inventory_submission["feed_id"])
        repository.update_walmart_draft_publish_state(
            accepted_offer_skus,
            "offer_processed_inventory_pending",
            inventory_feed_id=inventory_feed_id,
        )
        try:
            inventory_feed = await _wait_for_walmart_feed(inventory_feed_id)
        except WalmartApiError as exc:
            walmart_auto_publish_status = {
                **base_result,
                "status": "offer_processed_inventory_pending",
                "offer_feed_id": offer_feed_id,
                "offer_feed_ids": offer_feed_ids,
                "inventory_feed_id": inventory_feed_id,
                "offer_states": offer_states,
                "message": str(exc),
            }
            return walmart_auto_publish_status

        inventory_states = _apply_walmart_inventory_feed_results(
            inventory_feed,
            set(accepted_offer_skus),
        )
        submitted_skus = [
            sku for sku in accepted_offer_skus if inventory_states.get(sku) == "submitted"
        ]
        pending_inventory_skus = [
            sku for sku in accepted_offer_skus if inventory_states.get(sku) != "submitted"
        ]
        final_status = "submitted" if len(submitted_skus) == len(ready_skus) else "processed_with_errors"
        walmart_auto_publish_status = {
            **base_result,
            "status": final_status,
            "submitted_items": len(submitted_skus),
            "submitted_skus": submitted_skus,
            "pending_inventory_skus": pending_inventory_skus,
            "offer_feed_id": offer_feed_id,
            "offer_feed_ids": offer_feed_ids,
            "inventory_feed_id": inventory_feed_id,
            "offer_states": offer_states,
            "inventory_states": inventory_states,
            "processing_offer_skus": processing_offer_skus,
            "offer_submission_errors": offer_submission_errors,
            "offer_wait_errors": offer_wait_errors,
            "message": (
                "Walmart inventory was sent only for offers that its feed processor accepted."
            ),
        }
        walmart_sync_status = {
            "status": f"auto_publish_{final_status}",
            "configured": True,
            "last_submission": {
                "offer_feed_id": offer_feed_id,
                "offer_feed_ids": offer_feed_ids,
                "inventory_feed_id": inventory_feed_id,
            },
            "submitted_items": len(submitted_skus),
            "last_attempt_at": attempted_at,
        }
        return walmart_auto_publish_status


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
    ready_skus = [str(sku) for sku in preflight.get("match_ready_skus") or []]
    if "match_ready_skus" not in preflight:
        ready_skus = [
            str(item["sku"])
            for item in preflight.get("items") or []
            if item.get("ready")
        ]
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
        "offer_feed_types": ["MP_ITEM_MATCH", "MP_ITEM"],
        "offer_spec_source": "Walmart catalog search responseFormat=SPEC",
        "last_sync": walmart_sync_status,
    }
    if test_auth:
        try:
            status["authentication"] = await walmart_client.verify_credentials()
        except WalmartApiError as exc:
            raise _walmart_http_error(exc) from exc
    return status


@app.get("/walmart/catalog/items")
async def walmart_catalog_items(
    request: Request,
    published_status: str | None = None,
    lifecycle_status: str | None = None,
    limit: int = 1000,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    if not walmart_client.configured:
        raise HTTPException(
            status_code=503,
            detail="WALMART_CLIENT_ID and WALMART_CLIENT_SECRET are not configured.",
        )
    try:
        return await walmart_client.list_catalog_items(
            published_status=published_status,
            lifecycle_status=lifecycle_status,
            limit=limit,
        )
    except WalmartApiError as exc:
        raise _walmart_http_error(exc) from exc


@app.post("/walmart/catalog/repair-apple-errors")
async def repair_walmart_apple_catalog_errors(
    repair_request: WalmartCatalogRepairRequest,
    request: Request,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    if not walmart_client.configured:
        raise HTTPException(
            status_code=503,
            detail="WALMART_CLIENT_ID and WALMART_CLIENT_SECRET are not configured.",
        )
    try:
        catalog_response = await walmart_client.list_catalog_items(limit=1000)
    except WalmartApiError as exc:
        raise _walmart_http_error(exc) from exc

    raw_items: object = catalog_response.get("ItemResponse") or catalog_response.get("items") or []
    if isinstance(raw_items, dict):
        raw_items = raw_items.get("Item") or raw_items.get("items") or []
    catalog_items = raw_items if isinstance(raw_items, list) else []
    requested_skus = {str(sku).strip() for sku in repair_request.skus if str(sku).strip()}
    apple_errors = [
        item
        for item in catalog_items
        if isinstance(item, dict)
        and str(item.get("publishedStatus") or "").upper() == "SYSTEM_PROBLEM"
        and any(
            marker in str(item.get("productName") or "").lower()
            for marker in ("apple", "iphone")
        )
        and (not requested_skus or str(item.get("sku") or "") in requested_skus)
    ][: repair_request.max_items]
    catalog_skus = [str(item.get("sku") or "").strip() for item in apple_errors]
    drafts = {
        str(draft.get("sku") or "").strip(): draft
        for draft in repository.walmart_drafts(catalog_skus, limit=repair_request.max_items)
    }
    inventory_rows = {
        item.sku: item
        for item in repository.ebay_items(
            catalog_skus,
            limit=repair_request.max_items,
            include_inactive=True,
        )
    }

    payload_rows: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for catalog_item in apple_errors:
        walmart_sku = str(catalog_item.get("sku") or "").strip()
        source_item = inventory_rows.get(walmart_sku)
        draft = drafts.get(walmart_sku) or {}
        source_snapshot = draft.get("source_snapshot")
        if isinstance(source_snapshot, dict):
            try:
                source_item = InventoryItem.model_validate(source_snapshot)
            except (TypeError, ValueError):
                pass
        if source_item is None:
            results.append(
                {"sku": walmart_sku, "ready": False, "reason": "No stored eBay source snapshot was found."}
            )
            continue

        product_id_type = "GTIN" if catalog_item.get("gtin") else "UPC"
        product_id = str(catalog_item.get("gtin") or catalog_item.get("upc") or "").strip()
        price = walmart_price(source_item.price, settings.walmart_price_markup_percent)
        resolved = {
            "product_id_type": product_id_type,
            "product_id": product_id,
            "shipping_weight_lbs": (
                settings.walmart_default_shipping_weight_lbs
                or estimated_shipping_weight_lbs(source_item)
            ),
            "condition": "Open Box",
            "price": price,
            "quantity": max(1, int(source_item.quantity or 0)),
            # Apple repairs must inherit Walmart-owned catalog content and images.
            # Supplying an eBay image here turns setup-by-match into a content
            # contribution and can keep an Intellectual Property review active.
            "main_image_url": None,
            # Keep the exact Walmart match template and identifier, but do not
            # re-submit Walmart catalog descriptions/specs as seller content.
            # Some Apple templates contain internally contradictory text that
            # Walmart's content-standard checks reject when it is echoed back.
            "offer_only": True,
        }
        try:
            spec = await walmart_client.search_catalog(product_id_type, product_id)
            if spec.get("matched") is not True:
                raise ValueError("Walmart did not return a setup-by-match template for its current catalog GTIN.")
            walmart_item = source_item.model_copy(update={"sku": walmart_sku, "condition": "Open Box"})
            payload = build_offer_match_from_catalog_template(walmart_item, spec, resolved)
        except (WalmartApiError, KeyError, TypeError, ValueError) as exc:
            results.append({"sku": walmart_sku, "ready": False, "reason": str(exc)})
            continue
        payload_rows.append({"sku": walmart_sku, "payload": payload})
        results.append(
            {
                "sku": walmart_sku,
                "ready": True,
                "source_ebay_item_id": source_item.ebay_item_id,
                "product_id_type": product_id_type,
                "product_id": product_id,
                "price": price,
                "condition": "Open Box",
            }
        )

    groups = _group_walmart_match_item_payloads(payload_rows)
    submissions: list[dict[str, Any]] = []
    if repair_request.confirm:
        for group in groups:
            try:
                submission = await walmart_client.submit_offer_match_feed(group["payload"])
            except WalmartApiError as exc:
                for sku in group["skus"]:
                    repository.update_walmart_draft_publish_state(
                        [sku], "offer_failed", error_message=str(exc)
                    )
                submissions.append(
                    {"status": "failed", "skus": group["skus"], "message": str(exc)}
                )
            else:
                repository.update_walmart_draft_publish_state(
                    group["skus"],
                    "offer_submitted_inventory_pending",
                    offer_feed_id=str(submission["feed_id"]),
                    increment_attempts=True,
                )
                submissions.append({**submission, "skus": group["skus"]})

    return {
        "status": "submitted" if repair_request.confirm and submissions else "previewed",
        "apple_error_items_found": len(apple_errors),
        "ready": len(payload_rows),
        "blocked": len(apple_errors) - len(payload_rows),
        "items": results,
        "submissions": submissions,
        "policy_note": (
            "Template resubmission can correct item-data errors, but Walmart may retain an "
            "Intellectual Property policy block until its separate review is satisfied."
        ),
    }


@app.get("/walmart/drafts/summary")
def walmart_drafts_summary() -> dict[str, Any]:
    return {
        "status": walmart_draft_status,
        "stored": repository.walmart_draft_summary(),
        "storage": "render_database",
        "walmart_feed_submitted": walmart_auto_publish_status.get("status") == "submitted",
        "note": (
            "Walmart Marketplace APIs do not expose Seller Center draft creation. "
            "The automatic publisher uses setup-by-match for existing catalog items and "
            "Walmart's returned current-spec template for full item setup. Ambiguous records stay staged."
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


@app.get("/walmart/auto-publish/status")
def walmart_auto_publish_current_status() -> dict[str, Any]:
    return {
        **walmart_auto_publish_status,
        "enabled": settings.walmart_auto_publish_enabled,
        "interval_seconds": max(900, int(settings.walmart_auto_publish_interval_seconds)),
        "initial_delay_seconds": max(60, int(settings.walmart_auto_publish_initial_delay_seconds)),
        "catalog_limit": int(settings.walmart_auto_publish_catalog_limit),
        "gtin_lookup": {
            "enabled": bool(settings.walmart_gtin_lookup_enabled),
            "configured": product_identifier_lookup.configured,
            "model": settings.walmart_gtin_lookup_model,
            "max_per_run": int(settings.walmart_gtin_lookup_max_per_run),
            "retry_seconds": int(settings.walmart_gtin_lookup_retry_seconds),
            **(
                walmart_auto_publish_status.get("gtin_lookup")
                if isinstance(walmart_auto_publish_status.get("gtin_lookup"), dict)
                else {}
            ),
        },
        "excluded_terms": [
            term.strip()
            for term in str(settings.walmart_auto_publish_excluded_terms or "").split(",")
            if term.strip()
        ],
        "startup_retry": repository.service_run_marker(WALMART_OPEN_BOX_RETRY_MARKER),
        "publish_failures": repository.walmart_draft_publish_failures(),
        "stored": repository.walmart_draft_summary(),
    }


@app.post("/walmart/auto-publish/run")
async def run_walmart_auto_publish(
    auto_request: WalmartAutoPublishRequest,
    request: Request,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    return await _run_walmart_auto_publish_once(auto_request)


@app.post("/walmart/auto-publish/reconcile")
async def reconcile_walmart_auto_publish(
    request: Request,
    confirm: bool = False,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    return await _reconcile_walmart_auto_publish_feeds(
        confirm_inventory_actions=confirm
    )


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
    offer_groups = _walmart_offer_groups_from_preflight(preview)
    submissions: list[dict[str, Any]] = []
    submission_errors: list[dict[str, Any]] = []
    for group in offer_groups:
        try:
            if group["feed_type"] == "MP_ITEM":
                submission = await walmart_client.submit_full_item_feed(group["payload"])
            else:
                submission = await walmart_client.submit_offer_match_feed(group["payload"])
        except WalmartApiError as exc:
            repository.update_walmart_draft_publish_state(
                group["skus"], "offer_failed", error_message=str(exc)
            )
            submission_errors.append(
                {"feed_type": group["feed_type"], "skus": group["skus"], "message": str(exc)}
            )
        else:
            repository.update_walmart_draft_publish_state(
                group["skus"],
                "offer_submitted_inventory_pending",
                offer_feed_id=str(submission["feed_id"]),
                increment_attempts=True,
            )
            submissions.append({**submission, "skus": group["skus"]})
    if not submissions:
        message = (
            submission_errors[0]["message"]
            if submission_errors
            else "No Walmart item-setup payloads were generated."
        )
        walmart_sync_status = {
            "status": "failed",
            "configured": walmart_client.configured,
            "last_submission": None,
            "message": message,
            "last_attempt_at": datetime.now(timezone.utc).isoformat(),
        }
        raise HTTPException(status_code=502, detail=message)

    walmart_sync_status = {
        "status": "submitted" if not submission_errors else "partially_submitted",
        "configured": True,
        "last_submission": submissions,
        "submitted_items": sum(len(submission["skus"]) for submission in submissions),
        "submission_errors": submission_errors,
        "last_attempt_at": datetime.now(timezone.utc).isoformat(),
    }
    first_submission = submissions[0]
    return {
        "status": walmart_sync_status["status"],
        "feed_id": first_submission["feed_id"],
        "feed_type": first_submission["feed_type"],
        "submissions": submissions,
        "submission_errors": submission_errors,
        "submitted_items": walmart_sync_status["submitted_items"],
        "blocked_items": preview["blocked"],
        "items": preview["items"],
        "next_step": "Check each returned feed ID until every feed is PROCESSED.",
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


@app.post("/walmart/import/reconcile")
async def reconcile_walmart_import(
    reconcile_request: WalmartOfferFeedReconcileRequest,
    request: Request,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    if not reconcile_request.confirm:
        raise HTTPException(
            status_code=400,
            detail="Set confirm=true to persist this Walmart offer feed result.",
        )
    try:
        feed = await walmart_client.get_feed_status(
            reconcile_request.feed_id,
            include_details=True,
        )
    except WalmartApiError as exc:
        raise _walmart_http_error(exc) from exc

    feed_status = str(feed.get("feedStatus") or "").upper()
    repository.update_walmart_draft_publish_state(
        reconcile_request.skus,
        "offer_submitted_inventory_pending",
        offer_feed_id=reconcile_request.feed_id,
    )
    states: dict[str, str] = {}
    if feed_status == "PROCESSED":
        states = _apply_walmart_offer_feed_results(feed, set(reconcile_request.skus))
    return {
        "feed_id": reconcile_request.feed_id,
        "feed_status": feed_status,
        "states": states,
    }


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


@app.get("/inventory/sync/marketplaces/status")
def marketplace_inventory_status() -> dict[str, Any]:
    return {
        **marketplace_inventory_sync_status,
        "enabled": settings.marketplace_inventory_sync_enabled,
        "interval_seconds": max(30, int(settings.marketplace_inventory_sync_interval_seconds)),
        "stored": repository.marketplace_inventory_sync_summary(),
    }


@app.post("/inventory/sync/marketplaces")
async def sync_marketplace_inventory(
    request: Request,
    confirm: bool = False,
    x_horizon_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    verify_secret(x_horizon_secret, request.query_params.get("secret"))
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="Set confirm=true to reconcile live eBay and published Walmart quantities.",
        )
    if not walmart_client.configured:
        raise HTTPException(
            status_code=503,
            detail="WALMART_CLIENT_ID and WALMART_CLIENT_SECRET are not configured.",
        )
    return await _run_marketplace_inventory_sync_once()


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
        # This webhook is called once per hour by Render. Keep the rotation
        # idempotent even if a caller supplies stale bulk/backfill options.
        ignore_recent_history=False,
        query="all inventory",
        max_products_per_run=1,
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
