"""Trend-based value forecast shaped by the retirement cycle.

BrickEconomy-style curve rather than a flat compound line: each projected
year applies the multiplier of the phase the item will be in *that* year —
damped while the set is still in stores, a one-time step when it retires,
accelerated growth for the first two retired years, then stabilization.
The uncertainty band widens with the horizon. All tuning constants live in
PARAMS. Outputs are labeled "trend estimate" in the UI — this is not a
statistical model.
"""
from datetime import datetime

from . import growth as growth_mod
from . import lifecycle
from .. import db as dbq
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
    "retirement_step_pct": 10.0,  # one-time bump in the retirement year
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


def forecast(conn, item_id, condition="new"):
    """Returns {'basis', 'growth_pct', 'growth_effective_pct', 'phase',
    'points': [{year, value, low, high}, ...],
    'horizons': {1..N: {value, low, high, year, band_pct}},
    'at_retirement': {'year', 'value'} | None}
    or None when there's no current value to project from."""
    value, _, _ = current_value(conn, item_id, condition)
    if not value or value <= 0:
        return None

    g, basis = growth_mod.best_growth_estimate(conn, item_id, condition)
    if g is None:
        g, basis = 5.0, "default"
    g = max(PARAMS["clamp_min_pct"], min(PARAMS["clamp_max_pct"], g))

    item = dbq.get_item(conn, item_id)
    ph = lifecycle.phase(item["year"] if item else None,
                         item["theme"] if item else None)

    now_year = datetime.now().year
    ret_year = ph["retirement_year"]
    projected = value
    points, horizons = [], {}
    at_retirement = None
    first_year_mult = PARAMS["phase_multiplier"].get(
        _phase_in_year(now_year + 1, ret_year, ph["phase"]), 1.0)

    for t in range(1, PARAMS["horizon_years"] + 1):
        yr = now_year + t
        mult = PARAMS["phase_multiplier"].get(
            _phase_in_year(yr, ret_year, ph["phase"]), 1.0)
        projected *= 1 + g * mult / 100.0
        if ret_year and yr == ret_year:
            projected *= 1 + PARAMS["retirement_step_pct"] / 100.0
        band = (PARAMS["band_start_pct"] + PARAMS["band_step_pct"] * (t - 1)) / 100.0
        point = {
            "year": yr,
            "value": round(projected, 2),
            "low": round(projected * (1 - band), 2),
            "high": round(projected * (1 + band), 2),
        }
        points.append(point)
        horizons[t] = {**point, "band_pct": round(band * 100)}
        if ret_year and yr == ret_year:
            at_retirement = {"year": ret_year, "value": round(projected, 2)}

    return {"basis": basis, "growth_pct": round(g, 2),
            "growth_effective_pct": round(g * first_year_mult, 2),
            "phase": ph, "points": points, "horizons": horizons,
            "at_retirement": at_retirement}
