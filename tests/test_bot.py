"""Selbsttests mit erfundenen Kursen. Sie prüfen nur die Programmlogik, sie bewerten die Strategie nicht."""
import tempfile
import unittest
from datetime import date

from paperbot.bot import Bot, Store, parse_args
from paperbot.lighter_api import LighterAPI
from paperbot.paper import PaperAccount, fill_price, funding_cost
from paperbot.strategy import BAR_MS, Bar, NoiseBand, SessionSpec, decide, vwap

SPEC = SessionSpec()
TODAY = date(2026, 9, 29)
BARS_PER_DAY = 13  # 09:30–16:00 in 30-Minuten-Kerzen
MARKETS = [{"market_id": 1, "symbol": "BTC", "size_decimals": 4, "min_quote_amount": "10"},
           {"market_id": 2, "symbol": "ETH", "size_decimals": 4, "min_quote_amount": "10"}]


def day_bars(day, closes, open_=100.0):
    s, _ = SPEC.bounds(day)
    return [Bar(s + i * BAR_MS, open_ if i == 0 else closes[i - 1], 0, 0, c, 1.0, c) for i, c in enumerate(closes)]


def history_bars():
    """15 Vortage, abwechselnd +1 % und -1 %, der letzte ist ein Plustag."""
    days = SPEC.previous_days(TODAY, 15)
    bars = []
    for k, d in enumerate(days):
        up = (len(days) - 1 - k) % 2 == 0
        bars += day_bars(d, [101.0 if up else 99.0] * BARS_PER_DAY)
    return bars


class FakeAPI:
    """Beide Märkte bekommen dieselben Kurse; ETH bleibt heute flach."""

    def __init__(self, today_closes):
        self.bars = {1: history_bars() + day_bars(TODAY, today_closes),
                     2: history_bars() + day_bars(TODAY, [100.0] * BARS_PER_DAY)}
        self.price = {"BTC": 100.0, "ETH": 100.0}
        self.candle_calls = 0

    def candles(self, market_id, start, end):
        self.candle_calls += 1
        return [b for b in self.bars[market_id] if start <= b.t < end]

    def order_book(self, market_id, limit=100):
        p = self.price["BTC" if market_id == 1 else "ETH"]
        return [(p - 0.5, 1000.0)], [(p + 0.5, 1000.0)]

    def markets(self):
        return [{"symbol": s, "mark_price": str(p)} for s, p in self.price.items()]

    def fundings(self, market_id, start, end):
        return []


class StrategyTests(unittest.TestCase):
    def test_session_follows_us_daylight_saving(self):
        self.assertEqual(SPEC.bounds(date(2026, 9, 29))[0] % 86_400_000, 13.5 * 3_600_000)
        self.assertEqual(SPEC.bounds(date(2026, 12, 1))[0] % 86_400_000, 14.5 * 3_600_000)

    def test_decide(self):
        self.assertEqual(decide(0, 103, 102, 99, 101), 1)
        self.assertEqual(decide(1, 103, 102, 99, 102.5), 1)
        self.assertEqual(decide(1, 102.2, 102, 99, 102.5), 0)   # unter VWAP: Stop, kein sofortiger Wiedereinstieg
        self.assertEqual(decide(1, 98, 102, 99, 100), -1)       # Stop und Umkehr
        self.assertEqual(decide(-1, 97, 102, 99, 98), -1)
        self.assertEqual(decide(0, 100, 102, 99, 100), 0)

    def test_bands_use_average_move_and_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = Bot(FakeAPI([100.0] * BARS_PER_DAY), MARKETS, parse_args([]), Store(tmp))
            bot.warmup(SPEC.bounds(TODAY)[0])
            hist = bot.markets["BTC"].history
            self.assertEqual(len(hist), 15)
            upper, lower = NoiseBand().bands(hist, 100.0, 101.0, 30)
            self.assertAlmostEqual(upper, 101.0 * 1.01)
            self.assertAlmostEqual(lower, 100.0 * 0.99)

    def test_second_warmup_uses_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = FakeAPI([100.0] * BARS_PER_DAY)
            Bot(api, MARKETS, parse_args([]), Store(tmp)).warmup(SPEC.bounds(TODAY)[0])
            calls = api.candle_calls
            again = Bot(api, MARKETS, parse_args([]), Store(tmp))
            again.warmup(SPEC.bounds(TODAY)[0])
            self.assertEqual(api.candle_calls, calls)
            self.assertEqual(len(again.markets["BTC"].history), 15)

    def test_vwap(self):
        bars = [Bar(0, 1, 1, 1, 10, 1, 10), Bar(BAR_MS, 1, 1, 1, 20, 3, 60)]
        self.assertAlmostEqual(vwap(bars, 2 * BAR_MS), 17.5)


