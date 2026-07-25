from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from scripts.optimize_ebay_listings import (
    BACKUP_CHUNK_PREFIX,
    BACKUP_MANIFEST_PREFIX,
    decode_backup_chunks,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recover and checksum-validate an eBay optimization backup."
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    log_text = sys.stdin.read()
    manifest_matches = re.findall(
        rf"{re.escape(BACKUP_MANIFEST_PREFIX)}=(\{{[^\r\n]+\}})",
        log_text,
    )
    if not manifest_matches:
        raise ValueError("No eBay backup manifest was found in the supplied logs.")
    manifest = json.loads(manifest_matches[-1])

    chunk_matches = re.findall(
        rf"{re.escape(BACKUP_CHUNK_PREFIX)}=(\d{{4}}):([A-Za-z0-9+/=]+)",
        log_text,
    )
    chunks_by_index = {int(index): chunk for index, chunk in chunk_matches}
    expected_count = int(manifest["chunks"])
    missing = [
        index for index in range(expected_count) if index not in chunks_by_index
    ]
    if missing:
        raise ValueError(f"Backup chunks are missing: {missing}")

    payload = decode_backup_chunks(
        [chunks_by_index[index] for index in range(expected_count)],
        str(manifest["sha256"]),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "listing_count": payload.get("listing_count"),
                "sha256": manifest["sha256"],
                "chunks": expected_count,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
