#!/usr/bin/env python3
"""
teslamate-supercharger-costs
============================
Fetches real Supercharger session costs from the Tesla ownership API
and writes them into TeslaMate's PostgreSQL database automatically.

Configuration is done via environment variables or a .env file.
See .env.example for all available options.
"""

import os
import sys
import json
import logging
import requests
import argparse
from datetime import datetime, timezone, timedelta
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

def setup_logging(log_file: str) -> logging.Logger:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as e:
            print(f"WARNING: Cannot create log file {log_file}: {e}")

    logging.basicConfig(
        level=logging.INFO,
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


def fetch_charging_sessions() -> list[dict]:
    log.info(f"Connecting to Tesla API as {TESLA_EMAIL}")

    cache_path = Path(TESLA_CACHE_FILE)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    with teslapy.Tesla(TESLA_EMAIL, cache_file=str(cache_path)) as tesla:
        if not tesla.authorized:
            refresh_token = _interactive_auth(tesla)
            tesla.refresh_token(refresh_token=refresh_token)

        vehicles = tesla.vehicle_list()
        vehicle = vehicles[0]
        vin = vehicle["vin"]

        target_vin = _cfg("TESLA_VIN") or vin

        response = tesla.get(OWNERSHIP_API_URL, params={
            "vin": target_vin,
            "deviceLanguage": "en",
            "deviceCountry": "US",
            "operationName": "getChargingHistoryV2",
        })

        sessions = response.get("data") or []
        log.info(f"Retrieved {len(sessions)} sessions from Tesla API")
        return sessions


def _interactive_auth(tesla: teslapy.Tesla) -> str:
    value = input("Paste refresh_token: ").strip()
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
    return psycopg2.connect(
        host=DB_HOST,
        port=int(DB_PORT),
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS,
    )


# ── FX ────────────────────────────────────────────────────────────────────────

_rates_cache = {}

def get_rate(date, currency):
    key = (date.strftime("%Y-%m-%d"), currency)
    if key in _rates_cache:
        return _rates_cache[key]

    url = f"https://api.exchangerate.host/{key[0]}"
    params = {"base": "EUR", "symbols": currency}

    r = requests.get(url, params=params, timeout=5)
    r.raise_for_status()
    rate = r.json()["rates"][currency]

    _rates_cache[key] = rate
    return rate


def convert_currency(amount, from_currency, to_currency, date):
    if from_currency == to_currency:
        return amount

    if from_currency != "EUR":
        amount = amount / get_rate(date, from_currency)

    if to_currency != "EUR":
        amount = amount * get_rate(date, to_currency)

    return amount


# ── MAIN ──────────────────────────────────────────────────────────────────────

def import_to_teslamate(sessions, dry_run):
    conn = _get_db_connection()
    cur = conn.cursor()

    for session in sessions:
        start = session.get("chargeStartDateTime")
        if not start:
            continue

        start_dt = dateparser.parse(start).astimezone(timezone.utc)

        cost_info = extract_cost(session)
        if not cost_info:
            continue

        cur.execute("""
            SELECT id, start_date, cost
            FROM charging_processes
            WHERE ABS(EXTRACT(EPOCH FROM (start_date - %s))) < %s
            LIMIT 1
        """, (start_dt, TIME_TOLERANCE_S))

        row = cur.fetchone()
        if not row:
            continue

        tm_id, tm_start, tm_cost = row

        if tm_cost and not OVERWRITE_EXISTING:
            continue

        currency_code = cost_info["currency"]

        converted = convert_currency(
            cost_info["total"],
            currency_code,
            TARGET_CURRENCY,
            start_dt
        )

        if dry_run:
            log.info(f"{cost_info['total']} {currency_code} -> {converted} {TARGET_CURRENCY}")
            continue

        cur.execute(
            "UPDATE charging_processes SET cost = %s WHERE id = %s",
            (converted, tm_id),
        )

        log.info(f"UPDATED {tm_id}: {converted} {TARGET_CURRENCY}")

    if not dry_run:
        conn.commit()

    cur.close()
    conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sessions = fetch_charging_sessions()
    import_to_teslamate(sessions, args.dry_run)


if __name__ == "__main__":
    main()
