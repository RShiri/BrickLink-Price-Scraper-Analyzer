"""Per-item Scan button — the endpoint queues a forced scan and the page
banner tracks it. Offline: the worker's run_refresh is replaced by a fake."""
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from brickonomy import db as dbq
from brickonomy.config import get_config
from brickonomy.web import jobs
from brickonomy.web.app import app


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from brickonomy.currency import reset_rate_cache
    reset_rate_cache()
    db_path = tmp_path / "scan.db"
    monkeypatch.setattr(get_config(), "db_path", str(db_path))
    conn = dbq.connect(db_path=str(db_path))
    dbq.upsert_rate(conn, "USD", "ILS", 3.5)
    dbq.upsert_item(conn, "75192", name="Millennium Falcon", theme="Star Wars",
                    year=2017, minifigs=1)
    dbq.upsert_set_minifigs(conn, "75192",
                            [{"id": "sw0879", "name": "Han Solo", "qty": 1}])
    dbq.upsert_item(conn, "10294", name="Titanic", theme="Icons", year=2021)
    # Fresh blended snapshots → neither page auto-queues a scan on view.
    for iid in ("75192", "10294"):
        dbq.insert_snapshot(conn, iid, "blended", "new", "market", "ILS",
                            market_price=3200.0, confidence="HIGH")
    conn.commit()
    conn.close()

    calls = []
    started = threading.Event()
    release = threading.Event()

    def fake_run_refresh(scope="portfolio", item_id=None, force=False,
                         progress=None, log=print, **kw):
        calls.append({"item_id": item_id, "force": force,
                      "limit": kw.get("limit"), "min_year": kw.get("min_year")})
        if progress:
            progress(0, 1, item_id, [])
        started.set()
        release.wait(timeout=5)
        if progress:
            progress(1, 1, None, [])

    monkeypatch.setattr("brickonomy.refresh.run_refresh", fake_run_refresh)
    jobs.reset()
    yield SimpleNamespace(client=TestClient(app, follow_redirects=False),
                          calls=calls, started=started, release=release)
    release.set()
    for t in threading.enumerate():
        if t.name == "brickonomy-refresh":
            t.join(timeout=5)
    jobs.reset()


class TestScanButton:
    def test_button_renders_on_fresh_page_without_queueing(self, env):
        r = env.client.get("/sets/75192")
        assert r.status_code == 200
        assert '/sets/75192/scan' in r.text
        assert 'id="autoScan"' not in r.text        # fresh → no auto-scan
        assert jobs.status()["queue_len"] == 0

    def test_scan_endpoint_forces_a_scan_and_shows_banner(self, env):
        r = env.client.post("/sets/75192/scan")
        assert r.status_code == 303
        assert r.headers["location"] == "/sets/75192"
        assert env.started.wait(5)

        # While the scan runs, the page shows the banner despite fresh prices.
        page = env.client.get("/sets/75192")
        assert 'id="autoScan"' in page.text

        env.release.set()
        for t in threading.enumerate():
            if t.name == "brickonomy-refresh":
                t.join(timeout=5)
        assert env.calls == [{"item_id": "75192", "force": True, "limit": None, "min_year": None}]

    def test_scope_scan_carries_the_form_limit(self, env):
        r = env.client.post("/refresh", data={"scope": "missing", "limit": "5"})
        assert r.status_code == 303
        assert env.started.wait(5)
        env.release.set()
        for t in threading.enumerate():
            if t.name == "brickonomy-refresh":
                t.join(timeout=5)
        assert env.calls == [{"item_id": None, "force": False, "limit": 5, "min_year": None}]

    def test_scope_scan_carries_the_form_min_year(self, env):
        r = env.client.post("/refresh", data={"scope": "missing",
                                              "min_year": "2010"})
        assert r.status_code == 303
        assert env.started.wait(5)
        env.release.set()
        for t in threading.enumerate():
            if t.name == "brickonomy-refresh":
                t.join(timeout=5)
        assert env.calls == [{"item_id": None, "force": False, "limit": None,
                              "min_year": 2010}]

    def test_stop_finishes_current_item_and_drops_queue(self, env):
        env.client.post("/sets/75192/scan")
        assert env.started.wait(5)
        env.client.post("/sets/10294/scan")        # queued behind the first
        r = env.client.post("/refresh/stop")
        assert r.status_code == 303
        assert jobs.status()["queue"] == []        # queued work dropped
        assert jobs.status()["stop_requested"] is True

        env.release.set()
        for t in threading.enumerate():
            if t.name == "brickonomy-refresh":
                t.join(timeout=5)
        s = jobs.status()
        assert s["running"] is False and s["stop_requested"] is False
        # The in-flight item completed; the queued one never ran.
        assert env.calls == [{"item_id": "75192", "force": True, "limit": None, "min_year": None}]

    def test_queued_banner_counts_the_scanning_item_as_ahead(self, env):
        env.client.post("/sets/75192/scan")         # being scraped
        assert env.started.wait(5)
        env.client.post("/sets/10294/scan")         # queued behind it
        page = env.client.get("/sets/10294")
        assert "1 ahead" in page.text               # not "0 ahead"

    def test_stop_with_nothing_running_is_a_noop(self, env):
        assert jobs.stop() is False

    def test_queued_scan_keeps_its_force_flag(self, env):
        env.client.post("/sets/75192/scan")
        assert env.started.wait(5)
        env.client.post("/sets/10294/scan")     # queued behind the first
        assert jobs.status()["queue"] == ["10294"]

        env.release.set()
        for t in threading.enumerate():
            if t.name == "brickonomy-refresh":
                t.join(timeout=5)
        assert env.calls == [{"item_id": "75192", "force": True, "limit": None, "min_year": None},
                             {"item_id": "10294", "force": True, "limit": None, "min_year": None}]


