"""Seed brickonomy.db from the data this repo already has.

  python -m brickonomy.importer

Idempotent:
  1. BrickEconomy CSV export → items metadata + portfolio rows.
  2. Legacy bricklink_data.db JSON blobs → items metadata + one seed snapshot
     set per item (source='bricklink', ILS, timestamped with the blob's
     updated_at) so history charts start with a data point.
"""
import csv
import json
import re
import sqlite3
from pathlib import Path

from . import db as dbq
from .config import get_config
from .snapshots import write_snapshots

MINIFIG_ID_RE = re.compile(r"^[a-z]{2,4}\d+", re.IGNORECASE)


def normalize_item_id(raw: str) -> str:
    """'75192-1' -> '75192'; minifig ids pass through."""
    raw = raw.strip()
    if "-" in raw and raw.split("-")[0].isdigit():
        return raw.split("-")[0]
    return raw


def item_type_for(item_id: str) -> str:
    return "M" if MINIFIG_ID_RE.match(item_id) and not item_id[0].isdigit() else "S"


def parse_money(raw: str):
    """'$549.99' -> (549.99, 'USD'); returns (None, None) when unparsable."""
    if not raw:
        return None, None
    raw = raw.strip()
    ccy = {"$": "USD", "€": "EUR", "£": "GBP", "₪": "ILS"}.get(raw[:1])
    digits = re.sub(r"[^\d.]", "", raw)
    if not digits:
        return None, None
    return float(digits), (ccy or "USD")


def import_csv(conn, csv_path: str) -> int:
    path = Path(csv_path)
    if not path.exists():
        print(f"[importer] CSV not found: {path} — skipping portfolio seed")
        return 0
    count = 0
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            number = (row.get("Number") or "").strip()
            if not number:
                continue
            item_id = normalize_item_id(number)
            retail, retail_ccy = parse_money(row.get("Retail", ""))
            year = None
            if (row.get("Year") or "").strip().isdigit():
                year = int(row["Year"].strip())

            dbq.upsert_item(
                conn, item_id,
                item_type=item_type_for(item_id),
                name=(row.get("Name") or "").strip() or None,
                theme=(row.get("Theme") or "").strip() or None,
                subtheme=(row.get("Subtheme") or "").strip() or None,
                year=year,
                retail_price=retail,
                retail_currency=retail_ccy,
            )

            owned = int(row.get("Owned") or 0)
            wanted = int(row.get("Wanted") or 0)
            if owned > 0 or wanted > 0:
                dbq.upsert_portfolio(
                    conn, item_id,
                    owned=owned, wanted=wanted,
                    purchase_price=retail, purchase_currency=retail_ccy or "USD",
                )
            count += 1
    return count


def import_legacy_blobs(conn, legacy_db_path: str) -> int:
    path = Path(legacy_db_path)
    if not path.exists():
        print(f"[importer] legacy DB not found: {path} — skipping snapshot seed")
        return 0

    legacy = sqlite3.connect(path)
    legacy.row_factory = sqlite3.Row
    seeded = 0
    for row in legacy.execute("SELECT item_id, json_data, updated_at FROM items"):
        item_id = normalize_item_id(row["item_id"])
        try:
            data = json.loads(row["json_data"])
        except (TypeError, json.JSONDecodeError):
            continue
        meta = data.get("meta", {})
        specs = meta.get("specs", {})

        name = meta.get("item_name")
        dbq.upsert_item(
            conn, item_id,
            item_type=item_type_for(item_id),
            name=name if name and name != "Unknown" else None,
            year=meta.get("year_released"),
            parts=specs.get("parts") or None,
            minifigs=specs.get("minifigs") or None,
            weight_g=specs.get("weight_g") or None,
        )

        scraped_at = row["updated_at"] or meta.get("timestamp")
        # Idempotence: one seed per (item, source, timestamp)
        existing = conn.execute(
            """SELECT 1 FROM price_snapshots
               WHERE item_id=? AND source='bricklink' AND scraped_at=? LIMIT 1""",
            (item_id, scraped_at),
        ).fetchone()
        if existing:
            continue

        analysis = write_snapshots(conn, item_id, "bricklink", "ILS", data,
                                   scraped_at=scraped_at)

        # With a single source, the blended series equals it — seed that too so
        # charts and deltas have a blended point from day one.
        for condition in ("new", "used"):
            mkt = analysis[condition]["market_price"]
            if mkt and mkt > 0:
                dbq.insert_snapshot(
                    conn, item_id, "blended", condition, "market", "ILS",
                    market_price=mkt,
                    confidence=analysis[condition]["confidence"],
                    scraped_at=scraped_at,
                )
        conn.commit()
        seeded += 1
    legacy.close()
    return seeded


def main():
    cfg = get_config()
    conn = dbq.connect()

    n_csv = import_csv(conn, cfg.portfolio_csv)
    print(f"[importer] CSV rows processed: {n_csv}")

    n_blobs = import_legacy_blobs(conn, cfg.legacy_db_path)
    print(f"[importer] legacy items seeded with snapshots: {n_blobs}")

    items = conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
    pf = conn.execute("SELECT COUNT(*) c FROM portfolio").fetchone()["c"]
    snaps = conn.execute("SELECT COUNT(*) c FROM price_snapshots").fetchone()["c"]
    print(f"[importer] totals — items: {items}, portfolio: {pf}, snapshots: {snaps}")
    conn.close()


if __name__ == "__main__":
    main()
