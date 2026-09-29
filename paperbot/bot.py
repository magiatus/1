"""Papierhandels-Bot: Live-Daten von Lighter, simulierte Ausführung, keine echten Orders.

Jeder Markt hat ein eigenes Papierkonto, damit sich die Märkte vergleichen lassen.
"""
import argparse
import csv
import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .lighter_api import LighterAPI
from .paper import PaperAccount, fill_price, funding_cost
from .strategy import (BAR_MS, DayData, NoiseBand, SessionSpec, closed_bars,
                       decide, position_notional, vwap)

TRADE_FIELDS = ["symbol", "day", "side", "entry_time", "entry_price", "exit_time", "exit_price", "qty",
                "notional_usd", "funding_usd", "pnl_usd", "pnl_bps", "reason", "equity_after",
                "entry_ms", "exit_ms"]
SESSION_FIELDS = ["day", "symbol", "entries", "pnl_usd", "equity", "loss_limit_hit"]
DECISION_FIELDS = ["day", "time", "symbol", "price", "lower", "upper", "vwap", "before", "after"]
LABEL = {1: "LONG", 0: "flat", -1: "SHORT"}


def now_ms():
    return int(time.time() * 1000)


def fmt_utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Store:
    """Zustand, Trades, Entscheidungen, Tagesbilanz und Log im Datenordner."""

    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.dir / "state.json"
        self.trades_file = self.dir / "trades.csv"
        self.sessions_file = self.dir / "sessions.csv"
        self.decisions_file = self.dir / "decisions.csv"
        self.log_file = self.dir / "bot.log"

    def log(self, msg):
        line = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC | {msg}"
        print(line, flush=True)
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def load_state(self):
        if not self.state_file.exists():
            return None
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def save_state(self, state):
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def _append(self, path, fields, row):
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)

    def append_trade(self, row):
        self._append(self.trades_file, TRADE_FIELDS, row)

    def append_session(self, row):
        self._append(self.sessions_file, SESSION_FIELDS, row)

    def append_decision(self, row):
        self._append(self.decisions_file, DECISION_FIELDS, row)

    def stop_requested(self):
        return (self.dir / "STOP").exists()


class Market:
    """Ein Markt mit eigener Historie, eigenem Papierkonto und Tageszustand."""

    def __init__(self, info, equity):
        self.id = info["market_id"]
        self.symbol = info["symbol"]
        self.min_qty = float(info.get("min_base_amount", 0) or 0)
        self.min_quote = float(info.get("min_quote_amount", 0) or 0)
        self.size_decimals = int(info.get("size_decimals", 5))
        self.history = []
        self.account = PaperAccount(equity)
        self.start_equity = equity
        self.locked = False
        self.entries = 0

    def to_dict(self):
        return {"account": self.account.to_dict(), "start_equity": self.start_equity,
                "locked": self.locked, "entries": self.entries}

    def load(self, d):
        self.account = PaperAccount.from_dict(d["account"])
        self.start_equity = d.get("start_equity", self.account.equity)
        self.locked = d.get("locked", False)
        self.entries = d.get("entries", 0)


