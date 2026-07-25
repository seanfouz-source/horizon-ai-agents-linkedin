import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.integrations import METRICOOL_PUBLICATION_FORMAT, POSTING_TIMEZONE
from app.reports import (
    METRICOOL_BASE_URL,
    MetricoolReportError,
    _resolve_metricool_brand,
    _retrieve_scheduled_posts,
)


logger = logging.getLogger(__name__)
METRICOOL_NETWORKS = ("facebook", "instagram", "tiktok", "linkedin")
METRICOOL_PUBLISH_CONCURRENCY = 4
METRICOOL_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class MetricoolPublishError(RuntimeError):
    pass


async def scheduled_post_counts_by_day(
    *,
    start_at: str | None,
    days: int,
    settings: Settings | None = None,
) -> dict[str, int]:
    resolved_settings = settings or get_settings()
    if not resolved_settings.metricool_api_token:
        return {}

    start_date = _start_date(start_at)
    day_count = max(1, min(days, 60))

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            brand = await _resolve_metricool_brand(client, resolved_settings)
            counts: dict[str, int] = {}
            for offset in range(day_count):
                report_date = start_date + timedelta(days=offset)
                posts = await _retrieve_scheduled_posts(client, resolved_settings, brand, report_date)
                counts[report_date.isoformat()] = len(posts)
            return counts
    except (httpx.HTTPError, MetricoolReportError, ValueError, KeyError) as exc:
        logger.warning("Could not check existing Metricool scheduled posts: %s", exc)
        return {}


async def schedule_metricool_payloads(
    payloads: list[dict[str, object]],
    *,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, object]]:
    """Create Metricool planner posts from the app's Zapier-compatible payloads."""
    resolved_settings = settings or get_settings()
    if not resolved_settings.metricool_api_token:
        raise MetricoolPublishError("METRICOOL_API_TOKEN is not configured.")
    if not payloads:
        return []

    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=60)
    try:
        brand = await _resolve_metricool_brand(active_client, resolved_settings)
        semaphore = asyncio.Semaphore(METRICOOL_PUBLISH_CONCURRENCY)

        async def schedule_one(index: int, payload: dict[str, object]) -> dict[str, object]:
            async with semaphore:
                try:
                    post = await _schedule_metricool_payload(
                        active_client,
                        resolved_settings,
                        brand,
                        payload,
                    )
                except (httpx.HTTPError, MetricoolPublishError, ValueError, KeyError) as exc:
                    logger.warning("Metricool post %s failed: %s", index, exc)
                    return {
                        "index": index,
                        "status": "failed",
                        "publication_date_time": _publication_date_time(payload),
                        "product_sku": payload.get("product_sku"),
                        "error": str(exc),
                    }
                return {
                    "index": index,
                    "status": "scheduled",
                    "metricool_post_id": post.get("id"),
                    "publication_date_time": _publication_date_time(payload),
                    "product_sku": payload.get("product_sku"),
                    "providers": [
                        provider.get("network")
                        for provider in post.get("providers", [])
                        if isinstance(provider, dict) and provider.get("network")
                    ],
                }

        return await asyncio.gather(
            *(schedule_one(index, payload) for index, payload in enumerate(payloads))
        )
    finally:
        if owns_client:
            await active_client.aclose()


async def _schedule_metricool_payload(
    client: httpx.AsyncClient,
    settings: Settings,
    brand: dict[str, Any],
    payload: dict[str, object],
) -> dict[str, Any]:
    body = await _scheduled_post_body(client, settings, brand, payload)
    response = await _metricool_request(
        client,
        settings,
        "POST",
        "/v2/scheduler/posts",
        params=_brand_params(brand),
        json=body,
    )
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        raise MetricoolPublishError("Metricool did not return the created scheduled post.")
    return data


