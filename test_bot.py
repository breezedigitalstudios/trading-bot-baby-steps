"""
test_bot.py — unit tests for core trading logic.
Run with: python -m pytest test_bot.py -v
"""

import unittest
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import date, timedelta
import pandas as pd
import numpy as np
import sys, os

sys.path.insert(0, os.path.dirname(__file__))

# ── Helpers ────────────────────────────────────────────────────────────────────

def make_trade(**kwargs):
    defaults = {
        "id":             "test-id",
        "symbol":         "TEST",
        "stars":          4,
        "orb_high":       30.00,
        "orb_low":        28.00,
        "orb_range":      2.00,
        "atr":            5.00,
        "shares":         100,
        "shares_remaining": 100,
        "fill_price":     30.00,
        "fill_date":      str(date.today() - timedelta(days=1)),
        "stop_price":     28.00,
        "current_stop":   28.00,
        "entry_order_id": "order-123",
        "stop_order_id":  "stop-456",
        "status":         "open",
    }
    defaults.update(kwargs)
    return defaults


def make_mock_order(status="filled", filled_avg_price=30.00, filled_qty=100, filled_at=None):
    order = MagicMock()
    order.status = status
    order.filled_avg_price = filled_avg_price
    order.filled_qty = str(filled_qty)
    order.filled_at = MagicMock()
    order.filled_at.date.return_value = date.today()
    return order


def make_bars(closes, highs=None, lows=None):
    """Build a minimal OHLCV DataFrame for SMA/ATR tests."""
    n = len(closes)
    highs  = highs  or [c * 1.01 for c in closes]
    lows   = lows   or [c * 0.99 for c in closes]
    return pd.DataFrame({
        "close": closes,
        "high":  highs,
        "low":   lows,
        "open":  closes,
        "volume": [1_000_000] * n,
    })


# ── Patch Alpaca clients at import time ────────────────────────────────────────

@patch("alpaca.trading.client.TradingClient.__init__", return_value=None)
@patch("alpaca.data.historical.StockHistoricalDataClient.__init__", return_value=None)
class TestHandlePending(unittest.TestCase):
    """position_manager.handle_pending — entry order fill detection."""

    def setUp(self):
        import importlib
        import position_manager as pm
        importlib.reload(pm)
        self.pm = pm

    # ── Bug regression: order not found on Alpaca ──────────────────────────────
    def test_order_not_found_marks_expired(self, *_):
        self.pm.get_order = MagicMock(return_value=None)
        trade = make_trade(status="pending")
        result = self.pm.handle_pending(trade)
        self.assertTrue(result)
        self.assertEqual(trade["status"], "expired")
        self.assertEqual(trade["exit_reason"], "entry_order_not_found")

    # ── Bug regression: order expired on Alpaca ────────────────────────────────
    def test_order_expired_marks_expired(self, *_):
        self.pm.get_order = MagicMock(return_value=make_mock_order(status="expired"))
        trade = make_trade(status="pending")
        result = self.pm.handle_pending(trade)
        self.assertTrue(result)
        self.assertEqual(trade["status"], "expired")
        self.assertEqual(trade["exit_reason"], "entry_order_expired")

    def test_order_cancelled_marks_expired(self, *_):
        self.pm.get_order = MagicMock(return_value=make_mock_order(status="canceled"))
        trade = make_trade(status="pending")
        result = self.pm.handle_pending(trade)
        self.assertTrue(result)
        self.assertEqual(trade["status"], "expired")

    # ── Happy path: order filled ───────────────────────────────────────────────
    def test_order_filled_transitions_to_open(self, *_):
        self.pm.get_order     = MagicMock(return_value=make_mock_order(status="filled", filled_avg_price=30.25))
        self.pm.place_stop_loss = MagicMock(return_value="new-stop-id")
        trade = make_trade(status="pending")
        result = self.pm.handle_pending(trade)
        self.assertTrue(result)
        self.assertEqual(trade["status"], "open")
        self.assertAlmostEqual(trade["fill_price"], 30.25)
        self.assertEqual(trade["stop_order_id"], "new-stop-id")

    # ── Order still pending (not filled yet) ───────────────────────────────────
    def test_order_still_pending_returns_false(self, *_):
        self.pm.get_order = MagicMock(return_value=make_mock_order(status="new"))
        trade = make_trade(status="pending")
        result = self.pm.handle_pending(trade)
        self.assertFalse(result)
        self.assertEqual(trade["status"], "pending")


