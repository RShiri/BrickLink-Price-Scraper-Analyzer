"""Brickonomy web app.

Run:  uvicorn brickonomy.web.app:app --reload
"""
import csv
import io
import json
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db as dbq
from ..analytics import forecast as forecast_mod
from ..analytics import growth as growth_mod
from ..analytics import lifecycle
from ..analytics.valuation import blend, current_value
from ..compat import LEGACY_DB_PATH
from ..config import SUPPORTED_CURRENCIES, get_config
from ..currency import CURRENCY_SYMBOLS, convert, money, rates_status
from ..importer import item_type_for, normalize_item_id, parse_money
from . import jobs

BASE_DIR = Path(__file__).resolve().parent

# Static-export mode: brickonomy.export flips these, then crawls the app with
# a TestClient. Links become RELATIVE .html paths (so the exported site works
# at any mount point — repo root, /docs, a custom domain) and server-only UI
# (refresh/import/edit/currency) is hidden.
STATIC_MODE = bool(os.environ.get("BRICKONOMY_STATIC_EXPORT"))
# Directory depth of the page currently being rendered, e.g. sets/75192.html
# has depth 1. The exporter sets this before each request.
STATIC_DEPTH = 0


def slugify(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]", "-", (text or "").lower())).strip("-")


def static_prefix() -> str:
    """'' at the site root, '../' one level down, and so on."""
    return "../" * STATIC_DEPTH


def static_url(path: str) -> str:
    """Rewrite an app route to its exported static file, relative to the page
    being rendered."""
    if not STATIC_MODE:
        return path
    path, _, query = path.partition("?")
    if path == "/":
        rel = "index.html"
    elif path == "/sets":
        m = re.search(r"theme=([^&]+)", query)
        if m:
            from urllib.parse import unquote_plus
            rel = f"sets/theme-{slugify(unquote_plus(m.group(1)))}.html"
        else:
            rel = "sets/index.html"
    elif path == "/portfolio":
        rel = "portfolio.html"
    elif path.startswith("/api/"):
        rel = f"{path.lstrip('/')}.json"
    elif path.startswith("/static/"):
        rel = path.lstrip("/")
    else:
        rel = f"{path.lstrip('/')}.html"
    return static_prefix() + rel


app = FastAPI(title="Brickonomy")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.filters["money"] = money


# ── helpers ──────────────────────────────────────────────────────────────

def get_conn():
    return dbq.connect()


def display_ccy(request: Request) -> str:
    ccy = request.cookies.get("ccy", get_config().display_currency).upper()
    return ccy if ccy in SUPPORTED_CURRENCIES else get_config().display_currency


def img_url(item_id: str, item_type: str = "S") -> str:
    if item_type == "M" or any(c.isalpha() for c in item_id):
        return f"https://img.bricklink.com/ItemImage/MN/0/{item_id}.png"
    suffix = item_id if "-" in item_id else f"{item_id}-1"
    return f"https://img.bricklink.com/ItemImage/SN/0/{suffix}.png"


def ctx(request: Request, conn, **extra):
    ccy = display_ccy(request)
    return {
        "request": request,
        "ccy": ccy,
        "ccy_symbol": CURRENCY_SYMBOLS.get(ccy, ccy),
        "currencies": SUPPORTED_CURRENCIES,
        "rates": rates_status(),
        "job": jobs.status(),
        "img_url": img_url,
        "static_mode": STATIC_MODE,
        "base_path": static_prefix(),
        "u": static_url,
        **extra,
    }


def disp(conn, amount, from_ccy, to_ccy):
    if amount is None:
        return None
    try:
        return convert(conn, amount, from_ccy, to_ccy)
    except ValueError:
        return None


