#!/usr/bin/env python3
"""
teslamate-supercharger-costs
============================
Fetches real Supercharger session costs from the Tesla ownership API
and writes them into TeslaMate's PostgreSQL database automatically.
"""

import os
import sys
import json
import logging
import requests
import argparse
from datetime import datetime, timezone
from pathlib import Path

try:
    import teslapy
    import psycopg2
    from dateutil import parser as dateparser
except ImportError as e:
    print(f"ERROR: Missing dependency: {e}")
    print("Install with: pip install -r requirements.txt")
    sys.exit(1)

# ── Logging ──────────────────────────────────────────────────────────────────

def setup_logging(log_file: str, verbose: bool = False) -> logging.Logger:
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as e:
            print(f"WARNING: Cannot create log file {log_file}: {e}")

    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    return logging.getLogger("teslamate_suc")

# ── Configuration ─────────────────────────────────────────────────────────────

def _cfg(key: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.getenv(key, default)
    if required and not val:
        print(f"ERROR: Required environment variable not set: {key}")
        sys.exit(1)
    return val

TESLA_EMAIL        = _cfg("TESLA_EMAIL",        required=True)
TESLA_CACHE_FILE   = _cfg("TESLA_CACHE_FILE",   "/data/tesla_cache.json")

DB_HOST            = _cfg("TESLAMATE_DB_HOST",  "database")
DB_PORT            = _cfg("TESLAMATE_DB_PORT",  "5432")
DB_NAME            = _cfg("TESLAMATE_DB_NAME",  "teslamate")
DB_USER            = _cfg("TESLAMATE_DB_USER",  "teslamate")
DB_PASS            = _cfg("TESLAMATE_DB_PASS",  required=True)

LOOKBACK_DAYS      = int(_cfg("LOOKBACK_DAYS",      "30"))
TIME_TOLERANCE_S   = int(_cfg("TIME_TOLERANCE_S",   "120"))
OVERWRITE_EXISTING = _cfg("OVERWRITE_EXISTING", "false").lower() == "true"
LOG_FILE           = _cfg("LOG_FILE",           "/logs/importer.log")
TARGET_CURRENCY    = _cfg("TARGET_CURRENCY",    "EUR")

log = setup_logging(LOG_FILE)

# ── Tesla API ─────────────────────────────────────────────────────────────────

OWNERSHIP_API_URL = "https://ownership.tesla.com/mobile-app/charging/history"

def fetch_charging_sessions(input_file: str | None = None) -> list[dict]:
    if input_file:
        log.info(f"Loading sessions from local file: {input_file}")
        try:
            with open(input_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Handle both direct list or { "data": [...] } format
            return data if isinstance(data, list) else data.get("data", [])
        except (FileNotFoundError, json.JSONDecodeError) as e:
            log.error(f"Failed to load input file: {e}")
            sys.exit(1)

    log.info(f"Connecting to Tesla API as {TESLA_EMAIL}")
    cache_path = Path(TESLA_CACHE_FILE)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    with teslapy.Tesla(TESLA_EMAIL, cache_file=str(cache_path)) as tesla:
        if not tesla.authorized:
            refresh_token = _interactive_auth(tesla)
            tesla.refresh_token(refresh_token=refresh_token)

        vehicles = tesla.vehicle_list()
        if not vehicles:
            log.error("No vehicles found for this Tesla account.")
            sys.exit(1)
            
        target_vin = _cfg("TESLA_VIN") or vehicles[0]["vin"]
        log.info(f"Using vehicle VIN: {target_vin}")

        # teslapy.get() returns a requests.Response object
        response = tesla.get(OWNERSHIP_API_URL, params={
            "vin": target_vin,
            "deviceLanguage": "en",
            "deviceCountry": "US",
            "operationName": "getChargingHistoryV2",
        })
        response.raise_for_status()
        
        sessions = response.json().get("data") or []
        log.info(f"Retrieved {len(sessions)} sessions from Tesla API")
        return sessions

def _interactive_auth(tesla: teslapy.Tesla) -> str:
    print("\n=== Tesla OAuth Authentication ===")
    print("Please paste your refresh_token (or the full redirect URL).")
    print("If you pasted a URL, the token will be extracted automatically.\n")
    value = input("Token/URL: ").strip()
    
    # Extract token if user pasted a redirect URL
    if "refresh_token=" in value:
        value = value.split("refresh_token=")[1].split("&")[0]
    elif "code=" in value:
        print("ERROR: You pasted an authorization CODE. Please refresh your token or use tesla-info.com to get a REFRESH_TOKEN.")
        sys.exit(1)
        
    return value

# ── Cost extraction ───────────────────────────────────────────────────────────

def extract_cost(session: dict) -> dict | None:
    fees = session.get("fees") or []
    if not fees:
        return None

    total = sum(float(f.get("totalDue") or 0) for f in fees)
    if total == 0:
        return None

    return {
        "total": total,
        "currency": fees[0].get("currencyCode") or "?"
    }

# ── DB ────────────────────────────────────────────────────────────────────────

def _get_db_connection():
    try:
        return psycopg2.connect(
            host=DB_HOST,
            port=int(DB_PORT),
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASS,
        )
    except psycopg2.OperationalError as e:
        log.error(f"Database connection failed: {e}")
        sys.exit(1)

# ── FX ────────────────────────────────────────────────────────────────────────

_rates_cache = {}

def get_rate(date: datetime, currency: str) -> float:
    key = (date.strftime("%Y-%m-%d"), currency)
    if key in _rates_cache:
        return _rates_cache[key]

    url = f"https://api.frankfurter.app/{key[0]}"
    params = {"from": "EUR", "to": currency}
    
    try:
        r = requests.get(url, params=params, timeout=5)
        r.raise_for_status()
        data = r.json()
        
        if "rates" not in data or currency not in data["rates"]:
            # Fallback to latest rate if historical is missing
            log.warning(f"Historical rate missing for {currency} on {key[0]}, using latest.")
            r2 = requests.get("https://api.frankfurter.app/latest", params={"to": currency}, timeout=5)
            r2.raise_for_status()
            rate = r2.json()["rates"][currency]
        else:
            rate = data["rates"][currency]
            
        _rates_cache[key] = rate
        return rate
    except requests.RequestException as e:
        log.error(f"FX rate fetch failed for {currency} on {key[0]}: {e}")
        raise

def convert_currency(amount: float, from_currency: str, to_currency: str, date: datetime) -> float:
    if from_currency == to_currency:
        return round(amount, 2)

    if from_currency != "EUR":
        rate_from = get_rate(date, from_currency)
        amount = amount / rate_from

    if to_currency != "EUR":
        rate_to = get_rate(date, to_currency)
        amount = amount * rate_to

    return round(amount, 2)

# ── MAIN ──────────────────────────────────────────────────────────────────────

def import_to_teslamate(sessions, dry_run, verbose):
    conn = _get_db_connection()
    cur = conn.cursor()
    updated_count = 0
    skipped_count = 0

    print("\n" + "="*60)
    print(f"{'DRY-RUN' if dry_run else 'IMPORT'} MODE")
    print(f"DB: {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
    print(f"Lookback: {LOOKBACK_DAYS} days | Tolerance: {TIME_TOLERANCE_S}s")
    print(f"Target Currency: {TARGET_CURRENCY}")
    print("="*60)

    for session in sessions:
        start = session.get("chargeStartDateTime")
        if not start:
            continue

        # Tesla API returns UTC ISO8601. TeslaMate expects naive UTC TIMESTAMP.
        start_dt = dateparser.parse(start).astimezone(timezone.utc).replace(tzinfo=None)

        cost_info = extract_cost(session)
        if not cost_info:
            if verbose:
                log.debug(f"SKIPPED (no fees): {start}")
            continue

        # Safe query for TeslaMate's TIMESTAMP column
        cur.execute("""
            SELECT id, start_date, cost
            FROM charging_processes
            WHERE ABS(EXTRACT(EPOCH FROM (start_date - %s))) < %s
            LIMIT 1
        """, (start_dt, TIME_TOLERANCE_S))

        row = cur.fetchone()
        if not row:
            if verbose:
                log.debug(f"NOT FOUND: {start}")
            continue

        tm_id, tm_start, tm_cost = row

        if tm_cost and not OVERWRITE_EXISTING:
            skipped_count += 1
            continue

        currency_code = cost_info["currency"]
        try:
            converted = convert_currency(
                cost_info["total"],
                currency_code,
                TARGET_CURRENCY,
                start_dt
            )
        except Exception as e:
            log.error(f"Currency conversion failed for session {tm_id}: {e}")
            continue

        # Extract kWh for logging
        charge_kwh = session.get("chargeEnergyAdded") or session.get("chargeMilesAdded") or "N/A"
        
        if dry_run:
            print(f"  {converted} {TARGET_CURRENCY} [{charge_kwh} kWh] (from {cost_info['total']} {currency_code})")
        else:
            try:
                cur.execute(
                    "UPDATE charging_processes SET cost = %s WHERE id = %s",
                    (converted, tm_id),
                )
                updated_count += 1
                log.info(f"UPDATED {tm_id}: {converted} {TARGET_CURRENCY}")
            except psycopg2.Error as e:
                log.error(f"DB update failed for {tm_id}: {e}")
                conn.rollback()
                raise

    if not dry_run and updated_count > 0:
        try:
            conn.commit()
            log.info("Changes committed successfully.")
        except psycopg2.Error as e:
            log.error(f"Commit failed, rolling back: {e}")
            conn.rollback()

    cur.close()
    conn.close()

    print("\n" + "-"*60)
    print(f"SUMMARY: Updated {updated_count} | Skipped {skipped_count} | Total processed {len(sessions)}")
    print("-"*60 + "\n")

def main():
    parser = argparse.ArgumentParser(description="TeslaMate Supercharger Cost Importer")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing to the database")
    parser.add_argument("-i", "--input", type=str, help="Load sessions from a saved JSON file (for testing)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show debug output (skipped/free sessions)")
    parser.add_argument("--lookback", type=int, help="Override LOOKBACK_DAYS for this run")
    args = parser.parse_args()

    global LOOKBACK_DAYS
    if args.lookback:
        LOOKBACK_DAYS = args.lookback

    sessions = fetch_charging_sessions(args.input)
    import_to_teslamate(sessions, args.dry_run, args.verbose)

if __name__ == "__main__":
    main()