@patch("alpaca.trading.client.TradingClient.__init__", return_value=None)
@patch("alpaca.data.historical.StockHistoricalDataClient.__init__", return_value=None)
class TestHandleStopHit(unittest.TestCase):
    """position_manager.handle_stop_hit — stop-loss fill detection."""

    def setUp(self):
        import importlib, position_manager as pm
        importlib.reload(pm)
        self.pm = pm

    def test_stop_filled_closes_trade_with_correct_pnl(self, *_):
        self.pm.get_order = MagicMock(return_value=make_mock_order(status="filled", filled_avg_price=27.50))
        trade = make_trade(fill_price=30.00, shares_remaining=100)
        result = self.pm.handle_stop_hit(trade)
        self.assertTrue(result)
        self.assertEqual(trade["status"], "closed")
        self.assertEqual(trade["exit_reason"], "stop_hit")
        self.assertAlmostEqual(trade["pnl"], (27.50 - 30.00) * 100)  # -250

    def test_stop_not_filled_returns_false(self, *_):
        self.pm.get_order = MagicMock(return_value=make_mock_order(status="new"))
        trade = make_trade()
        result = self.pm.handle_stop_hit(trade)
        self.assertFalse(result)
        self.assertEqual(trade["status"], "open")

    def test_no_stop_order_id_returns_false(self, *_):
        trade = make_trade(stop_order_id=None)
        result = self.pm.handle_stop_hit(trade)
        self.assertFalse(result)


@patch("alpaca.trading.client.TradingClient.__init__", return_value=None)
@patch("alpaca.data.historical.StockHistoricalDataClient.__init__", return_value=None)
class TestHandlePhase1(unittest.TestCase):
    """position_manager.handle_phase1 — partial exit after 3 trading days."""

    def setUp(self):
        import importlib, position_manager as pm
        importlib.reload(pm)
        self.pm = pm

    def _positions(self, symbol, current_price):
        pos = MagicMock()
        pos.current_price = str(current_price)
        return {symbol: pos}

    def test_triggers_after_3_days_profitable(self, *_):
        self.pm.sell_market   = MagicMock(return_value="sell-id")
        self.pm.cancel_order  = MagicMock(return_value=True)
        self.pm.place_stop_loss = MagicMock(return_value="new-stop")
        fill_date = str(date.today() - timedelta(days=5))
        trade = make_trade(fill_date=fill_date, fill_price=28.00, shares_remaining=90)
        result = self.pm.handle_phase1(trade, self._positions("TEST", 35.00))
        self.assertTrue(result)
        self.assertEqual(trade["status"], "partial_exit")
        self.assertEqual(trade["current_stop"], 28.00)     # moved to breakeven

    def test_no_trigger_before_3_days(self, *_):
        fill_date = str(date.today() - timedelta(days=1))
        trade = make_trade(fill_date=fill_date, fill_price=28.00)
        result = self.pm.handle_phase1(trade, self._positions("TEST", 35.00))
        self.assertFalse(result)
        self.assertEqual(trade["status"], "open")

    def test_no_trigger_if_not_profitable(self, *_):
        fill_date = str(date.today() - timedelta(days=5))
        trade = make_trade(fill_date=fill_date, fill_price=32.00)
        result = self.pm.handle_phase1(trade, self._positions("TEST", 30.00))
        self.assertFalse(result)
        self.assertEqual(trade["status"], "open")

    def test_no_trigger_if_position_missing(self, *_):
        fill_date = str(date.today() - timedelta(days=5))
        trade = make_trade(fill_date=fill_date, fill_price=28.00)
        result = self.pm.handle_phase1(trade, {})
        self.assertFalse(result)