def item_view(conn, row, ccy):
    """Common per-item view model for lists."""
    iid = row["item_id"]
    val_new, conf, _ = current_value(conn, iid, "new")
    val_used, _, _ = current_value(conn, iid, "used")
    g, g_basis = growth_mod.best_growth_estimate(conn, iid)
    ph = lifecycle.phase(row["year"], row["theme"] if "theme" in row.keys() else None)
    return {
        "item_id": iid,
        "item_type": row["item_type"] if "item_type" in row.keys() else item_type_for(iid),
        "name": row["name"],
        "theme": row["theme"] if "theme" in row.keys() else None,
        "year": row["year"],
        "retail": disp(conn, row["retail_price"],
                       row["retail_currency"] or "USD", ccy) if "retail_price" in row.keys() and row["retail_price"] else None,
        "value_new": disp(conn, val_new, "ILS", ccy),
        "value_used": disp(conn, val_used, "ILS", ccy),
        "confidence": conf,
        "growth": g,
        "growth_basis": g_basis,
        "delta30": dbq.market_delta(conn, iid, days=30),
        "phase": ph,
        "spark": spark_points(conn, iid),
    }


def spark_points(conn, item_id, width=72, height=20, pad=2):
    pts = growth_mod.series(conn, item_id)[-12:]
    if len(pts) < 2:
        return None
    vals = [p for _, p, _ in pts]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    step = (width - 2 * pad) / (len(vals) - 1)
    return " ".join(
        f"{pad + i * step:.1f},{height - pad - (v - lo) / span * (height - 2 * pad):.1f}"
        for i, v in enumerate(vals)
    )


def legacy_minifigs(set_id):
    """Minifig inventory captured by the root scraper, if any."""
    if not LEGACY_DB_PATH.exists():
        return []
    conn = sqlite3.connect(LEGACY_DB_PATH)
    try:
        row = conn.execute(
            "SELECT json_data FROM inventory_lists WHERE set_id IN (?, ?)",
            (set_id, f"{set_id}-1"),
        ).fetchone()
        return json.loads(row[0]) if row else []
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def cheapest_stock(conn, item_id, condition="new"):
    """{source: {price(display later), currency, description}} from the latest
    stock snapshot's retained raw listings."""
    out = {}
    for source in ("bricklink", "ebay", "brickowl"):
        row = dbq.latest_snapshot(conn, item_id, source, condition, kind="stock")
        if not row or not row["raw_json"]:
            continue
        try:
            listings = json.loads(row["raw_json"])
        except json.JSONDecodeError:
            continue
        clean = [l for l in listings if l.get("price", 0) > 0]
        if not clean:
            continue
        best = min(clean, key=lambda l: l["price"])
        out[source] = {"price": best["price"], "currency": row["currency"],
                       "description": (best.get("description") or "")[:110],
                       "scraped_at": row["scraped_at"]}
    return out


# ── pages ────────────────────────────────────────────────────────────────

@app.get("/")
def dashboard(request: Request):
    conn = get_conn()
    try:
        ccy = display_ccy(request)
        rows = dbq.get_portfolio(conn)

        total_value = total_retail = 0.0
        movers = []
        theme_totals = {}
        for row in rows:
            val, _, _ = current_value(conn, row["item_id"], "new")
            v = disp(conn, val, "ILS", ccy) or 0.0
            qty = row["owned"] or 1
            total_value += v * qty
            if row["retail_price"]:
                total_retail += (disp(conn, row["retail_price"],
                                      row["retail_currency"] or "USD", ccy) or 0.0) * qty
            theme = row["theme"] or "Other"
            theme_totals[theme] = theme_totals.get(theme, 0.0) + v * qty
            delta = dbq.market_delta(conn, row["item_id"], days=30)
            if delta is not None and v > 0:
                movers.append({"item_id": row["item_id"], "name": row["name"],
                               "value": v, "delta": delta})

        movers.sort(key=lambda m: m["delta"], reverse=True)
        gainers, decliners = movers[:5], sorted(movers[-5:], key=lambda m: m["delta"])
        decliners = [m for m in decliners if m["delta"] < 0]
        gainers = [m for m in gainers if m["delta"] > 0]

        themes = sorted(theme_totals.items(), key=lambda kv: kv[1], reverse=True)
        top_themes = themes[:5]
        rest = sum(v for _, v in themes[5:])
        if rest > 0:
            top_themes.append((f"Other ({len(themes) - 5} themes)", rest))
        max_theme = max((v for _, v in top_themes), default=1.0)

        counts = {
            "items": conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"],
            "portfolio": len(rows),
            "retired": sum(1 for r in conn.execute("SELECT year FROM items")
                           if r["year"] and lifecycle.phase(r["year"])["phase"] in
                           ("RETIRED_ACCEL", "RETIRED_STABLE")),
        }
        last_scan = conn.execute(
            "SELECT MAX(scraped_at) ts FROM price_snapshots WHERE source != 'blended'"
        ).fetchone()["ts"]

        gain_pct = ((total_value - total_retail) / total_retail * 100.0) if total_retail else None
        return templates.TemplateResponse(request, "index.html", ctx(
            request, conn,
            total_value=total_value, total_retail=total_retail, gain_pct=gain_pct,
            gainers=gainers, decliners=decliners,
            themes=top_themes, max_theme=max_theme,
            counts=counts, last_scan=last_scan,
        ))
    finally:
        conn.close()


