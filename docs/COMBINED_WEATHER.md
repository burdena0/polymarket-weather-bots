# Run both weather strategies through one executor

## Available-cash sizing update

The updated coordinator removes the two-position count limit. Each entry is sized
to the smaller of $5 and freshly reported USD buying power/current balance above
the $40 reserve. Quantity can shrink below five shares if the market minimum
allows it. Actual preview fees must fit; one station/date claim still applies.
For the old budget mode the remaining cumulative budget is also checked.

To remove the old $10 cumulative cap, stop the coordinator with Ctrl+C, then run:

```powershell
cd 'C:\path\to\polymarket-weather-bots'
New-Item -ItemType File -Force '.\data\us-weather-combined-v1\STOP' | Out-Null
.\.venv-paper\Scripts\python.exe -m supahtrade.combined_weather --upgrade-cash-sizing
```

Only after `upgraded` or `already_upgraded`, remove that STOP and restart using
the existing `--live` command below. The offline upgrade backs up the complete
order database, preserves positions and historical debits, and refuses pending
orders or an active account lock. Cash sizing removes the former $10 aggregate
loss constraint: losses can accumulate beyond $10, and additional deposits can
fund further entries. The $40 reserve is checked before each order; it does not
prevent losses on already-held positions. The internal ledger uses a large integer
sentinel for cumulative accounting, not a claim of actual account funds.

The dashboard performance review groups closed-position realized results by
strategy/station. Open positions and rejected orders are separate. It reports
outcomes without automatically adjusting the uncalibrated model; profitability
improvement has not been demonstrated.

The two-position references in the original setup description below describe the
previous policy; the available-cash update above supersedes them.

This coordinator runs the reference copier and independent intraday v2 strategy
sequentially. Both may submit orders, but share one account execution lock,
one order journal, a $10 cumulative debit budget, a $5 per-order cap, the existing
$40 account cash reserve checks, and a two-position limit. Existing spending is
carried forward. Proceeds do not reset the cumulative budget.

A station/date can be claimed only once across both strategies, including
different brackets and opposite outcomes. Claims survive rejected submissions,
process crashes, and restarts. An uncertain submission blocks subsequent entries.
The reference copier can sell only positions it owns. Independent positions keep
their corroborated-settlement exit policy. The copier preserves its existing
explicit CLI-basis policy; this does not establish identical settlement rules
between the reference venue and US markets.

The coordinator preserves the existing lock. Do not run the old standalone bots
alongside it. This is tested software, not evidence of improved profitability.

## First-time migration on this PC

In the existing trading terminal press Ctrl+C and wait for the PowerShell prompt.
Then run this block. It stops the old roots but does not cancel existing orders
or sell positions. Migration refuses an active process, pending orders, a halted
budget, or any orders in the old copier journal (merging a nonempty second ledger
is deliberately not supported).

```powershell
cd 'C:\path\to\polymarket-weather-bots'
New-Item -ItemType File -Force '.\data\us-weather-independent-user-live-v1\STOP' | Out-Null
New-Item -ItemType File -Force '.\data\us-weather-live-v1\STOP' | Out-Null
.\.venv-paper\Scripts\python.exe -m supahtrade.combined_weather --prepare
```

Continue only after `status: prepared`. A lock error means an old supervisor
still owns its lock: wait for its current request to finish or exit it with Ctrl+C,
then rerun preparation. Never delete lock files. Other failures need diagnosis;
do not delete a partial target or reset journals to force migration.

Preparation uses SQLite backups including WAL contents. Original journals stay
in place, with STOP and a migration marker that prevents their old launchers from
trading. The new root is `data/us-weather-combined-v1`. It retains all orders,
receipts, settlement accounting, independent signal history, and station/date
claims. It starts with STOP and does not trade. A completed migration is not
repeated on subsequent startups.

## Start both strategies (real order submission)

```powershell
cd 'C:\path\to\polymarket-weather-bots'
Remove-Item -LiteralPath '.\data\us-weather-combined-v1\STOP' -ErrorAction SilentlyContinue
.\.venv-paper\Scripts\python.exe -m supahtrade.combined_weather --live
```

Keep that terminal open. The scheduler end date remains September 17, 2026;
this command does not extend it. Both strategies execute within one process, so
they scan sequentially, with a 15-second pause after both cycles. Source requests
add latency. Existing two-position holdings can prevent further entries.

The updated dashboard automatically selects the completed default combined root.
Open http://127.0.0.1:8090/bots. If the dashboard was started before this update,
restart only that server once; do not start a duplicate dashboard instance.
Strategy heartbeats are separate, but portfolio totals refer to the SAME shared
account. Do not add them together. No new credentials are required on this PC.

## Stop

Press Ctrl+C in the coordinator terminal, or in another terminal run:

```powershell
cd 'C:\path\to\polymarket-weather-bots'
New-Item -ItemType File -Force '.\data\us-weather-combined-v1\STOP' | Out-Null
```

STOP blocks new submissions after in-progress requests complete. It does not
cancel outstanding orders, sell holdings, or erase uncertainty. Reuse the combined
root on every restart. Never revert to an old journal containing a prior balance.
