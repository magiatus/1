# Paperbot für Lighter

Papierhandels-Bot für die Perp-Börse [Lighter](https://lighter.xyz). Er nutzt die echten Live-Kurse und das
echte Orderbuch, führt aber **keine echten Orders** aus. Jeder Trade wird nur simuliert und protokolliert.

Er braucht keinen API-Schlüssel und keine Zusatzpakete, nur Python 3.9 oder neuer.

## Strategie

Intraday-Momentum mit Rauschband nach Zarattini, Aziz & Barbon (2024), übertragen auf Lighter:

1. **Session:** US-Handelszeit 09:30–16:00 New York, Montag bis Freitag. Die Umstellung auf Sommer- und
   Winterzeit ist berücksichtigt.
2. **Rauschband:** Sessioneröffnung × (1 ± durchschnittliche Bewegung bis zu dieser Uhrzeit in den letzten
   14 Sessions). Eine Kurslücke seit dem letzten Sessionende erweitert das Band.
3. **Entscheidung** alle 30 Minuten ab 10:00: über dem Band long, unter dem Band short.
4. **Ausstieg:** Long, sobald der Kurs unter max(obere Grenze, VWAP) fällt, Short entsprechend. Spätestens zum
   Sessionende wird glattgestellt.
5. **Größe:** Position so groß, dass sie im Schnitt 2 % Tagesschwankung des Kontos ergibt, höchstens 2× Hebel.
6. **Tagesverlustlimit:** 2 % je Markt. Danach handelt dieser Markt bis zum nächsten Tag nicht mehr.

Jeder Markt hat ein eigenes Papierkonto (Standard 1.000 USD), damit sich die Märkte vergleichen lassen.

### Simulierte Ausführung

- Kauf und Verkauf laufen durch das **aktuelle Live-Orderbuch** (Durchschnittspreis über die Stufen).
- Zusätzlich 0,5 bp Zuschlag pro Ausführung für die 300-ms-Verzögerung des Standard-Kontos.
- Gebühren: 0, wie beim Standard-Konto von Lighter.
- Funding: die stündlichen Zahlungen von Lighter werden für die Haltedauer abgerechnet. Annahme: Das Feld
  `direction` nennt die zahlende Seite.

## Start

```bash
git clone https://github.com/magiatus/1.git paperbot && cd paperbot

# Status prüfen: lädt den Vorlauf und zeigt, wie viele Märkte bereit sind
python3 -m paperbot --once

# Alle Märkte, je 1.000 USD
python3 -m paperbot --symbols all --equity 1000

# Nur bestimmte Märkte
python3 -m paperbot --symbols BTC,ETH,SOL
```

Beim Start lädt der Bot die letzten 15 Sessions als Vorlauf, weil das Rauschband sie braucht. Bei allen Märkten
dauert das wegen des API-Limits (60 Abrufe pro Minute) etwa 12 Minuten. Rückwirkend gehandelt wird nichts.

Alle Einstellungen: `python3 -m paperbot --help`

## Dauerbetrieb auf dem VPS

`deploy/paperbot.service` ist eine systemd-Unit. Pfad und Benutzer darin anpassen, dann:

```bash
sudo cp deploy/paperbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now paperbot
journalctl -u paperbot -f        # Log live ansehen
```

Stoppen mit `systemctl stop paperbot` behält offene Positionen; beim nächsten Start laufen sie weiter.
**Not-Aus:** `touch data/STOP` schließt alle Positionen und beendet den Bot.

## Ergebnisse

Alles liegt im Ordner `data/`:

| Datei | Inhalt |
|---|---|
| `trades.csv` | jeder abgeschlossene Trade mit Ein- und Ausstieg, Funding und Ergebnis |
| `sessions.csv` | Tagesergebnis je Markt |
| `decisions.csv` | jede Entscheidung mit Kurs, Band und VWAP |
| `bot.log` | Protokoll |
| `state.json` | aktueller Stand, wird beim Neustart gelesen |

## Tests

```bash
python3 -m unittest -v
```

Die Tests nutzen erfundene Kurse und prüfen nur die Programmlogik.

## Grenzen

- Papierhandel zeigt Signale und Kosten realistisch, aber nicht, wie sich eigene Orders auf das Orderbuch
  auswirken würden.
- Asiatische Aktien-Perps werden ebenfalls in der US-Session gehandelt, also außerhalb ihrer Heimatbörse.
- Neue Märkte mit weniger als 15 Sessions Historie werden übersprungen, bis genug Daten da sind.
- Keine Anlageberatung.
