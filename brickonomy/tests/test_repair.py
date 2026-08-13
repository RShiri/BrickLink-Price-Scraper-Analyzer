"""eBay currency history repair — offline, seeded tmp DB."""
from datetime import datetime, timedelta

import pytest

from brickonomy import db as dbq
from brickonomy.repair import repair_ebay_currency


@pytest.fixture()
def conn(tmp_path):
    from brickonomy.currency import reset_rate_cache
    reset_rate_cache()
    c = dbq.connect(db_path=str(tmp_path / "rep.db"))
    dbq.upsert_rate(c, "USD", "ILS", 3.5)
    yield c
    c.close()


def ts(hours_ago):
    return (datetime.now() - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def seed(conn, iid, source, ccy, price, hours_ago, cond="new"):
    dbq.insert_snapshot(conn, iid, source, cond, "market", ccy,
                        market_price=price, confidence="HIGH",
                        scraped_at=ts(hours_ago))


class TestEbayCurrencyRepair:
    def test_relabels_shekel_scale_usd_rows_and_rebuilds_blend(self, conn):
        dbq.upsert_item(conn, "76178", name="Daily Bugle")
        seed(conn, "76178", "bricklink", "ILS", 1241, hours_ago=3)
        seed(conn, "76178", "ebay", "USD", 1357, hours_ago=2)   # really ILS
        seed(conn, "76178", "blended", "ILS", 2708, hours_ago=1)  # inflated
        conn.commit()

        n = repair_ebay_currency(conn, log=lambda *a: None)
        assert n == 1
        assert conn.execute(
            "SELECT currency FROM price_snapshots WHERE source='ebay'"
        ).fetchone()["currency"] == "ILS"
        # The inflated blended point is gone; the fresh one is on the
        # shekel scale (both sources HIGH → plain average).
        row = dbq.latest_snapshot(conn, "76178", "blended", "new")
        assert row["market_price"] == pytest.approx((1241 + 1357) / 2, rel=1e-6)

    def test_genuine_usd_rows_untouched(self, conn):
        dbq.upsert_item(conn, "75192", name="Millennium Falcon")
        seed(conn, "75192", "bricklink", "ILS", 3500, hours_ago=3)
        seed(conn, "75192", "ebay", "USD", 1000, hours_ago=2)   # = 3500 ILS, sane
        conn.commit()

        n = repair_ebay_currency(conn, log=lambda *a: None)
        assert n == 0
        assert conn.execute(
            "SELECT currency FROM price_snapshots WHERE source='ebay'"
        ).fetchone()["currency"] == "USD"

    def test_prune_drops_spikes_above_current_value(self, conn):
        from brickonomy.repair import prune_blended_spikes

        dbq.upsert_item(conn, "76178", name="Daily Bugle")
        seed(conn, "76178", "blended", "ILS", 1250, hours_ago=96)   # sane, old
        seed(conn, "76178", "blended", "ILS", 2708, hours_ago=24)   # the spike
        seed(conn, "76178", "blended", "ILS", 1300, hours_ago=1)    # corrected
        conn.commit()

        n = prune_blended_spikes(conn, log=lambda *a: None)
        assert n == 1
        vals = [r["market_price"] for r in conn.execute(
            "SELECT market_price FROM price_snapshots WHERE source='blended' "
            "ORDER BY scraped_at")]
        assert vals == [1250, 1300]                 # spike gone, history kept

    def test_prune_keeps_normal_history(self, conn):
        from brickonomy.repair import prune_blended_spikes

        dbq.upsert_item(conn, "75192", name="Falcon")
        seed(conn, "75192", "blended", "ILS", 3400, hours_ago=96)
        seed(conn, "75192", "blended", "ILS", 3600, hours_ago=24)   # normal drift
        seed(conn, "75192", "blended", "ILS", 3500, hours_ago=1)
        conn.commit()
        assert prune_blended_spikes(conn, log=lambda *a: None) == 0
        assert conn.execute("SELECT COUNT(*) c FROM price_snapshots "
                            "WHERE source='blended'").fetchone()["c"] == 3

    def test_no_reference_prices_leaves_rows_alone(self, conn):
        dbq.upsert_item(conn, "0012", name="Space Mini-Figures")
        seed(conn, "0012", "ebay", "USD", 400, hours_ago=2)     # nothing to compare
        conn.commit()
        assert repair_ebay_currency(conn, log=lambda *a: None) == 0