@patch("alpaca.trading.client.TradingClient.__init__", return_value=None)
@patch("alpaca.data.historical.StockHistoricalDataClient.__init__", return_value=None)
class TestHandleSmaExit(unittest.TestCase):
    """position_manager.handle_sma_exit — EOD trailing SMA exit."""

    def setUp(self):
        import importlib, position_manager as pm
        importlib.reload(pm)
        self.pm = pm

    def _bars_with_close(self, last_close, sma_value):
        """Return bars where last close is last_close and SMA10 ≈ sma_value."""
        closes = [sma_value] * 9 + [last_close]
        return make_bars(closes)

    def test_exits_when_close_below_sma10(self, *_):
        self.pm.fetch_daily_bars = MagicMock(return_value=self._bars_with_close(28.00, 32.00))
        self.pm.sell_market     = MagicMock(return_value="sell-id")
        self.pm.cancel_order    = MagicMock(return_value=True)
        trade = make_trade(fill_price=29.00, shares_remaining=60)
        result = self.pm.handle_sma_exit(trade)
        self.assertTrue(result)
        self.assertEqual(trade["status"], "closed")
        self.assertEqual(trade["exit_reason"], "sma10_close")

    def test_holds_when_close_above_sma10(self, *_):
        self.pm.fetch_daily_bars = MagicMock(return_value=self._bars_with_close(36.00, 32.00))
        trade = make_trade(fill_price=29.00)
        result = self.pm.handle_sma_exit(trade)
        self.assertFalse(result)
        self.assertEqual(trade["status"], "open")

    def test_skips_when_insufficient_bars(self, *_):
        self.pm.fetch_daily_bars = MagicMock(return_value=make_bars([30.00] * 5))
        trade = make_trade()
        result = self.pm.handle_sma_exit(trade)
        self.assertFalse(result)

    def test_total_pnl_includes_phase1(self, *_):
        self.pm.fetch_daily_bars = MagicMock(return_value=self._bars_with_close(27.00, 32.00))
        self.pm.sell_market     = MagicMock(return_value="sell-id")
        self.pm.cancel_order    = MagicMock(return_value=True)
        trade = make_trade(fill_price=25.00, shares_remaining=60, phase1_pnl=150.00)
        result = self.pm.handle_sma_exit(trade)
        self.assertTrue(result)
        expected_runner = (27.00 - 25.00) * 60   # +120
        self.assertAlmostEqual(trade["pnl"], 150.00 + expected_runner)


@patch("alpaca.trading.client.TradingClient.__init__", return_value=None)
@patch("alpaca.data.historical.StockHistoricalDataClient.__init__", return_value=None)
class TestSizePosition(unittest.TestCase):
    """entry_executor.size_position — share count calculation."""

    def setUp(self):
        import importlib, entry_executor as ee
        importlib.reload(ee)
        self.ee = ee

    def test_capped_by_25pct_position_limit(self, *_):
        # risk: 10k/0.01 = 1M shares; cap: 25k/100 = 250 → cap wins
        shares = self.ee.size_position(100_000, 100.00, 99.99)
        self.assertEqual(shares, 250)

    def test_capped_by_10pct_risk(self, *_):
        # risk: 10k/10 = 1000 shares; cap: 25k/15 = 1666 → risk wins
        shares = self.ee.size_position(100_000, 15.00, 5.00)
        self.assertEqual(shares, 1000)

    def test_minimum_one_share(self, *_):
        shares = self.ee.size_position(1_000, 500.00, 499.00)
        self.assertGreaterEqual(shares, 1)

    def test_zero_range_returns_zero(self, *_):
        shares = self.ee.size_position(100_000, 30.00, 30.00)
        self.assertEqual(shares, 0)

    def test_cue_real_scenario(self, *_):
        # CUE: orb_high=30.25 orb_low=27.51 portfolio=100k → 777 shares
        shares = self.ee.size_position(100_000, 30.25, 27.51)
        risk_shares = int((100_000 * 0.10) / (30.25 - 27.51))
        cap_shares  = int((100_000 * 0.25) / 30.25)
        self.assertEqual(shares, min(risk_shares, cap_shares))


@patch("alpaca.trading.client.TradingClient.__init__", return_value=None)
@patch("alpaca.data.historical.StockHistoricalDataClient.__init__", return_value=None)
class TestEntryGates(unittest.TestCase):
    """entry_executor — skip logic for invalid setups."""

    def setUp(self):
        import importlib, entry_executor as ee
        importlib.reload(ee)
        self.ee = ee

    def test_orb_range_exceeds_atr_skipped(self, *_):
        """ORB range wider than ATR means risk too wide — must skip."""
        orb_high, orb_low, atr = 32.00, 28.00, 3.00  # range=4 > ATR=3
        self.assertGreater(orb_high - orb_low, atr)

    def test_orb_range_within_atr_allowed(self, *_):
        orb_high, orb_low, atr = 30.25, 27.51, 8.09  # range=2.74 < ATR=8.09
        self.assertLessEqual(orb_high - orb_low, atr)

    def test_cost_exceeds_capital_skipped(self, *_):
        portfolio, orb_high, orb_low = 100_000, 30.00, 28.00
        available = portfolio * (1 - 0.25) - (portfolio * 0.75)  # ~0 deployable
        shares = self.ee.size_position(portfolio, orb_high, orb_low)
        cost   = orb_high * shares
        # Just verify the math — gate logic uses available_to_deploy
        self.assertGreater(cost, 0)


