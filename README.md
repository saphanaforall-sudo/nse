# NSE sec_bhavdata_full — automated daily pipeline

Downloads NSE's daily "Full Bhavcopy and Security Deliverable data" report
and loads it into a Postgres database every weekday evening, automatically,
for **$0/month**.

## What's in this repo

- `load_bhavcopy.py` — download, clean, and upsert logic
- `requirements.txt` — Python dependencies
- `.github/workflows/nse-bhavcopy-daily.yml` — the free scheduler (GitHub Actions)

## Setup (15 minutes, one time)

### 1. Create the free database

Pick one (both are $0 for this workload):

**Supabase** (recommended — simplest UI)
1. Go to supabase.com → New project (free tier).
2. Once created: Project Settings → Database → Connection string → copy the
   "URI" one (starts with `postgresql://postgres:...`).
3. Use the **Session pooler** connection string, not the direct one — GitHub
   Actions runners get a fresh IP every run, and the pooler handles that better.

**Neon** (alternative — scale-to-zero serverless Postgres)
1. Go to neon.tech → New project (free tier).
2. Copy the connection string shown on the dashboard.

Either way, you'll end up with something like:
```
postgresql://user:password@host:5432/dbname
```

### 2. Put this code in a GitHub repo

1. Create a new repo (public repo = unlimited free GitHub Actions minutes;
   private also works, just capped at 2,000 free minutes/month — one run/day
   uses only a couple minutes, so this is a non-issue either way).
2. Push these files to it.

### 3. Add your database URL as a secret

In the repo: Settings → Secrets and variables → Actions → New repository secret
- Name: `DB_URL`
- Value: the connection string from step 1

### 4. Turn it on

The workflow is already scheduled for 7:00 PM IST, Mon–Fri. To also load
historical data right now instead of waiting for tomorrow:

1. Go to the repo's **Actions** tab → "nse-bhavcopy-daily" → **Run workflow**.
2. Fill in `backfill_days` (e.g. `30` for the last month) and run it.
3. From then on, it runs automatically every weekday — no further action needed.

You can also just let it run untouched starting tonight with no backfill —
it'll begin building up history from today onward.

## Querying your data

```sql
-- Latest close for a symbol
SELECT trade_date, close_price, deliv_per
FROM bhavdata_full
WHERE symbol = 'RELIANCE' AND series = 'EQ'
ORDER BY trade_date DESC
LIMIT 10;
```

## Cost reality check

- GitHub Actions: $0 (well under free-tier minutes for one run/day)
- Supabase/Neon free tier: $0, and at ~2–5MB/day this report won't approach
  the 500MB free storage cap for **years**.
- The only thing that ever costs money: if storage eventually exceeds the
  free cap, or you outgrow it and upgrade to a paid compute tier by choice.

## Notes / gotchas

- NSE has no file on weekends and exchange holidays — the script detects a
  failed download and just skips that date; nothing breaks.
- Re-running the same date is always safe (upsert on `symbol + series + trade_date`,
  no duplicate rows).
- If a run ever fails outright (NSE occasionally blocks/rate-limits), just
  re-trigger it manually from the Actions tab with `workflow_dispatch`.
