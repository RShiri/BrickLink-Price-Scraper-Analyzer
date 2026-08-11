# Brickonomy

A self-hosted, BrickEconomy-style LEGO value tracker that lives inside this
repo and builds on its existing BrickLink scraper and `PriceAnalyzer`.

- **Three price sources, no API keys**: BrickLink (the repo's existing Selenium
  scraper), **eBay** (sold + active listings) and **BrickOwl** (current asks) —
  all plain scraping.
- **Real price history**: every scan appends snapshots to `brickonomy.db`
  instead of overwriting, so every set gets a price-over-time chart.
- **Blended market value**: per-source PriceAnalyzer market prices are
  currency-converted and combined with confidence weights (HIGH=3, MEDIUM=2,
  LOW=1) into one blended series.
- **Growth & forecast**: growth vs retail, annualized CAGR, and a 2y/5y trend
  forecast with retirement-phase adjustment (±20% band — a trend estimate,
  not financial advice).
- **Parts inventory & part-out value**: the exact parts list from the
  BrickLink set page plus the part-out total from its POV page.
- **Portfolio**: seeded from your BrickEconomy CSV export; import more via
  CSV / BrickLink wanted-list XML / plain set lists; edit rows inline.
- **Configurable currency**: ILS / USD / EUR / GBP, ECB rates via
  frankfurter.app (no key), with cached and hardcoded fallbacks.

## Quick start

```bash
pip install -r brickonomy/requirements.txt

# 1. Seed the catalog + portfolio from what the repo already has
#    (BrickEconomy-Sets(2).csv and bricklink_data.db)
python -m brickonomy.importer

# 2. Start the web app
uvicorn brickonomy.web.app:app --reload
# open http://127.0.0.1:8000
```

Refresh prices from the header button, the Refresh & Sources page, or the CLI:

```bash
python -m brickonomy.refresh --item 75192       # one set
python -m brickonomy.refresh --scope portfolio  # everything you own
python -m brickonomy.refresh --scope stale      # only sets older than the TTL
```

Cron example (daily 06:00):

```
0 6 * * * cd /path/to/repo && python -m brickonomy.refresh --scope stale
```

## Configuration

Copy `brickonomy/config.example.json` to `brickonomy/config.json` and edit, or
use `BRICKONOMY_*` environment variables (e.g. `BRICKONOMY_DISPLAY_CURRENCY=USD`,
`BRICKONOMY_SCRAPE_TTL_DAYS=1`). Keys: `display_currency`, `scrape_ttl_days`,
`request_delay_range`, `sources_enabled`, `rates_ttl_hours`, `fixture_mode`.

## Scrapers

Each scraper runs standalone and prints the normalized result + a
PriceAnalyzer summary:

```bash
python -m brickonomy.scrapers.ebay 75192
python -m brickonomy.scrapers.brickowl 75192
python -m brickonomy.scrapers.bricklink 75192
```

Flags: `--fixture` parses saved HTML from `brickonomy/tests/fixtures/`
(no network), `--save-fixture` stores freshly fetched HTML there — useful for
locking parser behavior into tests after eBay/BrickOwl markup changes.

Notes per source:

- **BrickLink** — wraps the root `scraper.py` (headless Chrome; prices in the
  session currency, ILS by default). Also fetches the set's full parts list
  and part-out value over plain HTTP.
- **eBay** — headless Chrome with stealth flags; sold/completed
  (`LH_Sold=1&LH_Complete=1`) and Buy-It-Now searches; listings are
  title-classified into new/used and junk lots (instructions-only,
  minifig-only, bulk, clones) are filtered. Titles run through
  PriceAnalyzer's completeness blacklist like any other description.
- **BrickOwl** — plain HTTP; catalog search resolves the item page, whose
  "Buy" offers become current asks. BrickOwl has no public sold history, so
  its confidence is LOW by design.

Scrapers never raise: failures return an empty result with an `error` field,
the refresh records it, and the UI keeps serving the last snapshots.

## Tests

```bash
python -m pytest brickonomy/tests   # 38 tests, fully offline
```

## Layout

```
brickonomy/
  compat.py       bridge to the repo root's PriceAnalyzer / scraper / Database
  config.py       config.json + BRICKONOMY_* env
  db.py           brickonomy.db schema (append-only price_snapshots) + queries
  currency.py     frankfurter.app rates, cached + hardcoded fallbacks
  importer.py     seeds items/portfolio/snapshots from the repo's existing data
  refresh.py      multi-source refresh orchestrator (CLI + web job)
  snapshots.py    PriceAnalyzer results → snapshot rows
  analytics/      valuation (blend), growth, forecast, lifecycle
  scrapers/       base + bricklink / ebay / brickowl
  web/            FastAPI app, Jinja2 templates, Chart.js UI
  tests/          fixture-based parser tests + analytics/currency tests
```

The existing root scripts (`runner.py`, `dashboard.py`, …) are untouched and
keep working; Brickonomy only reads `bricklink_data.db`, never writes it.
