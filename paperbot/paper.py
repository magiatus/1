"""Simuliertes Konto: Ausführung gegen das echte Live-Orderbuch, aber ohne echte Orders."""
from dataclasses import asdict, dataclass


@dataclass
class Position:
    side: int          # +1 long, -1 short
    qty: float         # in Coins
    entry_price: float
    entry_ms: int
    day: str           # Sessiontag (ISO-Datum)


def walk_book(levels, qty):
    """Durchschnittspreis, wenn qty gegen die Orderbuchstufen ausgeführt wird; None, wenn das Buch nicht reicht."""
    left, cost = qty, 0.0
    for price, size in levels:
        take = min(size, left)
        cost += take * price
        left -= take
        if left <= 1e-12:
            return cost / qty
    return None


def fill_price(bids, asks, side, qty, slippage_bps):
    """Kauf läuft durch die Asks, Verkauf durch die Bids; dazu Zuschlag für die 300-ms-Verzögerung."""
    levels = asks if side > 0 else bids
    if not levels:
        raise ValueError("Orderbuch ist leer")
    px = walk_book(levels, qty)
    thin = px is None
    if thin:
        px = levels[-1][0]
    return px * (1 + side * slippage_bps / 1e4), thin


def funding_cost(side, qty, fundings, entry_ms, exit_ms):
    """Funding in USD, das die Position zahlt (negativ = erhalten). Zahlende Seite laut Lighter: 'long' oder 'short'."""
    cost = 0.0
    for ts, value, payer in fundings:
        if entry_ms < ts <= exit_ms:
            ours = "long" if side > 0 else "short"
            cost += value * qty if payer == ours else -value * qty
    return cost


class PaperAccount:
    def __init__(self, equity, position=None):
        self.equity = equity
        self.position = position

    def open(self, side, qty, price, ts_ms, day):
        if self.position is not None:
            raise RuntimeError("Es ist schon eine Position offen")
        self.position = Position(side, qty, price, ts_ms, day)
        return self.position

    def close(self, price, ts_ms, funding_usd, reason):
        p = self.position
        if p is None:
            raise RuntimeError("Keine offene Position")
        gross = p.side * p.qty * (price - p.entry_price)
        pnl = gross - funding_usd
        self.equity += pnl
        self.position = None
        notional = p.qty * p.entry_price
        return {
            "day": p.day,
            "side": "long" if p.side > 0 else "short",
            "entry_ms": p.entry_ms,
            "entry_price": round(p.entry_price, 2),
            "exit_ms": ts_ms,
            "exit_price": round(price, 2),
            "qty": round(p.qty, 6),
            "notional_usd": round(notional, 2),
            "funding_usd": round(funding_usd, 4),
            "pnl_usd": round(pnl, 4),
            "pnl_bps": round(pnl / notional * 1e4, 2) if notional else 0.0,
            "reason": reason,
            "equity_after": round(self.equity, 2),
        }

    def unrealized(self, mark):
        p = self.position
        return 0.0 if p is None else p.side * p.qty * (mark - p.entry_price)

    def to_dict(self):
        return {"equity": self.equity, "position": asdict(self.position) if self.position else None}

    @classmethod
    def from_dict(cls, d):
        pos = Position(**d["position"]) if d.get("position") else None
        return cls(d["equity"], pos)
