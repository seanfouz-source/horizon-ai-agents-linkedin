from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any


PHONE_CATEGORY_ID = "9355"
SPEAKER_CATEGORY_ID = "111694"
OPEN_BOX_LABEL = "Open Box"
KNOWN_COLOR_PHRASES = (
    "Titanium Silverblue",
    "Titanium Whitesilver",
    "Titanium Black",
    "Titanium Gray",
    "Pacific Blue",
    "Space Black",
    "(PRODUCT)RED",
    "Various Colors",
    "Slipstream",
    "Starlight",
    "Midnight",
    "Graphite",
    "Lavender",
    "Burgundy",
    "Silver",
    "Purple",
    "Yellow",
    "White",
    "Black",
    "Cream",
    "Green",
    "Gray",
    "Grey",
    "Gold",
    "Navy",
    "Blue",
    "Pink",
    "Red",
)


def propose_listing_optimization(snapshot: dict[str, Any]) -> dict[str, Any]:
    original_title = str(snapshot.get("title") or "").strip()
    original_specifics = deepcopy(snapshot.get("item_specifics") or [])
    proposed_specifics, additions = _add_truthful_item_specifics(
        original_specifics,
        title=original_title,
        category_id=str(snapshot.get("category_id") or ""),
    )
    proposed_title = _optimized_title(
        title=original_title,
        condition=str(snapshot.get("condition") or ""),
        category_id=str(snapshot.get("category_id") or ""),
        item_specifics=proposed_specifics,
        has_variations=bool(snapshot.get("has_variations")),
    )
    title_changed = proposed_title != original_title
    specifics_changed = bool(additions)
    distinct_photo_count = count_distinct_ebay_photos(
        snapshot.get("picture_urls") or []
    )
    requires_photo_capture = (
        str(snapshot.get("condition") or "").casefold() == "open box"
        and distinct_photo_count <= 1
    )
    return {
        "item_id": snapshot.get("item_id"),
        "original_title": original_title,
        "proposed_title": proposed_title,
        "title_changed": title_changed,
        "item_specifics_changed": specifics_changed,
        "item_specific_additions": additions,
        "proposed_item_specifics": proposed_specifics,
        "distinct_photo_count": distinct_photo_count,
        "requires_photo_capture": requires_photo_capture,
        "changed": title_changed or specifics_changed,
    }


