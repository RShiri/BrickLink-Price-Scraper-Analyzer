"""Blended valuation across sources.

Each source's PriceAnalyzer market price (in its native currency) is converted
to a common currency and combined with confidence weights. The blend is stored
as its own snapshot rows (source='blended') so charts and deltas read a
persisted series instead of recomputing at page load.
"""
from datetime import datetime, timedelta

from .. import db as dbq
from ..currency import convert

CONFIDENCE_WEIGHT = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
BLEND_CURRENCY = "ILS"          # blended snapshots are stored in this currency
MAX_SOURCE_AGE_DAYS = 14        # ignore sources with no recent scan
OUTLIER_FACTOR = 2.5            # a source this far off the median is dropped

SOURCES = ("bricklink", "ebay", "brickowl")


def latest_per_source(conn, item_id, condition):
    """{source: snapshot_row} of the freshest market snapshot per source."""
    out = {}
    for source in SOURCES:
        row = dbq.latest_snapshot(conn, item_id, source, condition, kind="market")
        if row and row["market_price"]:
            out[source] = row
    return out


def blend(conn, item_id, condition, max_age_days=MAX_SOURCE_AGE_DAYS):
    """Confidence-weighted blended market price in BLEND_CURRENCY.

    Returns (value, confidence, per_source) or (None, None, per_source);
    per_source: {source: {'price', 'native', 'currency', 'confidence', 'scraped_at'}}.
    """
    cutoff = datetime.now() - timedelta(days=max_age_days)
    per_source = {}
    eligible = {}                # source -> (converted, confidence)

    for source, row in latest_per_source(conn, item_id, condition).items():
        try:
            age_ok = datetime.fromisoformat(row["scraped_at"]) >= cutoff
        except (TypeError, ValueError):
            age_ok = True
        converted = convert(conn, row["market_price"], row["currency"], BLEND_CURRENCY)
        per_source[source] = {
            "price": converted,
            "native": row["market_price"],
            "currency": row["currency"],
            "confidence": row["confidence"],
            "scraped_at": row["scraped_at"],
            "stale": not age_ok,
        }
        if age_ok and converted and converted > 0:
            eligible[source] = (converted, row["confidence"])

    # One marketplace wildly off the rest (a mislabeled currency, scalper
    # asks) must not drag the blend. With three or more fresh sources the
    # median is trustworthy: drop whatever sits more than OUTLIER_FACTOR
    # away from it, and mark it so the UI can say the source was ignored.
    if len(eligible) >= 3:
        prices = sorted(p for p, _ in eligible.values())
        n = len(prices)
        median = prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) / 2
        for source, (price, _) in list(eligible.items()):
            if price > median * OUTLIER_FACTOR or price < median / OUTLIER_FACTOR:
                per_source[source]["outlier"] = True
                del eligible[source]

    weighted_sum, weight_total = 0.0, 0
    best_confidence = "LOW"
    for source, (converted, confidence) in eligible.items():
        w = CONFIDENCE_WEIGHT.get(confidence, 1)
        weighted_sum += converted * w
        weight_total += w
        if w > CONFIDENCE_WEIGHT.get(best_confidence, 1):
            best_confidence = confidence

    if weight_total == 0:
        return None, None, per_source
    return weighted_sum / weight_total, best_confidence, per_source


def store_blended(conn, item_id):
    """Compute and persist blended market snapshots for both conditions.
    Returns {condition: value}."""
    out = {}
    for condition in ("new", "used"):
        value, confidence, _ = blend(conn, item_id, condition)
        if value and value > 0:
            dbq.insert_snapshot(
                conn, item_id, "blended", condition, "market", BLEND_CURRENCY,
                market_price=round(value, 2), confidence=confidence,
            )
            out[condition] = round(value, 2)
    conn.commit()
    return out


def current_value(conn, item_id, condition="new"):
    """Latest stored blended value (falls back to live blend if none stored)."""
    row = dbq.latest_snapshot(conn, item_id, "blended", condition, kind="market")
    if row and row["market_price"]:
        return row["market_price"], row["confidence"], row["scraped_at"]
    value, confidence, _ = blend(conn, item_id, condition)
    return value, confidence, None