@app.get("/sets")
def sets_page(request: Request, q: str = "", filter: str = "", sort: str = "growth",
              theme: str = ""):
    conn = get_conn()
    try:
        ccy = display_ccy(request)
        rows = dbq.list_items(conn, search=q, limit=400)
        if theme:
            rows = [r for r in rows if (r["theme"] or "") == theme]
        if filter == "portfolio":
            owned = {r["item_id"] for r in dbq.get_portfolio(conn)}
            rows = [r for r in rows if r["item_id"] in owned]

        items = [item_view(conn, r, ccy) for r in rows]
        if filter == "retired":
            items = [i for i in items if i["phase"]["phase"] in ("RETIRED_ACCEL", "RETIRED_STABLE")]

        keyfns = {
            "growth": lambda i: i["growth"] if i["growth"] is not None else -1e9,
            "value": lambda i: i["value_new"] or 0,
            "year": lambda i: i["year"] or 0,
            "id": lambda i: i["item_id"],
        }
        items.sort(key=keyfns.get(sort, keyfns["growth"]),
                   reverse=sort != "id")

        themes = conn.execute(
            """SELECT theme, COUNT(*) n FROM items
               WHERE theme IS NOT NULL AND theme != ''
               GROUP BY theme ORDER BY n DESC LIMIT 30"""
        ).fetchall()
        n_categories = conn.execute("SELECT COUNT(*) c FROM categories").fetchone()["c"]
        return templates.TemplateResponse(request, "sets.html", ctx(
            request, conn, items=items[:200], q=q, filter=filter, sort=sort,
            theme=theme, themes=themes, n_categories=n_categories,
            total=len(items),
        ))
    finally:
        conn.close()


