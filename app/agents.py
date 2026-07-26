import json
import logging
import re
from datetime import datetime, time, timedelta
from typing import cast

from agents import Agent, Runner, function_tool

from app.campaigns import request_campaign_media_url
from app.config import get_settings
from app.integrations import (
    METRICOOL_PUBLICATION_FORMAT,
    POSTING_MINIMUM_LEAD_TIME,
    POSTING_TIMEZONE,
    apply_tiktok_daily_post_cap,
    default_metricool_publication_times,
    metricool_payload,
)
from app.inventory import InventoryRepository
from app.metricool import scheduled_inventory_state
from app.models import (
    CustomerAnswer,
    CustomerQuestion,
    GroupOutreachPlan,
    GroupOutreachRequest,
    GroupReplyDraft,
    GroupReplyRequest,
    InventoryItem,
    SlowMoverMetric,
    SlowMoverOutreachDraft,
    SlowMoverOutreachPlan,
    SlowMoverOutreachRequest,
    SocialDraftBatch,
    SocialDraftPlan,
    SocialDraftRequest,
    SocialPlatform,
    SocialPost,
)


logger = logging.getLogger(__name__)
INVENTORY_ROTATION_CANDIDATE_LIMIT = 200
INVENTORY_POSTS_PER_HOUR = 1
INVENTORY_POSTING_INTERVAL = timedelta(hours=1)
INVENTORY_DEFAULT_START_TIME = time(9, 0)
INVENTORY_DEFAULT_END_TIME = time(21, 0)


def get_repository() -> InventoryRepository:
    settings = get_settings()
    return InventoryRepository(settings.resolved_database_path)


def _item_summary(item) -> dict[str, object]:
    return {
        "sku": item.sku,
        "title": item.title,
        "description": item.description,
        "condition": item.condition,
        "price": item.price,
        "currency": item.currency,
        "quantity": item.quantity,
        "ebay_url": item.ebay_url,
        "image_url": item.image_url,
        "image_urls": item.image_urls,
        "category": item.category,
        "listing_status": item.listing_status,
        "item_specifics": item.item_specifics,
    }


@function_tool
def search_stock(query: str, limit: int = 6) -> str:
    """Search current eBay inventory by SKU, title, description, category, or item specifics."""
    items = get_repository().search(query, limit=limit)
    return json.dumps([_item_summary(item) for item in items])


@function_tool
def get_stock_item(sku: str) -> str:
    """Get full details for one inventory item by SKU."""
    item = get_repository().get(sku)
    if item is None:
        return json.dumps({"error": "No item found for that SKU."})
    return json.dumps(_item_summary(item))


@function_tool
def list_promotable_stock(limit: int = 8) -> str:
    """List in-stock eBay items that are ready to promote."""
    items = get_repository().all_promotable(limit=limit)
    return json.dumps([_item_summary(item) for item in items])


STOPWORDS = {
    "about",
    "available",
    "carry",
    "have",
    "hello",
    "iphone",
    "iphones",
    "need",
    "please",
    "stock",
    "there",
    "with",
    "you",
    "your",
}

PHONE_KEYWORDS = {
    "iphone",
    "samsung",
    "galaxy",
    "motorola",
    "moto",
    "pixel",
    "phone",
    "smartphone",
    "oneplus",
    "nokia",
    "xperia",
    "flip",
    "edge",
}


def _candidate_inventory_queries(message: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z0-9]+", message.lower())
    candidates: list[str] = []
    for token in tokens:
        normalized = token[:-1] if token.endswith("s") and len(token) > 4 else token
        if normalized == "iphon":
            normalized = "iphone"
        if normalized in STOPWORDS and normalized != "iphone":
            continue
        if len(normalized) < 3:
            continue
        if normalized not in candidates:
            candidates.append(normalized)
    return candidates


def _matched_items_for_message(message: str, repository: InventoryRepository | None = None):
    repository = repository or get_repository()
    matched_items = [item for item in repository.search(message, limit=3) if _is_active_inventory_answer_item(item)]
    if matched_items:
        return matched_items
    for query in _candidate_inventory_queries(message):
        matched_items = [item for item in repository.search(query, limit=3) if _is_active_inventory_answer_item(item)]
        if matched_items:
            return matched_items
    return []


def _is_active_inventory_answer_item(item: InventoryItem) -> bool:
    if item.quantity <= 0:
        return False
    status = (item.listing_status or "ACTIVE").strip().upper()
    return status in {"ACTIVE", "IN_STOCK", "PUBLISHED", "LIVE"}


CUSTOMER_AGENT_INSTRUCTIONS = """
You answer shopper questions for Horizon's eBay inventory.

Rules:
- Always use inventory tools before answering questions about availability, price, condition, fit, specs, or links.
- Only say an item is available when inventory data shows quantity above zero.
- If you cannot find the exact item, ask one short clarifying question and offer the closest matches.
- Keep replies short enough for social DMs.
- Include the eBay link when there is a relevant listing URL.
- Do not invent discounts, shipping promises, warranties, compatibility, or stock details.
- If a buyer asks for negotiation, returns, order problems, or personal account details, suggest a human follow-up.
"""


SOCIAL_AGENT_INSTRUCTIONS = """
You create social media post drafts for promoting Horizon's eBay listings.

Rules:
- Use inventory tools to ground every post in real in-stock products.
- Write platform-native copy for Facebook, Instagram, TikTok, and LinkedIn.
- Make the eBay listing the call to action when an eBay URL is available.
- Do not invent sale prices, discounts, free shipping, scarcity, or claims that are not in inventory data.
- Avoid spammy wording, excessive hashtags, all caps, or engagement bait.
- For TikTok, write caption-style copy that can accompany a short product video.
- For LinkedIn, write more professional product copy for a business/audience feed.
- Leave suggested_schedule blank; the backend assigns exact Metricool publish times.
- Return structured output only.
"""


GROUP_OUTREACH_AGENT_INSTRUCTIONS = """
You create compliant Facebook Group outreach plans for Horizon Wireless.

Rules:
- Do not tell anyone to auto-join groups, scrape members, cold-DM members, or bypass group rules.
- Treat Facebook Group posts and comments as manual-review work unless the user has an approved, supported inbox/DM trigger.
- Prioritize groups only when the audience and rules are compatible with eBay listings, wholesale phones, resellers, repair shops, or electronics buyers.
- If rules are missing or unclear, recommend review instead of posting.
- Draft short, non-spammy posts that disclose Horizon Wireless and avoid hype, fake scarcity, discounts, or unsupported claims.
- Use the eBay store and campaign video when provided.
- Return structured output only.
"""


def _customer_agent() -> Agent:
    settings = get_settings()
    return Agent(
        name="eBay Customer Support Agent",
        model=settings.openai_model,
        instructions=CUSTOMER_AGENT_INSTRUCTIONS,
        tools=[search_stock, get_stock_item],
    )


def _social_agent() -> Agent:
    settings = get_settings()
    return Agent(
        name="Social Promotion Agent",
        model=settings.openai_model,
        instructions=SOCIAL_AGENT_INSTRUCTIONS,
        tools=[search_stock, get_stock_item, list_promotable_stock],
        output_type=SocialDraftPlan,
    )


def _group_outreach_agent() -> Agent:
    settings = get_settings()
    return Agent(
        name="Facebook Group Outreach Planner",
        model=settings.openai_model,
        instructions=GROUP_OUTREACH_AGENT_INSTRUCTIONS,
        tools=[search_stock, list_promotable_stock],
        output_type=GroupOutreachPlan,
    )