async def _scheduled_post_body(
    client: httpx.AsyncClient,
    settings: Settings,
    brand: dict[str, Any],
    payload: dict[str, object],
) -> dict[str, object]:
    publication_date_time = _publication_date_time(payload)
    if not publication_date_time:
        raise MetricoolPublishError("Metricool payload is missing publication_date_time.")
    text = str(payload.get("post_content") or "").strip()
    if not text:
        raise MetricoolPublishError("Metricool payload is missing post_content.")

    providers = [
        {"network": network}
        for network in METRICOOL_NETWORKS
        if payload.get(network) is True
    ]
    if not providers:
        raise MetricoolPublishError("Metricool payload has no enabled social networks.")

    media_url = str(payload.get("media_01") or "").strip()
    normalized_media_url = (
        await _normalize_metricool_image_url(client, settings, brand, media_url)
        if media_url
        else None
    )
    draft = bool(payload.get("as_draft", payload.get("draft", False)))
    auto_publish = bool(payload.get("auto_publish", True))
    body: dict[str, object] = {
        "publicationDate": {
            "dateTime": publication_date_time.replace(" ", "T"),
            "timezone": str(POSTING_TIMEZONE),
        },
        "text": text,
        "firstCommentText": "",
        "providers": providers,
        "autoPublish": auto_publish,
        "saveExternalMediaFiles": True,
        "shortener": False,
        "draft": draft,
    }
    if normalized_media_url:
        body["media"] = [normalized_media_url]
        title = str(payload.get("product_title") or "Horizon Wireless product").strip()
        body["mediaAltText"] = [f"{title} product photo"]

    enabled_networks = {provider["network"] for provider in providers}
    if "facebook" in enabled_networks:
        body["facebookData"] = {"type": "POST"}
    if "instagram" in enabled_networks:
        body["instagramData"] = {"autoPublish": auto_publish, "type": "POST"}
    if "tiktok" in enabled_networks:
        body["tiktokData"] = {"photoCoverIndex": 0}
    if "linkedin" in enabled_networks:
        body["linkedinData"] = {"previewIncluded": True, "type": "POST"}
    return body


async def _normalize_metricool_image_url(
    client: httpx.AsyncClient,
    settings: Settings,
    brand: dict[str, Any],
    media_url: str,
) -> str:
    response = await _metricool_request(
        client,
        settings,
        "GET",
        "/actions/normalize/image/url",
        params={**_brand_params(brand), "url": media_url},
    )
    if isinstance(response, str) and response.strip():
        return response.strip()
    if isinstance(response, dict):
        for key in ("data", "url", "mediaUrl"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    raise MetricoolPublishError("Metricool could not normalize the product image URL.")


async def _metricool_request(
    client: httpx.AsyncClient,
    settings: Settings,
    method: str,
    path: str,
    *,
    params: dict[str, object],
    json: dict[str, object] | None = None,
) -> Any:
    response: httpx.Response | None = None
    for attempt in range(3):
        response = await client.request(
            method,
            f"{METRICOOL_BASE_URL}{path}",
            params=params,
            json=json,
            headers={
                "X-Mc-Auth": settings.metricool_api_token or "",
                "Content-Type": "application/json",
            },
        )
        if response.status_code not in METRICOOL_RETRYABLE_STATUS_CODES:
            break
        await asyncio.sleep(2**attempt)

    if response is None:
        raise MetricoolPublishError(f"Metricool {path} did not return a response.")
    if response.status_code >= 400:
        raise MetricoolPublishError(
            f"Metricool {path} returned HTTP {response.status_code}: {response.text[:300]}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise MetricoolPublishError(f"Metricool {path} did not return JSON.") from exc


def _publication_date_time(payload: dict[str, object]) -> str:
    value = payload.get("publication_date_time") or payload.get("publicationDate")
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate:
        return ""
    try:
        parsed = datetime.strptime(candidate.replace("T", " "), METRICOOL_PUBLICATION_FORMAT)
    except ValueError as exc:
        raise MetricoolPublishError(
            f"Invalid Metricool publication date/time: {candidate}"
        ) from exc
    return parsed.strftime(METRICOOL_PUBLICATION_FORMAT)


def _brand_params(brand: dict[str, Any]) -> dict[str, object]:
    return {"blogId": brand["blog_id"], "userId": brand["user_id"]}


def _start_date(start_at: str | None) -> date:
    if start_at:
        try:
            return datetime.strptime(start_at, METRICOOL_PUBLICATION_FORMAT).date()
        except ValueError:
            pass
    return datetime.now(POSTING_TIMEZONE).date()