class TestEmptyScanMemory:
    def test_recent_empty_scan_is_not_requeued_on_view(self, env):
        conn = dbq.connect()
        dbq.upsert_item(conn, "0012", name="Space Mini-Figures", year=1979)
        dbq.record_scan_attempt(
            conn, "0012", "bricklink: empty, ebay: empty, brickowl: empty")
        conn.close()
        r = env.client.get("/sets/0012")
        assert r.status_code == 200
        assert "no listings found" in r.text
        assert 'id="autoScan"' not in r.text
        assert jobs.status()["queue_len"] == 0     # no futile rescan queued

    def test_old_empty_attempt_rescans(self, env):
        conn = dbq.connect()
        dbq.upsert_item(conn, "0013", name="Castle Mini-Figures", year=1979)
        conn.execute(
            "INSERT INTO scan_attempts (item_id, attempted_at, note) VALUES (?,?,?)",
            ("0013", "2020-01-01T00:00:00", "bricklink: empty"))
        conn.commit()
        conn.close()
        r = env.client.get("/sets/0013")
        assert 'id="autoScan"' in r.text           # stale attempt → try again
        assert env.started.wait(5)

    def test_nodata_page_lists_empty_items_with_retry(self, env):
        conn = dbq.connect()
        dbq.upsert_item(conn, "0012", name="Space Mini-Figures", year=1979)
        dbq.record_scan_attempt(
            conn, "0012", "bricklink: empty, ebay: empty, brickowl: empty")
        conn.close()
        r = env.client.get("/nodata")
        assert r.status_code == 200
        assert "Space Mini-Figures" in r.text
        assert "/sets/0012/scan" in r.text          # retry button
        assert "75192" not in r.text                # never attempted → not listed

    def test_minifig_brickeconomy_link_goes_via_search(self, env):
        from brickonomy.web.app import brickeconomy_url
        assert brickeconomy_url("sh0727", "M") == \
            "https://www.brickeconomy.com/search?query=sh0727"
        assert brickeconomy_url("76178", "S") == \
            "https://www.brickeconomy.com/set/76178-1"


class TestGracefulStopLoop:
    def test_run_refresh_stops_between_items(self, tmp_path, monkeypatch):
        """should_stop is honored between items: the current item finishes,
        the rest of the target list is skipped."""
        from brickonomy import refresh as refresh_mod
        from brickonomy.config import get_config

        monkeypatch.setattr(get_config(), "db_path", str(tmp_path / "stop.db"))
        conn = dbq.connect(db_path=str(tmp_path / "stop.db"))
        for iid in ("1111", "2222", "3333"):
            dbq.upsert_item(conn, iid, name=f"Set {iid}")
        conn.commit()
        conn.close()

        scanned = []
        monkeypatch.setattr(refresh_mod, "refresh_item",
                            lambda conn, iid, itype=None, force=False, log=print:
                            scanned.append(iid) or {})
        monkeypatch.setattr(refresh_mod, "polite_sleep", lambda: None)
        out = refresh_mod.run_refresh(scope="all", log=lambda *a: None,
                                      should_stop=lambda: len(scanned) >= 1)
        assert scanned == ["1111"] and out["done"] == 1


class TestMissingScope:
    def test_targets_only_items_never_scraped(self, tmp_path, monkeypatch):
        """scope='missing' scans only items no marketplace was scraped for;
        imported-only values count as missing, and sets go before minifigs."""
        from brickonomy import refresh as refresh_mod
        from brickonomy.config import get_config

        monkeypatch.setattr(get_config(), "db_path", str(tmp_path / "miss.db"))
        conn = dbq.connect(db_path=str(tmp_path / "miss.db"))
        dbq.upsert_item(conn, "1111", name="Bare set")
        dbq.upsert_item(conn, "2222", name="Scraped set")
        dbq.insert_snapshot(conn, "2222", "bricklink", "new", "market", "ILS",
                            market_price=100.0)
        dbq.upsert_item(conn, "3333", name="CSV-imported set")
        dbq.insert_snapshot(conn, "3333", "brickeconomy", "new", "market", "USD",
                            market_price=50.0)
        dbq.upsert_item(conn, "sw0001", name="Bare fig", item_type="M")
        # 4444: tried recently, nothing anywhere → sits out until the TTL.
        dbq.upsert_item(conn, "4444", name="Collection pack")
        dbq.record_scan_attempt(conn, "4444", "bricklink: empty, ebay: empty")
        # 5555: tried long ago → retried, but after the never-attempted sets.
        dbq.upsert_item(conn, "5555", name="Old attempt")
        conn.execute(
            "INSERT INTO scan_attempts (item_id, attempted_at, note) VALUES (?,?,?)",
            ("5555", "2020-01-01T00:00:00", "bricklink: empty"))
        conn.commit()
        conn.close()

        scanned = []
        monkeypatch.setattr(refresh_mod, "refresh_item",
                            lambda conn, iid, itype=None, force=False, log=print:
                            scanned.append(iid) or {})
        monkeypatch.setattr(refresh_mod, "polite_sleep", lambda: None)
        out = refresh_mod.run_refresh(scope="missing", log=lambda *a: None)
        # Sets first (never-attempted before old-attempted), then figs;
        # 2222 has data and 4444 was tried recently — both skipped.
        assert scanned == ["1111", "3333", "5555", "sw0001"]
        assert out["done"] == 4