async def answer_customer_question(question: CustomerQuestion) -> CustomerAnswer:
    repository = get_repository()
    context_item = _item_from_customer_context(question, repository)
    matched_items = _matched_items_for_message(question.message, repository)
    if matched_items and not context_item and _should_filter_recommendations(question.message):
        matched_items = _filter_recommendations(question.message, matched_items)
        if "cheapest" in question.message.lower() or "under" in question.message.lower():
            matched_items = sorted(matched_items, key=lambda item: item.price if item.price is not None else 999999)
    if context_item:
        matched_items = _dedupe_inventory_items([context_item, *matched_items])

    social_post_id = _metadata_value(
        question.metadata,
        "post_id",
        "facebook_post_id",
        "instagram_post_id",
        "metricool_post_id",
        "history_id",
    )
    conversation_id = _metadata_value(
        question.metadata,
        "conversation_id",
        "messenger_conversation_id",
        "thread_id",
        "chat_id",
    )
    primary_item = matched_items[0] if matched_items else None
    redirect_to_ebay = _requires_ebay_redirect(question.message)

    if primary_item and redirect_to_ebay:
        reply = _ebay_redirect_reply(primary_item, question.message)
        recommended_items: list[InventoryItem] = []
        conversation_allowed = False
        needs_human = _is_order_support_message(question.message)
    elif primary_item and not _is_active_inventory_answer_item(primary_item):
        recommended_items = _recommend_inventory_items(question.message, primary_item, repository)
        reply = _sold_or_ended_reply(primary_item, recommended_items)
        conversation_allowed = True
        needs_human = False
    elif primary_item:
        recommended_items = (
            _dedupe_inventory_items([item for item in matched_items if item.sku != primary_item.sku])[:3]
            if _message_requests_multiple_options(question.message)
            else []
        )
        reply = _manychat_inventory_reply(question.message, matched_items, repository)
        conversation_allowed = True
        needs_human = False
    else:
        recommended_items = _recommend_inventory_items(question.message, None, repository)
        reply = _manychat_no_exact_match_reply(question.message, recommended_items)
        conversation_allowed = True
        needs_human = False

    logger.info(
        "ManyChat eBay sales assistant handled message: profile_id=%s post_id=%s conversation_id=%s "
        "ebay_item_id=%s redirected_to_ebay=%s stayed_in_messenger=%s recommendations=%s",
        question.user_id,
        social_post_id,
        conversation_id,
        _item_ebay_item_id(primary_item) if primary_item else None,
        redirect_to_ebay,
        conversation_allowed,
        [_item_ebay_item_id(item) for item in recommended_items],
    )
    return CustomerAnswer(
        reply=reply,
        channel=question.channel,
        matched_items=matched_items,
        recommended_items=recommended_items,
        needs_human=needs_human,
        redirect_to_ebay=redirect_to_ebay,
        conversation_allowed=conversation_allowed,
        ebay_item_id=_item_ebay_item_id(primary_item) if primary_item else None,
        ebay_listing_url=_buy_url_for_item(primary_item) if primary_item else None,
        social_post_id=social_post_id,
        messenger_conversation_id=conversation_id,
    )


ORDER_SUPPORT_KEYWORDS = {
    "address change",
    "buyer protection",
    "cancel",
    "cancellation",
    "damaged",
    "delivery",
    "dispute",
    "order",
    "payment",
    "refund",
    "return",
    "tracking",
    "warranty",
}


PURCHASE_REDIRECT_KEYWORDS = {
    "best price",
    "buy",
    "cash app",
    "cashapp",
    "checkout",
    "discount",
    "invoice",
    "lower price",
    "make an offer",
    "negotiate",
    "offer",
    "pay",
    "payment",
    "purchase",
    "venmo",
    "zelle",
}


def _requires_ebay_redirect(message: str) -> bool:
    normalized = message.lower()
    if _asks_listing_return_policy(normalized):
        return False
    return any(keyword in normalized for keyword in ORDER_SUPPORT_KEYWORDS | PURCHASE_REDIRECT_KEYWORDS)


def _is_order_support_message(message: str) -> bool:
    normalized = message.lower()
    if _asks_listing_return_policy(normalized):
        return False
    return any(keyword in normalized for keyword in ORDER_SUPPORT_KEYWORDS)


def _asks_listing_return_policy(normalized_message: str) -> bool:
    return "return policy" in normalized_message or (
        "return" in normalized_message and any(word in normalized_message for word in ("accept", "accepted", "policy"))
    )


def _manychat_inventory_reply(
    message: str,
    matched_items: list[InventoryItem],
    repository: InventoryRepository,
) -> str:
    items_to_send = matched_items[:3] if _message_requests_multiple_options(message) else matched_items[:1]
    if len(items_to_send) == 1:
        item = items_to_send[0]
        answer_line = _pre_sale_answer_line(message, item)
        qualifier = _qualifying_question(message)
        reply = (
            f"{answer_line}\n\n"
            f"{item.title}\n"
            f"Condition: {item.condition or 'See eBay listing'}\n"
            f"Price: {_manychat_price(item)}\n"
            f"Availability: {_availability_text(item)}\n\n"
            "You can ask me general product questions here. "
            f"For the full listing or to message us directly through eBay: {_buy_url_for_item(item)}"
        )
        if qualifier:
            reply += f"\n\n{qualifier}"
        return reply

    lines = ["Here are a few active eBay listings that match:"]
    for item in items_to_send:
        lines.append(_manychat_listing_line(item))
    lines.append("\nI can compare these here, but checkout, offers, and order support should happen through eBay.")
    return "\n".join(lines)


def _pre_sale_answer_line(message: str, item: InventoryItem) -> str:
    normalized = message.lower()
    if _asks_availability(normalized):
        return "Yes, this item is currently available." if item.quantity > 0 else "This listing is no longer available."
    if any(keyword in normalized for keyword in ("price", "cost", "how much")):
        return f"The listed price is {_manychat_price(item)}."
    if "condition" in normalized:
        return f"The listed condition is {item.condition or 'shown on the eBay listing'}."
    if "unlocked" in normalized:
        unlocked = _specific_value(item, "unlocked", "lock status", "network")
        if unlocked:
            return f"The listing shows: {unlocked}."
        return _title_confirms_or_listing("unlocked", item)
    if "carrier" in normalized or "compatible" in normalized:
        value = _specific_value(item, "carrier", "network", "lock status")
        return f"The listed carrier/network detail is: {value}." if value else "Carrier compatibility is shown on the eBay listing."
    if "storage" in normalized or re.search(r"\b\d+\s?gb\b", normalized):
        value = _specific_value(item, "storage", "capacity", "hard drive capacity")
        return f"The listed storage/capacity is: {value}." if value else _title_confirms_or_listing("storage", item)
    if "color" in normalized or "colour" in normalized:
        value = _specific_value(item, "color", "colour")
        return f"The listed color is: {value}." if value else _title_confirms_or_listing("color", item)
    if "model" in normalized:
        value = _specific_value(item, "model")
        return f"The listed model is: {value}." if value else f"The listing title shows: {item.title}."
    if "shipping" in normalized or "ship" in normalized:
        value = _specific_value(item, "shipping", "shipping cost", "delivery")
        return f"The listing shipping detail shows: {value}." if value else "Shipping estimates and options are shown on the eBay listing."
    if "return" in normalized:
        value = _specific_value(item, "return", "returns", "return policy")
        return f"The listing return policy shows: {value}." if value else "The return policy is shown on the eBay listing."
    if "accessor" in normalized or "included" in normalized or "box" in normalized:
        value = _specific_value(item, "accessories", "included", "package", "bundle")
        return f"The listing included-items detail shows: {value}." if value else "Included accessories are described in the eBay listing."
    return "Thanks for your interest. I can help answer general product questions here."


