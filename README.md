# UK Soft-Book Value Scanner

Read-only scanner for finding potential value bets at UK soft bookmakers by
comparing their prices with sharper reference books and exchanges.

It does not place bets.

## Strategy

The scanner:

1. Fetches odds from The Odds API.
2. Treats UK soft books as target bookmakers.
3. Treats sharp books and exchanges as reference prices.
4. De-vigs the reference market into an estimated fair probability.
5. Flags a bet when:

```text
edge = target_odds * reference_probability - 1
```

The CSV tells you what to bet in `bet_to_place`, including decimal and UK
fractional odds.

## Included Books

Target UK soft books:

```text
bet365
betfred
betvictor
betway
boylesports
coral
grosvenor
ladbrokes
livescorebet
paddypower
skybet
sport888 / 888sport
unibet_uk
virginbet
williamhill
```

Sharp reference books/exchanges:

```text
pinnacle
betfair
smarkets
matchbook
```

## Quick Start

Create a `.env` file:

```bash
THE_ODDS_API_KEY=...
```

Install locally:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Run the default UK soft-book value scan:

```bash
scan-exchanges \
  --min-edge 0.02 \
  --min-reference-books 2 \
  --max-age-seconds 900 \
  --unique-events \
  --resolve-event-pages
```

The default scan uses:

```text
--sports-profile uk-soft-edge-core
--regions uk,eu
--markets h2h
--max-event-days 2
```

For paper staking, pass a bankroll:

```bash
scan-exchanges \
  --min-edge 0.02 \
  --min-reference-books 2 \
  --max-age-seconds 900 \
  --unique-events \
  --resolve-event-pages \
  --bankroll 1000
```

Stake sizing uses fractional Kelly, capped by bankroll percentage and max stake.

## Output

Important CSV columns:

- `bet_to_place`: plain-English action, e.g. `Back Team with Book at 4.5 (7/2)+`.
- `target_odds`: decimal odds from the target bookmaker.
- `target_odds_fractional`: UK fractional odds.
- `target_odds_american`: American odds.
- `target_implied_probability`: implied probability from the offered price.
- `reference_fair_odds`: de-vigged fair decimal odds from sharp references.
- `edge`: estimated value edge.
- `event_page_url`: exact event page when resolved; otherwise the bookmaker's main sports link.
- `event_page_status`: `resolved`, `not_found`, or another resolver status.
- `recommended_stake`: populated only when `--bankroll` is provided.

## Sports Profiles

`uk-soft-edge-core` scans lower-tier and less efficient markets such as selected
football leagues, cricket, WNBA, AFL, and CFL. It intentionally skips some
headline markets like EPL and La Liga.

Use the wider profile when you are willing to spend more The Odds API requests:

```bash
scan-exchanges --sports-profile uk-soft-edge --max-api-requests 40
```

Estimate request count before a broad scan:

```bash
scan-exchanges --sports-profile uk-soft-edge --dry-run-estimate
```

## Caching

Use a short cache for retries during development:

```bash
scan-exchanges \
  --odds-cache-dir .odds-api-cache \
  --odds-cache-ttl-seconds 120
```

Keep cache TTLs short because betting data moves quickly.

## Backtesting

If you have historical The Odds API-style odds snapshots and settled results,
run:

```bash
scan-exchanges \
  --backtest \
  --historical-odds historical/odds \
  --results historical/results.csv \
  --min-edge 0.02 \
  --min-reference-books 2 \
  --max-age-seconds 900 \
  --unique-events \
  --backtest-daily-time 22:00 \
  --backtest-stake 10
```

`--historical-odds` can be a JSON file, JSONL file, or directory of JSON files.
Supported JSON shapes:

```json
{
  "fetched_at": "2026-08-12T12:00:00Z",
  "payload": [
    {
      "id": "event-1",
      "sport_key": "soccer_epl",
      "home_team": "Arsenal",
      "away_team": "Chelsea",
      "commence_time": "2026-08-13T15:00:00Z",
      "bookmakers": []
    }
  ]
}
```

or a plain The Odds API event list.

`results.csv` should contain:

```csv
event_id,market,winner
event-1,h2h,Arsenal
```

The default backtest samples one decision snapshot per day at `22:00` UTC and
does not re-bet an event already selected on an earlier day. Use
`--allow-rebet-same-event` to disable that protection.

The backtest outputs one settled row per bet plus a summary on stderr. Settlement
is exact by `event_id`, `market`, and winning outcome name.

Backtest quality columns:

- `profit`: flat-stake settled profit/loss.
- `target_clv`: price taken versus same-book closing price.
- `beat_closing_line`: true when the taken price is better than the same-book close.
- `closing_fair_edge`: original price versus closing sharp reference fair odds.
- `positive_closing_fair_edge`: true when the original edge still exists at close.

If ROI is positive but CLV is poor, the sample may be lucky. If CLV is positive
over many bets, the signal is more likely to be real.

## Paper Trading

Log nightly candidates without placing real bets:

```bash
scan-exchanges \
  --min-edge 0.02 \
  --min-reference-books 2 \
  --max-age-seconds 900 \
  --unique-events \
  --resolve-event-pages \
  --paper-log \
  --paper-stake 1
```

The paper log is stored in `paper_trades.sqlite3` by default. It only logs one
trade per `event_id` and market, so rerunning the scanner will not re-bet the
same event.

Export the paper log:

```bash
scan-exchanges --paper-export
```

Near event close, update closing values:

```bash
scan-exchanges \
  --paper-update-closing \
  --max-event-days 7 \
  --min-edge -999 \
  --min-reference-books 2
```

Important paper columns:

- `target_clv`: price taken versus same-book closing price.
- `beat_closing_line`: true when the paper price beats the same-book close.
- `closing_edge`: original paper price versus closing sharp fair odds.
- `positive_closing_edge`: true when the original value still exists at close.

## GitHub Actions Live Test

The repository includes `.github/workflows/live-paper-trading.yml` for unattended
paper trading.

Before enabling it, add this repository secret in GitHub:

```text
THE_ODDS_API_KEY
```

The workflow:

- Logs nightly candidates at `20:00` UK time.
- Updates closing values every 2 hours.
- Settles recent completed results every 6 hours using The Odds API scores endpoint.
- Commits `data/paper_trades.sqlite3` and `data/paper_trades.csv` back to the repo.
- Writes a paper-trading summary into each GitHub Actions run.

Optional alert webhook secret:

```text
PAPER_ALERT_WEBHOOK_URL
```

If present, the workflow posts the latest opportunities and performance summary
to that webhook. The payload includes both `text` and `content` fields, so it
works with many Slack-style or Discord-style endpoints.

You can also trigger it manually from the Actions tab with one of:

```text
paper-log
update-closing
settle-results
```

Credit estimate for the default profile:

- Nightly candidate scan: about 36 credits.
- Closing update: about 36 credits per run.
- Result settlement: 2 credits per sport with open trades.

If the workflow starts finding many trades, consider narrowing the schedule or
sports profile to control quota usage.