@app.get("/sets/{item_id}")
def set_detail(request: Request, item_id: str, parts_q: str = ""):
    conn = get_conn()
    try:
        ccy = display_ccy(request)
        item_id = normalize_item_id(item_id)
        row = dbq.get_item(conn, item_id)
        if row is None:
            return RedirectResponse(f"/sets?q={item_id}", status_code=303)

        if row["item_type"] == "M" or (any(c.isalpha() for c in item_id)
                                       and not item_id[0].isdigit()):
            return _minifig_detail(request, conn, row)

        values = {}
        per_source_all = {}
        for condition in ("new", "used"):
            val, conf, ts = current_value(conn, item_id, condition)
            _, _, per_source = blend(conn, item_id, condition)
            for s, info in per_source.items():
                info = dict(info)
                info["display"] = disp(conn, info["native"], info["currency"], ccy)
                per_source_all.setdefault(s, {})[condition] = info
            values[condition] = {"value": disp(conn, val, "ILS", ccy),
                                 "confidence": conf, "as_of": ts}

        retail_disp = disp(conn, row["retail_price"], row["retail_currency"] or "USD", ccy) \
            if row["retail_price"] else None
        g_total, g_cagr = growth_mod.growth_vs_retail(conn, item_id)
        fc = forecast_mod.forecast(conn, item_id)
        if fc:
            for h in fc["horizons"].values():
                for k in ("value", "low", "high"):
                    h[k] = disp(conn, h[k], "ILS", ccy)
        ph = lifecycle.phase(row["year"], row["theme"])

        buy_target = values["new"]["value"] * 0.8 if values["new"]["value"] else None

        offers = cheapest_stock(conn, item_id)
        for s, o in offers.items():
            o["display"] = disp(conn, o["price"], o["currency"], ccy)
        best_source = min(offers, key=lambda s: offers[s]["display"] or 1e18) if offers else None

        inv = [{"id": r["fig_id"], "name": r["fig_name"], "qty": r["qty"]}
               for r in dbq.get_set_minifigs(conn, item_id)] or legacy_minifigs(item_id)
        figs = []
        for f in inv:
            fv, _, _ = current_value(conn, f["id"], "new")
            name = f.get("name")
            if not name:
                known = dbq.get_item(conn, f["id"])
                name = known["name"] if known else ""
            v = disp(conn, fv, "ILS", ccy)
            figs.append({**f, "name": name or "", "value": v,
                         "total": (v or 0) * (f.get("qty") or 1)})
        figs_total = sum(f["total"] or 0 for f in figs)

        parts = dbq.get_set_parts(conn, item_id, search=parts_q, limit=100)
        psum = dbq.parts_summary(conn, item_id)
        pov = dbq.get_part_out(conn, item_id)
        pov_disp = disp(conn, pov["pov_total"], pov["currency"], ccy) if pov else None
        pov_premium = None
        if pov_disp and values["new"]["value"]:
            pov_premium = (pov_disp / values["new"]["value"] - 1) * 100.0

        # sold/listing counts per source for the comparison table
        stats = {}
        for s in ("bricklink", "ebay", "brickowl"):
            sold = dbq.latest_snapshot(conn, item_id, s, "new", kind="sold")
            stock = dbq.latest_snapshot(conn, item_id, s, "new", kind="stock")
            stats[s] = {"sold": sold["listing_count"] if sold else None,
                        "stock": stock["listing_count"] if stock else None}

        return templates.TemplateResponse(request, "set_detail.html", ctx(
            request, conn, item=row, values=values, retail=retail_disp,
            growth_total=g_total, growth_cagr=g_cagr, forecast=fc, phase=ph,
            buy_target=buy_target, per_source=per_source_all, source_stats=stats,
            offers=offers, best_source=best_source,
            figs=figs, figs_total=figs_total,
            parts=parts, parts_summary=psum, parts_q=parts_q,
            pov=pov_disp, pov_premium=pov_premium,
        ))
    finally:
        conn.close()


def _minifig_detail(request: Request, conn, row):
    """Dedicated minifig page: values, history, and which sets contain it."""
    ccy = display_ccy(request)
    fig_id = row["item_id"]

    values = {}
    per_source_all = {}
    for condition in ("new", "used"):
        val, conf, ts = current_value(conn, fig_id, condition)
        _, _, per_source = blend(conn, fig_id, condition)
        for s, info in per_source.items():
            info = dict(info)
            info["display"] = disp(conn, info["native"], info["currency"], ccy)
            per_source_all.setdefault(s, {})[condition] = info
        values[condition] = {"value": disp(conn, val, "ILS", ccy),
                             "confidence": conf, "as_of": ts}

    g_total, g_cagr = growth_mod.growth_vs_retail(conn, fig_id)
    delta30 = dbq.market_delta(conn, fig_id, days=30)

    appears_in = []
    for s in dbq.sets_containing_fig(conn, fig_id):
        sv, _, _ = current_value(conn, s["set_id"], "new")
        appears_in.append({
            "set_id": s["set_id"], "name": s["name"] or "", "theme": s["theme"],
            "year": s["year"], "qty": s["qty"],
            "value": disp(conn, sv, "ILS", ccy),
        })

    offers = cheapest_stock(conn, fig_id)
    for s, o in offers.items():
        o["display"] = disp(conn, o["price"], o["currency"], ccy)
    best_source = min(offers, key=lambda s: offers[s]["display"] or 1e18) if offers else None

    return templates.TemplateResponse(request, "minifig_detail.html", ctx(
        request, conn, item=row, values=values, per_source=per_source_all,
        growth_cagr=g_cagr, delta30=delta30, appears_in=appears_in,
        offers=offers, best_source=best_source,
    ))


