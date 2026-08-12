"""BrickLink source.

Price guide: delegates to the root BrickLinkScraper (same Selenium flow and
HTML parser the repo already uses; prices arrive in ILS).

Additions over the root scraper:
  - fetch_parts_inventory(): the exact parts list from the set page
    (catalogItemInv.asp — the page root code already scrapes for minifigs,
    here with viewItemType=P).
  - fetch_part_out_value(): the set's part-out total from catalogPOV.asp,
    one request per set instead of per-part price lookups.
"""
import re

import requests
from bs4 import BeautifulSoup

from ..compat import get_bricklink_scraper
from ..models import ScrapeResult
from .base import USER_AGENT, BaseScraper, run_cli

BASE_URL = "https://www.bricklink.com/v2/catalog/catalogitem.page"

INV_URL = "https://www.bricklink.com/catalogItemInv.asp"
POV_URL = "https://www.bricklink.com/catalogPOV.asp"
TREE_URL = "https://www.bricklink.com/catalogTree.asp"
LIST_URL = "https://www.bricklink.com/catalogList.asp"


class BrickLinkSource(BaseScraper):
    source = "bricklink"
    currency = "ILS"          # default; the real one is detected per scrape
    BASE_URL = BASE_URL

    def parse(self, html, item_id: str) -> ScrapeResult:
        scraper_cls = get_bricklink_scraper()
        data = scraper_cls._parse_html(scraper_cls.__new__(scraper_cls), item_id, html)
        res = self.empty_result(item_id)
        res.meta = data.get("meta", {})
        res.new = data.get("new", res.new)
        res.used = data.get("used", res.used)
        res.currency = self._dominant_currency(res)
        return res

    @staticmethod
    def _dominant_currency(res: ScrapeResult) -> str:
        """BrickLink renders prices in the session's currency, so all rows of a
        scrape share one. Take the most common tag the parser detected."""
        counts = {}
        for condition in ("new", "used"):
            for kind in ("sold", "stock"):
                for row in res.__dict__[condition][kind]:
                    ccy = row.get("currency")
                    if ccy:
                        counts[ccy] = counts.get(ccy, 0) + 1
        return max(counts, key=counts.get) if counts else BrickLinkSource.currency

    def _fetch_html(self, item_id: str, item_type: str = "S"):
        """Playwright engine: fetch the price-guide page's HTML.

        Selenium goes through the root scraper instead (see fetch()), which
        already owns the driver lifecycle and its own caching.
        """
        from playwright.sync_api import sync_playwright

        url = f"{self.BASE_URL}?{item_type}={item_id}#T=P"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                context = browser.new_context(user_agent=USER_AGENT)
                page = context.new_page()
                # Warm the session first: BrickLink redirects catalog deep
                # links when it has never seen the client before.
                page.goto("https://www.bricklink.com", wait_until="domcontentloaded",
                          timeout=30000)
                page.goto(url, wait_until="networkidle", timeout=40000)
                page.wait_for_selector(".pcipgInnerTable", timeout=20000)
                # The tables render as placeholders first; wait for real prices.
                page.wait_for_function(
                    "() => /[$₪€£]|~/.test(document.body.innerText)", timeout=15000
                )
                return page.content()
            finally:
                browser.close()

    def fetch(self, item_id: str, item_type: str = "S",
              fixture: bool = None, save_fixture: bool = False) -> ScrapeResult:
        from ..config import get_config

        cfg = get_config()
        use_fixture = cfg.fixture_mode if fixture is None else fixture
        if use_fixture:
            try:
                return self.parse(self._load_fixture(item_id), item_id)
            except Exception as exc:
                return self.empty_result(item_id, error=f"{type(exc).__name__}: {exc}")

        if getattr(cfg, "scrape_engine", "selenium") == "playwright":
            try:
                html = self._fetch_html(item_id, item_type)
                if save_fixture:
                    self._save_fixture(item_id, html)
                return self.parse(html, item_id)
            except ImportError:
                pass  # playwright not installed — fall back to Selenium
            except Exception as exc:
                return self.empty_result(item_id, error=f"{type(exc).__name__}: {exc}")

        try:
            scraper = get_bricklink_scraper()()
            data = scraper.scrape(item_id, item_type, force=True)
            res = self.empty_result(item_id)
            res.meta = data.get("meta", {})
            res.new = data.get("new", res.new)
            res.used = data.get("used", res.used)
            res.currency = self._dominant_currency(res)
            return res
        except Exception as exc:
            return self.empty_result(item_id, error=f"{type(exc).__name__}: {exc}")

    # ── parts inventory ──────────────────────────────────────────────────

    # Common BrickLink colour names, longest-first, for splitting the
    # "ColorName PartName" description text the inventory page renders.
    _COLOR_NAMES = sorted([
        "Light Bluish Gray", "Dark Bluish Gray", "Reddish Brown", "Dark Red",
        "Dark Blue", "Dark Green", "Dark Tan", "Light Gray", "Dark Gray",
        "Sand Blue", "Sand Green", "Medium Blue", "Medium Azure", "Dark Azure",
        "Bright Light Orange", "Bright Light Yellow", "Bright Green",
        "Olive Green", "Dark Brown", "Medium Nougat", "Light Nougat",
        "Pearl Gold", "Pearl Dark Gray", "Flat Silver", "Metallic Silver",
        "Trans-Clear", "Trans-Red", "Trans-Light Blue", "Trans-Orange",
        "White", "Black", "Red", "Blue", "Yellow", "Green", "Tan", "Orange",
        "Brown", "Lime", "Purple", "Magenta", "Coral", "Lavender", "Azure",
    ], key=len, reverse=True)

    def parse_parts_inventory(self, html: str):
        """Rows: {part_no, part_name, color_id, color_name, qty}."""
        soup = BeautifulSoup(html, "html.parser")
        parts = []
        for tr in soup.find_all("tr"):
            links = tr.find_all("a", href=re.compile(r"\?P="))
            if not links:
                continue
            m = re.search(r"\?P=([^&\"]+)", links[0].get("href", ""))
            if not m:
                continue
            part_no = m.group(1)

            tds = tr.find_all("td")
            qty = None
            for td in tds:
                txt = td.get_text(strip=True)
                if txt.isdigit():
                    qty = int(txt)
                    break
            if qty is None:
                continue

            # The description link is the longest-text ?P= link in the row
            # (the item-number link's text is just the part number itself).
            desc = max((l.get_text(" ", strip=True) for l in links), key=len)
            color_name, part_name = None, desc
            for cname in self._COLOR_NAMES:
                if desc.startswith(cname + " "):
                    color_name = cname
                    part_name = desc[len(cname):].strip()
                    break

            color_id = 0
            cid = re.search(r"(?:idColor|colorID)=(\d+)", str(tr))
            if cid:
                color_id = int(cid.group(1))

            parts.append({
                "part_no": part_no,
                "part_name": part_name,
                "color_id": color_id,
                "color_name": color_name,
                "qty": qty,
            })
        return parts

    def fetch_parts_inventory(self, set_id: str):
        """Returns (parts, error). Uses plain HTTP first (server-rendered page),
        Selenium as fallback."""
        url = f"{INV_URL}?S={set_id if '-' in set_id else set_id + '-1'}&viewItemType=P"
        html, error = self._get(url)
        if html:
            parts = self.parse_parts_inventory(html)
            if parts:
                return parts, None
            error = "no parts parsed from inventory page"
        return [], error

    # ── part-out value ───────────────────────────────────────────────────

    # The page renders one line per basis, e.g.
    #   "* Average of last 6 months Sales: US $1,234.66 Including 7521 Items in 684 Lots."
    #   "Current Items For Sale Average: US $1,620.09 Including …"
    # Sold prices first — that is what the parts actually fetch; the asking
    # average is the fallback for a set with no recent sales.
    POV_PATTERNS = (
        r"Average\s+of\s+last\s+\d+\s+months?\s+Sales\s*:",
        r"Current\s+Items?\s+For\s+Sale\s+Average\s*:",
        r"Average\s+Value[^:]{0,40}:",          # older layout
    )
    # "US $1,234.66", "ILS ₪1,234.66", "£1,234.66"
    POV_AMOUNT = r"\s*(?:(US|CA|AU|NZ)\s*)?(?:([A-Z]{2,3})\s*)?([$₪€£])?\s*([\d,]+\.\d{2})"
    CCY_BY_SYMBOL = {"$": "USD", "₪": "ILS", "€": "EUR", "£": "GBP"}

    def parse_part_out_value(self, html: str):
        """Part-out total from catalogPOV.asp.

        Returns (value, currency) — the page is fetched without a BrickLink
        session, so it comes back in the site default (USD) rather than the
        scraper's session currency, and the caller has to convert."""
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        for pattern in self.POV_PATTERNS:
            m = re.search(pattern + self.POV_AMOUNT, text, re.IGNORECASE)
            if not m:
                continue
            value = float(m.group(4).replace(",", ""))
            if value <= 0:
                continue
            ccy = (m.group(2) if m.group(2) and len(m.group(2)) == 3 else None)
            if not ccy:
                ccy = "USD" if m.group(1) else self.CCY_BY_SYMBOL.get(m.group(3), "USD")
            return value, ccy
        return None, None

    def fetch_part_out_value(self, set_id: str, condition: str = "N"):
        """Returns (value, currency, error)."""
        num = set_id.split("-")[0]
        url = (f"{POV_URL}?itemType=S&itemNo={num}&itemSeq=1&itemQty=1"
               f"&breakType=M&itemCondition={condition}")
        html, error = self._get(url)
        if html:
            value, ccy = self.parse_part_out_value(html)
            if value is not None:
                return value, ccy, None
            error = "POV total not found on page"
        return None, None, error

    # ── catalog tree (themes / subthemes) ────────────────────────────────

    def parse_category_tree(self, html: str):
        """Parse catalogTree.asp into ordered rows
        [{cat_id, name, depth}, ...]; depth comes from the indentation
        BrickLink renders before each category link."""
        soup = BeautifulSoup(html, "html.parser")
        cats, seen = [], set()
        for a in soup.find_all("a", href=re.compile(r"catalogList\.asp\?[^\"]*catID=\d+")):
            m = re.search(r"catID=(\d+)", a["href"])
            if not m:
                continue
            cat_id = int(m.group(1))
            name = a.get_text(" ", strip=True)
            if not name or cat_id in seen:
                continue
            seen.add(cat_id)

            # Indentation: BrickLink pads nested categories with &nbsp; runs
            # (or nested lists) before the link inside the same cell/row.
            depth = 0
            cell = a.find_parent(["td", "div", "li"])
            if cell is not None:
                if cell.name == "li":
                    depth = max(0, len(cell.find_parents("ul")) - 1)
                else:
                    prefix = []
                    for node in cell.descendants:
                        if node is a:
                            break
                        if isinstance(node, str):
                            prefix.append(node)
                    nbsp = "".join(prefix).count("\xa0")
                    depth = nbsp // 3
            cats.append({"cat_id": cat_id, "name": name, "depth": max(0, depth)})
        return cats

    def fetch_category_tree(self, item_type: str = "S"):
        html, error = self._get(f"{TREE_URL}?itemType={item_type}")
        if html:
            cats = self.parse_category_tree(html)
            if cats:
                return cats, None
            error = "no categories parsed from tree page"
        return [], error

    # ── catalog list (all sets in a category) ────────────────────────────

    def parse_catalog_list(self, html: str):
        """Parse one catalogList.asp page.

        Returns (sets, total_pages) with sets =
        [{item_id, name, year}, ...] (item_id without the -1 suffix)."""
        soup = BeautifulSoup(html, "html.parser")
        sets, seen = [], set()
        for a in soup.find_all("a", href=re.compile(r"catalogitem\.page\?S=")):
            m = re.search(r"\?S=([\w.-]+)", a["href"])
            if not m:
                continue
            raw = m.group(1)
            item_id = raw.split("-")[0]
            text = a.get_text(" ", strip=True)
            if item_id in seen:
                # second link for the same set usually carries the name
                if text and text != raw:
                    for s in sets:
                        if s["item_id"] == item_id and not s["name"]:
                            s["name"] = text
                continue
            seen.add(item_id)
            entry = {"item_id": item_id, "name": text if text != raw else "", "year": None}

            row = a.find_parent("tr") or a.parent
            if row:
                ym = re.search(r"itemYear=(\d{4})", str(row))
                if not ym:
                    ym = re.search(r"\b(19[5-9]\d|20[0-4]\d)\b", row.get_text(" ", strip=True))
                if ym:
                    entry["year"] = int(ym.group(1))
            sets.append(entry)

        total_pages = 1
        for pg in soup.find_all("a", href=re.compile(r"pg=(\d+)")):
            m = re.search(r"pg=(\d+)", pg["href"])
            if m:
                total_pages = max(total_pages, int(m.group(1)))
        m = re.search(r"Page\s+\d+\s+of\s+(\d+)", soup.get_text(" ", strip=True))
        if m:
            total_pages = max(total_pages, int(m.group(1)))
        return sets, total_pages

    def fetch_catalog_page(self, cat_id: int, page: int = 1, item_type: str = "S"):
        url = (f"{LIST_URL}?catType={item_type}&catID={cat_id}"
               f"&v=0&pg={page}&sortBy=Y&sortAsc=D")
        html, error = self._get(url)
        if html:
            return self.parse_catalog_list(html) + (None,)
        return [], 0, error

    # ── minifig inventory of a set ───────────────────────────────────────

    def parse_minifig_inventory(self, html: str):
        """[{id, name, qty}, ...] from catalogItemInv.asp?...&viewItemType=M.
        Unlike the root scraper, real quantities are parsed."""
        soup = BeautifulSoup(html, "html.parser")
        figs, seen = [], set()
        for tr in soup.find_all("tr"):
            links = tr.find_all("a", href=re.compile(r"\?M="))
            if not links:
                continue
            m = re.search(r"\?M=([\w.-]+)", links[0]["href"])
            if not m:
                continue
            fig_id = m.group(1)
            if fig_id in seen:
                continue
            qty = 1
            for td in tr.find_all("td"):
                txt = td.get_text(strip=True)
                if txt.isdigit():
                    qty = int(txt)
                    break
            name = max((l.get_text(" ", strip=True) for l in links), key=len)
            if name == fig_id:
                name = ""
            seen.add(fig_id)
            figs.append({"id": fig_id, "name": name, "qty": max(1, qty)})
        return figs

    def fetch_minifig_inventory(self, set_id: str):
        url = f"{INV_URL}?S={set_id if '-' in set_id else set_id + '-1'}&viewItemType=M"
        html, error = self._get(url)
        if html:
            figs = self.parse_minifig_inventory(html)
            if figs:
                return figs, None
            error = "no minifigs parsed from inventory page"
        return [], error

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _get(url):
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=25)
            resp.raise_for_status()
            return resp.text, None
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"


if __name__ == "__main__":
    run_cli(BrickLinkSource())
