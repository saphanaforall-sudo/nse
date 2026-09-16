"""
NSE sec_bhavdata_full daily loader.

Downloads the day's "Full Bhavcopy and Security Deliverable data" report
(sec_bhavdata_full_DDMMYYYY.csv) from NSE archives, cleans it, and
upserts it into a Postgres table (Supabase / Neon / any Postgres works).

Usage:
    python load_bhavcopy.py                 # loads today
    python load_bhavcopy.py --date 15-09-2026
    python load_bhavcopy.py --backfill-days 30   # loads last N calendar days
"""

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta

import pandas as pd
from jugaad_data.nse import bhavcopy_save
from sqlalchemy import create_engine, text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("nse_bhavcopy")

DOWNLOAD_DIR = "/tmp/nse_bhavcopy"
TABLE_NAME = "bhavdata_full"
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 10


def get_engine():
    db_url = os.environ.get("DB_URL")
    if not db_url:
        log.error("DB_URL environment variable is not set.")
        sys.exit(1)
    return create_engine(db_url)


def ensure_schema(engine):
    """Creates the target table if it doesn't already exist."""
    ddl = """
    CREATE TABLE IF NOT EXISTS bhavdata_full (
        symbol          TEXT NOT NULL,
        series          TEXT NOT NULL,
        trade_date      DATE NOT NULL,
        prev_close      NUMERIC,
        open_price      NUMERIC,
        high_price      NUMERIC,
        low_price       NUMERIC,
        last_price      NUMERIC,
        close_price     NUMERIC,
        avg_price       NUMERIC,
        ttl_trd_qnty    BIGINT,
        turnover_lacs   NUMERIC,
        no_of_trades    BIGINT,
        deliv_qty       BIGINT,
        deliv_per       NUMERIC,
        loaded_at       TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (symbol, series, trade_date)
    );
    """
    with engine.begin() as conn:
        conn.execute(text(ddl))
    log.info("Schema check OK (table '%s' ready).", TABLE_NAME)


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """NSE's CSV headers have stray spaces and inconsistent casing — normalize them."""
    df.columns = [c.strip().upper().replace(" ", "_") for c in df.columns]
    rename_map = {
        "SYMBOL": "symbol",
        "SERIES": "series",
        "DATE1": "trade_date",
        "DATE": "trade_date",
        "PREV_CLOSE": "prev_close",
        "OPEN_PRICE": "open_price",
        "HIGH_PRICE": "high_price",
        "LOW_PRICE": "low_price",
        "LAST_PRICE": "last_price",
        "CLOSE_PRICE": "close_price",
        "AVG_PRICE": "avg_price",
        "TTL_TRD_QNTY": "ttl_trd_qnty",
        "TURNOVER_LACS": "turnover_lacs",
        "NO_OF_TRADES": "no_of_trades",
        "DELIV_QTY": "deliv_qty",
        "DELIV_PER": "deliv_per",
    }
    df = df.rename(columns=rename_map)
    keep_cols = [c for c in rename_map.values() if c in df.columns]
    df = df[keep_cols]

    # Drop fully-empty unnamed trailing column NSE sometimes appends
    df = df.dropna(axis=1, how="all")

    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].astype(str).str.strip()
    if "series" in df.columns:
        df["series"] = df["series"].astype(str).str.strip()

    if "trade_date" in df.columns:
        df["trade_date"] = pd.to_datetime(
            df["trade_date"].astype(str).str.strip(), errors="coerce", dayfirst=True
        ).dt.date

    numeric_cols = [
        "prev_close", "open_price", "high_price", "low_price", "last_price",
        "close_price", "avg_price", "ttl_trd_qnty", "turnover_lacs",
        "no_of_trades", "deliv_qty", "deliv_per",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df.dropna(subset=["symbol", "series", "trade_date"])


def download_bhavcopy(target_date: date) -> pd.DataFrame:
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
            path = bhavcopy_save(target_date, DOWNLOAD_DIR)
            df = pd.read_csv(path)
            log.info("Downloaded %s rows for %s (attempt %d).", len(df), target_date, attempt)
            return df
        except Exception as exc:  # noqa: BLE001 - retry on any download/parse failure
            last_err = exc
            log.warning(
                "Download attempt %d/%d failed for %s: %s",
                attempt, MAX_RETRIES, target_date, exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
    raise RuntimeError(f"All download attempts failed for {target_date}: {last_err}")


def upsert(engine, df: pd.DataFrame):
    if df.empty:
        log.warning("Nothing to load — dataframe is empty after cleaning.")
        return

    staging_table = "stg_bhavdata_full"
    df.to_sql(staging_table, engine, if_exists="replace", index=False)

    cols = list(df.columns)
    non_key_cols = [c for c in cols if c not in ("symbol", "series", "trade_date")]
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in non_key_cols)
    col_list = ", ".join(cols)

    upsert_sql = f"""
        INSERT INTO {TABLE_NAME} ({col_list})
        SELECT {col_list} FROM {staging_table}
        ON CONFLICT (symbol, series, trade_date)
        DO UPDATE SET {update_clause};
    """
    with engine.begin() as conn:
        conn.execute(text(upsert_sql))
        conn.execute(text(f"DROP TABLE IF EXISTS {staging_table};"))

    log.info("Upserted %d rows into '%s'.", len(df), TABLE_NAME)


def run_for_date(engine, target_date: date):
    log.info("=== Processing %s ===", target_date)
    try:
        raw = download_bhavcopy(target_date)
    except RuntimeError as exc:
        log.error("Skipping %s: %s (likely a holiday/weekend or NSE outage).", target_date, exc)
        return
    cleaned = normalize_columns(raw)
    upsert(engine, cleaned)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="DD-MM-YYYY, defaults to today")
    parser.add_argument("--backfill-days", type=int, default=0,
                         help="Load the last N calendar days instead of a single date")
    args = parser.parse_args()

    engine = get_engine()
    ensure_schema(engine)

    if args.backfill_days > 0:
        end = date.today()
        for i in range(args.backfill_days, -1, -1):
            d = end - timedelta(days=i)
            if d.weekday() < 5:  # skip Sat/Sun; NSE holidays are caught by the try/except above
                run_for_date(engine, d)
    else:
        target = (
            datetime.strptime(args.date, "%d-%m-%Y").date()
            if args.date else date.today()
        )
        run_for_date(engine, target)


if __name__ == "__main__":
    main()