def _ebay_redirect_reply(item: InventoryItem, message: str) -> str:
    if _is_order_support_message(message):
        return (
            "I can help with general product questions here, but order status, returns, warranty, payment, "
            "address changes, cancellations, damaged shipment issues, and buyer protection need to be handled "
            "through eBay messages because they are tied to your eBay order record.\n\n"
            f"Please message us directly through this eBay listing: {_buy_url_for_item(item)}"
        )
    if any(keyword in message.lower() for keyword in ("offer", "lower price", "best price", "negotiate", "discount")):
        return (
            "Pricing and offers are handled through eBay. If offers are enabled, you can send an offer directly "
            f"through the listing here: {_buy_url_for_item(item)}"
        )
    return (
        "Thanks for your interest. I can help answer general product questions here. "
        f"This item is available on our eBay store here: {_buy_url_for_item(item)}. "
        "For purchase, offers, checkout, order status, or buyer protection, please message us directly through the eBay listing."
    )


def _sold_or_ended_reply(item: InventoryItem, recommendations: list[InventoryItem]) -> str:
    lines = [
        f"That listing is no longer available on eBay: {item.title}.",
    ]
    if recommendations:
        lines.append("\nHere are similar active listings:")
        lines.extend(_manychat_listing_line(recommendation) for recommendation in recommendations[:3])
    else:
        lines.append(f"\nYou can browse current listings here: {get_settings().ebay_store_backup_url or get_settings().ebay_store_url}")
    return "\n".join(lines)


def _manychat_no_exact_match_reply(message: str, recommendations: list[InventoryItem]) -> str:
    if recommendations:
        lines = ["I do not see that exact item, but these active eBay listings may fit:"]
        lines.extend(_manychat_listing_line(item) for item in recommendations[:3])
        lines.append("\nWhich carrier, storage size, color, or budget range are you trying to stay within?")
        return "\n".join(lines)
    return (
        "I do not see an exact active eBay listing for that yet. "
        "Which carrier, model, storage size, color, and budget range are you looking for? "
        f"You can also browse current listings here: {get_settings().ebay_store_backup_url or get_settings().ebay_store_url}"
    )


def _manychat_listing_line(item: InventoryItem) -> str:
    return (
        "\n"
        f"{item.title}\n"
        f"Condition: {item.condition or 'See eBay listing'}\n"
        f"Price: {_manychat_price(item)}\n"
        f"Link: {_buy_url_for_item(item)}"
    )


def _message_requests_multiple_options(message: str) -> bool:
    normalized = message.lower()
    return any(
        phrase in normalized
        for phrase in (
            "do you have",
            "what do you have",
            "show me",
            "options",
            "any",
            "iphones",
            "phones",
            "tablets",
            "watches",
            "computers",
            "alternatives",
            "cheapest",
            "under",
            "newer model",
        )
    )


def _should_filter_recommendations(message: str) -> bool:
    normalized = message.lower()
    return _message_requests_multiple_options(message) or any(
        token in normalized
        for token in ("unlocked", "carrier", "compatible", "storage", "color", "colour")
    )


def _item_from_customer_context(question: CustomerQuestion, repository: InventoryRepository) -> InventoryItem | None:
    metadata = question.metadata or {}
    for value in _metadata_values(
        metadata,
        "sku",
        "product_sku",
        "metricool_product_sku",
    ):
        item = repository.get(value)
        if item:
            return item
    for value in _metadata_values(
        metadata,
        "ebay_item_id",
        "item_id",
        "metricool_ebay_item_id",
        "product_item_id",
    ):
        item = repository.get_by_ebay_item_id(value) if hasattr(repository, "get_by_ebay_item_id") else repository.get(f"EBAY-{value}")
        if item:
            return item
    for value in _metadata_values(metadata, "ebay_url", "link_url", "buy_url", "metricool_ebay_url"):
        item_id = _item_id_from_text(value)
        if not item_id:
            continue
        item = repository.get_by_ebay_item_id(item_id) if hasattr(repository, "get_by_ebay_item_id") else repository.get(f"EBAY-{item_id}")
        if item:
            return item
    for value in _metadata_values(
        metadata,
        "post_id",
        "facebook_post_id",
        "instagram_post_id",
        "metricool_post_id",
        "history_id",
    ):
        if hasattr(repository, "item_for_social_reference"):
            item = repository.item_for_social_reference(value)
            if item:
                return item
    item_id = _item_id_from_text(question.message)
    if item_id:
        item = repository.get_by_ebay_item_id(item_id) if hasattr(repository, "get_by_ebay_item_id") else repository.get(f"EBAY-{item_id}")
        if item:
            return item
    return None


def _metadata_value(metadata: dict[str, str], *keys: str) -> str | None:
    values = _metadata_values(metadata, *keys)
    return values[0] if values else None


def _metadata_values(metadata: dict[str, str], *keys: str) -> list[str]:
    normalized_lookup = {str(key).lower(): str(value) for key, value in metadata.items() if str(value).strip()}
    values = []
    for key in keys:
        value = normalized_lookup.get(key.lower())
        if value and value not in values:
            values.append(value)
    return values


def _item_id_from_text(value: str) -> str | None:
    match = re.search(r"ebay\.com/itm/(?:[^/?#\s]+/)?(\d+)", value)
    if match:
        return match.group(1)
    match = re.search(r"\bEBAY-(\d+)\b", value)
    if match:
        return match.group(1)
    match = re.search(r"\b(\d{10,15})\b", value)
    return match.group(1) if match else None


def _dedupe_inventory_items(items: list[InventoryItem]) -> list[InventoryItem]:
    deduped = []
    seen = set()
    for item in items:
        key = _item_ebay_item_id(item) or item.sku
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _recommend_inventory_items(
    message: str,
    reference_item: InventoryItem | None,
    repository: InventoryRepository,
) -> list[InventoryItem]:
    query = _recommendation_query(message, reference_item)
    candidates = [item for item in repository.search(query, limit=25) if _is_active_inventory_answer_item(item)]
    if not candidates and hasattr(repository, "all_promotable"):
        candidates = [item for item in repository.all_promotable(limit=25) if _is_active_inventory_answer_item(item)]
    candidates = [item for item in candidates if not reference_item or item.sku != reference_item.sku]
    candidates = _filter_recommendations(message, candidates)
    if "cheapest" in message.lower() or "under" in message.lower():
        candidates = sorted(candidates, key=lambda item: item.price if item.price is not None else 999999)
    return _dedupe_inventory_items(candidates)[:3]


def _recommendation_query(message: str, reference_item: InventoryItem | None) -> str:
    normalized = message.lower()
    if "samsung" in normalized:
        return "Samsung"
    if "iphone" in normalized or "apple" in normalized:
        return "iPhone"
    if "watch" in normalized:
        return "Watch"
    if "tablet" in normalized or "ipad" in normalized:
        return "tablet"
    if reference_item:
        title_tokens = [
            token
            for token in re.findall(r"[A-Za-z0-9]+", reference_item.title)
            if len(token) > 2 and token.lower() not in STOPWORDS
        ]
        return " ".join(title_tokens[:3]) or reference_item.title
    return message


