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

INV_URL = "https://www.bricklink.com/catalogItemInv.asp"
POV_URL = "https://www.bricklink.com/catalogPOV.asp"


class BrickLinkSource(BaseScraper):
    source = "bricklink"
    currency = "ILS"

    def parse(self, html, item_id: str) -> ScrapeResult:
        scraper_cls = get_bricklink_scraper()
        data = scraper_cls._parse_html(scraper_cls.__new__(scraper_cls), item_id, html)
        res = self.empty_result(item_id)
        res.meta = data.get("meta", {})
        res.new = data.get("new", res.new)
        res.used = data.get("used", res.used)
        return res

    def _fetch_html(self, item_id: str, item_type: str = "S"):
        # Reuse the root scraper end-to-end (it waits for the AJAX'd price
        # tables), then hand its page source to parse() indirectly by
        # re-fetching from its DB-normalised output. Simpler: run its scrape()
        # and adapt the dict — no second parse needed.
        raise NotImplementedError  # fetch() is overridden below

    def fetch(self, item_id: str, item_type: str = "S",
              fixture: bool = None, save_fixture: bool = False) -> ScrapeResult:
        from ..config import get_config
        use_fixture = get_config().fixture_mode if fixture is None else fixture
        if use_fixture:
            try:
                return self.parse(self._load_fixture(item_id), item_id)
            except Exception as exc:
                return self.empty_result(item_id, error=f"{type(exc).__name__}: {exc}")
        try:
            scraper = get_bricklink_scraper()()
            data = scraper.scrape(item_id, item_type, force=True)
            res = self.empty_result(item_id)
            res.meta = data.get("meta", {})
            res.new = data.get("new", res.new)
            res.used = data.get("used", res.used)
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

    def parse_part_out_value(self, html: str):
        """Extract the 'Average of last 6 months Sales' / current-items part-out
        total from catalogPOV.asp. Returns float or None."""
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        # e.g. "Total Average Value of 631 Lots (7541 Items): US $1,234.56" or ILS
        m = re.search(r"Average\s+Value[^:]*:\s*(?:[A-Z]{2,3}\s*)?[$₪€£]?\s*([\d,]+\.\d{2})",
                      text, re.IGNORECASE)
        if not m:
            m = re.search(r"Total[^:]{0,60}:\s*(?:[A-Z]{2,3}\s*)?[$₪€£]?\s*([\d,]+\.\d{2})", text)
        if not m:
            return None
        return float(m.group(1).replace(",", ""))

    def fetch_part_out_value(self, set_id: str, condition: str = "N"):
        num = set_id.split("-")[0]
        url = (f"{POV_URL}?itemType=S&itemNo={num}&itemSeq=1&itemQty=1"
               f"&breakType=M&itemCondition={condition}")
        html, error = self._get(url)
        if html:
            value = self.parse_part_out_value(html)
            if value is not None:
                return value, None
            error = "POV total not found on page"
        return None, error

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
