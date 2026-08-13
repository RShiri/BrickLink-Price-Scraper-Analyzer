"""One-off data repairs.

  python -m brickonomy.repair            # eBay currency history repair

eBay renders prices in the visitor's locale, and until the scraper learned to
read the page's own currency symbols it stamped everything USD. A shekel
price recorded as dollars converts ~3.5x too high, inflating the blended
value and the portfolio total. This repair finds those rows by comparing
each eBay "USD" snapshot with the same item's ILS prices from the other
marketplaces: when the raw number sits on the shekel scale while the USD
label puts it far above every other source, the label was wrong — the row
is relabeled ILS and the blended history recomputed.
"""
from .analytics.valuation import store_blended
from .currency import convert


def repair_ebay_currency(conn, log=print):
    """Relabel mislabeled eBay USD snapshots to ILS and rebuild the blended
    series they polluted. Returns the number of items repaired."""
    try:
        rate = convert(conn, 1.0, "USD", "ILS")
    except ValueError:
        log("✘ no USD→ILS exchange rate stored — run a scan first")
        return 0

    n_rows = 0
    first_bad = {}                     # item_id -> earliest mislabeled ts
    for row in conn.execute(
            """SELECT id, item_id, condition, market_price, scraped_at
               FROM price_snapshots
               WHERE source='ebay' AND currency='USD' AND kind='market'
                 AND market_price IS NOT NULL""").fetchall():
        ref = conn.execute(
            """SELECT AVG(market_price) v FROM (
                 SELECT market_price FROM price_snapshots
                 WHERE item_id=? AND condition=? AND kind='market'
                   AND source IN ('bricklink', 'brickowl') AND currency='ILS'
                   AND market_price > 0
                 ORDER BY scraped_at DESC LIMIT 4)""",
            (row["item_id"], row["condition"])).fetchone()["v"]
        if not ref or ref <= 0:
            continue                   # nothing to compare against — leave it
        raw = row["market_price"]
        # The raw number matches the shekel scale of the other sources, but
        # read as USD it lands far above them: the label was wrong.
        if raw * rate > ref * 2 and ref / 3 <= raw <= ref * 3:
            conn.execute("UPDATE price_snapshots SET currency='ILS' WHERE id=?",
                         (row["id"],))
            n_rows += 1
            ts = row["scraped_at"] or ""
            if row["item_id"] not in first_bad or ts < first_bad[row["item_id"]]:
                first_bad[row["item_id"]] = ts

    for item_id, ts in first_bad.items():
        # Sibling rows (sold/stock stats and raw listings) from the same
        # scrapes carry the same wrong label.
        conn.execute(
            """UPDATE price_snapshots SET currency='ILS'
               WHERE item_id=? AND source='ebay' AND currency='USD'
                 AND scraped_at >= ?""", (item_id, ts))
        # Blended points computed while the bad rows were current are wrong
        # too — drop them and store a fresh, correct one.
        conn.execute(
            """DELETE FROM price_snapshots
               WHERE item_id=? AND source='blended' AND scraped_at >= ?""",
            (item_id, ts))
        store_blended(conn, item_id)
    conn.commit()

    if n_rows:
        log(f"🩹 relabeled {n_rows} eBay snapshots on {len(first_bad)} items "
            f"and rebuilt their blended history")
    else:
        log("✔ no mislabeled eBay snapshots found — history is clean")
    return len(first_bad)


def main():
    from . import db as dbq

    conn = dbq.connect()
    try:
        n = repair_ebay_currency(conn)
        print(f"Repaired {n} item(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
