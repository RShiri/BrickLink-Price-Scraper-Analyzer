"""Turn a normalized scrape into price_snapshots rows.

For each condition (new/used) we write:
  kind='sold'   — stats over the analyzer's cleaned sold listings
  kind='stock'  — stats over the cleaned current listings
  kind='market' — the PriceAnalyzer market price + confidence

The blended series (source='blended') is written separately by
analytics.valuation after all sources for an item finished.
"""
import statistics

from . import db as dbq
from .compat import PriceAnalyzer


def _stats(items):
    prices = [x["price"] for x in items if x.get("price", 0) > 0]
    if not prices:
        return None
    return {
        "price_min": min(prices),
        "price_avg": sum(x["price"] * x["qty"] for x in items) / max(1, sum(x["qty"] for x in items)),
        "price_median": statistics.median(prices),
        "price_max": max(prices),
        "listing_count": len(items),
        "total_qty": sum(x["qty"] for x in items),
    }


def minifig_floor_values(conn, set_id, currency):
    """(new, used) total value of the set's minifigs, in `currency`.

    Feeds PriceAnalyzer's minifig floor: a "used set" listing worth less than
    its own figures is a parted-out listing, not a complete set.
    """
    from . import db as dbq
    from .analytics.valuation import BLEND_CURRENCY, current_value
    from .currency import convert

    totals = {"new": 0.0, "used": 0.0}
    figs = dbq.get_set_minifigs(conn, set_id)
    if not figs:
        return 0.0, 0.0
    for fig in figs:
        for condition in ("new", "used"):
            value, _, _ = current_value(conn, fig["fig_id"], condition)
            if value:
                totals[condition] += value * max(1, fig["qty"] or 1)
    try:
        return (convert(conn, totals["new"], BLEND_CURRENCY, currency),
                convert(conn, totals["used"], BLEND_CURRENCY, currency))
    except ValueError:
        return 0.0, 0.0


def write_snapshots(conn, item_id, source, currency, data, scraped_at=None,
                    keep_raw=False, minifig_values=(0.0, 0.0)):
    """data: the {meta, new:{sold,stock}, used:{sold,stock}} dict.
    Returns the PriceAnalyzer result. Commits."""
    analysis = PriceAnalyzer(data).analyze(*minifig_values)

    for condition in ("new", "used"):
        cond = analysis[condition]
        sold_clean = cond["stats"]["sold"]["clean_items"]
        stock_clean = cond["stats"]["stock"]["clean_items"]

        for kind, items in (("sold", sold_clean), ("stock", stock_clean)):
            st = _stats(items)
            if st:
                dbq.insert_snapshot(
                    conn, item_id, source, condition, kind, currency,
                    scraped_at=scraped_at,
                    raw=items[:60] if keep_raw else None,
                    **st,
                )

        if cond["market_price"] and cond["market_price"] > 0:
            dbq.insert_snapshot(
                conn, item_id, source, condition, "market", currency,
                market_price=cond["market_price"],
                confidence=cond["confidence"],
                listing_count=cond["stats"]["sold"]["final_count"] or None,
                scraped_at=scraped_at,
            )

    conn.commit()
    return analysis