@app.get("/api/sets/{item_id}/history")
def history_api(request: Request, item_id: str, condition: str = "new"):
    conn = get_conn()
    try:
        ccy = display_ccy(request)
        item_id = normalize_item_id(item_id)
        out = {"currency": ccy, "series": {}}
        for source in ("blended", "bricklink", "ebay", "brickowl"):
            pts = growth_mod.series(conn, item_id, condition=condition, source=source)
            out["series"][source] = [
                {"t": ts.strftime("%Y-%m-%d"), "v": round(disp(conn, v, c, ccy) or 0, 2)}
                for ts, v, c in pts
            ]
        fc = forecast_mod.forecast(conn, item_id, condition)
        if fc and out["series"]["blended"]:
            last = out["series"]["blended"][-1]
            fpts = [{"t": last["t"], "v": last["v"], "lo": last["v"], "hi": last["v"]}]
            for years, h in sorted(fc["horizons"].items()):
                fpts.append({"t": f"{h['year']}-12-31",
                             "v": round(disp(conn, h["value"], "ILS", ccy) or 0, 2),
                             "lo": round(disp(conn, h["low"], "ILS", ccy) or 0, 2),
                             "hi": round(disp(conn, h["high"], "ILS", ccy) or 0, 2)})
            out["forecast"] = fpts
        return JSONResponse(out)
    finally:
        conn.close()


# ── portfolio ────────────────────────────────────────────────────────────

@app.get("/portfolio")
def portfolio_page(request: Request, edit: str = None, imported: int = None,
                   skipped: int = None):
    conn = get_conn()
    try:
        ccy = display_ccy(request)
        rows = dbq.get_portfolio(conn)
        entries, total_value, total_paid, total_qty = [], 0.0, 0.0, 0
        for row in rows:
            val, _, _ = current_value(conn, row["item_id"], "new")
            v = disp(conn, val, "ILS", ccy)
            paid = disp(conn, row["purchase_price"], row["purchase_currency"] or "USD", ccy) \
                if row["purchase_price"] else None
            qty = row["owned"] or 1
            gain = ((v - paid) / paid * 100.0) if (v and paid) else None
            entries.append({
                "item_id": row["item_id"], "name": row["name"], "theme": row["theme"],
                "item_type": row["item_type"], "qty": qty,
                "paid": paid, "paid_raw": row["purchase_price"],
                "paid_ccy": row["purchase_currency"] or "USD",
                "purchase_date": row["purchase_date"], "condition": row["condition"],
                "value": v, "gain": gain,
                "delta30": dbq.market_delta(conn, row["item_id"], days=30),
            })
            total_value += (v or 0) * qty
            total_paid += (paid or 0) * qty
            total_qty += qty
        total_gain = ((total_value - total_paid) / total_paid * 100.0) if total_paid else None
        return templates.TemplateResponse(request, "portfolio.html", ctx(
            request, conn, entries=entries, edit=edit,
            total_value=total_value, total_paid=total_paid,
            total_gain=total_gain, total_qty=total_qty,
            imported=imported, skipped=skipped,
        ))
    finally:
        conn.close()


