# Polymarket weather bots

Standalone extraction of the Polymarket weather components: independent NWS
intraday estimates, public reference-wallet signals, one shared executor,
durable order accounting, available-cash sizing, and a local dashboard.
Execution targets **Polymarket US**. Public Polymarket.com data is used for
reference research; international account execution is not implemented.

No account keys, personal account data, trading journals, or original experiment
artifacts are included. No Robinhood integration, memecoin bot, or GPTHeist app
is included. A small compatibility module named `robinhood_read.py` contains only
the generic HTTP redirect blocker used by the US client, not broker integration.

This software and its probability model are experimental. Tests use synthetic
broker responses; passing tests does not establish profitability or live reliability.

## Install on Windows

From this repository's directory:

```powershell
py -3.10 -m venv .venv-paper
.\.venv-paper\Scripts\python.exe -m pip install -e .
.\.venv-paper\Scripts\python.exe -m unittest discover -s tests -q
.\.venv-paper\Scripts\python.exe -m supahtrade.polymarket_dashboard
```

Open http://127.0.0.1:8090/bots. The dashboard is read-only and does not activate
trading. Use an available Python 3.10+ interpreter if `py -3.10` is unavailable.
Run from the checkout, since the application reads the local `routines` directory.

## Accounts and journals

In another PowerShell tab, from this directory:

```powershell
.\.venv-paper\Scripts\python.exe -m supahtrade.polymarket_setup --enroll
.\.venv-paper\Scripts\python.exe -m supahtrade.weather_preflight
```

Enrollment uses hidden terminal input, verifies account reads, and stores keys
with Windows user encryption. Never paste keys into source files or GitHub.

For a **new experiment only**, initialize the empty journal:

```powershell
.\.venv-paper\Scripts\python.exe -m supahtrade.polymarket_setup --initialize
```

Initialization preserves STOP and the original $10 cumulative budget. The code
sizes entries based on cash and remaining budget without a fixed position count.
To explicitly remove the cumulative cap, use the backed-up offline cash-sizing
upgrade in [the operator guide](docs/COMBINED_WEATHER.md).

**Existing users must preserve their current combined journal.** Do not initialize
a fresh journal to replace existing positions, reset spending, or escape an unknown
order. Journal transfer is deliberately not done through Git. Stop the old process
before moving journals; SQLite WAL contents must be included using a proper backup.
Enroll credentials separately on a different Windows account or PC. Never run the
same account from two computers: the execution lock is local to one computer.

## Explicit live startup

Only after initialization/migration and account checks:

```powershell
Remove-Item -LiteralPath '.\data\us-weather-combined-v1\STOP' -ErrorAction SilentlyContinue
.\.venv-paper\Scripts\python.exe -m supahtrade.combined_weather --live
```

This submits real orders. Both strategies share one lock and ledger. The $40
cash reserve, $5 per-order cap, market minimums, and one station/date claim remain.
The cash-sizing upgrade permits cumulative losses beyond $10. Pending/unknown
orders prevent further entries. No automatic reset or resend is provided.

The schedule retains the original **September 17, 2026** end time in
`routines/schedule.json`; startup does not silently extend the experiment.

Stop with Ctrl+C or create `data/us-weather-combined-v1/STOP`. Stopping does not
cancel outstanding orders or liquidate positions.

## Held-position forecast review

The independent strategy reviews one holding each cycle, oldest review first.
It compares the current intraday estimate against a matching recorded entry
baseline. A fall of at least 10 percentage points in the lower sensitivity estimate,
or a 2°F forecast-high change, flags review. These are review thresholds, not
calibrated stop-loss rules. As the day progresses, the remaining forecast high can
change naturally. Missing/stale data is reported as unavailable, not a price signal.

The panel is informational: it never submits exits. Independent positions still
hold through corroborated settlement; reference-owned positions can follow reference
sell signals. Old entries without a matching model baseline display current estimates
without inventing a change from entry.

Closed-position feedback groups realized wins/losses by strategy and station;
rejected orders and open holdings are separate. It does not automatically retrain
or promote a model. Out-of-sample validation is still required.

See [combined operation](docs/COMBINED_WEATHER.md) and
[intraday assumptions](docs/INTRADAY_WEATHER.md).
