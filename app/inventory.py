import json
import sqlite3
import string
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from app.models import InventoryItem


STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "cost",
    "do",
    "does",
    "for",
    "have",
    "how",
    "i",
    "in",
    "is",
    "it",
    "much",
    "of",
    "on",
    "or",
    "price",
    "that",
    "the",
    "this",
    "to",
    "with",
    "you",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory_items (
    sku TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    condition TEXT,
    price REAL,
    currency TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    ebay_item_id TEXT,
    ebay_url TEXT,
    image_url TEXT,
    image_urls TEXT NOT NULL DEFAULT '[]',
    category TEXT,
    listing_status TEXT,
    item_specifics TEXT NOT NULL,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inventory_title ON inventory_items(title);
CREATE INDEX IF NOT EXISTS idx_inventory_ebay_item_id ON inventory_items(ebay_item_id);

CREATE TABLE IF NOT EXISTS social_post_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ebay_item_id TEXT,
    sku TEXT,
    title TEXT NOT NULL,
    item_url TEXT,
    image_url TEXT,
    caption TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    posted_at TEXT,
    platform TEXT NOT NULL,
    metricool_post_id TEXT,
    status TEXT NOT NULL,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_social_history_day ON social_post_history(scheduled_at);
CREATE INDEX IF NOT EXISTS idx_social_history_ebay_item_id ON social_post_history(ebay_item_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_social_history_unique_scheduled_item
ON social_post_history(ebay_item_id, scheduled_at, platform);

CREATE TABLE IF NOT EXISTS walmart_listing_drafts (
    sku TEXT PRIMARY KEY,
    ebay_item_id TEXT,
    source_snapshot TEXT NOT NULL,
    prepared_listing TEXT NOT NULL,
    catalog_query TEXT,
    catalog_candidates TEXT NOT NULL DEFAULT '[]',
    catalog_status TEXT NOT NULL,
    status TEXT NOT NULL,
    missing_fields TEXT NOT NULL DEFAULT '[]',
    lookup_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_walmart_drafts_status ON walmart_listing_drafts(status);
CREATE INDEX IF NOT EXISTS idx_walmart_drafts_ebay_item_id ON walmart_listing_drafts(ebay_item_id);

CREATE TABLE IF NOT EXISTS walmart_unpublished_jobs (
    batch_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    matched_skus TEXT NOT NULL DEFAULT '[]',
    skipped_skus TEXT NOT NULL DEFAULT '[]',
    offer_feed_id TEXT,
    inventory_feed_id TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_walmart_unpublished_jobs_status
ON walmart_unpublished_jobs(status);

CREATE TABLE IF NOT EXISTS marketplace_inventory_sync_state (
    sku TEXT PRIMARY KEY,
    ebay_item_id TEXT,
    ebay_quantity INTEGER NOT NULL,
    walmart_quantity INTEGER NOT NULL,
    synced_quantity INTEGER NOT NULL,
    pending_walmart_quantity INTEGER,
    pending_walmart_at TEXT,
    ebay_price REAL,
    synced_walmart_price REAL,
    price_currency TEXT,
    ebay_image_signature TEXT,
    ebay_primary_image_url TEXT,
    last_ebay_image_scan_at TEXT,
    synced_image_signature TEXT,
    pending_walmart_image_signature TEXT,
    pending_walmart_image_at TEXT,
    last_image_feed_id TEXT,
    last_source TEXT NOT NULL,
    status TEXT NOT NULL,
    error_message TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_marketplace_inventory_sync_status
ON marketplace_inventory_sync_state(status);

CREATE TABLE IF NOT EXISTS ebay_seller_hub_draft_jobs (
    batch_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    task_id TEXT,
    requested_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    result_excerpt TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ebay_seller_hub_draft_jobs_status
ON ebay_seller_hub_draft_jobs(status);
"""


class InventoryRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def init_db(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._ensure_inventory_columns(connection)
            self._ensure_marketplace_sync_columns(connection)

    def _ensure_inventory_columns(self, connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(inventory_items)").fetchall()
        }
        if "image_urls" not in columns:
            connection.execute("ALTER TABLE inventory_items ADD COLUMN image_urls TEXT NOT NULL DEFAULT '[]'")
        if "listing_status" not in columns:
            connection.execute("ALTER TABLE inventory_items ADD COLUMN listing_status TEXT")

    def _ensure_marketplace_sync_columns(self, connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(marketplace_inventory_sync_state)"
            ).fetchall()
        }
        for name in (
            "ebay_image_signature",
            "ebay_primary_image_url",
            "last_ebay_image_scan_at",
            "synced_image_signature",
            "pending_walmart_image_signature",
            "pending_walmart_image_at",
            "last_image_feed_id",
        ):
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE marketplace_inventory_sync_state ADD COLUMN {name} TEXT"
                )
        for name in ("ebay_price", "synced_walmart_price"):
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE marketplace_inventory_sync_state ADD COLUMN {name} REAL"
                )
        if "price_currency" not in columns:
            connection.execute(
                "ALTER TABLE marketplace_inventory_sync_state ADD COLUMN price_currency TEXT"
            )

    def count(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS total FROM inventory_items").fetchone()
        return int(row["total"])

    def upsert_items(self, items: Iterable[InventoryItem]) -> int:
        rows = [self._to_row(item) for item in items]
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO inventory_items (
                    sku, title, description, condition, price, currency, quantity,
                    ebay_item_id, ebay_url, image_url, image_urls, category,
                    listing_status, item_specifics,
                    source, updated_at
                )
                VALUES (
                    :sku, :title, :description, :condition, :price, :currency, :quantity,
                    :ebay_item_id, :ebay_url, :image_url, :image_urls, :category,
                    :listing_status, :item_specifics,
                    :source, :updated_at
                )
                ON CONFLICT(sku) DO UPDATE SET
                    title = excluded.title,
                    description = excluded.description,
                    condition = excluded.condition,
                    price = excluded.price,
                    currency = excluded.currency,
                    quantity = excluded.quantity,
                    ebay_item_id = excluded.ebay_item_id,
                    ebay_url = excluded.ebay_url,
                    image_url = excluded.image_url,
                    image_urls = excluded.image_urls,
                    category = excluded.category,
                    listing_status = excluded.listing_status,
                    item_specifics = excluded.item_specifics,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def replace_ebay_inventory_snapshot(self, items: Iterable[InventoryItem]) -> int:
        current_items = list(items)
        count = self.upsert_items(current_items)
        active_skus = {item.sku for item in current_items if item.sku}

        with self.connect() as connection:
            if active_skus:
                connection.execute(
                    """
                    UPDATE inventory_items
                    SET quantity = 0,
                        listing_status = 'ENDED',
                        updated_at = ?
                    WHERE (
                        sku LIKE 'EBAY-%'
                        OR source LIKE 'ebay-%'
                    )
                    AND sku NOT IN ({sku_placeholders})
                    """.format(
                        sku_placeholders=", ".join("?" for _ in active_skus) or "''",
                    ),
                    (
                        datetime.now(timezone.utc).isoformat(),
                        *sorted(active_skus),
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE inventory_items
                    SET quantity = 0,
                        listing_status = 'ENDED',
                        updated_at = ?
                    WHERE sku LIKE 'EBAY-%'
                    OR source LIKE 'ebay-%'
                    """,
                    (datetime.now(timezone.utc).isoformat(),),
                )
        return count

    def get(self, sku: str) -> InventoryItem | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM inventory_items WHERE sku = ?",
                (sku,),
            ).fetchone()
        return self._from_row(row) if row else None

    def get_by_ebay_item_id(self, ebay_item_id: str) -> InventoryItem | None:
        item_id = str(ebay_item_id or "").strip()
        if not item_id:
            return None
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM inventory_items
                WHERE ebay_item_id = ?
                OR sku = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (item_id, f"EBAY-{item_id}"),
            ).fetchone()
        return self._from_row(row) if row else None

    def item_for_social_reference(self, reference: str) -> InventoryItem | None:
        social_reference = str(reference or "").strip()
        if not social_reference:
            return None
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT ebay_item_id, sku
                FROM social_post_history
                WHERE metricool_post_id = ?
                OR CAST(id AS TEXT) = ?
                OR ebay_item_id = ?
                OR sku = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (social_reference, social_reference, social_reference, social_reference),
            ).fetchone()
        if not row:
            return None
        if row["sku"]:
            item = self.get(str(row["sku"]))
            if item:
                return item
        if row["ebay_item_id"]:
            return self.get_by_ebay_item_id(str(row["ebay_item_id"]))
        return None

    def search(self, query: str | None, limit: int = 8, in_stock_only: bool = True) -> list[InventoryItem]:
        limit = max(1, min(limit, 25))
        terms = []
        for raw_term in (query or "").split():
            term = raw_term.strip(string.punctuation).lower()
            if term and term not in STOPWORDS:
                terms.append(term)
        where = []
        params: list[object] = []

        if in_stock_only:
            where.append("quantity > 0")

        for term in terms:
            like = f"%{term}%"
            where.append(
                """
                (
                    lower(sku) LIKE ?
                    OR lower(title) LIKE ?
                    OR lower(coalesce(description, '')) LIKE ?
                    OR lower(coalesce(category, '')) LIKE ?
                    OR lower(item_specifics) LIKE ?
                )
                """
            )
            params.extend([like, like, like, like, like])

        sql = "SELECT * FROM inventory_items"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY quantity DESC, updated_at DESC LIMIT ?"
        params.append(limit)

        with self.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._from_row(row) for row in rows]

    def all_promotable(self, limit: int = 12) -> list[InventoryItem]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM inventory_items
                WHERE quantity > 0
                AND image_url IS NOT NULL
                AND image_url != ''
                AND (
                    listing_status IS NULL
                    OR upper(listing_status) IN ('ACTIVE', 'IN_STOCK', 'PUBLISHED', 'LIVE')
                )
                ORDER BY updated_at DESC, quantity DESC
                LIMIT ?
                """,
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def ebay_items(
        self,
        skus: Iterable[str] | None = None,
        *,
        limit: int = 200,
        include_inactive: bool = False,
    ) -> list[InventoryItem]:
        selected_skus = [str(sku).strip() for sku in (skus or []) if str(sku).strip()]
        where = ["(sku LIKE 'EBAY-%' OR source LIKE 'ebay-%')"]
        params: list[object] = []
        if not include_inactive:
            where.append("quantity > 0")
            where.append(
                "(listing_status IS NULL OR upper(listing_status) IN ('ACTIVE', 'IN_STOCK', 'PUBLISHED', 'LIVE'))"
            )
        if selected_skus:
            where.append(f"sku IN ({', '.join('?' for _ in selected_skus)})")
            params.extend(selected_skus)
        params.append(max(1, min(limit, 200)))

        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM inventory_items
                WHERE {' AND '.join(where)}
                ORDER BY updated_at DESC, quantity DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def upsert_walmart_drafts(self, drafts: Iterable[dict[str, Any]]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        rows: list[dict[str, Any]] = []
        for draft in drafts:
            sku = str(draft.get("sku") or "").strip()
            if not sku:
                continue
            rows.append(
                {
                    "sku": sku,
                    "ebay_item_id": draft.get("ebay_item_id"),
                    "source_snapshot": json.dumps(draft.get("source_snapshot") or {}, sort_keys=True),
                    "prepared_listing": json.dumps(draft.get("prepared_listing") or {}, sort_keys=True),
                    "catalog_query": draft.get("catalog_query"),
                    "catalog_candidates": json.dumps(draft.get("catalog_candidates") or [], sort_keys=True),
                    "catalog_status": str(draft.get("catalog_status") or "not_requested"),
                    "status": str(draft.get("status") or "draft_needs_review"),
                    "missing_fields": json.dumps(draft.get("missing_fields") or []),
                    "lookup_error": draft.get("lookup_error"),
                    "created_at": str(draft.get("created_at") or now),
                    "updated_at": now,
                }
            )
        if not rows:
            return 0

        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO walmart_listing_drafts (
                    sku, ebay_item_id, source_snapshot, prepared_listing,
                    catalog_query, catalog_candidates, catalog_status, status,
                    missing_fields, lookup_error, created_at, updated_at
                )
                VALUES (
                    :sku, :ebay_item_id, :source_snapshot, :prepared_listing,
                    :catalog_query, :catalog_candidates, :catalog_status, :status,
                    :missing_fields, :lookup_error, :created_at, :updated_at
                )
                ON CONFLICT(sku) DO UPDATE SET
                    ebay_item_id = excluded.ebay_item_id,
                    source_snapshot = excluded.source_snapshot,
                    prepared_listing = excluded.prepared_listing,
                    catalog_query = excluded.catalog_query,
                    catalog_candidates = excluded.catalog_candidates,
                    catalog_status = excluded.catalog_status,
                    status = excluded.status,
                    missing_fields = excluded.missing_fields,
                    lookup_error = excluded.lookup_error,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def walmart_drafts(
        self,
        skus: Iterable[str] | None = None,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        selected_skus = [str(sku).strip() for sku in (skus or []) if str(sku).strip()]
        params: list[object] = []
        where = ""
        if selected_skus:
            where = f"WHERE sku IN ({', '.join('?' for _ in selected_skus)})"
            params.extend(selected_skus)
        params.append(max(1, min(limit, 200)))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM walmart_listing_drafts
                {where}
                ORDER BY updated_at DESC, sku
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._walmart_draft_from_row(row) for row in rows]

    def walmart_draft_summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            total_row = connection.execute(
                "SELECT COUNT(*) AS total FROM walmart_listing_drafts"
            ).fetchone()
            status_rows = connection.execute(
                """
                SELECT status, COUNT(*) AS total
                FROM walmart_listing_drafts
                GROUP BY status
                ORDER BY status
                """
            ).fetchall()
            catalog_rows = connection.execute(
                """
                SELECT catalog_status, COUNT(*) AS total
                FROM walmart_listing_drafts
                GROUP BY catalog_status
                ORDER BY catalog_status
                """
            ).fetchall()
            latest_row = connection.execute(
                "SELECT MAX(updated_at) AS latest_updated_at FROM walmart_listing_drafts"
            ).fetchone()
        return {
            "total": int(total_row["total"]),
            "by_status": {str(row["status"]): int(row["total"]) for row in status_rows},
            "by_catalog_status": {
                str(row["catalog_status"]): int(row["total"]) for row in catalog_rows
            },
            "latest_updated_at": latest_row["latest_updated_at"],
        }

    def set_walmart_draft_status(self, skus: Iterable[str], status: str) -> int:
        selected_skus = [str(sku).strip() for sku in skus if str(sku).strip()]
        if not selected_skus:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE walmart_listing_drafts
                SET status = ?, updated_at = ?
                WHERE sku IN ({', '.join('?' for _ in selected_skus)})
                """,
                (status, now, *selected_skus),
            )
        return int(cursor.rowcount)

    def upsert_walmart_unpublished_job(
        self,
        batch_id: str,
        *,
        status: str,
        matched_skus: Iterable[str] = (),
        skipped_skus: Iterable[str] = (),
        offer_feed_id: str | None = None,
        inventory_feed_id: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO walmart_unpublished_jobs (
                    batch_id, status, matched_skus, skipped_skus,
                    offer_feed_id, inventory_feed_id, error_message,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(batch_id) DO UPDATE SET
                    status = excluded.status,
                    matched_skus = excluded.matched_skus,
                    skipped_skus = excluded.skipped_skus,
                    offer_feed_id = COALESCE(excluded.offer_feed_id, offer_feed_id),
                    inventory_feed_id = COALESCE(excluded.inventory_feed_id, inventory_feed_id),
                    error_message = excluded.error_message,
                    updated_at = excluded.updated_at
                """,
                (
                    batch_id,
                    status,
                    json.dumps(list(matched_skus)),
                    json.dumps(list(skipped_skus)),
                    offer_feed_id,
                    inventory_feed_id,
                    error_message,
                    now,
                    now,
                ),
            )
        return self.walmart_unpublished_job(batch_id) or {}

    def walmart_unpublished_job(self, batch_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM walmart_unpublished_jobs WHERE batch_id = ?",
                (str(batch_id),),
            ).fetchone()
        return self._walmart_job_from_row(row) if row else None

    def latest_walmart_unpublished_job(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM walmart_unpublished_jobs
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
        return self._walmart_job_from_row(row) if row else None

    def update_inventory_quantity(self, sku: str, quantity: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE inventory_items
                SET quantity = ?, updated_at = ?
                WHERE sku = ?
                """,
                (
                    max(0, int(quantity)),
                    datetime.now(timezone.utc).isoformat(),
                    str(sku),
                ),
            )
        return bool(cursor.rowcount)

    def marketplace_inventory_sync_state(self, sku: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM marketplace_inventory_sync_state WHERE sku = ?",
                (str(sku),),
            ).fetchone()
        return dict(row) if row else None

    def upsert_marketplace_inventory_sync_state(
        self,
        *,
        sku: str,
        ebay_item_id: str | None,
        ebay_quantity: int,
        walmart_quantity: int,
        synced_quantity: int,
        pending_walmart_quantity: int | None = None,
        pending_walmart_at: str | None = None,
        ebay_price: float | None = None,
        synced_walmart_price: float | None = None,
        price_currency: str | None = None,
        ebay_image_signature: str | None = None,
        ebay_primary_image_url: str | None = None,
        last_ebay_image_scan_at: str | None = None,
        synced_image_signature: str | None = None,
        pending_walmart_image_signature: str | None = None,
        pending_walmart_image_at: str | None = None,
        last_image_feed_id: str | None = None,
        last_source: str,
        status: str,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO marketplace_inventory_sync_state (
                    sku, ebay_item_id, ebay_quantity, walmart_quantity,
                    synced_quantity, pending_walmart_quantity, pending_walmart_at,
                    ebay_price, synced_walmart_price, price_currency,
                    ebay_image_signature, ebay_primary_image_url,
                    last_ebay_image_scan_at, synced_image_signature,
                    pending_walmart_image_signature, pending_walmart_image_at,
                    last_image_feed_id,
                    last_source, status, error_message, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sku) DO UPDATE SET
                    ebay_item_id = excluded.ebay_item_id,
                    ebay_quantity = excluded.ebay_quantity,
                    walmart_quantity = excluded.walmart_quantity,
                    synced_quantity = excluded.synced_quantity,
                    pending_walmart_quantity = excluded.pending_walmart_quantity,
                    pending_walmart_at = excluded.pending_walmart_at,
                    ebay_price = excluded.ebay_price,
                    synced_walmart_price = excluded.synced_walmart_price,
                    price_currency = excluded.price_currency,
                    ebay_image_signature = excluded.ebay_image_signature,
                    ebay_primary_image_url = excluded.ebay_primary_image_url,
                    last_ebay_image_scan_at = excluded.last_ebay_image_scan_at,
                    synced_image_signature = excluded.synced_image_signature,
                    pending_walmart_image_signature = excluded.pending_walmart_image_signature,
                    pending_walmart_image_at = excluded.pending_walmart_image_at,
                    last_image_feed_id = excluded.last_image_feed_id,
                    last_source = excluded.last_source,
                    status = excluded.status,
                    error_message = excluded.error_message,
                    updated_at = excluded.updated_at
                """,
                (
                    str(sku),
                    ebay_item_id,
                    max(0, int(ebay_quantity)),
                    max(0, int(walmart_quantity)),
                    max(0, int(synced_quantity)),
                    (
                        max(0, int(pending_walmart_quantity))
                        if pending_walmart_quantity is not None
                        else None
                    ),
                    pending_walmart_at,
                    float(ebay_price) if ebay_price is not None else None,
                    (
                        float(synced_walmart_price)
                        if synced_walmart_price is not None
                        else None
                    ),
                    str(price_currency).upper() if price_currency else None,
                    ebay_image_signature,
                    ebay_primary_image_url,
                    last_ebay_image_scan_at,
                    synced_image_signature,
                    pending_walmart_image_signature,
                    pending_walmart_image_at,
                    last_image_feed_id,
                    str(last_source),
                    str(status),
                    error_message,
                    now,
                ),
            )
        return self.marketplace_inventory_sync_state(sku) or {}

    def update_marketplace_price_state(
        self,
        sku: str,
        *,
        ebay_price: float,
        synced_walmart_price: float,
        price_currency: str = "USD",
    ) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE marketplace_inventory_sync_state
                SET ebay_price = ?, synced_walmart_price = ?, price_currency = ?, updated_at = ?
                WHERE sku = ?
                """,
                (
                    float(ebay_price),
                    float(synced_walmart_price),
                    str(price_currency or "USD").upper(),
                    datetime.now(timezone.utc).isoformat(),
                    str(sku),
                ),
            )
        return bool(cursor.rowcount)

    def marketplace_inventory_sync_summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            total = connection.execute(
                "SELECT COUNT(*) AS total FROM marketplace_inventory_sync_state"
            ).fetchone()
            statuses = connection.execute(
                """
                SELECT status, COUNT(*) AS total
                FROM marketplace_inventory_sync_state
                GROUP BY status
                ORDER BY status
                """
            ).fetchall()
            latest = connection.execute(
                "SELECT MAX(updated_at) AS latest_updated_at FROM marketplace_inventory_sync_state"
            ).fetchone()
        return {
            "total": int(total["total"]),
            "by_status": {str(row["status"]): int(row["total"]) for row in statuses},
            "latest_updated_at": latest["latest_updated_at"],
        }

    def upsert_ebay_seller_hub_draft_job(
        self,
        batch_id: str,
        *,
        status: str,
        task_id: str | None = None,
        requested_count: int = 0,
        success_count: int = 0,
        failure_count: int = 0,
        result_excerpt: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO ebay_seller_hub_draft_jobs (
                    batch_id, status, task_id, requested_count,
                    success_count, failure_count, result_excerpt,
                    error_message, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(batch_id) DO UPDATE SET
                    status = excluded.status,
                    task_id = COALESCE(excluded.task_id, task_id),
                    requested_count = CASE
                        WHEN excluded.requested_count > 0 THEN excluded.requested_count
                        ELSE requested_count
                    END,
                    success_count = excluded.success_count,
                    failure_count = excluded.failure_count,
                    result_excerpt = COALESCE(excluded.result_excerpt, result_excerpt),
                    error_message = excluded.error_message,
                    updated_at = excluded.updated_at
                """,
                (
                    str(batch_id),
                    str(status),
                    task_id,
                    int(requested_count),
                    int(success_count),
                    int(failure_count),
                    result_excerpt,
                    error_message,
                    now,
                    now,
                ),
            )
        return self.ebay_seller_hub_draft_job(batch_id) or {}

    def ebay_seller_hub_draft_job(self, batch_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ebay_seller_hub_draft_jobs WHERE batch_id = ?",
                (str(batch_id),),
            ).fetchone()
        return dict(row) if row else None

    def latest_ebay_seller_hub_draft_job(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM ebay_seller_hub_draft_jobs
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def social_post_count_for_day(self, scheduled_day: date | str) -> int:
        day = scheduled_day.isoformat() if isinstance(scheduled_day, date) else str(scheduled_day)[:10]
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM social_post_history
                WHERE substr(scheduled_at, 1, 10) = ?
                AND status NOT IN ('failed', 'cancelled', 'skipped')
                """,
                (day,),
            ).fetchone()
        return int(row["total"])

    def social_post_count_for_hour(self, scheduled_hour: str) -> int:
        hour = str(scheduled_hour)[:13]
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM social_post_history
                WHERE substr(scheduled_at, 1, 13) = ?
                AND status NOT IN ('failed', 'cancelled', 'skipped')
                """,
                (hour,),
            ).fetchone()
        return int(row["total"])

    def social_post_count_for_slot(self, scheduled_at: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM social_post_history
                WHERE scheduled_at = ?
                AND status NOT IN ('failed', 'cancelled', 'skipped')
                """,
                (scheduled_at,),
            ).fetchone()
        return int(row["total"])

    def recently_promoted_ebay_item_ids(
        self,
        cooldown_days: int = 14,
        now: datetime | None = None,
    ) -> set[str]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        threshold = (current - timedelta(days=max(0, cooldown_days))).strftime("%Y-%m-%d %H:%M:%S")
        current_text = current.strftime("%Y-%m-%d %H:%M:%S")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT ebay_item_id
                FROM social_post_history
                WHERE ebay_item_id IS NOT NULL
                AND ebay_item_id != ''
                AND status NOT IN ('failed', 'cancelled', 'skipped')
                AND (
                    (scheduled_at >= ? AND scheduled_at <= ?)
                    OR (posted_at >= ? AND posted_at <= ?)
                )
                """,
                (threshold, current_text, threshold, current_text),
            ).fetchall()
        return {str(row["ebay_item_id"]) for row in rows}

    def promoted_ebay_item_ids_for_day(self, scheduled_day: date | str) -> set[str]:
        day = scheduled_day.isoformat() if isinstance(scheduled_day, date) else str(scheduled_day)[:10]
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT ebay_item_id
                FROM social_post_history
                WHERE substr(scheduled_at, 1, 10) = ?
                AND ebay_item_id IS NOT NULL
                AND ebay_item_id != ''
                AND status NOT IN ('failed', 'cancelled', 'skipped')
                """,
                (day,),
            ).fetchall()
        return {str(row["ebay_item_id"]) for row in rows}

    def last_social_post_at_by_ebay_item_id(self) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT ebay_item_id, MAX(COALESCE(posted_at, scheduled_at, created_at)) AS last_at
                FROM social_post_history
                WHERE ebay_item_id IS NOT NULL
                AND ebay_item_id != ''
                AND status NOT IN ('failed', 'cancelled', 'skipped')
                GROUP BY ebay_item_id
                """
            ).fetchall()
        return {str(row["ebay_item_id"]): str(row["last_at"]) for row in rows if row["last_at"]}

    def record_social_post(
        self,
        *,
        ebay_item_id: str | None,
        sku: str | None,
        title: str,
        item_url: str | None,
        image_url: str | None,
        caption: str,
        scheduled_at: str,
        platform: str,
        metricool_post_id: str | None = None,
        status: str = "scheduled",
        error_message: str | None = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO social_post_history (
                    ebay_item_id, sku, title, item_url, image_url, caption,
                    scheduled_at, posted_at, platform, metricool_post_id, status,
                    error_message, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ebay_item_id, scheduled_at, platform) DO UPDATE SET
                    title = excluded.title,
                    item_url = excluded.item_url,
                    image_url = excluded.image_url,
                    caption = excluded.caption,
                    metricool_post_id = COALESCE(excluded.metricool_post_id, metricool_post_id),
                    status = excluded.status,
                    error_message = excluded.error_message,
                    updated_at = excluded.updated_at
                """,
                (
                    ebay_item_id,
                    sku,
                    title,
                    item_url,
                    image_url,
                    caption,
                    scheduled_at,
                    platform,
                    metricool_post_id,
                    status,
                    error_message,
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT last_insert_rowid() AS id").fetchone()
        return int(row["id"] or cursor.lastrowid or 0)

    def _to_row(self, item: InventoryItem) -> dict[str, object]:
        return {
            **item.model_dump(exclude={"item_specifics", "image_urls", "updated_at"}),
            "item_specifics": json.dumps(item.item_specifics, sort_keys=True),
            "image_urls": json.dumps(item.image_urls),
            "updated_at": item.updated_at.isoformat(),
        }

    def _from_row(self, row: sqlite3.Row) -> InventoryItem:
        data = dict(row)
        data["item_specifics"] = json.loads(data.get("item_specifics") or "{}")
        data["image_urls"] = json.loads(data.get("image_urls") or "[]")
        return InventoryItem.model_validate(data)

    @staticmethod
    def _walmart_draft_from_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key, default in (
            ("source_snapshot", {}),
            ("prepared_listing", {}),
            ("catalog_candidates", []),
            ("missing_fields", []),
        ):
            try:
                data[key] = json.loads(data.get(key) or json.dumps(default))
            except (TypeError, ValueError):
                data[key] = default
        return data

    @staticmethod
    def _walmart_job_from_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in ("matched_skus", "skipped_skus"):
            try:
                data[key] = json.loads(data.get(key) or "[]")
            except (TypeError, ValueError):
                data[key] = []
        return data