def _filter_recommendations(message: str, items: list[InventoryItem]) -> list[InventoryItem]:
    normalized = message.lower()
    filtered = items
    budget_match = re.search(r"(?:under|below|less than)\s*\$?\s*(\d+)", normalized)
    if budget_match:
        budget = float(budget_match.group(1))
        filtered = [item for item in filtered if item.price is not None and item.price <= budget]
    storage_match = re.search(r"\b(\d{2,4})\s?gb\b", normalized)
    if storage_match:
        storage = storage_match.group(1)
        filtered = [item for item in filtered if storage in _inventory_search_text(item)]
    for color in ("black", "white", "blue", "red", "green", "pink", "purple", "gold", "silver"):
        if color in normalized:
            filtered = [item for item in filtered if color in _inventory_search_text(item)]
            break
    if "unlocked" in normalized:
        filtered = [item for item in filtered if "unlocked" in _inventory_search_text(item)]
    return filtered or items


def _inventory_search_text(item: InventoryItem) -> str:
    return " ".join(
        [
            item.title,
            item.description or "",
            item.condition or "",
            item.category or "",
            " ".join(f"{key} {value}" for key, value in item.item_specifics.items()),
        ]
    ).lower()


def _asks_availability(normalized_message: str) -> bool:
    return any(phrase in normalized_message for phrase in ("available", "in stock", "still have", "sold"))


def _availability_text(item: InventoryItem) -> str:
    if item.quantity > 0 and _is_active_inventory_answer_item(item):
        return "Available on eBay"
    return "No longer available"


def _specific_value(item: InventoryItem, *keys: str) -> str | None:
    normalized_keys = [key.lower() for key in keys]
    for key, value in item.item_specifics.items():
        key_lower = str(key).lower()
        if any(target in key_lower for target in normalized_keys) and str(value).strip():
            return str(value).strip()
    return None


def _title_confirms_or_listing(term: str, item: InventoryItem) -> str:
    if term.lower() in item.title.lower():
        return f"The listing title shows {term}: {item.title}."
    return f"That detail is shown on the eBay listing when available."


def _qualifying_question(message: str) -> str | None:
    normalized = message.lower()
    if any(phrase in normalized for phrase in ("not sure", "recommend", "which one", "looking for", "need a phone")):
        return "Which carrier, storage size, color, and budget range are you trying to stay within?"
    return None


def _manychat_price(item: InventoryItem) -> str:
    if item.price is None:
        return "See eBay listing"
    if float(item.price).is_integer():
        return f"${int(item.price)}"
    return f"${item.price:.2f}"


def _matched_items_from_reply(reply: str):
    repository = get_repository()
    items = []
    seen_skus = set()
    item_ids = re.findall(r"ebay\.com/itm/(?:[^/?#\s]+/)?(\d+)", reply)
    item_ids.extend(re.findall(r"\bEBAY-(\d+)\b", reply))
    for item_id in item_ids:
        sku = f"EBAY-{item_id}"
        if sku in seen_skus:
            continue
        item = repository.get(sku)
        if item:
            items.append(item)
            seen_skus.add(sku)
    return items


def _reply_indicates_no_inventory_match(reply: str) -> bool:
    normalized = reply.lower().replace("\u2019", "'")
    no_match_phrases = (
        "don't see",
        "do not see",
        "dont see",
        "don't have",
        "do not have",
        "dont have",
        "not currently",
        "not in our inventory",
        "not available",
        "no matching",
        "no exact",
    )
    return any(phrase in normalized for phrase in no_match_phrases)


async def create_social_drafts(request: SocialDraftRequest) -> SocialDraftBatch:
    if request.promote_all_inventory:
        return await _create_all_inventory_social_drafts(request)

    repository = get_repository()
    campaign_media_url = request_campaign_media_url(request)
    prompt = (
        "Create social drafts for this eBay promotion request.\n"
        f"{request.model_dump_json(indent=2)}"
    )
    if campaign_media_url:
        prompt += (
            "\nUse this campaign media URL for every generated post and write the copy so it fits the video: "
            f"{campaign_media_url}"
        )
    result = await Runner.run(_social_agent(), prompt, max_turns=6)
    plan = result.final_output
    if isinstance(plan, SocialDraftPlan):
        batch = SocialDraftBatch(
            campaign_name=plan.campaign_name,
            posts=plan.posts,
            notes=plan.notes,
        )
    else:
        batch = SocialDraftBatch(
            campaign_name="eBay product promotion",
            posts=[],
            notes=str(plan),
        )
    if campaign_media_url:
        for post in batch.posts:
            post.media_url = campaign_media_url
    if batch.posts:
        metricool_counts = await _metricool_existing_counts(len(batch.posts), request)
        batch.posts = _schedule_metricool_posts(repository, batch.posts, request, metricool_counts)
    batch.metricool_payloads = [metricool_payload(post, request) for post in batch.posts]
    _apply_tiktok_cap_to_batch(batch, request.tiktok_daily_post_cap)
    _record_metricool_payloads(repository, batch.posts, batch.metricool_payloads)
    return batch


def create_slow_mover_outreach(request: SlowMoverOutreachRequest) -> SlowMoverOutreachPlan:
    repository = get_repository()
    items = _slow_mover_items_for_outreach(repository, request)
    if not items:
        return SlowMoverOutreachPlan(
            campaign_name="Slow-mover social outreach",
            notes="No in-stock inventory items matched the slow-mover outreach request.",
        )

    metrics_by_sku = {metric.sku: metric for metric in request.slow_mover_metrics}
    drafts: list[SlowMoverOutreachDraft] = []
    posts: list[SocialPost] = []
    for item in items:
        metric = metrics_by_sku.get(item.sku)
        score, reason = _slow_mover_priority(item, metric)
        keyword = _comment_keyword_for_item(item)
        item_posts = _slow_mover_posts_for_item(item, request, keyword)
        drafts.append(
            SlowMoverOutreachDraft(
                sku=item.sku,
                title=item.title,
                ebay_url=_buy_url_for_item(item),
                priority_score=score,
                reason=reason,
                comment_keyword=keyword,
                manychat_reply=_slow_mover_manychat_reply(item),
                outreach_posts=item_posts,
                manual_outreach_notes=(
                    "Use this for public posts and replies to inbound comments only. "
                    "Do not cold-DM group members; review group rules before posting in groups."
                ),
            )
        )
        posts.extend(item_posts)

    social_request = SocialDraftRequest(
        brand_name=request.brand_name,
        platforms=request.platforms,
        promote_all_inventory=request.cross_post_to_all_platforms,
        cross_post_to_all_platforms=request.cross_post_to_all_platforms,
        publish_after=request.publish_after,
        tiktok_daily_post_cap=request.tiktok_daily_post_cap,
        as_draft=request.as_draft,
        auto_publish=request.auto_publish,
    )
    posts = _schedule_metricool_posts(repository, posts, social_request)
    metricool_payloads = [metricool_payload(post, social_request) for post in posts]
    suppressed_tiktok = apply_tiktok_daily_post_cap(metricool_payloads, request.tiktok_daily_post_cap)
    for payload, post in zip(metricool_payloads, posts, strict=False):
        payload["comment_keyword"] = _comment_keyword_for_sku(post.product_sku)
        payload["manychat_reply"] = _slow_mover_manychat_reply_from_post(post)
    _record_metricool_payloads(repository, posts, metricool_payloads)

    return SlowMoverOutreachPlan(
        campaign_name="Slow-mover social outreach",
        drafts=drafts,
        posts=posts,
        metricool_payloads=metricool_payloads,
        manychat_keywords=[draft.comment_keyword for draft in drafts],
        notes=(
            f"Generated {len(posts)} engagement-focused outreach posts for {len(drafts)} slow-moving items. "
            "Use Looping by Zapier over metricool_payloads to schedule every post, and connect the comment keywords to ManyChat replies."
            + _tiktok_cap_note(suppressed_tiktok, request.tiktok_daily_post_cap)
        ),
    )


