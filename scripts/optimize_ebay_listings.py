from __future__ import annotations

import argparse
import asyncio
import base64
import gzip
import hashlib
import json
import sys
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.ebay import EbayClient
from app.ebay_listing_optimizer import (
    listing_price_signature,
    propose_listing_optimization,
)


BACKUP_CHUNK_PREFIX = "EBAY_OPTIMIZATION_BACKUP_CHUNK"
BACKUP_MANIFEST_PREFIX = "EBAY_OPTIMIZATION_BACKUP_MANIFEST"
SUMMARY_PREFIX = "EBAY_OPTIMIZATION_SUMMARY"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Back up and optimize active eBay listing titles and truthful item "
            "specifics without sending price fields."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply proposed revisions after emitting the complete backup.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum active listings to inspect.",
    )
    parser.add_argument(
        "--backup-chunk-size",
        type=int,
        default=9000,
        help="Maximum base64 characters per backup log chunk.",
    )
    args = parser.parse_args()
    return asyncio.run(
        run(
            apply=bool(args.apply),
            limit=max(1, min(int(args.limit), 200)),
            backup_chunk_size=max(1000, int(args.backup_chunk_size)),
        )
    )


async def run(
    *,
    apply: bool,
    limit: int,
    backup_chunk_size: int,
) -> int:
    client = EbayClient(get_settings())
    item_ids = await client.list_active_trading_item_ids(limit=limit)
    snapshots = await client.fetch_trading_listing_snapshots(item_ids)
    proposals = [propose_listing_optimization(snapshot) for snapshot in snapshots]
    created_at = datetime.now(timezone.utc).isoformat()
    backup = {
        "created_at": created_at,
        "mode": "apply" if apply else "dry_run",
        "listing_count": len(snapshots),
        "listings": [
            {
                "item_id": snapshot.get("item_id"),
                "title": snapshot.get("title"),
                "listing_type": snapshot.get("listing_type"),
                "has_variations": snapshot.get("has_variations"),
                "condition": snapshot.get("condition"),
                "category_id": snapshot.get("category_id"),
                "category_name": snapshot.get("category_name"),
                "item_specifics": snapshot.get("item_specifics"),
                "picture_urls": snapshot.get("picture_urls"),
                "prices": snapshot.get("prices"),
                "price_signature": listing_price_signature(snapshot),
                "raw_xml": snapshot.get("raw_xml"),
                "proposal": proposal,
            }
            for snapshot, proposal in zip(snapshots, proposals, strict=True)
        ],
    }
    emit_backup(backup, chunk_size=backup_chunk_size)

    changed = [proposal for proposal in proposals if proposal["changed"]]
    photo_capture = [
        proposal for proposal in proposals if proposal["requires_photo_capture"]
    ]
    summary: dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "created_at": created_at,
        "discovered": len(item_ids),
        "backed_up": len(snapshots),
        "proposed_revisions": len(changed),
        "title_revisions": sum(
            1 for proposal in changed if proposal["title_changed"]
        ),
        "item_specific_revisions": sum(
            1 for proposal in changed if proposal["item_specifics_changed"]
        ),
        "photo_capture_listings": len(photo_capture),
        "prices_submitted": False,
        "prices_verified_unchanged": not apply,
    }
    if not apply:
        print(f"{SUMMARY_PREFIX}={json.dumps(summary, sort_keys=True)}")
        return 0

    snapshots_by_id = {
        str(snapshot.get("item_id")): snapshot for snapshot in snapshots
    }
    revisions = [
        {
            "item_id": proposal["item_id"],
            "listing_type": snapshots_by_id[str(proposal["item_id"])].get(
                "listing_type"
            ),
            "has_variations": snapshots_by_id[str(proposal["item_id"])].get(
                "has_variations"
            ),
            "title": (
                proposal["proposed_title"]
                if proposal["title_changed"]
                else None
            ),
            "item_specifics": (
                proposal["proposed_item_specifics"]
                if proposal["item_specifics_changed"]
                else None
            ),
        }
        for proposal in changed
    ]
    revision_results, revision_failures = await apply_revisions_with_failures(
        client,
        revisions,
    )
    revised_item_ids = [str(result["item_id"]) for result in revision_results]
    verified_snapshots = (
        await client.fetch_trading_listing_snapshots(revised_item_ids)
        if revised_item_ids
        else []
    )
    verified_by_id = {
        str(snapshot.get("item_id")): snapshot for snapshot in verified_snapshots
    }

    failures: list[dict[str, Any]] = list(revision_failures)
    for proposal in changed:
        item_id = str(proposal["item_id"])
        before = snapshots_by_id[item_id]
        after = verified_by_id.get(item_id)
        if after is None:
            failures.append({"item_id": item_id, "error": "verification_missing"})
            continue
        if listing_price_signature(before) != listing_price_signature(after):
            failures.append({"item_id": item_id, "error": "price_changed"})
        if proposal["title_changed"] and after.get("title") != proposal["proposed_title"]:
            failures.append(
                {
                    "item_id": item_id,
                    "error": "title_mismatch",
                    "expected": proposal["proposed_title"],
                    "actual": after.get("title"),
                }
            )
        if proposal["item_specifics_changed"]:
            after_specifics = _specifics_lookup(after.get("item_specifics") or [])
            for addition in proposal["item_specific_additions"]:
                actual_values = after_specifics.get(
                    _normalized_name(addition["name"]),
                    [],
                )
                if addition["value"] not in actual_values:
                    failures.append(
                        {
                            "item_id": item_id,
                            "error": "item_specific_missing",
                            "name": addition["name"],
                            "value": addition["value"],
                        }
                    )

    summary.update(
        {
            "revised": len(revision_results),
            "revision_attempted": len(revisions),
            "revision_failures": revision_failures,
            "verified": len(verified_snapshots),
            "prices_verified_unchanged": not any(
                failure["error"] == "price_changed" for failure in failures
            ),
            "verification_failures": failures,
        }
    )
    print(f"{SUMMARY_PREFIX}={json.dumps(summary, sort_keys=True)}")
    return 1 if failures else 0


