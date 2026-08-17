"""Trend-based value forecast modeled on BrickEconomy's retirement logic.

Two regimes, matching how BrickEconomy forecasts:

  Still available — a set in stores doesn't appreciate: its value tracks
  retail until the estimated retirement date (the "value at retirement"
  anchor = the higher of current value and retail). Appreciation starts
  AFTER retirement, at the theme's median growth (comparable sets), with a
  one-time bump and accelerated growth in the first two retired years,
  then stabilization.

  Already retired — the item's own trend (observed growth, else retail
  CAGR, else theme comparables) compounds year by year with the phase
  multiplier of the phase the item will be in that year.

The uncertainty band widens with the horizon. All tuning constants live in
PARAMS. Outputs are labeled "trend estimate" in the UI — this is not a
statistical model.
"""
from datetime import datetime

from . import growth as growth_mod
from . import lifecycle
from .. import db as dbq
from ..currency import convert
from .valuation import current_value

PARAMS = {
    "clamp_min_pct": -10.0,       # annual growth clamp
    "clamp_max_pct": 25.0,
    "phase_multiplier": {
        "NEW": 0.5,               # flooded market damps the trend
        "EOL WATCH": 1.0,
        "RETIRED_ACCEL": 1.5,     # 0-2y post retirement
        "RETIRED_STABLE": 0.7,
        None: 1.0,
    },
    "retirement_step_pct": 10.0,  # one-time bump right after retirement
    "default_post_growth_pct": 8.0,  # post-retirement growth with no data at
                                     # all — BrickEconomy's ballpark average
    "band_start_pct": 12.0,       # ±band at +1y ...
    "band_step_pct": 4.5,         # ... widening per additional year out
    "horizon_years": 5,
}


def _phase_in_year(year, retirement_year, current_phase):
    """Which lifecycle phase the item will be in during `year`."""
    if not retirement_year:
        return current_phase
    if year < retirement_year - 1:
        return "NEW"
    if year <= retirement_year:
        return "EOL WATCH"
    if year <= retirement_year + 2:
        return "RETIRED_ACCEL"
    return "RETIRED_STABLE"


def _retail_ils(conn, item):
    if not item or not item["retail_price"]:
        return None
    try:
        return convert(conn, item["retail_price"],
                       item["retail_currency"] or "USD", "ILS")
    except ValueError:
        return None


def _band(t):
    return (PARAMS["band_start_pct"] + PARAMS["band_step_pct"] * (t - 1)) / 100.0


def _point(t, yr, projected):
    band = _band(t)
    return {"year": yr, "value": round(projected, 2),
            "low": round(projected * (1 - band), 2),
            "high": round(projected * (1 + band), 2)}


def _post_retirement_growth(conn, item, g_own, own_basis, condition):
    """Growth once the set is retired. Its own pre-retirement trend reflects
    discounting, not appreciation — theme comparables come first, exactly
    like BrickEconomy's theme-informed forecasts."""
    if item and item["theme"]:
        g = growth_mod.theme_growth(conn, item["theme"], condition)
        if g is not None:
            return g, "theme"
    if g_own is not None:
        return g_own, own_basis
    return PARAMS["default_post_growth_pct"], "default"


def forecast(conn, item_id, condition="new"):
    """Returns {'basis', 'growth_pct', 'growth_effective_pct', 'phase',
    'points': [{year, value, low, high}, ...],
    'horizons': {1..N: {value, low, high, year, band_pct}},
    'at_retirement': {'year', 'value'} | None}
    or None when there's no current value to project from."""
    value, _, _ = current_value(conn, item_id, condition)
    if not value or value <= 0:
        return None

    g_own, own_basis = growth_mod.best_growth_estimate(conn, item_id, condition)
    item = dbq.get_item(conn, item_id)
    ph = lifecycle.phase(item["year"] if item else None,
                         item["theme"] if item else None)
    now_year = datetime.now().year
    ret_year = ph["retirement_year"]

    if ret_year and now_year < ret_year:
        out = _forecast_available(conn, item, value, g_own, own_basis,
                                  now_year, ret_year, condition)
    else:
        out = _forecast_retired(value, g_own, own_basis, ph, now_year, ret_year)
    out["phase"] = ph
    return out


def _forecast_available(conn, item, value, g_own, own_basis,
                        now_year, ret_year, condition):
    """BrickEconomy model for a set still in stores: value drifts to the
    retirement anchor (max of current value and retail), then appreciates."""
    g_post, basis = _post_retirement_growth(conn, item, g_own, own_basis, condition)
    g_post = max(PARAMS["clamp_min_pct"], min(PARAMS["clamp_max_pct"], g_post))

    retail = _retail_ils(conn, item)
    anchor = max(value, retail or 0.0)
    years_to_ret = ret_year - now_year

    projected = value
    points, horizons = [], {}
    for t in range(1, PARAMS["horizon_years"] + 1):
        yr = now_year + t
        if yr < ret_year:
            # In stores: no appreciation, just the drift toward the anchor.
            projected = value + (anchor - value) * (t / years_to_ret)
        elif yr == ret_year:
            projected = anchor
        else:
            post_age = yr - ret_year
            mult = PARAMS["phase_multiplier"][
                "RETIRED_ACCEL" if post_age <= 2 else "RETIRED_STABLE"]
            projected *= 1 + g_post * mult / 100.0
            if post_age == 1:
                projected *= 1 + PARAMS["retirement_step_pct"] / 100.0
        points.append(_point(t, yr, projected))
        horizons[t] = {**points[-1], "band_pct": round(_band(t) * 100)}

    return {"basis": basis, "growth_pct": round(g_post, 2),
            "growth_effective_pct": round((points[0]["value"] / value - 1) * 100, 2),
            "points": points, "horizons": horizons,
            "at_retirement": {"year": ret_year, "value": round(anchor, 2)}}


def _forecast_retired(value, g_own, own_basis, ph, now_year, ret_year):
    """Already retired (or release year unknown): compound the item's own
    trend with per-year phase multipliers."""
    g, basis = (g_own, own_basis) if g_own is not None else (5.0, "default")
    g = max(PARAMS["clamp_min_pct"], min(PARAMS["clamp_max_pct"], g))

    projected = value
    points, horizons = [], {}
    for t in range(1, PARAMS["horizon_years"] + 1):
        yr = now_year + t
        mult = PARAMS["phase_multiplier"].get(
            _phase_in_year(yr, ret_year, ph["phase"]), 1.0)
        projected *= 1 + g * mult / 100.0
        points.append(_point(t, yr, projected))
        horizons[t] = {**points[-1], "band_pct": round(_band(t) * 100)}

    return {"basis": basis, "growth_pct": round(g, 2),
            "growth_effective_pct": round((points[0]["value"] / value - 1) * 100, 2),
            "points": points, "horizons": horizons, "at_retirement": None}
