# Matchbook CLV Paper Trader

Read-only scanner for finding potential Matchbook h2h value bets by comparing
Matchbook prices with a sharpness-weighted reference consensus.

It does not place bets.

## Strategies

The default `sharp-weighted-clv` strategy:

1. Fetches odds from The Odds API.
2. Treats Matchbook as the only target venue.
3. Treats all other complete books as reference prices.
4. De-vigs the reference market into an estimated fair probability.
5. Applies Matchbook's assumed 2% winning-market commission.
6. Flags a bet when:

```text
edge = matchbook_effective_odds * reference_probability - 1
```

The CSV tells you what to back in `bet_to_place`, including decimal, UK
fractional, and American odds. Matchbook liquidity enrichment adds visible
order-book size at or above the target price. Betfair delayed-API enrichment can
also add Betfair best back/lay, spread, and available size diagnostics when a
Betfair app key and session token are configured.

## Included Books

Target venue:

```text
matchbook
```

Reference books are all other complete books available for the same event and
market, weighted by the built-in or learned sharpness weights.

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

Run the Matchbook h2h CLV scan:

```bash
scan-exchanges \
  --sports-profile matchbook-h2h-expanded \
  --markets h2h \
  --max-api-requests 80 \
  --min-edge 0.025 \
  --max-edge 0.10 \
  --min-reference-books 5 \
  --max-age-seconds 900 \
  --unique-events
```

`sharp-weighted-clv` only targets Matchbook for potential execution, but
compares each Matchbook price against a sharpness-weighted consensus from all
other complete books on the same event. Pinnacle, Betfair, Smarkets, and
Matchbook get the highest reference weights, stronger mainstream books get
medium weights, and unknown books get low default weight. Matchbook's own price
is excluded from the reference calculation for a Matchbook target.

The live workflow intentionally does not store raw odds snapshots. It only keeps
the paper trade ledgers, exported trade CSVs, and summary markdown so the repo
stays comfortably below GitHub's normal file-size limits.

For the future full-universe odds history and bookmaker sharpness store, see
[Historical Odds Storage Plan](docs/historical-odds-storage-plan.md).

The default scan uses:

```text
--strategy sharp-weighted-clv
--sports-profile matchbook-h2h-expanded
--regions uk,eu
--markets h2h
--max-event-days 2
```

For paper staking, pass a bankroll:

```bash
scan-exchanges \
  --min-edge 0.025 \
  --min-reference-books 5 \
  --max-age-seconds 900 \
  --unique-events \
  --bankroll 1000
```

Stake sizing uses fractional Kelly, capped by bankroll percentage and max stake.

## Output

Important CSV columns:

- `bet_to_place`: plain-English action, e.g. `Back Team with Book at 4.5 (7/2)+`.
- `target_odds`: decimal odds from the target bookmaker.
- `target_effective_odds`: target odds after Matchbook commission.
- `target_odds_fractional`: UK fractional odds.
- `target_odds_american`: American odds.
- `target_implied_probability`: implied probability from the offered price.
- `reference_fair_odds`: de-vigged fair decimal odds from sharp references.
- `edge`: estimated value edge.
- `recommended_stake`: populated only when `--bankroll` is provided.

## Sports Profiles

`matchbook-h2h-expanded` scans event-level h2h sports and competitions where
Matchbook may have exchange markets. It excludes futures and outright winner
markets.

```bash
scan-exchanges --sports-profile matchbook-h2h-expanded --markets h2h --max-api-requests 80
```

Estimate request count before a broad scan:

```bash
scan-exchanges --sports-profile matchbook-h2h-expanded --markets h2h --dry-run-estimate
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

The deployed workflow logs candidates without placing real bets. It scans h2h
prices on Matchbook, Smarkets, and Betfair Exchange, keeps only the best price
for each event/market/outcome, enriches Matchbook rows with visible Matchbook
liquidity when available, and logs every selected signal with a nominal `1 GBP`
paper stake.

```bash
scan-exchanges \
  --strategy exchange-clv \
  --sports-profile matchbook-h2h-expanded \
  --markets h2h,h2h_lay \
  --max-api-requests 80 \
  --min-edge 0.025 \
  --min-reference-books 5 \
  --max-age-seconds 900 \
  --unique-bets > data/latest_opportunities.csv

python scripts/enrich_matchbook_liquidity.py \
  --input-csv data/latest_opportunities.csv \
  --output-csv data/latest_opportunities_with_matchbook_liquidity.csv

python scripts/enrich_betfair_liquidity.py \
  --input-csv data/latest_opportunities_with_matchbook_liquidity.csv \
  --output-csv data/latest_opportunities_with_liquidity.csv

python scripts/log_exchange_paper_trades.py \
  --paper-db data/paper_trades.sqlite3 \
  --opportunities-csv data/latest_opportunities_with_liquidity.csv \
  --paper-stake 1