class Bot:
    def __init__(self, api, markets, cfg, store):
        self.api = api
        self.cfg = cfg
        self.store = store
        self.spec = SessionSpec(cfg.session_tz, cfg.session_start, cfg.session_end, not cfg.all_days)
        self.model = NoiseBand(cfg.lookback, cfg.band_mult, cfg.interval)
        self.checkpoints = self.model.checkpoints(self.spec.length_min())
        self.markets = {m["symbol"]: Market(m, cfg.equity) for m in markets}
        self.day = None   # {"day": ISO-Datum, "done": [Minuten], "finished": bool}

        state = store.load_state()
        if state:
            self.day = state.get("day")
            for sym, d in state.get("markets", {}).items():
                if sym in self.markets:
                    self.markets[sym].load(d)

    # --- Hilfen -----------------------------------------------------------

    def _log(self, msg):
        self.store.log(msg)

    def _save(self):
        self.store.save_state({
            "updated_utc": fmt_utc(now_ms()),
            "day": self.day,
            "markets": {s: m.to_dict() for s, m in self.markets.items()},
        })

    def _local(self, ms):
        return datetime.fromtimestamp(ms / 1000, ZoneInfo(self.spec.tz)).strftime("%H:%M")

    def _open_positions(self):
        return [m for m in self.markets.values() if m.account.position]

    def ready(self, m):
        return len(m.history) >= self.model.lookback + 1

    # --- Vorlauf ----------------------------------------------------------

    def warmup(self, now):
        """Lädt für jeden Markt die letzten abgeschlossenen Sessions, die das Rauschband braucht."""
        today = self.spec.day_of(now)
        days = self.spec.previous_days(today, self.model.lookback + 1)
        if self.spec.is_trading_day(today) and now >= self.spec.bounds(today)[1]:
            days = days[1:] + [today]
        start = self.spec.bounds(days[0])[0]
        end = self.spec.bounds(days[-1])[1]
        self._log(f"Vorlauf für {len(self.markets)} Märkte: Sessions {days[0]} bis {days[-1]}")
        for i, m in enumerate(self.markets.values(), 1):
            try:
                bars = self.api.candles(m.id, start, end)
            except RuntimeError as e:
                self._log(f"{m.symbol}: Vorlauf fehlgeschlagen ({e})")
                continue
            m.history = []
            for d in days:
                s, e = self.spec.bounds(d)
                day_bars = [b for b in bars if s <= b.t < e]
                if day_bars:
                    m.history.append(DayData(d, s, e, day_bars))
            if i % 25 == 0 or i == len(self.markets):
                self._log(f"Vorlauf: {i}/{len(self.markets)} Märkte geladen")
        ready = [m for m in self.markets.values() if self.ready(m)]
        self._log(f"Vorlauf fertig: {len(ready)} von {len(self.markets)} Märkten haben genug Historie "
                  f"({self.model.lookback + 1} Sessions). Die übrigen werden übersprungen, bis genug Daten da sind.")

    # --- Ablauf -----------------------------------------------------------

    def step(self, now):
        day = self.spec.day_of(now)
        if self.day is None or self.day["day"] != day.isoformat():
            self._begin_day(day, now)
        if not self.spec.is_trading_day(day):
            self._save()
            return
        start, end = self.spec.bounds(day)
        if now < start:
            self._save()
            return
        if now >= end:
            for m in self._open_positions():
                self._close(m, now, "Sessionende")
            if not self.day["finished"]:
                self._finish_day(day, start, end)
            self._save()
            return

        self._check_loss_limits(now)

        elapsed = (now - start) // 60_000
        due = [c for c in self.checkpoints if c <= elapsed and c not in self.day["done"]]
        if due:
            cp = max(due)
            for skipped in due[:-1]:
                self.day["done"].append(skipped)
                self._log(f"Entscheidung um {self._local(start + skipped * 60_000)} verpasst (Bot lief nicht).")
            cp_ms = start + cp * 60_000
            if now >= cp_ms + 3_000:
                self._checkpoint(day, start, cp, cp_ms)
                self.day["done"].append(cp)
        self._save()

    def _begin_day(self, day, now):
        for m in self._open_positions():
            if m.account.position.day != day.isoformat():
                self._close(m, now, "Position aus Vortag geschlossen")
        for m in self.markets.values():
            m.start_equity = m.account.equity
            m.locked = False
            m.entries = 0
        self.day = {"day": day.isoformat(), "done": [], "finished": False}
        if self.spec.is_trading_day(day):
            s, e = self.spec.bounds(day)
            self._log(f"Handelstag {day}: Session {fmt_utc(s)[11:16]}–{fmt_utc(e)[11:16]} UTC "
                      f"({self.spec.start}–{self.spec.end} {self.spec.tz})")

    def _check_loss_limits(self, now):
        positions = [m for m in self._open_positions() if not m.locked]
        if not positions:
            return
        marks = {m["symbol"]: float(m["mark_price"]) for m in self.api.markets()}
        for m in positions:
            mark = marks.get(m.symbol)
            if mark is None:
                continue
            if m.account.equity + m.account.unrealized(mark) <= m.start_equity * (1 - self.cfg.max_daily_loss):
                self._close(m, now, "Tagesverlustlimit")
                m.locked = True

    def _checkpoint(self, day, start, cp, cp_ms):
        # Märkte mit offener Position zuerst, damit Stops möglichst nah am Signal ausgeführt werden.
        order = sorted((m for m in self.markets.values() if self.ready(m) and not m.locked),
                       key=lambda m: m.account.position is None)
        counts = {"checked": 0, "entries": 0, "exits": 0, "missing": 0}
        began = time.monotonic()
        for m in order:
            try:
                bars = closed_bars(self.api.candles(m.id, start, cp_ms), cp_ms)
                if not bars or bars[-1].t + BAR_MS != cp_ms:
                    counts["missing"] += 1
                    continue
                self._decide(m, day, bars, cp, cp_ms, counts)
            except (RuntimeError, ValueError, IndexError) as e:
                self._log(f"{m.symbol}: Fehler bei der Entscheidung ({e})")
            counts["checked"] += 1
        self._log(f"Entscheidung {self._local(cp_ms)}: {counts['checked']} Märkte geprüft, "
                  f"{counts['entries']} Einstiege, {counts['exits']} Ausstiege, {counts['missing']} ohne Kerze, "
                  f"{len(self._open_positions())} offene Positionen ({time.monotonic() - began:.0f} s)")

    def _decide(self, m, day, bars, cp, cp_ms, counts):
        bands = self.model.bands(m.history, bars[0].o, m.history[-1].close, cp)
        if bands is None:
            return
        upper, lower = bands
        price = bars[-1].c
        vw = vwap(bars, cp_ms)
        pos = m.account.position.side if m.account.position else 0
        target = decide(pos, price, upper, lower, vw)
        self.store.append_decision({"day": day.isoformat(), "time": self._local(cp_ms), "symbol": m.symbol,
                                    "price": price, "lower": round(lower, 8), "upper": round(upper, 8),
                                    "vwap": round(vw, 8), "before": LABEL[pos], "after": LABEL[target]})
        if target == pos:
            return
        book = self.api.order_book(m.id)
        if pos != 0:
            reason = "Stop: unter max(Band, VWAP)" if pos > 0 else "Stop: über min(Band, VWAP)"
            self._close(m, now_ms(), reason, book)
            counts["exits"] += 1
        if target != 0 and self._open(m, target, now_ms(), day, book):
            counts["entries"] += 1

    def _open(self, m, side, now, day, book):
        bids, asks = book
        if not bids or not asks:
            self._log(f"{m.symbol}: Orderbuch leer, kein Einstieg.")
            return False
        dv = self.model.daily_vol(m.history)
        mark = (bids[0][0] + asks[0][0]) / 2
        notional = position_notional(m.account.equity, dv, self.cfg.target_vol, self.cfg.max_leverage)
        qty = round(notional / mark, m.size_decimals)
        if qty <= 0 or qty < m.min_qty or qty * mark < m.min_quote:
            self._log(f"{m.symbol}: Positionsgröße {qty} unter der Mindestgröße, kein Einstieg.")
            return False
        px, thin = fill_price(bids, asks, side, qty, self.cfg.slippage_bps)
        m.account.open(side, qty, px, now, day.isoformat())
        m.entries += 1
        self._log(f"{m.symbol}: EINSTIEG {LABEL[side]} {qty} @ {px:,.6g} (≈ {qty * px:,.0f} USD, "
                  f"Tagesschwankung {dv:.2%}, Spread {(asks[0][0] - bids[0][0]) / mark * 1e4:.1f} bp"
                  f"{', Buch zu dünn' if thin else ''})")
        return True

    def _close(self, m, now, reason, book=None):
        p = m.account.position
        bids, asks = book if book else self.api.order_book(m.id)
        if not (bids if p.side > 0 else asks):
            self._log(f"{m.symbol}: Orderbuch leer, Ausstieg zum letzten Einstiegspreis gebucht.")
            px, thin = p.entry_price, True
        else:
            px, thin = fill_price(bids, asks, -p.side, p.qty, self.cfg.slippage_bps)
        try:
            fund = funding_cost(p.side, p.qty, self.api.fundings(m.id, p.entry_ms, now), p.entry_ms, now)
        except Exception as e:  # Funding darf den Ausstieg nicht verhindern
            fund = 0.0
            self._log(f"{m.symbol}: Funding nicht abrufbar, mit 0 gerechnet ({e})")
        trade = m.account.close(px, now, fund, reason)
        trade["symbol"] = m.symbol
        trade["entry_time"] = fmt_utc(trade["entry_ms"])
        trade["exit_time"] = fmt_utc(trade["exit_ms"])
        self.store.append_trade(trade)
        self._log(f"{m.symbol}: AUSSTIEG {trade['side']} @ {px:,.6g} ({reason}) | {trade['pnl_usd']:+,.2f} USD "
                  f"({trade['pnl_bps']:+.1f} bp, Funding {fund:+.2f}){', Buch zu dünn' if thin else ''}")

    def _finish_day(self, day, start, end):
        total = 0.0
        for m in self.markets.values():
            try:
                bars = self.api.candles(m.id, start, end)
            except RuntimeError as e:
                self._log(f"{m.symbol}: Tageskerzen nicht abrufbar ({e})")
                bars = []
            if bars and (not m.history or m.history[-1].day != day):
                m.history.append(DayData(day, start, end, bars))
                m.history = m.history[-(self.model.lookback + 1):]
            pnl = m.account.equity - m.start_equity
            total += pnl
            if m.entries or pnl:
                self.store.append_session({"day": day.isoformat(), "symbol": m.symbol, "entries": m.entries,
                                           "pnl_usd": round(pnl, 4), "equity": round(m.account.equity, 2),
                                           "loss_limit_hit": m.locked})
        self.day["finished"] = True
        traded = sum(1 for m in self.markets.values() if m.entries)
        self._log(f"Session {day} beendet: {traded} Märkte gehandelt, Ergebnis aller Märkte {total:+,.2f} USD")

    # --- Anzeige und Schleife --------------------------------------------

    def status(self, now):
        day = self.spec.day_of(now)
        s, e = self.spec.bounds(day)
        ready = sum(1 for m in self.markets.values() if self.ready(m))
        self._log(f"Heute {day}: Session {fmt_utc(s)[11:16]}–{fmt_utc(e)[11:16]} UTC | "
                  f"Handelstag: {'ja' if self.spec.is_trading_day(day) else 'nein'} | "
                  f"{ready}/{len(self.markets)} Märkte bereit | {len(self._open_positions())} offene Positionen")

    def run(self):
        stop = {"flag": False}

        def on_signal(signum, frame):
            stop["flag"] = True

        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        c = self.cfg
        self._log(f"Start Papierhandel: {len(self.markets)} Märkte, je {c.equity:,.0f} USD, Ziel-Schwankung "
                  f"{c.target_vol:.1%}, Hebel max. {c.max_leverage}, Tageslimit {c.max_daily_loss:.1%}, "
                  f"Slippage {c.slippage_bps} bp")
        while not stop["flag"]:
            if self.store.stop_requested():
                for m in self._open_positions():
                    self._close(m, now_ms(), "Not-Aus (STOP-Datei)")
                self._save()
                self._log("Not-Aus: alle Positionen geschlossen, Bot beendet.")
                return
            try:
                self.step(now_ms())
            except Exception as e:  # ein Netzwerkfehler soll den Bot nicht beenden
                self._log(f"FEHLER: {e!r}")
            wake = time.monotonic() + c.poll
            while not stop["flag"] and time.monotonic() < wake:
                time.sleep(min(1.0, wake - time.monotonic()))
        self._save()
        self._log("Beendet. Offene Positionen bleiben gespeichert und laufen beim nächsten Start weiter.")


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="paperbot",
        description="Papierhandel auf Lighter: Intraday-Momentum mit Rauschband. Keine echten Orders.")
    p.add_argument("--symbols", default="all", help="Kommagetrennte Liste, z. B. BTC,ETH, oder 'all'")
    p.add_argument("--equity", type=float, default=1_000.0, help="Startkapital je Markt in USD")
    p.add_argument("--target-vol", type=float, default=0.02, help="Ziel-Tagesschwankung der Position, 0.02 = 2 Prozent")
    p.add_argument("--max-leverage", type=float, default=2.0, help="höchster Hebel je Markt")
    p.add_argument("--max-daily-loss", type=float, default=0.02, help="Tagesverlustlimit je Markt als Anteil des Kontos")
    p.add_argument("--slippage-bps", type=float, default=0.5, help="Zuschlag je Ausführung in Basispunkten")
    p.add_argument("--lookback", type=int, default=14, help="Sessions für das Rauschband")
    p.add_argument("--band-mult", type=float, default=1.0, help="Breite des Rauschbands")
    p.add_argument("--interval", type=int, default=30, help="Minuten zwischen zwei Entscheidungen")
    p.add_argument("--session-tz", default="America/New_York")
    p.add_argument("--session-start", default="09:30")
    p.add_argument("--session-end", default="16:00")
    p.add_argument("--all-days", action="store_true", help="auch Samstag und Sonntag handeln")
    p.add_argument("--data-dir", default="data", help="Ordner für Zustand, Trades und Log")
    p.add_argument("--poll", type=float, default=30.0, help="Sekunden zwischen zwei Prüfungen")
    p.add_argument("--once", action="store_true", help="Vorlauf laden, Status zeigen, beenden")
    return p.parse_args(argv)


def select_markets(markets, symbols):
    if symbols.strip().lower() == "all":
        return markets
    wanted = [s.strip() for s in symbols.split(",") if s.strip()]
    by_symbol = {m["symbol"]: m for m in markets}
    missing = [s for s in wanted if s not in by_symbol]
    if missing:
        raise SystemExit(f"Nicht als aktiver Perp-Markt bei Lighter gefunden: {', '.join(missing)}")
    return [by_symbol[s] for s in wanted]


def main(argv=None):
    cfg = parse_args(argv)
    store = Store(cfg.data_dir)
    api = LighterAPI()
    bot = Bot(api, select_markets(api.markets(), cfg.symbols), cfg, store)
    bot.warmup(now_ms())
    if cfg.once:
        bot.status(now_ms())
        return 0
    bot.run()
    return 0