def _slow_mover_items_for_outreach(
    repository: InventoryRepository,
    request: SlowMoverOutreachRequest,
) -> list[InventoryItem]:
    metrics_by_sku = {metric.sku: metric for metric in request.slow_mover_metrics}
    requested_skus = [*request.skus, *metrics_by_sku.keys()]
    items: list[InventoryItem] = []
    seen_skus: set[str] = set()

    for sku in requested_skus:
        item = repository.get(sku)
        if item and item.quantity > 0 and item.sku not in seen_skus:
            items.append(item)
            seen_skus.add(item.sku)

    if not items:
        query = request.query.strip().lower() if request.query else ""
        if query in {"all phones", "phones"}:
            phone_candidate_limit = max(request.max_items * 4, request.max_items + 10)
            items = [
                item
                for item in repository.all_promotable(limit=phone_candidate_limit)
                if _looks_like_phone(item)
            ]
        elif query and query not in {"all", "all inventory", "daily inventory"}:
            items = repository.search(request.query, limit=request.max_items)
        else:
            items = repository.all_promotable(limit=request.max_items)

    return sorted(
        items,
        key=lambda item: _slow_mover_priority(item, metrics_by_sku.get(item.sku))[0],
        reverse=True,
    )[: request.max_items]


def _slow_mover_priority(item: InventoryItem, metric: SlowMoverMetric | None) -> tuple[int, str]:
    if not metric:
        return (
            45,
            "Fallback slow-mover candidate from in-stock inventory; pass eBay views, watchers, and sales metrics for sharper ranking.",
        )

    score = 40
    reasons: list[str] = []
    age_days = metric.days_since_sale if metric.days_since_sale is not None else metric.listing_age_days
    if age_days is not None:
        score += min(age_days, 45)
        reasons.append(f"{age_days} days without a sale or listing movement")
    if metric.views is not None and metric.views <= 25:
        score += 15
        reasons.append(f"low views ({metric.views})")
    if metric.watchers is not None and metric.watchers <= 1:
        score += 10
        reasons.append(f"low watchers ({metric.watchers})")
    if metric.quantity_sold == 0:
        score += 10
        reasons.append("no recorded sales")
    if item.quantity > 1:
        score += min(item.quantity, 10)
        reasons.append(f"{item.quantity} units still in stock")
    if metric.notes:
        reasons.append(metric.notes)
    return min(score, 100), "; ".join(reasons) or "Slow-mover metrics supplied by Zapier."


def _slow_mover_posts_for_item(
    item: InventoryItem,
    request: SlowMoverOutreachRequest,
    keyword: str,
) -> list[SocialPost]:
    angle_names = ("question", "use_case", "comparison")[: request.angles_per_item]
    posts: list[SocialPost] = []
    if request.cross_post_to_all_platforms:
        platform = request.platforms[0] if request.platforms else "facebook"
        for angle in angle_names:
            posts.append(_slow_mover_social_post(item, request, platform, keyword, angle))
        return posts

    for angle in angle_names:
        for platform in request.platforms:
            posts.append(_slow_mover_social_post(item, request, platform, keyword, angle))
    return posts


def _slow_mover_social_post(
    item: InventoryItem,
    request: SlowMoverOutreachRequest,
    platform: str,
    keyword: str,
    angle: str,
) -> SocialPost:
    return SocialPost(
        platform=cast(SocialPlatform, platform),
        text=_slow_mover_post_text(item, request, platform, keyword, angle),
        product_sku=item.sku,
        product_title=item.title,
        ebay_url=_buy_url_for_item(item),
        media_url=item.image_url,
        hashtags=_hashtags_for_item(item),
        post_type="engagement",
    )


def _slow_mover_post_text(
    item: InventoryItem,
    request: SlowMoverOutreachRequest,
    platform: str,
    keyword: str,
    angle: str,
) -> str:
    brand = request.brand_name
    title = item.title.strip()
    price = _price_text(item)
    condition = f" Condition: {item.condition}." if item.condition else ""
    url = _buy_url_for_item(item)
    link_line = f"Comment {keyword} for the eBay link, or buy now: {url}"

    if angle == "use_case":
        lead = f"{brand} practical pick: {title}.{price}{condition}"
        hook = "Good fit for a backup phone, reseller shelf, repair-shop customer, or everyday replacement."
    elif angle == "comparison":
        lead = f"Quick comparison check from {brand}: {title}.{price}{condition}"
        hook = "Would you choose this model for value, storage, condition, or unlocked compatibility?"
    else:
        lead = f"{brand} slow-mover spotlight: {title}.{price}{condition}"
        hook = "Who is looking for a clean phone option without paying full flagship retail?"

    if platform == "tiktok":
        hook = "Worth a closer look for shoppers comparing phone value."
    elif platform == "linkedin":
        hook = "Useful for resellers, repair shops, and teams sourcing reliable device inventory."

    return f"{lead}\n{hook}\n{link_line}"


def _comment_keyword_for_item(item: InventoryItem) -> str:
    return _comment_keyword_for_sku(item.sku)


