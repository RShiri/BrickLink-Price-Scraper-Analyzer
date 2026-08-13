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
        calls.append({"item_id": item_id, "force": force})
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
        assert env.calls == [{"item_id": "75192", "force": True}]

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
        assert env.calls == [{"item_id": "75192", "force": True}]

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
        assert env.calls == [{"item_id": "75192", "force": True},
                             {"item_id": "10294", "force": True}]


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
        conn.commit()
        conn.close()

        scanned = []
        monkeypatch.setattr(refresh_mod, "refresh_item",
                            lambda conn, iid, itype=None, force=False, log=print:
                            scanned.append(iid) or {})
        monkeypatch.setattr(refresh_mod, "polite_sleep", lambda: None)
        out = refresh_mod.run_refresh(scope="missing", log=lambda *a: None)
        assert scanned == ["1111", "3333", "sw0001"]     # sets first, no 2222
        assert out["done"] == 3
