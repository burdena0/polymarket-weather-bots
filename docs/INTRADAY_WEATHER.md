# Same-day NWS model v2

The opt-in `--intraday` mode combines qualified station readings already observed with hourly forecasts for the rest of the CLI day. It also handles complete future CLI days. It does not need reference-wallet trades.

CLI days run midnight to midnight local **standard** time year-round. The parser requires each elapsed hour to have an accepted station reading, a reading within 90 minutes, and every remaining hourly forecast including the current hour. Read receipts expire after five minutes; forecast revisions after six hours. Missing, wrong-station, conflicting, nonfinite, future-dated and truncated observations are rejected. Observation pages are bounded to four and recorded as public receipts.

Public readings may miss the eventual official daily maximum. The experimental distribution therefore models the maximum of two uncertain components: the sampled past maximum (normal sigma 1F, missing-peak shift 0/2F) and remaining forecast maximum (bias -1/0/+1F, sigma 2/4F). Component independence and error parameters are assumptions. The resulting sensitivity range is not calibrated, a confidence interval, or a settlement guarantee. Existing fee, edge, position and budget checks remain in force.

## Observe and inspect

```powershell
cd 'C:\path\to\polymarket-weather-bots'
.\.venv-paper\Scripts\python.exe -m supahtrade.independent_weather_supervisor --root data/us-weather-intraday-observer-v2 --intraday --once
```

Omit `--once` to keep observing. This command has no live order permission. Status and preserved cycles are in that new root. The existing v1 live experiment is not upgraded by these commands. A different strategy cannot open its v1 journal; do not reset or abandon existing accounting to switch live modes.

## Upgrade an existing v1 account journal

Create its STOP file and wait for the supervisor to exit, then run `python -m supahtrade.weather_upgrade --root data/us-weather-independent-user-live-v1`. The migration requires no unresolved submissions, makes SQLite backups including WAL contents, preserves the original mode and an upgrade receipt, and changes only the strategy configuration. All orders, recovery records, cumulative budget and station-day claims stay in place. Repeating the upgrade is harmless. It leaves STOP in place and does not start trading.

After migration, the user can remove that STOP file and restart the same root with both `--intraday` and `--live`. Never open a fresh live root to discard earlier account spending. The dashboard already pointed at that root will show v2 after its next completed cycle.

Sources: [NWS CLI day and observations FAQ](https://www.weather.gov/lot/weather_observations_faq), [LAX temperature reporting](https://preview.weather.gov/lox/asostemperature), [NWS API](https://www.weather.gov/documentation/services-web-api).