@app.get("/api/portfolio/history")
def portfolio_history(request: Request):
    """Daily portfolio total: forward-filled sum of blended values."""
    conn = get_conn()
    try:
        ccy = display_ccy(request)
        rows = dbq.get_portfolio(conn)
        per_item = {}
        dates = set()
        for row in rows:
            qty = row["owned"] or 1
            pts = growth_mod.series(conn, row["item_id"])
            if not pts:
                continue
            daily = {}
            for ts, v, c in pts:
                daily[ts.strftime("%Y-%m-%d")] = (disp(conn, v, c, ccy) or 0) * qty
            per_item[row["item_id"]] = daily
            dates.update(daily)

        series = []
        last_known = {}
        for d in sorted(dates):
            for iid, daily in per_item.items():
                if d in daily:
                    last_known[iid] = daily[d]
            series.append({"t": d, "v": round(sum(last_known.values()), 2)})
        return JSONResponse({"currency": ccy, "series": series})
    finally:
        conn.close()


@app.post("/portfolio/add")
def portfolio_add(request: Request, item_id: str = Form(...),
                  qty: int = Form(1), condition: str = Form("new")):
    conn = get_conn()
    try:
        iid = normalize_item_id(item_id.strip())
        if iid:
            dbq.upsert_item(conn, iid, item_type=item_type_for(iid))
            dbq.upsert_portfolio(conn, iid, owned=max(1, qty), condition=condition,
                                 merge_qty=True)
        return RedirectResponse("/portfolio", status_code=303)
    finally:
        conn.close()


@app.post("/portfolio/{item_id}")
def portfolio_edit(request: Request, item_id: str,
                   qty: int = Form(...), condition: str = Form("new"),
                   paid: str = Form(""), paid_ccy: str = Form("USD"),
                   purchase_date: str = Form("")):
    conn = get_conn()
    try:
        price, _ = parse_money(paid)
        if qty <= 0:
            dbq.delete_portfolio(conn, item_id)
        else:
            dbq.upsert_portfolio(conn, item_id, owned=qty, condition=condition,
                                 purchase_price=price,
                                 purchase_currency=paid_ccy if paid_ccy in SUPPORTED_CURRENCIES else "USD",
                                 purchase_date=purchase_date or None)
        return RedirectResponse("/portfolio", status_code=303)
    finally:
        conn.close()


@app.post("/portfolio/{item_id}/delete")
def portfolio_delete(request: Request, item_id: str):
    conn = get_conn()
    try:
        dbq.delete_portfolio(conn, item_id)
        return RedirectResponse("/portfolio", status_code=303)
    finally:
        conn.close()


# ── portfolio import ─────────────────────────────────────────────────────

def parse_import_file(filename: str, content: bytes):
    """Returns rows [{item_id, name, theme, year, qty, retail, retail_ccy}]."""
    text = content.decode("utf-8-sig", errors="replace")
    rows = []

    if filename.lower().endswith(".xml") or text.lstrip().startswith("<"):
        # BrickLink wanted-list XML
        root = ET.fromstring(text)
        for el in root.iter("ITEM"):
            iid = (el.findtext("ITEMID") or "").strip()
            if not iid:
                continue
            qty = int(el.findtext("MINQTY") or el.findtext("QTY") or 1)
            rows.append({"item_id": normalize_item_id(iid), "name": None,
                         "theme": None, "year": None, "qty": max(1, qty),
                         "retail": None, "retail_ccy": None})
        return rows

    sniff = text.splitlines()[0] if text.splitlines() else ""
    if "," in sniff and "number" in sniff.lower():
        # BrickEconomy CSV export
        for row in csv.DictReader(io.StringIO(text)):
            number = (row.get("Number") or "").strip()
            if not number:
                continue
            retail, retail_ccy = parse_money(row.get("Retail", ""))
            year = row.get("Year", "").strip()
            rows.append({
                "item_id": normalize_item_id(number),
                "name": (row.get("Name") or "").strip() or None,
                "theme": (row.get("Theme") or "").strip() or None,
                "year": int(year) if year.isdigit() else None,
                "qty": max(1, int(row.get("Owned") or 1)),
                "retail": retail, "retail_ccy": retail_ccy,
            })
        return rows

    # Plain set list: one id per line
    for line in text.splitlines():
        token = line.strip().split()[0] if line.strip() else ""
        if token and re.match(r"^[A-Za-z0-9-]+$", token):
            rows.append({"item_id": normalize_item_id(token), "name": None,
                         "theme": None, "year": None, "qty": 1,
                         "retail": None, "retail_ccy": None})
    return rows


