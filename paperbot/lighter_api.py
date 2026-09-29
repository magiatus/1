"""Lese-Client für die öffentliche Lighter-API.

Nur öffentliche Endpunkte: keine Schlüssel, keine Orders.
"""
import json
import time
import urllib.parse
import urllib.request

from .strategy import BAR_MS, RESOLUTION, Bar

BASE_URL = "https://mainnet.zklighter.elliot.ai/api/v1"
MAX_CANDLES = 500


class LighterAPI:
    def __init__(self, base_url=BASE_URL, min_interval=1.1, timeout=20, retries=5):
        # Standard-Limit ohne Anmeldung: 60 REST-Anfragen pro Minute und IP.
        self.base_url = base_url
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self._last = 0.0

    def _get(self, path, **params):
        url = f"{self.base_url}/{path}?{urllib.parse.urlencode(params)}"
        err = None
        for attempt in range(self.retries):
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                    data = json.load(resp)
                self._last = time.monotonic()
                if data.get("code", 200) != 200:
                    raise RuntimeError(f"Antwortcode {data.get('code')}: {data.get('message', '')}")
                return data
            except (OSError, ValueError, RuntimeError) as e:
                self._last = time.monotonic()
                err = e
                time.sleep(2 ** attempt)
        raise RuntimeError(f"Lighter-API nicht erreichbar ({url}): {err}")

    def markets(self):
        """Alle aktiven Perp-Märkte mit Stammdaten und aktuellen Preisen."""
        data = self._get("orderBookDetails")
        return [m for m in data.get("order_book_details", [])
                if m["market_type"] == "perp" and m["status"] == "active" and not m.get("is_frozen")]

    def candles(self, market_id, start_ms, end_ms):
        """Alle Kerzen (30 Minuten) mit Startzeit in [start_ms, end_ms), aufsteigend."""
        out = {}
        end = end_ms
        while end > start_ms:
            page_start = max(start_ms, end - MAX_CANDLES * BAR_MS)
            data = self._get(
                "candles", market_id=market_id, resolution=RESOLUTION,
                start_timestamp=page_start, end_timestamp=end, count_back=MAX_CANDLES,
            )
            for c in data.get("c", []):
                if start_ms <= c["t"] < end_ms:
                    out[c["t"]] = Bar(c["t"], c["o"], c["h"], c["l"], c["c"], c.get("v", 0.0), c.get("V", 0.0))
            end = page_start
        return [out[t] for t in sorted(out)]

    def order_book(self, market_id, limit=100):
        """(bids, asks) als Listen von (Preis, Menge), beste Preise zuerst."""
        data = self._get("orderBookOrders", market_id=market_id, limit=limit)
        asks = sorted((float(o["price"]), float(o["remaining_base_amount"])) for o in data.get("asks", []))
        bids = sorted(((float(o["price"]), float(o["remaining_base_amount"])) for o in data.get("bids", [])), reverse=True)
        return bids, asks

    def fundings(self, market_id, start_ms, end_ms):
        """Stündliche Funding-Zahlungen im Zeitraum, als Liste von (Zeit in ms, USD je Coin, zahlende Seite)."""
        data = self._get(
            "fundings", market_id=market_id, resolution="1h",
            start_timestamp=start_ms, end_timestamp=end_ms, count_back=750,
        )
        out = []
        for f in data.get("fundings", []):
            ts = int(f["timestamp"]) * 1000
            if start_ms < ts <= end_ms:
                out.append((ts, float(f["value"]), f["direction"]))
        return out
