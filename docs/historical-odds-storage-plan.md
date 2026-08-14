# Historical Odds Storage Plan

This is the proposed long-term storage design for measuring bookmaker sharpness
across the full tradeable universe.

The goal is to learn reliable sharpness weights by sport, league, market, and
time-to-start bucket. This should not be trained only on flagged trades, because
that would bias the dataset toward prices the strategy already thought were
unusual.

## Recommended Architecture

Use a two-layer setup:

1. Store full universe odds snapshots in S3 as compressed Parquet files.
2. Store compact aggregate performance tables in Neon Postgres later, if needed.

Start with S3 only. Add Neon once there is enough history to justify daily
analytics tables and learned weights.

## S3 Source Of Truth

Every scanner run should write the normalized tradeable universe to S3:

```text
s3://<bucket>/odds_snapshots/snapshot_date=2026-08-14/hour=20/odds.parquet
```

Use Parquet with compression rather than CSV or SQLite. It is much smaller,
fast to query with DuckDB, and better suited to long-term historical data.

Each row should include:

```text
snapshot_time
sport_key
event_id
event_name
commence_time
market
outcome_name
point
bookmaker_key
bookmaker_title
odds
implied_probability
devig_probability
last_update
days_to_start
```

This raw-ish history lets us later recompute sharpness differently without
losing information.

## Neon Aggregate Layer

Neon is optional at first. If added, it should hold small derived tables only:

```text
bookmaker_daily_performance
sharpness_weights
strategy_metrics
```

Example bookmaker performance fields:

```text
date
sport_key
market
bookmaker_key
days_to_start_bucket
sample_count
avg_abs_error_vs_closing_consensus
avg_squared_error_vs_closing_consensus
log_loss
brier_score
learned_weight
updated_at
```

The closing-consensus comparison should ideally be leave-one-out, meaning a
bookmaker is not included in the consensus used to score that same bookmaker.

## Setup Checklist

Create an AWS S3 bucket, for example:

```text
kamvysis-sports-odds-history
```

Create an IAM user or role with access limited to that bucket:

```text
s3:PutObject
s3:GetObject
s3:ListBucket
```

Add these GitHub repository secrets:

```text
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
AWS_REGION
ODDS_S3_BUCKET
```

Add Python dependencies when implementing:

```text
boto3
pyarrow
pandas
duckdb
```

Add a GitHub Actions step after the odds scan to:

1. Normalize all fetched prices.
2. Compute implied and de-vigged probabilities per bookmaker/event/market.
3. Write a compressed Parquet file locally.
4. Upload the Parquet file to S3.

## Querying Later

DuckDB can query Parquet files directly:

```sql
SELECT
  bookmaker_key,
  sport_key,
  market,
  COUNT(*) AS samples
FROM read_parquet('s3://<bucket>/odds_snapshots/**/*.parquet')
GROUP BY bookmaker_key, sport_key, market;
```

Once we have enough history, we can compute bookmaker sharpness by comparing
each book's de-vigged probability to the closing consensus and final outcomes.

## Notes

S3 should hold the large historical dataset. GitHub should only hold code,
paper-trade summaries, and small CSV exports.

Do not store raw full-universe snapshots in the repository. The database can
grow to millions of rows quickly if the scanner runs hourly across many sports.