class TestCatalogCleanup:
    def test_mark_untradeable_flags_bundles_and_gear(self, tmp_path, monkeypatch):
        from brickonomy.rebrickable import mark_untradeable

        conn = dbq.connect(db_path=str(tmp_path / "clean.db"))
        dbq.upsert_item(conn, "5006061", name="VIP Key Chain", theme="Gear")
        dbq.upsert_item(conn, "66674", name="Star Wars Value Pack 2 in 1",
                        theme="Star Wars")
        dbq.upsert_item(conn, "5008943", name="Batman Collection", theme="Batman")
        dbq.upsert_item(conn, "75192", name="Millennium Falcon", theme="Star Wars")
        conn.commit()

        n = mark_untradeable(conn, log=lambda *a: None)
        assert n == 3
        flags = {r["item_id"]: r["excluded"] for r in
                 conn.execute("SELECT item_id, excluded FROM items")}
        assert flags == {"5006061": 1, "66674": 1, "5008943": 1, "75192": 0}
        conn.close()

    def test_scope_scans_skip_excluded_and_old_items(self, tmp_path, monkeypatch):
        from brickonomy import refresh as refresh_mod
        from brickonomy.config import get_config

        monkeypatch.setattr(get_config(), "db_path", str(tmp_path / "yr.db"))
        conn = dbq.connect(db_path=str(tmp_path / "yr.db"))
        dbq.upsert_item(conn, "1111", name="Modern set", year=2015)
        dbq.upsert_item(conn, "2222", name="Vintage set", year=2005)
        dbq.upsert_item(conn, "3333", name="Some Bundle", year=2020)
        dbq.upsert_item(conn, "sw0001", name="Fig, year unknown", item_type="M")
        conn.execute("UPDATE items SET excluded=1 WHERE item_id='3333'")
        conn.commit()
        conn.close()

        scanned = []
        monkeypatch.setattr(refresh_mod, "refresh_item",
                            lambda conn, iid, itype=None, force=False, log=print:
                            scanned.append(iid) or {})
        monkeypatch.setattr(refresh_mod, "polite_sleep", lambda: None)
        out = refresh_mod.run_refresh(scope="missing", min_year=2010,
                                      log=lambda *a: None)
        # 2222 predates 2010, 3333 is excluded; the year-less fig stays.
        assert scanned == ["1111", "sw0001"]
        assert out["done"] == 2


class TestPortfolioEditing:
    def test_import_route_not_shadowed_by_item_id(self, env):
        r = env.client.post("/portfolio/import",
                            files={"file": ("sets.txt", b"75192\n", "text/plain")})
        assert r.status_code == 200         # preview page, not a 422
        assert "75192" in r.text

    def test_edit_saves_and_returns_to_the_row(self, env):
        conn = dbq.connect()
        dbq.upsert_portfolio(conn, "75192", owned=1)
        conn.close()
        r = env.client.post("/portfolio/75192",
                            data={"qty": "4", "condition": "used",
                                  "paid": "500", "paid_ccy": "USD",
                                  "purchase_date": ""})
        assert r.status_code == 303
        assert r.headers["location"].endswith("#row-75192")
        conn = dbq.connect()
        row = conn.execute(
            "SELECT * FROM portfolio WHERE item_id='75192'").fetchone()
        assert (row["owned"], row["condition"], row["purchase_price"]) == \
            (4, "used", 500.0)
        conn.close()

    def test_bad_qty_reopens_the_edit_row_instead_of_erroring(self, env):
        conn = dbq.connect()
        dbq.upsert_portfolio(conn, "75192", owned=2)
        conn.close()
        r = env.client.post("/portfolio/75192", data={"qty": "abc"})
        assert r.status_code == 303
        assert "edit=75192" in r.headers["location"]
        conn = dbq.connect()
        assert conn.execute(
            "SELECT owned FROM portfolio WHERE item_id='75192'"
        ).fetchone()["owned"] == 2          # nothing was lost or deleted
        conn.close()
