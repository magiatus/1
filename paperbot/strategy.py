"""Intraday-Momentum mit Rauschband (nach Zarattini, Aziz & Barbon 2024).

Reine Rechenlogik ohne Netzwerk, damit sie sich mit Testdaten prüfen lässt.
"""
import statistics
from collections import namedtuple
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

# 30-Minuten-Kerzen: Entscheidungen fallen nur alle 30 Minuten, und der VWAP aus
# Quote- und Basisvolumen ist bei gröberen Kerzen rechnerisch derselbe.
BAR_MS = 30 * 60 * 1000
RESOLUTION = "30m"

# t = Startzeit der Kerze in ms, v = Basisvolumen, V = Quote-Volumen
Bar = namedtuple("Bar", "t o h l c v V")


@dataclass
class SessionSpec:
    """Handelszeit, in der der Bot aktiv ist. Standard: US-Aktiensitzung."""
    tz: str = "America/New_York"
    start: str = "09:30"
    end: str = "16:00"
    weekdays_only: bool = True

    def _zone(self):
        return ZoneInfo(self.tz)

    def bounds(self, day):
        z = self._zone()
        s = datetime.combine(day, dtime.fromisoformat(self.start), tzinfo=z)
        e = datetime.combine(day, dtime.fromisoformat(self.end), tzinfo=z)
        return int(s.timestamp() * 1000), int(e.timestamp() * 1000)

    def length_min(self):
        s, e = dtime.fromisoformat(self.start), dtime.fromisoformat(self.end)
        return (e.hour * 60 + e.minute) - (s.hour * 60 + s.minute)

    def day_of(self, ts_ms):
        return datetime.fromtimestamp(ts_ms / 1000, self._zone()).date()

    def is_trading_day(self, day):
        return not self.weekdays_only or day.weekday() < 5

    def previous_days(self, day, n):
        """Die n Handelstage vor `day`, ältester zuerst."""
        out, d = [], day
        while len(out) < n:
            d -= timedelta(days=1)
            if self.is_trading_day(d):
                out.append(d)
        return out[::-1]


@dataclass
class DayData:
    day: date
    start_ms: int
    end_ms: int
    bars: list = field(default_factory=list)

    @property
    def open(self):
        return self.bars[0].o

    @property
    def close(self):
        return self.bars[-1].c


def closed_bars(bars, t_ms):
    return [b for b in bars if b.t + BAR_MS <= t_ms]


def close_at(bars, t_ms):
    """Schlusskurs der letzten Kerze, die bis t_ms abgeschlossen ist."""
    last = None
    for b in bars:
        if b.t + BAR_MS > t_ms:
            break
        last = b
    return None if last is None else last.c


def vwap(bars, t_ms):
    done = closed_bars(bars, t_ms)
    base = sum(b.v for b in done)
    if not done:
        return None
    if base <= 0:
        return done[-1].c
    return sum(b.V for b in done) / base


class NoiseBand:
    def __init__(self, lookback=14, mult=1.0, interval_min=30):
        self.lookback = lookback
        self.mult = mult
        self.interval_min = interval_min

    def checkpoints(self, session_len_min):
        """Minuten nach Sessionbeginn, zu denen entschieden wird (z. B. 10:00, 10:30, ...)."""
        return list(range(self.interval_min, session_len_min, self.interval_min))

    def sigma(self, history, offset_min):
        """Durchschnittliche absolute Bewegung seit Sessionbeginn bis zu dieser Uhrzeit."""
        moves = []
        for d in history[-self.lookback:]:
            if not d.bars:
                continue
            c = close_at(d.bars, d.start_ms + offset_min * 60_000)
            if c is not None:
                moves.append(abs(c / d.open - 1))
        if len(moves) < self.lookback:
            return None
        return statistics.fmean(moves) * self.mult

    def bands(self, history, today_open, prev_close, offset_min):
        """(obere, untere) Grenze; Kurslücken seit dem letzten Sessionende erweitern das Band."""
        s = self.sigma(history, offset_min)
        if s is None:
            return None
        return max(today_open, prev_close) * (1 + s), min(today_open, prev_close) * (1 - s)

    def daily_vol(self, history):
        """Schwankung der Sessionschlusskurse von Tag zu Tag."""
        closes = [d.close for d in history[-(self.lookback + 1):] if d.bars]
        if len(closes) < self.lookback + 1:
            return None
        rets = [b / a - 1 for a, b in zip(closes, closes[1:])]
        return statistics.stdev(rets)


def decide(position, price, upper, lower, vw):
    """Neue Richtung (+1 long, -1 short, 0 flat).

    Long bleibt offen, solange der Kurs über max(obere Grenze, VWAP) liegt,
    Short solange er unter min(untere Grenze, VWAP) liegt. Nach einem Stop
    geht es im selben Schritt nur in die Gegenrichtung, nie zurück in dieselbe.
    """
    if position > 0:
        if price >= max(upper, vw):
            return 1
        return -1 if price < lower else 0
    if position < 0:
        if price <= min(lower, vw):
            return -1
        return 1 if price > upper else 0
    if price > upper:
        return 1
    if price < lower:
        return -1
    return 0


def position_notional(equity, daily_vol, target_vol, max_leverage):
    if not daily_vol or daily_vol <= 0:
        return 0.0
    return equity * min(max_leverage, target_vol / daily_vol)
