# Matchbook CLV Paper Trader

Read-only scanner for finding potential exchange h2h value bets by comparing
target exchange prices with a sharp reference consensus.

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

Run the broad exchange h2h CLV scan:

```bash
scan-exchanges \
  --strategy exchange-clv \
  --sports-profile active-h2h \
  --markets h2h,h2h_lay \
  --max-api-requests 100 \
  --min-edge 0.015 \
  --max-edge 0.10 \
  --min-reference-books 2 \
  --max-age-seconds 180 \
  --max-event-days 4 \
  --unique-bets
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
--sports-profile active-h2h
--regions uk,eu
--markets h2h
--max-event-days 4
```

For paper staking, pass a bankroll:

```bash
scan-exchanges \
  --min-edge 0.015 \
  --min-reference-books 5 \
  --max-age-seconds 180 \
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

`active-h2h` scans every currently active The Odds API sport except futures,
outright winner markets, politics, and golf outrights. This is the deployed
paper-trading profile so seasonal sports are picked up automatically when they
become active.

`matchbook-h2h-expanded` scans event-level h2h sports and competitions where
Matchbook may have exchange markets. It excludes futures and outright winner
markets.

```bash
scan-exchanges --sports-profile active-h2h --markets h2h,h2h_lay --max-api-requests 100
```

Estimate request count before a broad scan:

```bash
scan-exchanges --sports-profile active-h2h --markets h2h,h2h_lay --dry-run-estimate
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
  --sports-profile active-h2h \
  --markets h2h,h2h_lay \
  --max-api-requests 100 \
  --min-edge 0.015 \
  --min-reference-books 2 \
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

The current `exchange-clv` strategy targets Matchbook, Smarkets, and Betfair
Exchange prices against a median de-vigged sharp reference consensus. It requires
at least two complete sharp reference books per h2h outcome. If The Odds API
includes a matching lay market such as `h2h_lay`, the exported venue fair
diagnostics use the target venue's back/lay midpoint in implied-probability
space; otherwise they fall back to that venue's regular h2h win/draw/win price.
Betfair target signals also require at least `0.5%` edge versus Betfair's own
top-of-book fair value.
Totals and spreads/handicaps are currently restricted to Matchbook soccer
targets and require at least 8 same-line reference books rather than a sharp
same-line reference. H2h remains available across Matchbook, Smarkets, and
Betfair targets.

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

The repository includes a small dashboard Lambda for the DynamoDB paper
trade table. It shows total trades, open/settled counts, win/loss record, settled
PnL, ROI, trades logged in the last 24 hours, average booked odds, median confirmed
liquidity, CLV beat/miss/tie counts, results by venue, results by sport, and
results by league. The page also supports quick filters for open, settled,
Matchbook, Betfair, multi-select sport and league inclusion, JSON output, and a
pause/resume control for new paper trade logging.

Required Lambda configuration:

```text
Handler: lambda_function.lambda_handler
Runtime: python3.11
Environment:
  DASHBOARD_TOKEN=<unguessable shared token>
  PAPER_TRADES_TABLE=sports-stat-arb-paper-trades
IAM:
  dynamodb:GetItem, dynamodb:PutItem, and dynamodb:Scan on the paper trades table
```

## Live Execution

The strategy runner can keep broad paper logging enabled while sending a narrower
set of signals to a live execution ledger. Live execution is disabled by default,
and dry-run is enabled by default when the live gate is turned on.

Initial production filter:

```text
LIVE_EXECUTION_ENABLED=false
LIVE_EXECUTION_DRY_RUN=true
LIVE_ORDER_TABLE=sports-stat-arb-live-orders
LIVE_ALLOWED_SPORT_PREFIXES=soccer_
LIVE_ALLOWED_BOOKMAKERS=matchbook,betfair,smarkets
LIVE_ALLOWED_BET_SIDES=back,lay
LIVE_MAX_REFERENCE_DISAGREEMENT_PCT=0.03
LIVE_REQUIRE_CONFIRMED_LIQUIDITY=true
LIVE_ALLOW_UNCONFIRMED_LIQUIDITY_BOOKMAKERS=betfair
LIVE_PREVENT_STACKED_EVENT_EXPOSURE=true
LIVE_SIZING_METHOD=flat
LIVE_FLAT_ORDER_RISK=1
```

Sizing and risk controls:

```text
LIVE_SIZING_METHOD=flat
LIVE_FLAT_ORDER_RISK=1
LIVE_BANKROLL=1000
LIVE_KELLY_FRACTION=0.10
LIVE_MAX_ORDER_RISK_PCT=0.005
LIVE_MAX_DAILY_RISK_PCT=0.02
LIVE_MIN_ORDER_RISK=1
LIVE_MAX_ORDER_RISK=10
```

Dry-run mode writes deterministic order-intent rows to the live order table with
`execution_mode=dry_run` and `status=dry_run`. With dry-run off, the runner calls
configured venue executors and records submitted, rejected, or failed attempts in
the same table. Paper trades continue to be logged separately. Flat sizing treats
`LIVE_FLAT_ORDER_RISK` as stake for backs and worst-case liability for lays.

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

- Scans `active-h2h` h2h, h2h_lay, totals, and spreads markets every hour.
- Targets Matchbook, Smarkets, and Betfair Exchange prices.
- Restricts totals and spreads/handicaps to Matchbook soccer targets.
- Requires `1.5%` post-fee edge and at most `10%` edge.
- Requires h2h signals to have at least 1 sharp reference book.
- Requires Matchbook soccer totals/spreads to have at least 8 same-line
  reference books.
- Requires Betfair target prices to beat Betfair top-of-book fair value by at
  least `0.5%`.
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

Credit estimate for the deployed active profile:

- Candidate scan: one odds request per active non-outright sport.
- Closing update: one odds request per active non-outright sport.
- Result settlement: 2 credits per sport with open trades.

The deployed workflow uses the `active-h2h` profile. It scans h2h/h2h_lay plus
Matchbook soccer totals/spreads every hour and updates closing values every 2
hours. On August 18, 2026, this profile
planned 66 odds requests per scan, so base odds usage would be roughly `71,000`
credits per 30-day month before result settlement. This number changes as sports
move in and out of season.

Sharp books are treated as a reference-price proxy, not as guaranteed truth. The
strategy gives Pinnacle, Betfair, Smarkets, and Matchbook the highest reference
weights, but allows softer books to contribute to coverage at lower weights. It
excludes new booked opportunities above `10%` edge by default, because very
large edges are usually stale prices, market mismatches, or unusable exchange
quotes rather than clean value. Matchbook liquidity is stored directly on trade
rows when the selected best-price venue is Matchbook.

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

3. Test additional non-h2h markets.
   Matchbook soccer spreads/handicaps and totals are now included in paper
   trading. Selected soccer secondary markets such as draw no bet, both teams to
   score, double chance, and to qualify remain future candidates. Each market
   needs exact line matching, liquidity tracking, and separate CLV validation
   before any live execution.

Scaling target:

```text
daily theoretical EV = sum(executable_liquidity * after-fee edge)
target = 100 GBP/day
```
