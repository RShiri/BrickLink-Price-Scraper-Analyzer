"""Currency conversion with a no-key rate source and layered fallbacks.

Rates are stored as USD-base pairs in exchange_rates. Conversion between any
two supported currencies goes through USD cross-rates. Fetch order:
  1. cached DB rates newer than rates_ttl_hours
  2. frankfurter.app (ECB, no API key)
  3. stale cached DB rates (with a staleness flag)
  4. hardcoded last-resort table below
"""
from datetime import datetime, timedelta

import requests

from . import db as dbq
from .config import get_config

BASE = "USD"
QUOTES = ("ILS", "EUR", "GBP")
FRANKFURTER_URL = "https://api.frankfurter.app/latest"

# Last-resort rates (approximate, mid-2026) used only when no network and no cache.
FALLBACK_RATES = {"ILS": 3.65, "EUR": 0.92, "GBP": 0.78}

_state = {"stale": False, "source": None}


def rates_status():
    """UI hint: where the current rates came from and whether they are stale."""
    return dict(_state)


def _fetch_remote():
    resp = requests.get(
        FRANKFURTER_URL, params={"from": BASE, "to": ",".join(QUOTES)}, timeout=15
    )
    resp.raise_for_status()
    return resp.json()["rates"]


def get_usd_rates(conn) -> dict:
    """Return {quote: rate} with USD base, refreshing per the TTL."""
    cfg = get_config()
    cached = dbq.get_rates(conn, BASE)
    fresh_cutoff = datetime.now() - timedelta(hours=cfg.rates_ttl_hours)

    have_fresh = all(
        q in cached and datetime.fromisoformat(cached[q][1]) > fresh_cutoff
        for q in QUOTES
    )
    if have_fresh:
        _state.update(stale=False, source="cache")
        return {q: cached[q][0] for q in QUOTES}

    try:
        remote = _fetch_remote()
        for q in QUOTES:
            if q in remote:
                dbq.upsert_rate(conn, BASE, q, remote[q])
        _state.update(stale=False, source="frankfurter.app")
        return {q: remote.get(q, FALLBACK_RATES[q]) for q in QUOTES}
    except Exception:
        if all(q in cached for q in QUOTES):
            _state.update(stale=True, source="stale-cache")
            return {q: cached[q][0] for q in QUOTES}
        _state.update(stale=True, source="hardcoded-fallback")
        return dict(FALLBACK_RATES)


def convert(conn, amount: float, from_ccy: str, to_ccy: str) -> float:
    if amount is None:
        return None
    from_ccy, to_ccy = from_ccy.upper(), to_ccy.upper()
    if from_ccy == to_ccy:
        return amount
    rates = get_usd_rates(conn)
    rates = {BASE: 1.0, **rates}
    if from_ccy not in rates or to_ccy not in rates:
        raise ValueError(f"unsupported currency pair {from_ccy}->{to_ccy}")
    usd = amount / rates[from_ccy]
    return usd * rates[to_ccy]


CURRENCY_SYMBOLS = {"ILS": "₪", "USD": "$", "EUR": "€", "GBP": "£"}


def money(value, ccy: str) -> str:
    """Jinja filter: format an amount with its currency symbol."""
    if value is None:
        return "—"
    sym = CURRENCY_SYMBOLS.get(ccy.upper(), ccy + " ")
    return f"{sym}{value:,.0f}" if abs(value) >= 100 else f"{sym}{value:,.2f}"