def listing_price_signature(snapshot: dict[str, Any]) -> str:
    prices = snapshot.get("prices") or {}
    stable_prices = {
        "start_price": prices.get("start_price"),
        "variations": sorted(
            [
                {
                    "sku": variation.get("sku"),
                    "specifics": variation.get("specifics") or [],
                    "start_price": variation.get("start_price"),
                }
                for variation in prices.get("variations") or []
            ],
            key=lambda row: json.dumps(row, sort_keys=True),
        ),
    }
    encoded = json.dumps(
        stable_prices,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def count_distinct_ebay_photos(urls: list[str]) -> int:
    return len({_ebay_photo_identity(url) for url in urls if str(url).strip()})


def _ebay_photo_identity(url: str) -> str:
    clean_url = str(url).strip()
    for pattern in (r"/images/g/([^/]+)/", r"/z/([^/]+)/"):
        match = re.search(pattern, clean_url, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    clean_url = re.sub(r"/s-l\d+", "", clean_url, flags=re.IGNORECASE)
    clean_url = re.sub(
        r"([?&])(set_id|wid|hei)=[^&]+",
        "",
        clean_url,
        flags=re.IGNORECASE,
    )
    return clean_url.rstrip("?&")


def _add_truthful_item_specifics(
    item_specifics: list[dict[str, Any]],
    *,
    title: str,
    category_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    proposed = deepcopy(item_specifics)
    index = {
        _normalized_name(str(entry.get("name") or "")): entry
        for entry in proposed
        if str(entry.get("name") or "").strip()
    }
    additions: list[dict[str, str]] = []

    def add(name: str, value: str) -> None:
        normalized = _normalized_name(name)
        if normalized in index or not value:
            return
        entry = {"name": name, "values": [value]}
        proposed.append(entry)
        index[normalized] = entry
        additions.append({"name": name, "value": value})

    title_folded = title.casefold()
    if "apple" in title_folded:
        add("Brand", "Apple")
    elif "samsung" in title_folded:
        add("Brand", "Samsung")
    elif "motorola" in title_folded:
        add("Brand", "Motorola")
    elif re.search(r"\bjbl\b", title, flags=re.IGNORECASE):
        add("Brand", "JBL")
    elif "otterbox" in title_folded:
        add("Brand", "OtterBox")

    if category_id == PHONE_CATEGORY_ID:
        if "factory unlocked" in title_folded:
            add("Lock Status", "Factory Unlocked")
        storage_values = {
            f"{match} GB"
            for match in re.findall(r"\b(\d{2,4})\s*GB\b", title, flags=re.IGNORECASE)
        }
        if len(storage_values) == 1:
            add("Storage Capacity", next(iter(storage_values)))

    if category_id == SPEAKER_CATEGORY_ID and "bluetooth" in title_folded:
        add("Connectivity", "Bluetooth")

    proposed.sort(key=lambda entry: str(entry.get("name") or "").casefold())
    return proposed, additions


def _optimized_title(
    *,
    title: str,
    condition: str,
    category_id: str,
    item_specifics: list[dict[str, Any]],
    has_variations: bool,
) -> str:
    facts = _specific_values(item_specifics)
    if not has_variations and category_id == PHONE_CATEGORY_ID:
        candidate = _phone_title(title, condition, facts)
        if candidate:
            return candidate
    if not has_variations and category_id == SPEAKER_CATEGORY_ID:
        candidate = _speaker_title(title, condition, facts)
        if candidate:
            return candidate
    return _clean_existing_title(title, condition)


def _phone_title(
    current_title: str,
    condition: str,
    facts: dict[str, list[str]],
) -> str | None:
    model = _first_fact(facts, "Model")
    storage = _compact_capacity(_first_fact(facts, "Storage Capacity"))
    color = _color_from_title(current_title) or _first_fact(facts, "Color")
    lock_status = _first_fact(facts, "Lock Status")
    if not lock_status and _first_fact(facts, "Network").casefold() == "unlocked":
        lock_status = "Unlocked"
    if not model:
        return None

    required = [model]
    if re.search(r"\b5G\b", current_title, flags=re.IGNORECASE) and not re.search(
        r"\b5G\b",
        model,
        flags=re.IGNORECASE,
    ):
        required.append("5G")
    for value in (storage, color, lock_status):
        if value and not _contains_phrase(" ".join(required), value):
            required.append(value)
    if "complete in box" in current_title.casefold():
        required.append("Complete in Box")
    if condition.casefold() == "open box":
        required.append(OPEN_BOX_LABEL)
    candidate = " ".join(required)
    if len(candidate) > 80 and "Factory Unlocked" in candidate:
        candidate = candidate.replace("Factory Unlocked", "Unlocked")
    if len(candidate) > 80 and "Complete in Box" in candidate:
        candidate = candidate.replace(" Complete in Box", "")
    if len(candidate) > 80 and condition.casefold() == "open box":
        suffix = f" {OPEN_BOX_LABEL}"
        base = re.sub(
            rf"\s+{re.escape(OPEN_BOX_LABEL)}$",
            "",
            candidate,
            flags=re.IGNORECASE,
        )
        candidate = f"{base[: 80 - len(suffix)].rstrip(' -/,')}{suffix}"
    return candidate[:80].rstrip(" -/,")


def _speaker_title(
    current_title: str,
    condition: str,
    facts: dict[str, list[str]],
) -> str | None:
    model = _first_fact(facts, "Model")
    color = _first_fact(facts, "Color")
    connectivity = _first_fact(facts, "Connectivity")
    features = ", ".join(facts.get(_normalized_name("Features"), []))
    if not model:
        return None

    parts = [model]
    if color and not _contains_phrase(model, color):
        parts.append(color)
    parts.append("Portable")
    if connectivity and "bluetooth" in connectivity.casefold():
        parts.append("Bluetooth")
    if "waterproof" in features.casefold():
        parts.append("Waterproof")
    if "dustproof" in features.casefold():
        parts.append("Dustproof")
    parts.append("Speaker")
    if condition.casefold() == "open box":
        parts.append(OPEN_BOX_LABEL)
    return _fit_title(parts)


def _clean_existing_title(title: str, condition: str) -> str:
    candidate = title.replace("–", " ").replace("—", " ")
    candidate = re.sub(r"\s+-\s+", " ", candidate)
    candidate = re.sub(
        r"\s*-\s*(?=Wi-?Fi\b)",
        " ",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(r"\bWiFi\b", "Wi-Fi", candidate, flags=re.IGNORECASE)
    candidate = re.sub(
        r"\b(\d{2,4})\s+GB\b",
        lambda match: f"{match.group(1)}GB",
        candidate,
        flags=re.IGNORECASE,
    )
    if condition.casefold() == "open box":
        candidate = re.sub(r"\bnew\b", " ", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\bopen\s*box\b", " ", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s+", " ", candidate).strip(" -/,")
        suffix = f" {OPEN_BOX_LABEL}"
        candidate = f"{candidate[: 80 - len(suffix)].rstrip(' -/,')}{suffix}"
    candidate = re.sub(r"\s+", " ", candidate).strip(" -/,")
    return candidate[:80].rstrip(" -/,")


def _fit_title(parts: list[str]) -> str:
    clean_parts: list[str] = []
    for part in parts:
        clean = re.sub(r"\s+", " ", str(part or "")).strip(" -/,")
        if not clean or _contains_phrase(" ".join(clean_parts), clean):
            continue
        clean_parts.append(clean)

    candidate = " ".join(clean_parts)
    if len(candidate) <= 80:
        return candidate
    while len(candidate) > 80 and len(clean_parts) > 1:
        clean_parts.pop(-2)
        candidate = " ".join(clean_parts)
    return candidate[:80].rstrip(" -/,")


def _specific_values(
    item_specifics: list[dict[str, Any]],
) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for entry in item_specifics:
        name = _normalized_name(str(entry.get("name") or ""))
        clean_values = [
            str(value).strip()
            for value in entry.get("values") or []
            if str(value).strip()
        ]
        if name and clean_values:
            values[name] = clean_values
    return values


def _first_fact(facts: dict[str, list[str]], name: str) -> str:
    values = facts.get(_normalized_name(name), [])
    return values[0] if values else ""


def _compact_capacity(value: str) -> str:
    return re.sub(r"\b(\d{2,4})\s+GB\b", r"\1GB", value, flags=re.IGNORECASE)


def _color_from_title(title: str) -> str:
    for color in KNOWN_COLOR_PHRASES:
        if _contains_phrase(title, color):
            return color
    return ""


def _contains_phrase(haystack: str, needle: str) -> bool:
    normalized_haystack = re.sub(r"[^a-z0-9]+", " ", haystack.casefold()).strip()
    normalized_needle = re.sub(r"[^a-z0-9]+", " ", needle.casefold()).strip()
    return bool(normalized_needle and normalized_needle in normalized_haystack)


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())
