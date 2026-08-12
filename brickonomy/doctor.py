"""Diagnose a source that returns nothing.

  python -m brickonomy.doctor 75192            # all sources
  python -m brickonomy.doctor 75192 --source ebay
  python -m brickonomy.doctor 75192 --dump     # also save the HTML next to it

When eBay or BrickOwl report "no listings found", the cause is one of: the
request was blocked (challenge page / 403), the page shape changed, or the
item genuinely has no offers. This prints enough of what came back to tell
those apart — HTTP status, page size and title, whether the response smells
like a block, how many candidate rows each selector matches, and the most
common CSS classes on the page so a changed layout is obvious.

Everything it prints comes from the live fetch the scraper itself performs, so
a clean run here means the scraper's own fetch works too.
"""
import argparse
import re
from collections import Counter

SEP = "─" * 68


def _soup(html):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser")


def _describe(html, label):
    """Size, title and block-detection for one fetched page."""
    if not html:
        print(f"  {label}: EMPTY response")
        return None
    soup = _soup(html)
    title = (soup.title.get_text(strip=True) if soup.title else "")[:80]
    print(f"  {label}: {len(html):,} bytes · title={title!r}")
    low = html[:6000].lower()
    blocked = [w for w in ("captcha", "are you a human", "access denied",
                           "unusual traffic", "cloudflare", "please verify",
                           "pardon our interruption", "robot")
               if w in low]
    if blocked:
        print(f"    ⚠ looks blocked — page mentions: {', '.join(blocked)}")
    return soup


def _selector_counts(soup, selectors, label="selectors"):
    print(f"    {label}:")
    for sel in selectors:
        try:
            n = len(soup.select(sel))
        except Exception as exc:                       # bad selector, not fatal
            n = f"error: {exc}"
        print(f"      {sel:<34} {n}")


def _common_classes(soup, n=14):
    """Most frequent class tokens — a changed layout shows up here first."""
    counter = Counter()
    for el in soup.find_all(class_=True):
        for cls in el.get("class", []):
            counter[cls] += 1
    top = ", ".join(f"{c}×{k}" for c, k in counter.most_common(n))
    print(f"    most common classes: {top}")


def _prices_seen(soup, n=6):
    text = soup.get_text(" ", strip=True)
    found = re.findall(r"[\$£€₪]\s*[\d,]+(?:\.\d{2})?", text)[:n]
    print(f"    price-looking strings on the page: {found or 'NONE'}")


def _dump(html, path):
    from pathlib import Path
    Path(path).write_text(html, encoding="utf-8", errors="replace")
    print(f"    saved → {path}")


# ── per source ──────────────────────────────────────────────────────────

def check_brickowl(item_id, dump=False):
    from .scrapers.brickowl import BrickOwlSource

    print(f"\n{SEP}\nBRICKOWL — plain HTTP\n{SEP}")
    src = BrickOwlSource()

    try:
        search_html = src._get(f"https://www.brickowl.com/search/catalog?query={item_id}")
    except Exception as exc:
        print(f"  ✘ search request failed: {type(exc).__name__}: {exc}")
        return
    soup = _describe(search_html, "search page")
    if soup is None:
        return
    links = [a["href"] for a in soup.find_all("a", href=re.compile(r"^/catalog/"))][:8]
    print(f"    /catalog/ links found: {len(links)}")
    for href in links[:5]:
        print(f"      {href}")
    if dump:
        _dump(search_html, f"doctor_brickowl_{item_id}_search.html")

    try:
        url = src.resolve_item_url(item_id)
        print(f"  resolved item URL: {url}")
        item_html = src._get(url)
    except Exception as exc:
        print(f"  ✘ could not resolve/fetch the item page: {type(exc).__name__}: {exc}")
        return
    isoup = _describe(item_html, "item page")
    if isoup is None:
        return
    _selector_counts(isoup, [".buy-table tr", ".item-buy tr", "table tr",
                             "[class*=buy]", "[class*=offer]", "[class*=price]",
                             "[data-price]", "script[type='application/ld+json']"])
    _common_classes(isoup)
    _prices_seen(isoup)
    if dump:
        _dump(item_html, f"doctor_brickowl_{item_id}_item.html")

    res = src.parse({"item": item_html}, item_id)
    print(f"  → parser produced: new stock={len(res.new['stock'])} "
          f"used stock={len(res.used['stock'])} currency={res.currency}")


def check_ebay(item_id, dump=False):
    from .scrapers.ebay import EbaySource

    print(f"\n{SEP}\nEBAY — headless Chrome\n{SEP}")
    src = EbaySource()
    try:
        htmls = src._fetch_html(item_id)
    except Exception as exc:
        print(f"  ✘ fetch failed: {type(exc).__name__}: {exc}")
        return

    for key in ("sold", "active"):
        html = htmls.get(key, "")
        soup = _describe(html, f"{key} page")
        if soup is None:
            continue
        _selector_counts(soup, ["li.s-item", ".s-item", ".s-card",
                                ".s-item__price", ".s-card__price",
                                "[class*=s-item]", "[class*=s-card]",
                                "ul.srp-results li", "[data-testid]"])
        _common_classes(soup)
        _prices_seen(soup)
        if dump:
            _dump(html, f"doctor_ebay_{item_id}_{key}.html")

    res = src.parse(htmls, item_id)
    print(f"  → parser produced: new sold={len(res.new['sold'])} "
          f"new stock={len(res.new['stock'])} used sold={len(res.used['sold'])} "
          f"used stock={len(res.used['stock'])}")


def check_bricklink_pov(item_id, dump=False):
    from .scrapers.bricklink import POV_URL, BrickLinkSource

    print(f"\n{SEP}\nBRICKLINK — part-out value page\n{SEP}")
    src = BrickLinkSource()
    num = item_id.split("-")[0]
    url = (f"{POV_URL}?itemType=S&itemNo={num}&itemSeq=1&itemQty=1"
           f"&breakType=M&itemCondition=N")
    print(f"  {url}")
    html, error = src._get(url)
    if error:
        print(f"  ✘ fetch error: {error}")
        return
    soup = _describe(html, "POV page")
    if soup is None:
        return
    text = soup.get_text(" ", strip=True)
    for label in ("Average Value", "Total", "Qty Avg", "Sold", "no inventory",
                  "not available", "Sorry"):
        idx = text.lower().find(label.lower())
        if idx >= 0:
            print(f"    …{text[max(0, idx - 60):idx + 90]}…")
    _prices_seen(soup, n=8)
    if dump:
        _dump(html, f"doctor_bricklink_{item_id}_pov.html")
    print(f"  → parser produced: {src.parse_part_out_value(html)}")


def main():
    ap = argparse.ArgumentParser(description="Diagnose why a source returns nothing")
    ap.add_argument("item_id", nargs="?", default="75192")
    ap.add_argument("--source", choices=["ebay", "brickowl", "pov", "all"],
                    default="all")
    ap.add_argument("--dump", action="store_true",
                    help="write the fetched HTML to files in the current directory")
    args = ap.parse_args()

    print(f"Brickonomy doctor — item {args.item_id}")
    checks = {"brickowl": check_brickowl, "ebay": check_ebay,
              "pov": check_bricklink_pov}
    for name, fn in checks.items():
        if args.source in (name, "all"):
            try:
                fn(args.item_id, dump=args.dump)
            except Exception as exc:
                print(f"  ✘ {name} check crashed: {type(exc).__name__}: {exc}")
    print(f"\n{SEP}\nDone.")


if __name__ == "__main__":
    main()