def _comment_keyword_for_sku(sku: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", sku or "").upper()
    suffix = cleaned[-6:] if cleaned else "PHONE"
    return f"LINK{suffix}"


def _slow_mover_manychat_reply(item: InventoryItem) -> str:
    return f"Here is the eBay link for {item.title}: {_buy_url_for_item(item)}"


def _slow_mover_manychat_reply_from_post(post: SocialPost) -> str:
    title = post.product_title or "that Horizon Wireless listing"
    url = post.ebay_url or get_settings().ebay_store_url
    return f"Here is the eBay link for {title}: {url}"


async def _create_all_inventory_social_drafts(request: SocialDraftRequest) -> SocialDraftBatch:
    repository = get_repository()
    candidate_limit = min(
        INVENTORY_ROTATION_CANDIDATE_LIMIT,
        max(request.max_products_per_run, request.max_products_per_run * 10),
    )
    lookahead_days = max(7, (candidate_limit // _metricool_daily_post_limit()) + 3)
    metricool_counts, externally_scheduled_item_ids, externally_scheduled_slots = await scheduled_inventory_state(
        start_at=request.publish_after,
        days=lookahead_days,
    )
    items = _inventory_items_for_daily_promotion(
        repository,
        request,
        limit=candidate_limit,
        excluded_item_ids=externally_scheduled_item_ids,
    )
    if not items:
        return SocialDraftBatch(
            campaign_name="Daily all-inventory promotion",
            posts=[],
            notes="No in-stock inventory items matched the daily promotion request.",
        )

    posts: list[SocialPost] = []
    if request.cross_post_to_all_platforms:
        platform = request.platforms[0] if request.platforms else "facebook"
        posts = [
            _inventory_social_post(item, request, platform=platform)
            for item in items
        ]
    else:
        for item in items:
            for platform in request.platforms:
                posts.append(_inventory_social_post(item, request, platform=platform))

    posts = _schedule_metricool_posts(
        repository,
        posts,
        request,
        metricool_counts,
        external_scheduled_slots=externally_scheduled_slots,
        max_posts=request.max_products_per_run,
        avoid_duplicate_items_per_day=True,
    )

    batch = SocialDraftBatch(
        campaign_name=_inventory_campaign_name(request),
        posts=posts,
        notes=(
            f"Generated {len(posts)} scheduled Summer Sale post payloads from {len(items)} in-stock inventory items. "
            "Render sends the eBay listing image and post directly to Metricool at the hourly daytime cadence "
            "until the active inventory rotation runs out; Zapier is not part of this publishing path."
        ),
    )
    batch.metricool_payloads = [metricool_payload(post, request) for post in batch.posts]
    _apply_tiktok_cap_to_batch(batch, request.tiktok_daily_post_cap)
    _record_metricool_payloads(repository, batch.posts, batch.metricool_payloads)
    return batch


def _schedule_metricool_posts(
    repository: InventoryRepository,
    posts: list[SocialPost],
    request: SocialDraftRequest,
    external_daily_counts: dict[str, int] | None = None,
    external_scheduled_slots: set[str] | None = None,
    max_posts: int | None = None,
    avoid_duplicate_items_per_day: bool = False,
) -> list[SocialPost]:
    if not posts:
        return []

    target_count = min(len(posts), max_posts) if max_posts else len(posts)

    if not _repository_supports_post_history(repository):
        default_schedule = default_metricool_publication_times(target_count, start_at=request.publish_after)
        scheduled_posts = posts[:target_count]
        for post, publication_time in zip(scheduled_posts, default_schedule, strict=False):
            post.suggested_schedule = publication_time
        return scheduled_posts

    schedule = _available_metricool_publication_times(
        repository,
        len(posts),
        request.publish_after,
        external_daily_counts or {},
        external_scheduled_slots or set(),
    )
    if avoid_duplicate_items_per_day:
        return _assign_inventory_posts_to_daily_unique_slots(
            repository,
            posts,
            schedule,
            target_count,
            use_repository_history=not bool(external_daily_counts),
        )

    scheduled_posts = posts[: min(len(schedule), target_count)]
    for post, publication_time in zip(scheduled_posts, schedule, strict=False):
        post.suggested_schedule = publication_time
    return scheduled_posts


def _assign_inventory_posts_to_daily_unique_slots(
    repository: InventoryRepository,
    posts: list[SocialPost],
    schedule: list[str],
    target_count: int,
    *,
    use_repository_history: bool = True,
) -> list[SocialPost]:
    scheduled_posts: list[SocialPost] = []
    unscheduled_posts = posts.copy()
    used_ids_by_day: dict[str, set[str]] = {}

    for publication_time in schedule:
        scheduled_day = publication_time[:10]
        if scheduled_day not in used_ids_by_day:
            used_ids_by_day[scheduled_day] = (
                _promoted_ebay_item_ids_for_day(repository, scheduled_day)
                if use_repository_history
                else set()
            )
        used_ids = used_ids_by_day[scheduled_day]

        post_index = _first_post_not_used_on_day(unscheduled_posts, used_ids)
        if post_index is None:
            continue

        post = unscheduled_posts.pop(post_index)
        item_id = _post_ebay_item_id(post)
        if item_id:
            used_ids.add(item_id)
        post.suggested_schedule = publication_time
        scheduled_posts.append(post)
        if len(scheduled_posts) >= target_count:
            break

    return scheduled_posts


def _first_post_not_used_on_day(posts: list[SocialPost], used_item_ids: set[str]) -> int | None:
    for index, post in enumerate(posts):
        item_id = _post_ebay_item_id(post)
        if not item_id or item_id not in used_item_ids:
            return index
    return None


def _promoted_ebay_item_ids_for_day(repository: InventoryRepository, scheduled_day: str) -> set[str]:
    day_lookup = getattr(repository, "promoted_ebay_item_ids_for_day", None)
    if not callable(day_lookup):
        return set()
    return {
        canonical_id
        for value in day_lookup(scheduled_day)
        if (canonical_id := _canonical_ebay_item_id(value))
    }


def _available_metricool_publication_times(
    repository: InventoryRepository,
    count: int,
    start_at: str | None,
    external_daily_counts: dict[str, int],
    external_scheduled_slots: set[str],
) -> list[str]:
    if count <= 0:
        return []

    hourly_limit = _metricool_hourly_post_limit()
    daily_limit = _metricool_daily_post_limit()
    publication_times: list[str] = []
    hourly_counts: dict[str, int] = {}
    selected_daily_counts: dict[str, int] = {}
    probe_start = start_at

    for _ in range(370):
        candidates = _inventory_metricool_publication_times(max(count * 3, count + 20), start_at=probe_start)
        if not candidates:
            break

        for candidate in candidates:
            scheduled_day = candidate[:10]
            scheduled_hour = candidate[:13]
            external_day_confirmed = scheduled_day in external_daily_counts
            existing_daily_count = (
                int(external_daily_counts[scheduled_day])
                if external_day_confirmed
                else repository.social_post_count_for_day(scheduled_day)
            )
            if existing_daily_count + selected_daily_counts.get(scheduled_day, 0) >= daily_limit:
                continue
            if scheduled_hour not in hourly_counts:
                hourly_counts[scheduled_hour] = (
                    sum(slot[:13] == scheduled_hour for slot in external_scheduled_slots)
                    if external_day_confirmed
                    else repository.social_post_count_for_hour(scheduled_hour)
                )
            if hourly_counts[scheduled_hour] >= hourly_limit:
                continue
            if (
                candidate in external_scheduled_slots
                if external_day_confirmed
                else repository.social_post_count_for_slot(candidate) > 0
            ):
                continue
            publication_times.append(candidate)
            hourly_counts[scheduled_hour] += 1
            selected_daily_counts[scheduled_day] = selected_daily_counts.get(scheduled_day, 0) + 1
            if len(publication_times) == count:
                return publication_times

        last_candidate = datetime.strptime(candidates[-1], METRICOOL_PUBLICATION_FORMAT)
        next_slot = last_candidate + _inventory_posting_interval()
        probe_start = next_slot.strftime(METRICOOL_PUBLICATION_FORMAT)

    return publication_times


def _inventory_metricool_publication_times(
    count: int,
    start_at: str | datetime | None = None,
    now: datetime | None = None,
) -> list[str]:
    if count <= 0:
        return []

    current_time = now or datetime.now(POSTING_TIMEZONE)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=POSTING_TIMEZONE)

    minimum_time = current_time.astimezone(POSTING_TIMEZONE) + POSTING_MINIMUM_LEAD_TIME
    schedule_start = _coerce_inventory_schedule_start(start_at)
    if schedule_start and schedule_start > minimum_time:
        minimum_time = schedule_start

    posting_start = _inventory_daily_start_time()
    posting_end = _inventory_daily_end_time()
    posting_interval = _inventory_posting_interval()
    if posting_end < posting_start:
        posting_end = INVENTORY_DEFAULT_END_TIME

    publication_times: list[str] = []
    candidate_date = minimum_time.date()
    for _ in range(370):
        day_start = datetime.combine(candidate_date, posting_start, tzinfo=POSTING_TIMEZONE)
        day_end = datetime.combine(candidate_date, posting_end, tzinfo=POSTING_TIMEZONE)
        candidate = day_start
        while candidate <= day_end:
            if candidate >= minimum_time:
                publication_times.append(candidate.strftime(METRICOOL_PUBLICATION_FORMAT))
                if len(publication_times) == count:
                    return publication_times
            candidate += posting_interval
        candidate_date += timedelta(days=1)

    return publication_times


def _coerce_inventory_schedule_start(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        candidate = value
        if candidate.tzinfo is None:
            candidate = candidate.replace(tzinfo=POSTING_TIMEZONE)
        return candidate.astimezone(POSTING_TIMEZONE)
    try:
        return datetime.strptime(value.strip(), METRICOOL_PUBLICATION_FORMAT).replace(tzinfo=POSTING_TIMEZONE)
    except (AttributeError, ValueError):
        return None


def _inventory_daily_start_time() -> time:
    settings = get_settings()
    try:
        hour, minute = settings.metricool_morning_post_time.strip().split(":", maxsplit=1)
        return time(int(hour), int(minute))
    except (AttributeError, TypeError, ValueError):
        return INVENTORY_DEFAULT_START_TIME


def _inventory_daily_end_time() -> time:
    settings = get_settings()
    try:
        hour, minute = settings.metricool_inventory_post_end_time.strip().split(":", maxsplit=1)
        return time(int(hour), int(minute))
    except (AttributeError, TypeError, ValueError):
        return INVENTORY_DEFAULT_END_TIME


def _inventory_posting_interval() -> timedelta:
    settings = get_settings()
    try:
        hours = int(settings.metricool_inventory_post_interval_hours)
    except (AttributeError, TypeError, ValueError):
        return INVENTORY_POSTING_INTERVAL
    return timedelta(hours=max(1, min(hours, 12)))


def _metricool_hourly_post_limit() -> int:
    return INVENTORY_POSTS_PER_HOUR


def _metricool_daily_post_limit() -> int:
    start = datetime.combine(datetime.today(), _inventory_daily_start_time())
    end = datetime.combine(datetime.today(), _inventory_daily_end_time())
    if end < start:
        return 1
    return max(1, int((end - start) / _inventory_posting_interval()) + 1)


def _record_metricool_payloads(
    repository: InventoryRepository,
    posts: list[SocialPost],
    payloads: list[dict[str, object]],
) -> None:
    if not _repository_supports_post_history(repository):
        return

    for post, payload in zip(posts, payloads, strict=False):
        scheduled_at = payload.get("publication_date_time") or payload.get("publicationDate")
        if not isinstance(scheduled_at, str) or not scheduled_at:
            continue
        history_id = repository.record_social_post(
            ebay_item_id=_post_ebay_item_id(post),
            sku=post.product_sku,
            title=post.product_title or "Horizon Wireless eBay listing",
            item_url=post.ebay_url,
            image_url=str(payload.get("media_01") or post.media_url or ""),
            caption=post.text,
            scheduled_at=scheduled_at,
            platform=_metricool_platform_label(payload),
            metricool_post_id=str(payload.get("metricool_post_id") or "") or None,
            status="scheduled",
        )
        payload["history_id"] = history_id
        logger.info(
            "Inventory social post queued: history_id=%s ebay_item_id=%s sku=%s title=%r image_url=%s "
            "ebay_url=%s platform=%s scheduled_at=%s status=scheduled error=None",
            history_id,
            _post_ebay_item_id(post),
            post.product_sku,
            post.product_title,
            payload.get("media_01") or post.media_url,
            post.ebay_url,
            _metricool_platform_label(payload),
            scheduled_at,
        )


def _repository_supports_post_history(repository: object) -> bool:
    return all(
        callable(getattr(repository, method, None))
        for method in (
            "social_post_count_for_day",
            "social_post_count_for_hour",
            "social_post_count_for_slot",
            "recently_promoted_ebay_item_ids",
            "last_social_post_at_by_ebay_item_id",
            "record_social_post",
        )
    )


def _metricool_platform_label(payload: dict[str, object]) -> str:
    platforms = [
        platform
        for platform in ("facebook", "instagram", "tiktok", "linkedin")
        if payload.get(platform)
    ]
    return ",".join(platforms) if platforms else "unknown"


def _post_ebay_item_id(post: SocialPost) -> str | None:
    return _canonical_ebay_item_id(post.ebay_url) or _canonical_ebay_item_id(post.product_sku)


def _apply_tiktok_cap_to_batch(batch: SocialDraftBatch, daily_cap: int) -> None:
    suppressed_tiktok = apply_tiktok_daily_post_cap(batch.metricool_payloads, daily_cap)
    if suppressed_tiktok:
        batch.notes += _tiktok_cap_note(suppressed_tiktok, daily_cap)


def _tiktok_cap_note(suppressed_count: int, daily_cap: int) -> str:
    if not suppressed_count:
        return ""
    return (
        f" TikTok auto-publish was kept to {daily_cap} posts per scheduled day; "
        f"{suppressed_count} extra TikTok placements were disabled to avoid TikTok API daily-cap rejection."
    )


def _inventory_items_for_daily_promotion(
    repository: InventoryRepository,
    request: SocialDraftRequest,
    limit: int | None = None,
    excluded_item_ids: set[str] | None = None,
) -> list[InventoryItem]:
    candidate_limit = INVENTORY_ROTATION_CANDIDATE_LIMIT
    requested_limit = limit or request.max_products_per_run
    if request.sku:
        item = repository.get(request.sku)
        candidates = [item] if item and _is_active_promotable_item(item) and _is_ebay_listing(item) else []
        return _rotate_inventory_items(
            repository,
            candidates,
            requested_limit,
            ignore_recent_history=request.ignore_recent_history,
            excluded_item_ids=excluded_item_ids,
        )
    query = request.query.strip().lower() if request.query else ""
    if query in {"all phones", "phones"}:
        candidates = [
            item
            for item in repository.all_promotable(limit=candidate_limit)
            if _is_ebay_listing(item) and _looks_like_phone(item)
        ]
        return _rotate_inventory_items(
            repository,
            candidates,
            requested_limit,
            ignore_recent_history=request.ignore_recent_history,
            excluded_item_ids=excluded_item_ids,
        )
    if query and query not in {"all", "all inventory", "daily inventory"}:
        candidates = [item for item in repository.search(request.query, limit=candidate_limit) if _is_ebay_listing(item)]
        return _rotate_inventory_items(
            repository,
            candidates,
            requested_limit,
            ignore_recent_history=request.ignore_recent_history,
            excluded_item_ids=excluded_item_ids,
        )
    return _rotate_inventory_items(
        repository,
        [item for item in repository.all_promotable(limit=candidate_limit) if _is_ebay_listing(item)],
        requested_limit,
        ignore_recent_history=request.ignore_recent_history,
        excluded_item_ids=excluded_item_ids,
    )


def _rotate_inventory_items(
    repository: InventoryRepository,
    items: list[InventoryItem],
    limit: int,
    *,
    ignore_recent_history: bool = False,
    excluded_item_ids: set[str] | None = None,
) -> list[InventoryItem]:
    active_items = []
    for item in items:
        skip_reason = _promotable_skip_reason(item)
        if skip_reason:
            _log_skipped_inventory_post(item, skip_reason)
            continue
        active_items.append(item)
    externally_scheduled_ids = {
        canonical_id
        for value in (excluded_item_ids or set())
        if (canonical_id := _canonical_ebay_item_id(value))
    }
    active_items = [
        item
        for item in active_items
        if _item_ebay_item_id(item) not in externally_scheduled_ids
    ]
    if ignore_recent_history or not _repository_supports_post_history(repository):
        return active_items[:limit]

    settings = get_settings()
    recent_item_ids = {
        canonical_id
        for value in repository.recently_promoted_ebay_item_ids(
            settings.metricool_repost_cooldown_days,
            now=datetime.now(POSTING_TIMEZONE),
        )
        if (canonical_id := _canonical_ebay_item_id(value))
    }
    last_posted_at = {
        canonical_id: posted_at
        for value, posted_at in repository.last_social_post_at_by_ebay_item_id().items()
        if (canonical_id := _canonical_ebay_item_id(value))
    }

    eligible_items = [
        item
        for item in active_items
        if (_item_ebay_item_id(item) not in recent_item_ids)
    ]

    return sorted(
        eligible_items,
        key=lambda item: last_posted_at.get(_item_ebay_item_id(item) or "", ""),
    )[:limit]


def _is_active_promotable_item(item: InventoryItem) -> bool:
    return _promotable_skip_reason(item) is None


def _promotable_skip_reason(item: InventoryItem) -> str | None:
    if item.quantity <= 0:
        return "listing has no available quantity"
    if not item.image_url:
        return "listing has no valid primary eBay image"
    status = (item.listing_status or "ACTIVE").strip().upper()
    if status in {"ACTIVE", "IN_STOCK", "PUBLISHED", "LIVE"}:
        return None
    return f"listing status is {status}"


def _log_skipped_inventory_post(item: InventoryItem, reason: str) -> None:
    logger.warning(
        "Skipping eBay listing for automated inventory social post: ebay_item_id=%s sku=%s title=%r "
        "image_url=%s ebay_url=%s platform=inventory-queue scheduled_at=None status=skipped error=%s",
        _item_ebay_item_id(item),
        item.sku,
        item.title,
        item.image_url,
        _buy_url_for_item(item),
        reason,
    )


def _is_ebay_listing(item: InventoryItem) -> bool:
    if item.source.startswith("ebay-"):
        return True
    return item.sku.startswith("EBAY-")


def _item_ebay_item_id(item: InventoryItem) -> str | None:
    return (
        _canonical_ebay_item_id(item.ebay_url)
        or _canonical_ebay_item_id(item.ebay_item_id)
        or _canonical_ebay_item_id(item.sku)
    )


def _canonical_ebay_item_id(value: str | None) -> str | None:
    if not value:
        return None

    text = str(value).strip()
    if not text:
        return None

    url_match = re.search(r"/itm/(?:[^/?#]+/)?(\d+)", text)
    if url_match:
        return url_match.group(1)

    if text.startswith("EBAY-"):
        text = text.removeprefix("EBAY-")
    if text.isdigit():
        return text

    composite_match = re.search(r"(?:^|[|:/_-])(\d{9,})(?:$|[|:/_?#-])", text)
    if composite_match:
        return composite_match.group(1)

    return text


def _looks_like_phone(item: InventoryItem) -> bool:
    haystack = " ".join(
        [
            item.title,
            item.description or "",
            item.category or "",
            " ".join(str(value) for value in item.item_specifics.values()),
        ]
    ).lower()
    return any(keyword in haystack for keyword in PHONE_KEYWORDS)


def _inventory_social_post(
    item: InventoryItem,
    request: SocialDraftRequest,
    platform: str,
) -> SocialPost:
    return SocialPost(
        platform=cast(SocialPlatform, platform),
        text=_inventory_post_text(item, request, platform),
        product_sku=item.sku,
        product_title=item.title,
        ebay_url=_buy_url_for_item(item),
        media_url=item.image_url,
        hashtags=_hashtags_for_item(item),
    )


def _inventory_post_text(item: InventoryItem, request: SocialDraftRequest, platform: str) -> str:
    brand = request.brand_name or "Horizon Wireless"
    sale_name = (request.sale_name or f"{brand} eBay Store Sale").strip()
    price = f"Price: ${item.price:,.2f}" if item.price is not None else "Price: See eBay listing"
    condition = f"Condition: {item.condition}" if item.condition else "Condition: See eBay listing"
    url = _buy_url_for_item(item)
    title = item.title.strip()
    lines = [
        f"{sale_name} spotlight: {title}",
        "",
        condition,
        price,
    ]
    free_shipping_line = _free_shipping_line(item)
    if free_shipping_line:
        lines.append(free_shipping_line)
    lines.extend(
        [
            "",
            "Available while supplies last.",
            f"Shop Now - Buy direct on eBay: {url}",
        ]
    )
    return "\n".join(lines)


def _inventory_campaign_name(request: SocialDraftRequest) -> str:
    sale_name = (request.sale_name or "").strip()
    if sale_name:
        return f"{sale_name} inventory promotion"
    brand = request.brand_name or "Horizon Wireless"
    return f"{brand} inventory promotion"


def _store_url_for_request(request: SocialDraftRequest) -> str:
    configured = (request.store_url or "").strip()
    if configured:
        return configured
    return get_settings().ebay_store_url


def _buy_url_for_item(item: InventoryItem) -> str:
    if item.ebay_item_id:
        return f"https://www.ebay.com/itm/{item.ebay_item_id}"
    if item.ebay_url:
        canonical = _canonical_ebay_item_url(item.ebay_url)
        return canonical or item.ebay_url
    return get_settings().ebay_store_url


def _canonical_ebay_item_url(url: str) -> str | None:
    match = re.search(r"/itm/(?:[^/?#]+/)?(\d+)", url)
    if not match:
        return None
    return f"https://www.ebay.com/itm/{match.group(1)}"


def _free_shipping_line(item: InventoryItem) -> str | None:
    specifics = " ".join(
        f"{key} {value}"
        for key, value in item.item_specifics.items()
    ).lower()
    if "free shipping" in specifics or "shipping cost 0 " in specifics:
        return "Free Shipping available."
    return None


def _price_text(item: InventoryItem) -> str:
    if item.price is None:
        return ""
    if float(item.price).is_integer():
        return f" Price: ${int(item.price)}."
    return f" Price: ${item.price:.2f}."


def _hashtags_for_item(item: InventoryItem) -> list[str]:
    normalized = item.title.lower()
    hashtags = ["HorizonWireless", "eBayFinds"]
    if "iphone" in normalized:
        hashtags.append("iPhone")
    if "samsung" in normalized or "galaxy" in normalized:
        hashtags.append("SamsungGalaxy")
    if "pixel" in normalized:
        hashtags.append("GooglePixel")
    return hashtags


async def create_group_outreach_plan(request: GroupOutreachRequest) -> GroupOutreachPlan:
    prompt = (
        "Create a compliant Facebook Group outreach plan.\n"
        f"{request.model_dump_json(indent=2)}"
    )
    result = await Runner.run(_group_outreach_agent(), prompt, max_turns=5)
    plan = result.final_output
    if isinstance(plan, GroupOutreachPlan):
        return plan
    return GroupOutreachPlan(
        summary="Manual review required before Facebook Group outreach.",
        join_request_draft="Hi, I work with Horizon Wireless. I am interested in joining to learn and share relevant phone inventory only when it follows the group rules.",
        compliance_checklist=[
            "Do not auto-join groups.",
            "Do not post unless the group rules allow relevant business posts.",
            "Do not cold-message group members.",
        ],
        notes=str(plan),
    )


async def draft_group_reply(request: GroupReplyRequest) -> GroupReplyDraft:
    answer = await answer_customer_question(
        CustomerQuestion(
            message=request.message,
            channel=request.channel,
            user_id=request.author_name,
            first_name=request.author_name,
            metadata={
                "group_name": request.group_name or "",
                "group_url": request.group_url or "",
                "interaction_type": request.interaction_type,
                "post_context": request.post_context or "",
                "rules_text": request.rules_text or "",
            },
        )
    )
    can_auto_send = _can_auto_send_group_reply(request)
    return GroupReplyDraft(
        reply=answer.reply,
        channel=answer.channel,
        matched_items=answer.matched_items,
        needs_human=answer.needs_human,
        manual_review_required=not can_auto_send,
        can_auto_send=can_auto_send,
        compliance_notes=_group_reply_compliance_note(request, can_auto_send),
    )

def _can_auto_send_group_reply(request: GroupReplyRequest) -> bool:
    return request.interaction_type in {"group_dm_to_page", "page_dm", "instagram_dm"} and request.user_opted_in


def _group_reply_compliance_note(request: GroupReplyRequest, can_auto_send: bool) -> str:
    if can_auto_send:
        return "Can be sent through a supported inbound DM automation because the user messaged Horizon and opted in."
    return "Draft only. Facebook Group comments and member interactions require manual review; do not auto-send, cold-DM, or bypass group rules."
