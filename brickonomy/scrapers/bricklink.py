"""BrickLink source.

Price guide: delegates to the root BrickLinkScraper (same Selenium flow and
HTML parser the repo already uses; prices arrive in ILS).

Additions over the root scraper:
  - fetch_parts_inventory(): the exact parts list of a set or a minifig
    (catalogItemInv.asp with viewItemType=P).
  - fetch_part_out_value(): the set's part-out total from catalogPOV.asp,
    one request per set instead of per-part price lookups.
  - fetch_color_table(): BrickLink's color id → name table, used to split
    "ColorName PartName" descriptions without a hardcoded color list.
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
COLORS_URL = "https://www.bricklink.com/catalogColors.asp"


def inv_ref(item_id: str, item_type: str = "S") -> str:
    """Inventory-page reference: sets need the -1 sequence suffix, minifig
    ids are used as-is."""
    if item_type == "S" and "-" not in item_id:
        return f"{item_id}-1"
    return item_id


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

    def parse_parts_inventory(self, html: str, color_map=None):
        """Rows: {part_no, part_name, color_id, color_name, qty}.

        color_map: optional {color_id: color_name} from the DB-synced BrickLink
        color table (see fetch_color_table); the builtin _COLOR_NAMES list is
        only the fallback when the table hasn't been synced yet."""
        soup = BeautifulSoup(html, "html.parser")
        names = list((color_map or {}).values()) + self._COLOR_NAMES
        names = sorted(set(names), key=len, reverse=True)
        parts = []
        for tr in soup.find_all("tr"):
            links = tr.find_all("a", href=re.compile(r"\?P="))
            if not links:
                continue
            m = re.search(r"\?P=([^&\"]+)", links[0].get("href", ""))
            if not m:
                continue
            part_no = m.group(1)

            # Column order is Image | Qty | Item No | Description: the qty
            # cell sits right before the item-number link's cell. The
            # first-all-digit-cell scan is only the fallback — it grabs a
            # color id or year when the layout shifts.
            qty = None
            link_td = links[0].find_parent("td")
            if link_td is not None:
                prev = link_td.find_previous_sibling("td")
                if prev is not None and prev.get_text(strip=True).isdigit():
                    qty = int(prev.get_text(strip=True))
            if qty is None:
                for td in tr.find_all("td"):
                    txt = td.get_text(strip=True)
                    if txt.isdigit():
                        qty = int(txt)
                        break
            if qty is None:
                continue

            # The part's own links carry its color id; a row-wide search must
            # not win, because unrelated links earlier in the row (counterpart
            # or alternate-item references) can carry a different color.
            color_id = 0
            for link in links:
                cid = re.search(r"(?:idColor|colorID)=(\d+)", link.get("href", ""))
                if cid:
                    color_id = int(cid.group(1))
                    break
            if not color_id:
                cid = re.search(r"(?:idColor|colorID)=(\d+)", str(tr))
                if cid:
                    color_id = int(cid.group(1))

            # The description link is the longest-text ?P= link in the row
            # (the item-number link's text is just the part number itself).
            desc = max((l.get_text(" ", strip=True) for l in links), key=len)
            color_name, part_name = None, desc
            for cname in names:
                if desc.startswith(cname + " "):
                    color_name = cname
                    part_name = desc[len(cname):].strip()
                    break
            if color_name is None and color_id and color_map:
                # Name split failed but the id is known — resolve the name
                # from the table and un-glue it from the description.
                mapped = color_map.get(color_id)
                if mapped:
                    color_name = mapped
                    if desc.startswith(mapped + " "):
                        part_name = desc[len(mapped):].strip()

            parts.append({
                "part_no": part_no,
                "part_name": part_name,
                "color_id": color_id,
                "color_name": color_name,
                "qty": qty,
            })
        return parts

    def fetch_parts_inventory(self, item_id: str, item_type: str = "S",
                              color_map=None):
        """Parts inventory of a set ('S') or a minifig ('M').
        Returns (parts, error). Plain HTTP — the page is server-rendered."""
        url = f"{INV_URL}?{item_type}={inv_ref(item_id, item_type)}&viewItemType=P"
        html, error = self._get(url)
        if html:
            parts = self.parse_parts_inventory(html, color_map=color_map)
            if parts:
                return parts, None
            error = "no parts parsed from inventory page"
        return [], error

    # ── color table ──────────────────────────────────────────────────────

    def parse_color_table(self, html: str):
        """{color_id: name} from catalogColors.asp. Row shape is
        [swatch] [id] [name] [counts...]: the first all-digit cell is the id,
        the first following non-numeric cell is the name."""
        soup = BeautifulSoup(html, "html.parser")
        colors = {}
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            for i, td in enumerate(tds):
                txt = td.get_text(strip=True)
                if not txt.isdigit():
                    continue
                for td2 in tds[i + 1:]:
                    name = td2.get_text(" ", strip=True)
                    if name and not re.fullmatch(r"[\d,.\s%-]*", name):
                        if re.fullmatch(r"[A-Za-z][\w\s().'/-]*", name):
                            colors[int(txt)] = name
                        break
                break
        return colors

    def fetch_color_table(self):
        """Returns ({color_id: name}, error)."""
        html, error = self._get(COLORS_URL)
        if html:
            colors = self.parse_color_table(html)
            if colors:
                return colors, None
            error = "no colors parsed from color table page"
        return {}, error

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
        url = f"{INV_URL}?S={inv_ref(set_id)}&viewItemType=M"
        html, error = self._get(url)
        if html:
            figs = self.parse_minifig_inventory(html)
            if figs:
                return figs, None
            error = "no minifigs parsed from inventory page"
        return [], error

    # ── helpers ──────────────────────────────────────────────────────────

    _session = None
    _RETRY_DELAYS = (0, 2, 5, 12)     # seconds before each attempt, + jitter

    @classmethod
    def _get(cls, url):
        """GET with a shared session and exponential backoff. Retries network
        errors, 5xx and 429 (honoring Retry-After); other 4xx fail fast."""
        import random
        import time

        if cls._session is None:
            cls._session = requests.Session()
            cls._session.headers.update({
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            })
        last_err = None
        for delay in cls._RETRY_DELAYS:
            if delay:
                time.sleep(delay + random.uniform(0, 1.5))
            try:
                resp = cls._session.get(url, timeout=25)
                if resp.status_code == 429:
                    last_err = "HTTPError: 429 rate limited"
                    retry_after = resp.headers.get("Retry-After", "")
                    if retry_after.isdigit():
                        time.sleep(min(int(retry_after), 60))
                    continue
                resp.raise_for_status()
                return resp.text, None
            except requests.HTTPError as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                status = exc.response.status_code if exc.response is not None else 0
                if 400 <= status < 500:
                    break
            except requests.RequestException as exc:
                last_err = f"{type(exc).__name__}: {exc}"
        return None, last_err


if __name__ == "__main__":
    run_cli(BrickLinkSource())
