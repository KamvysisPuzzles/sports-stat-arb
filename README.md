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
order-book size at or above the target price.

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

The deployed workflow logs candidates without placing real bets. It first scans
for Matchbook h2h value, enriches the rows with Matchbook liquidity, then books
only executable rows as paper trades.

```bash
scan-exchanges \
  --sports-profile matchbook-h2h-expanded \
  --markets h2h \
  --max-api-requests 80 \
  --min-edge 0.025 \
  --min-reference-books 5 \
  --max-age-seconds 900 \
  --unique-events > data/latest_opportunities_h2h.csv

python scripts/enrich_matchbook_liquidity.py \
  --input-csv data/latest_opportunities_h2h.csv \
  --output-csv data/latest_opportunities_h2h_with_liquidity.csv

python scripts/log_matchbook_paper_trades.py \
  --paper-db data/paper_trades_h2h.sqlite3 \
  --opportunities-csv data/latest_opportunities_h2h_with_liquidity.csv
```

The paper stake is the visible Matchbook liquidity at or above the target price.
Rows without available liquidity are tracked in the liquidity snapshot file but
are not booked as paper trades. The paper log only logs one trade per `event_id`
and market, so rerunning the scanner will not re-bet the same event.

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

The workflow now paper-trades the Matchbook h2h strategy:

- Scans `matchbook-h2h-expanded` h2h markets every hour.
- Scans spreads/handicaps separately every 2 hours as an experimental bucket.
- Logs Matchbook candidates against the sharpness-weighted reference consensus.
- Enriches each flagged row with visible Matchbook liquidity.
- Books executable rows using visible liquidity as the paper stake.
- Updates closing values every 2 hours.
- Settles recent completed results every 6 hours using The Odds API scores endpoint.
- Commits market-specific paper databases and CSVs back to the repo.
- Writes a paper-trading summary with visible liquidity and theoretical executable EV into each GitHub Actions run.

The h2h and spreads paper tests use separate databases, CSVs, and summaries so
their CLV and P&L evidence can be evaluated separately.

The repository also includes `.github/workflows/exchange-clv-paper-trading.yml`
as a separate CLV-only experiment for the broader exchange universe:

- Targets Matchbook, Smarkets, and Betfair Exchange prices.
- Uses the same sharpness-weighted reference consensus and `2.5%` post-fee edge
  threshold.
- Books only the best price when the same event/market/outcome appears on
  multiple exchanges.
- Stores each row with a nominal `1 GBP` paper stake because The Odds API does
  not provide executable liquidity for Smarkets or Betfair.
- Runs candidate logging every 4 hours, closing updates every 4 hours, and
  result settlement every 6 hours.
- Keeps its own database, CSV, and summary:
  `data/paper_trades_exchange_clv.sqlite3`,
  `data/paper_trades_exchange_clv.csv`, and
  `data/paper_summary_exchange_clv.md`.

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

- Candidate scan: about 73 credits per run.
- Closing update: about 73 credits per run.
- Result settlement: 2 credits per sport with open trades.

The deployed workflow uses the `matchbook-h2h-expanded` profile. It scans h2h
only every hour and updates closing values every 2 hours, which keeps expected
monthly usage under the 100k-credit plan before result settlement.

Sharp books are treated as a reference-price proxy, not as guaranteed truth. The
strategy requires at least 5 reference books before logging a trade, which helps
avoid trusting a single stale or low-liquidity quote. It also excludes new
booked opportunities above `10%` edge by default, because very large edges are
usually stale prices, market mismatches, or unusable exchange quotes rather than
clean value. Matchbook liquidity snapshots are stored separately so we can
measure visible executable size over time.

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