async def apply_revisions_with_failures(
    client: EbayClient,
    revisions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for revision in revisions:
        item_id = str(revision.get("item_id") or "")
        try:
            results.extend(await client.revise_trading_listings([revision]))
        except Exception as exc:
            failures.append(
                {
                    "item_id": item_id,
                    "error": "revision_failed",
                    "message": str(exc),
                }
            )
    return results, failures


def emit_backup(backup: dict[str, Any], *, chunk_size: int = 9000) -> None:
    raw_json = json.dumps(
        backup,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    compressed = gzip.compress(raw_json, compresslevel=9)
    encoded = base64.b64encode(compressed).decode("ascii")
    chunks = [
        encoded[offset : offset + chunk_size]
        for offset in range(0, len(encoded), chunk_size)
    ]
    manifest = {
        "encoding": "base64+gzip+json",
        "chunks": len(chunks),
        "compressed_bytes": len(compressed),
        "json_bytes": len(raw_json),
        "sha256": hashlib.sha256(raw_json).hexdigest(),
    }
    print(f"{BACKUP_MANIFEST_PREFIX}={json.dumps(manifest, sort_keys=True)}")
    for index, chunk in enumerate(chunks):
        print(f"{BACKUP_CHUNK_PREFIX}={index:04d}:{chunk}")
    sys.stdout.flush()


def decode_backup_chunks(chunks: list[str], expected_sha256: str) -> dict[str, Any]:
    encoded = "".join(chunks)
    raw_json = gzip.decompress(base64.b64decode(encoded))
    actual_sha256 = hashlib.sha256(raw_json).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"Backup checksum mismatch: expected {expected_sha256}, got {actual_sha256}."
        )
    payload = json.loads(raw_json)
    if not isinstance(payload, dict):
        raise ValueError("Decoded eBay backup was not a JSON object.")
    return payload


def _specifics_lookup(
    item_specifics: list[dict[str, Any]],
) -> dict[str, list[str]]:
    return {
        _normalized_name(str(entry.get("name") or "")): [
            str(value)
            for value in entry.get("values") or []
        ]
        for entry in item_specifics
    }


def _normalized_name(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


if __name__ == "__main__":
    raise SystemExit(main())