@app.post("/portfolio/import")
async def portfolio_import(request: Request, file: UploadFile):
    conn = get_conn()
    try:
        content = await file.read()
        try:
            rows = parse_import_file(file.filename or "upload", content)
        except ET.ParseError as exc:
            rows, parse_error = [], str(exc)
        else:
            parse_error = None

        preview = []
        for r in rows:
            existing = conn.execute(
                "SELECT owned FROM portfolio WHERE item_id=?", (r["item_id"],)
            ).fetchone()
            known = dbq.get_item(conn, r["item_id"])
            preview.append({**r,
                            "status": "merge" if existing else ("known" if known else "new"),
                            "existing_qty": existing["owned"] if existing else 0})
        return templates.TemplateResponse(request, "import_preview.html", ctx(
            request, conn, rows=preview, filename=file.filename,
            payload=json.dumps(rows), parse_error=parse_error,
        ))
    finally:
        conn.close()


@app.post("/portfolio/import/confirm")
def portfolio_import_confirm(request: Request, payload: str = Form(...)):
    conn = get_conn()
    try:
        rows = json.loads(payload)
        imported = skipped = 0
        for r in rows:
            iid = normalize_item_id(str(r.get("item_id", "")).strip())
            if not iid:
                skipped += 1
                continue
            dbq.upsert_item(conn, iid, item_type=item_type_for(iid),
                            name=r.get("name"), theme=r.get("theme"),
                            year=r.get("year"), retail_price=r.get("retail"),
                            retail_currency=r.get("retail_ccy"))
            dbq.upsert_portfolio(conn, iid, owned=max(1, int(r.get("qty") or 1)),
                                 purchase_price=r.get("retail"),
                                 purchase_currency=r.get("retail_ccy") or "USD",
                                 merge_qty=True)
            imported += 1
        return RedirectResponse(f"/portfolio?imported={imported}&skipped={skipped}",
                                status_code=303)
    finally:
        conn.close()


# ── refresh & settings ───────────────────────────────────────────────────

@app.get("/refresh")
def refresh_page(request: Request):
    conn = get_conn()
    try:
        cfg = get_config()
        sources_health = []
        for source in ("bricklink", "ebay", "brickowl"):
            last = conn.execute(
                "SELECT MAX(scraped_at) ts, COUNT(DISTINCT item_id) n FROM price_snapshots "
                "WHERE source=? AND scraped_at > datetime('now', '-1 day')",
                (source,),
            ).fetchone()
            last_ever = conn.execute(
                "SELECT MAX(scraped_at) ts FROM price_snapshots WHERE source=?",
                (source,),
            ).fetchone()
            sources_health.append({"source": source, "items_24h": last["n"],
                                   "last_scan": last_ever["ts"]})
        n_portfolio = conn.execute("SELECT COUNT(*) c FROM portfolio WHERE owned > 0").fetchone()["c"]
        n_items = conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
        return templates.TemplateResponse(request, "refresh.html", ctx(
            request, conn, cfg=cfg, sources_health=sources_health,
            n_portfolio=n_portfolio, n_items=n_items,
        ))
    finally:
        conn.close()


@app.post("/refresh")
def refresh_start(request: Request, scope: str = Form("portfolio"),
                  item_id: str = Form(""), force: bool = Form(False)):
    started = jobs.start(scope=scope, item_id=item_id.strip() or None, force=force)
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse(jobs.status(), status_code=200 if started else 409)
    return RedirectResponse("/refresh", status_code=303)


@app.get("/api/refresh/status")
def refresh_status():
    return JSONResponse(jobs.status())


@app.post("/settings/currency")
def set_currency(request: Request, ccy: str = Form(...)):
    resp = RedirectResponse(request.headers.get("referer", "/"), status_code=303)
    if ccy.upper() in SUPPORTED_CURRENCIES:
        resp.set_cookie("ccy", ccy.upper(), max_age=365 * 24 * 3600)
    return resp