```

The trade log only logs one trade per `event_id`, market, and outcome, so
rerunning the scanner will not re-book the same bet. Matchbook rows include
available liquidity at or above the target price when the public Matchbook order
book can be matched. Betfair rows include delayed-API liquidity diagnostics when
`BETFAIR_APP_KEY`/`BETFAIR_APP_KEY_DELAYED` is set with either certificate-login
credentials or a temporary `BETFAIR_SESSION_TOKEN`; otherwise they are marked
`betfair_not_configured`.
Certificate login is preferred because it creates a fresh Betfair session token
per run. The workflow can also call Betfair `keepAlive` for short-term testing
with a manually supplied session token.

The current `exchange-clv` strategy also adds the target venue's own fair value
as a `3.0` weight `Target Venue Fair Value` reference in the consensus. If The
Odds API includes a matching lay market such as `h2h_lay`, the fair value uses
the target venue's back/lay midpoint in implied-probability space; otherwise it
falls back to that venue's regular h2h win/draw/win price.

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

## AWS Paper Trade Dashboard

The repository includes a small read-only dashboard Lambda for the DynamoDB paper
trade table. It shows total trades, open/settled counts, win/loss record, settled
PnL, ROI, trades logged in the last 24 hours, average booked odds, median confirmed
liquidity, CLV beat/miss/tie counts, results by venue, results by sport, and
results by league. The page also supports quick filters for open, settled,
Matchbook, Betfair, multi-select sport and league inclusion, and JSON output.

Required Lambda configuration:

```text
Handler: lambda_function.lambda_handler
Runtime: python3.11
Environment:
  DASHBOARD_TOKEN=<unguessable shared token>
  PAPER_TRADES_TABLE=sports-stat-arb-paper-trades
IAM:
  dynamodb:Scan on the paper trades table
```

Expose it with either a Lambda Function URL or API Gateway HTTP API. Open the
dashboard with:

```text
https://<dashboard-url>/?token=<DASHBOARD_TOKEN>
```

Useful query parameters:

```text
status=open
status=settled
bookmaker=Matchbook
bookmaker=Betfair
sport=soccer
sport=cricket&sport=soccer
league=soccer_epl
league=soccer_epl&league=cricket_caribbean_premier_league
format=json
```

Deploy updates from GitHub Actions by running the `Deploy Dashboard Lambda`
workflow. Configure these repository secrets first:

```text
AWS_ROLE_ARN
DASHBOARD_LAMBDA_FUNCTION_NAME
LAMBDA_DEPLOY_BUCKET
```

`ODDS_S3_BUCKET` can be used instead of `LAMBDA_DEPLOY_BUCKET` if you want to
reuse the existing odds storage bucket for Lambda deployment zips.

## GitHub Actions Live Test

The repository includes `.github/workflows/paper-trade-log.yml` for unattended
paper logging.

Before enabling it, add this repository secret in GitHub:

```text
THE_ODDS_API_KEY
```

To add Betfair delayed-API liquidity diagnostics, add:

```text
BETFAIR_APP_KEY_DELAYED
BETFAIR_USERNAME
BETFAIR_PASSWORD
BETFAIR_CERT_PEM
BETFAIR_KEY_PEM
```

`BETFAIR_CERT_PEM` should be the contents of the uploaded `.crt` file.
`BETFAIR_KEY_PEM` should be the contents of the matching private `.key` file.
Do not commit either file.

The workflow now keeps one combined trade log:

- Scans `matchbook-h2h-expanded` h2h markets every hour.
- Targets Matchbook, Smarkets, and Betfair Exchange prices.
- Requires `2.5%` post-fee edge, at least 5 reference books, and at most `10%`
  edge.
- Books only the best price when the same event/market/outcome appears on
  multiple exchanges.
- Logs a nominal `1 GBP` paper stake for each selected trade.
- Adds visible Matchbook liquidity fields when the selected target is Matchbook.
- Adds Betfair delayed-API liquidity fields when certificate-login secrets are
  configured.
- Updates closing values every 2 hours.
- Settles recent completed results every 6 hours using The Odds API scores endpoint.
- Commits one durable SQLite database, one CSV trade log, and one summary:
  `data/paper_trades.sqlite3`, `data/paper_trades.csv`, and
  `data/paper_summary.md`.

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

Credit estimate for the deployed wider profile:

- Candidate scan: about 75 credits per run.
- Closing update: about 75 credits per run.
- Result settlement: 2 credits per sport with open trades.

The deployed workflow uses the `matchbook-h2h-expanded` profile. It scans h2h
every hour and updates closing values every 2 hours. With the current 75-sport
profile, base odds usage is roughly `81,000` credits per 30-day month before
result settlement.

Sharp books are treated as a reference-price proxy, not as guaranteed truth. The
strategy requires at least 5 reference books before logging a trade, which helps
avoid trusting a single stale or low-liquidity quote. It also excludes new
booked opportunities above `10%` edge by default, because very large edges are
usually stale prices, market mismatches, or unusable exchange quotes rather than
clean value. Matchbook liquidity is stored directly on trade rows when the
selected best-price venue is Matchbook.

## If Edge Is Proven

Once the paper test shows enough positive CLV after fees, the next objective is
to scale from a research system into a controlled execution system targeting
about `100 GBP/day` in theoretical EV.

Planned next steps:

1. Add Kelly and portfolio sizing.
   Live execution should size each trade as the smaller of the Kelly-derived
   stake, the configured risk cap, and the maximum available exchange liquidity
   at an acceptable price.

2. Add more execution venues.
   Smarkets is the next likely exchange to add. Betfair is also attractive
   because of its deeper liquidity, if API access is worth the setup cost.
   BETDAQ can be reviewed later as an incremental venue.

3. Test markets beyond h2h.
   Spreads/handicaps and totals are the first candidates, followed by selected
   soccer secondary markets such as draw no bet, both teams to score, double
   chance, and to qualify. Each market needs exact line matching, liquidity
   tracking, and separate CLV validation before any live execution.

Scaling target:

```text
daily theoretical EV = sum(executable_liquidity * after-fee edge)
target = 100 GBP/day
```