class PaperTests(unittest.TestCase):
    def test_fill_walks_book_and_adds_slippage(self):
        px, thin = fill_price([(99.0, 5.0)], [(100.0, 1.0), (101.0, 1.0)], 1, 2.0, 10)
        self.assertAlmostEqual(px, 100.5 * 1.001)
        self.assertFalse(thin)

    def test_funding_sign(self):
        f = [(2, 1.0, "long"), (3, 0.5, "short")]
        self.assertAlmostEqual(funding_cost(1, 2.0, f, 0, 10), 2.0 - 1.0)
        self.assertAlmostEqual(funding_cost(-1, 2.0, f, 0, 10), -2.0 + 1.0)

    def test_no_funding_call_within_one_hour(self):
        api = LighterAPI(base_url="http://127.0.0.1:9")   # jeder echte Abruf würde scheitern
        self.assertEqual(api.fundings(1, 3_600_000 + 1, 7_199_999), [])

    def test_pnl(self):
        acc = PaperAccount(1000.0)
        acc.open(-1, 2.0, 100.0, 0, "d")
        t = acc.close(95.0, 1, 0.5, "x")
        self.assertAlmostEqual(t["pnl_usd"], 9.5)
        self.assertAlmostEqual(acc.equity, 1009.5)


class BotFlowTests(unittest.TestCase):
    def _bot(self, today_closes, tmp, **kw):
        api = FakeAPI(today_closes)
        cfg = parse_args([f"--{k.replace('_', '-')}={v}" for k, v in kw.items()])
        bot = Bot(api, MARKETS, cfg, Store(tmp))
        start = SPEC.bounds(TODAY)[0]
        bot.warmup(start - 60_000)
        return bot, api, start

    def test_entry_hold_and_vwap_stop(self):
        # 10:00 über dem Band, 10:30 weiter oben, 11:00 unter dem VWAP
        closes = [103.0, 103.5, 101.0] + [101.0] * 10
        with tempfile.TemporaryDirectory() as tmp:
            bot, api, start = self._bot(closes, tmp, max_daily_loss=0.5)
            btc = bot.markets["BTC"]
            for minute, price in [(30, 103), (60, 103.5), (90, 101)]:
                api.price["BTC"] = price
                bot.step(start + minute * 60_000 + 5_000)
                if minute == 30:
                    self.assertEqual(btc.account.position.side, 1)
                    self.assertIsNone(bot.markets["ETH"].account.position)
                if minute == 60:
                    self.assertIsNotNone(btc.account.position)
            self.assertIsNone(btc.account.position)
            rows = Store(tmp).trades_file.read_text().splitlines()
            self.assertEqual(len(rows), 2)
            self.assertIn("Stop", rows[1])
            self.assertTrue(rows[1].startswith("BTC,"))

    def test_session_end_and_restart_resume(self):
        closes = [103.0] + [104.0] * 12
        with tempfile.TemporaryDirectory() as tmp:
            bot, api, start = self._bot(closes, tmp)
            api.price["BTC"] = 103
            bot.step(start + 30 * 60_000 + 5_000)
            self.assertIsNotNone(bot.markets["BTC"].account.position)
            again, _, _ = self._bot(closes, tmp)       # Neustart liest den Zustand
            again.api = api
            self.assertIsNotNone(again.markets["BTC"].account.position)
            api.price["BTC"] = 104
            again.step(start + 391 * 60_000)
            self.assertIsNone(again.markets["BTC"].account.position)
            self.assertIn("Sessionende", Store(tmp).trades_file.read_text())
            self.assertIn("BTC", Store(tmp).sessions_file.read_text())

    def test_daily_loss_limit_locks_market(self):
        closes = [103.0] + [103.5] * 12
        with tempfile.TemporaryDirectory() as tmp:
            bot, api, start = self._bot(closes, tmp, max_daily_loss=0.01)
            api.price["BTC"] = 103
            bot.step(start + 30 * 60_000 + 5_000)
            api.price["BTC"] = 90
            bot.step(start + 40 * 60_000)
            btc = bot.markets["BTC"]
            self.assertIsNone(btc.account.position)
            self.assertTrue(btc.locked)
            api.price["BTC"] = 103.5
            bot.step(start + 60 * 60_000 + 5_000)
            self.assertIsNone(btc.account.position)


if __name__ == "__main__":
    unittest.main()