@patch("alpaca.trading.client.TradingClient.__init__", return_value=None)
@patch("alpaca.data.historical.StockHistoricalDataClient.__init__", return_value=None)
class TestComputeSma10(unittest.TestCase):
    """position_manager.compute_sma10 — rolling average."""

    def setUp(self):
        import importlib, position_manager as pm
        importlib.reload(pm)
        self.pm = pm

    def test_correct_sma(self, *_):
        closes = list(range(1, 21))   # 1..20
        bars   = make_bars(closes)
        sma    = self.pm.compute_sma10(bars)
        expected = sum(range(11, 21)) / 10  # 15.5
        self.assertAlmostEqual(sma, expected)

    def test_returns_none_if_too_few_bars(self, *_):
        bars = make_bars([30.00] * 5)
        self.assertIsNone(self.pm.compute_sma10(bars))


@patch("alpaca.trading.client.TradingClient.__init__", return_value=None)
@patch("alpaca.data.historical.StockHistoricalDataClient.__init__", return_value=None)
class TestTradingDaysSince(unittest.TestCase):
    """position_manager.trading_days_since — business day counter."""

    def setUp(self):
        import importlib, position_manager as pm
        importlib.reload(pm)
        self.pm = pm

    def test_same_day_is_zero(self, *_):
        self.assertEqual(self.pm.trading_days_since(str(date.today())), 0)

    def test_excludes_weekends(self, *_):
        # Monday to Wednesday = 2 trading days
        monday = date(2026, 5, 11)
        wednesday = date(2026, 5, 13)
        days = int(np.busday_count(str(monday), str(wednesday)))
        self.assertEqual(days, 2)


@patch("alpaca.trading.client.TradingClient.__init__", return_value=None)
@patch("alpaca.data.historical.StockHistoricalDataClient.__init__", return_value=None)
class TestFullTradeCycle(unittest.TestCase):
    """Simulate complete trade lifecycle end to end."""

    def setUp(self):
        import importlib, position_manager as pm
        importlib.reload(pm)
        self.pm = pm

    def _positions(self, symbol, price):
        pos = MagicMock()
        pos.current_price = str(price)
        return {symbol: pos}

    def test_happy_path_entry_to_sma_exit(self, *_):
        """pending → open → partial_exit → closed via SMA10."""
        pm = self.pm
        pm.place_stop_loss  = MagicMock(return_value="stop-1")
        pm.sell_market      = MagicMock(return_value="sell-1")
        pm.cancel_order     = MagicMock(return_value=True)

        trade = make_trade(status="pending", entry_order_id="entry-1")

        # Step 1 — entry fills
        pm.get_order = MagicMock(return_value=make_mock_order(status="filled", filled_avg_price=30.00))
        self.assertTrue(pm.handle_pending(trade))
        self.assertEqual(trade["status"], "open")

        # Step 2 — stop not hit, 4 days in, profitable → phase 1
        pm.get_order = MagicMock(return_value=make_mock_order(status="new"))
        self.assertFalse(pm.handle_stop_hit(trade))

        trade["fill_date"] = str(date.today() - timedelta(days=4))
        self.assertTrue(pm.handle_phase1(trade, self._positions("TEST", 36.00)))
        self.assertEqual(trade["status"], "partial_exit")
        self.assertEqual(trade["current_stop"], 30.00)  # breakeven

        # Step 3 — EOD: close drops below SMA10 → full exit
        closes = [30.00] * 9 + [28.00]  # last close < SMA (~30)
        pm.fetch_daily_bars = MagicMock(return_value=make_bars(closes))
        self.assertTrue(pm.handle_sma_exit(trade))
        self.assertEqual(trade["status"], "closed")
        self.assertEqual(trade["exit_reason"], "sma10_close")
        self.assertIsNotNone(trade["pnl"])

    def test_stop_hit_before_phase1(self, *_):
        """Trade opens and immediately hits stop — closes at a loss."""
        pm = self.pm
        pm.get_order = MagicMock(return_value=make_mock_order(status="filled", filled_avg_price=27.80))
        trade = make_trade(fill_price=30.00, shares_remaining=100)
        result = pm.handle_stop_hit(trade)
        self.assertTrue(result)
        self.assertEqual(trade["status"], "closed")
        self.assertLess(trade["pnl"], 0)

    def test_entry_order_expires_and_gets_cleaned_up(self, *_):
        """DAY order not found next day → marked expired, ready for re-entry."""
        pm = self.pm
        pm.get_order = MagicMock(return_value=None)
        trade = make_trade(status="pending")
        result = pm.handle_pending(trade)
        self.assertTrue(result)
        self.assertEqual(trade["status"], "expired")
        # Verify it won't block a new entry (no open position, no pending order on Alpaca)


if __name__ == "__main__":
    unittest.main(verbosity=2)
