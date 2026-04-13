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

log = setup_logging(LOG_FILE)


# ── Tesla API ─────────────────────────────────────────────────────────────────

OWNERSHIP_API_URL = "https://ownership.tesla.com/mobile-app/charging/history"


def fetch_charging_sessions() -> list[dict]:
    """
    Authenticate with the Tesla ownership API and return a list of
    charging sessions from the past LOOKBACK_DAYS days.
    """
    log.info(f"Connecting to Tesla API as {TESLA_EMAIL}")

    cache_path = Path(TESLA_CACHE_FILE)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    with teslapy.Tesla(TESLA_EMAIL, cache_file=str(cache_path)) as tesla:
        if not tesla.authorized:
            log.info("No cached token found — starting interactive authorisation")
            log.info("This only happens ONCE. The token is saved to cache afterwards.")
            refresh_token = _interactive_auth(tesla)
            tesla.refresh_token(refresh_token=refresh_token)

        vehicles = tesla.vehicle_list()
        if not vehicles:
            log.error("No vehicles found in your Tesla account.")
            sys.exit(1)

        vehicle = vehicles[0]
        vin = vehicle["vin"]
        log.info(f"Vehicle: {vehicle.get('display_name', 'N/A')} (VIN: {vin})")

        if len(vehicles) > 1:
            log.info(
                f"  Note: {len(vehicles)} vehicles found. Using the first one. "
                f"Set TESLA_VIN to select a specific vehicle."
            )

        # Allow overriding VIN via env var for multi-car households
        target_vin = _cfg("TESLA_VIN") or vin

        log.info(f"Fetching charging history for the last {LOOKBACK_DAYS} days...")
        try:
            response = tesla.get(OWNERSHIP_API_URL, params={
                "vin":           target_vin,
                "deviceLanguage": "en",
                "deviceCountry": "US",
                "operationName": "getChargingHistoryV2",
            })
        except Exception as e:
            log.error(f"Tesla API request failed: {e}")
            raise

        # Normalise varying response structures across API versions
        if isinstance(response, dict):
            sessions = (
                response.get("data") or
                response.get("response") or
                response.get("charging_history") or
                []
            )
        elif isinstance(response, list):
            sessions = response
        else:
            sessions = []

        log.info(f"Retrieved {len(sessions)} sessions from Tesla API")
        return sessions


def _interactive_auth(tesla: teslapy.Tesla) -> str:
    """
    Guide the user through one-time OAuth authorisation.
    Returns a refresh_token string.
    """
    print()
    print("=" * 65)
    print("  FIRST RUN — Tesla API authorisation required")
    print("=" * 65)
    print()
    print("  Option A (recommended): paste your refresh_token directly.")
    print("    Get it from: https://tesla-info.com/tesla-token.php")
    print("    or via the Tesla Fleet API developer portal.")
    print()
    print("  Option B: paste the full callback URL after logging in.")
    print("    Open this URL in your browser, log in, then paste")
    print("    the URL you were redirected to (starts with https://auth.tesla.com/void/).")
    print()

    value = input("  Paste refresh_token or callback URL: ").strip()

    if value.startswith("https://"):
        tesla.fetch_token(authorization_response=value)
        log.info("Token obtained from callback URL.")
        return tesla.token["refresh_token"]

    log.info("Token obtained from refresh_token input.")
    return value


# ── Cost extraction ───────────────────────────────────────────────────────────

def extract_cost(session: dict) -> dict | None:
    """
    Parse the fees array of a Tesla session and return cost details.

    Returns None for free Supercharging sessions or sessions with no fees.

    The Tesla API returns two fee types:
      - CHARGING:   energy cost (per kWh or per minute)
      - CONGESTION: idle / overstay fee after charging is complete

    Both are included in the total when applicable.
    """
    fees = session.get("fees") or []
    if not fees:
        return None

    charging_due   = 0.0
    congestion_due = 0.0
    currency       = None

    for fee in fees:
        currency = fee.get("currencyCode") or currency

        if fee.get("pricingType") == "NO_CHARGE":
            continue

        due      = float(fee.get("totalDue") or fee.get("calculatedDue") or 0)
        fee_type = fee.get("feeType", "")

        if fee_type == "CHARGING":
            charging_due = due
        elif fee_type == "CONGESTION":
            congestion_due = due

    total = round(charging_due + congestion_due, 2)
    if total == 0.0:
        return None  # Free Supercharging or genuinely zero charge

    return {
        "charging":   charging_due,
        "congestion": congestion_due,
        "total":      total,
        "currency":   currency or "?",
        "kwh":        _extract_kwh(fees),
        "rate":       _extract_rate(fees),
    }


def _extract_kwh(fees: list[dict]) -> float:
    for fee in fees:
        if fee.get("feeType") == "CHARGING" and fee.get("uom") == "kwh":
            return float(fee.get("usageBase") or 0)
    return 0.0


def _extract_rate(fees: list[dict]) -> float:
    for fee in fees:
        if fee.get("feeType") == "CHARGING":
            return float(fee.get("rateBase") or 0)
    return 0.0


# ── Database ──────────────────────────────────────────────────────────────────

def _get_db_connection():
    return psycopg2.connect(
        host=DB_HOST,
        port=int(DB_PORT),
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS,
        connect_timeout=10,
    )


def import_to_teslamate(sessions: list[dict], dry_run: bool) -> dict:
    """
    Match each Tesla session against TeslaMate's charging_processes table
    by start timestamp (within TIME_TOLERANCE_S seconds) and write the cost.

    Returns a summary dict with counts of outcomes.
    """
    stats = {
        "total":        len(sessions),
        "no_cost":      0,
        "too_old":      0,
        "not_found":    0,
        "already_set":  0,
        "updated":      0,
        "would_update": 0,
        "errors":       0,
    }

    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    conn = _get_db_connection()
    cur  = conn.cursor()

    for session in sessions:
        start_str = session.get("chargeStartDateTime")
        if not start_str:
            continue

        try:
            start_dt = dateparser.parse(start_str).astimezone(timezone.utc)
        except Exception:
            log.warning(f"Cannot parse date: {start_str!r}")
            continue

        if start_dt < cutoff:
            stats["too_old"] += 1
            continue

        location = (
            session.get("siteLocalizedName") or
            session.get("siteLocationName") or
            "unknown location"
        )

        cost_info = extract_cost(session)
        if cost_info is None:
            log.debug(f"  free/zero  {start_dt:%Y-%m-%d %H:%M}  {location}")
            stats["no_cost"] += 1
            continue

        # Find the closest matching record in TeslaMate
        try:
            cur.execute("""
                SELECT id, start_date, cost
                FROM   charging_processes
                WHERE  ABS(EXTRACT(EPOCH FROM (start_date - %s::timestamptz))) < %s
                ORDER BY ABS(EXTRACT(EPOCH FROM (start_date - %s::timestamptz)))
                LIMIT 1
            """, (start_dt, TIME_TOLERANCE_S, start_dt))
        except Exception as e:
            log.error(f"DB error during lookup: {e}")
            stats["errors"] += 1
            continue

        row = cur.fetchone()

        if row is None:
            log.warning(
                f"  NOT FOUND  {start_dt:%Y-%m-%d %H:%M}  {location}  "
                f"{cost_info['total']:.2f} {cost_info['currency']}"
            )
            stats["not_found"] += 1
            continue

        tm_id, tm_start, tm_cost = row

        if tm_cost is not None and not OVERWRITE_EXISTING:
            log.debug(
                f"  skipped    #{tm_id}  {tm_start:%Y-%m-%d %H:%M}  "
                f"{location}  (already set: {tm_cost:.2f})"
            )
            stats["already_set"] += 1
            continue

        # Build a human-readable detail string for the log line
        detail = f"{cost_info['charging']:.2f}"
        if cost_info["congestion"] > 0:
            detail += f" + idle: {cost_info['congestion']:.2f}"
        if cost_info["kwh"]:
            detail += f"  [{cost_info['kwh']:.3f} kWh @ {cost_info['rate']} {cost_info['currency']}/kWh]"

        if dry_run:
            log.info(
                f"  DRY-RUN    #{tm_id}  {tm_start:%Y-%m-%d %H:%M}  {location}  "
                f"→ {cost_info['total']:.2f} {cost_info['currency']}  ({detail})"
            )
            stats["would_update"] += 1
            continue

        try:
            cur.execute(
                "UPDATE charging_processes SET cost = %s WHERE id = %s",
                (cost_info["total"], tm_id),
            )
            log.info(
                f"  UPDATED    #{tm_id}  {tm_start:%Y-%m-%d %H:%M}  {location}  "
                f"→ {cost_info['total']:.2f} {cost_info['currency']}  ({detail})"
            )
            stats["updated"] += 1
        except Exception as e:
            log.error(f"DB error updating #{tm_id}: {e}")
            conn.rollback()
            stats["errors"] += 1

    if not dry_run:
        conn.commit()

    cur.close()
    conn.close()
    return stats


# ── Summary ───────────────────────────────────────────────────────────────────

def log_summary(stats: dict, dry_run: bool) -> None:
    mode = "DRY-RUN SUMMARY" if dry_run else "SUMMARY"
    log.info("─" * 55)
    log.info(f"  {mode}")
    log.info(f"  Sessions from Tesla API:   {stats['total']}")
    log.info(f"  Free / zero cost:          {stats['no_cost']}")
    log.info(f"  Older than lookback window:{stats['too_old']}")
    log.info(f"  Not found in TeslaMate:    {stats['not_found']}")
    log.info(f"  Already had cost (skipped):{stats['already_set']}")
    if dry_run:
        log.info(f"  Would be updated:          {stats['would_update']}")
    else:
        log.info(f"  Updated:                   {stats['updated']}")
    if stats["errors"]:
        log.warning(f"  Errors:                    {stats['errors']}")
    log.info("─" * 55)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    global LOOKBACK_DAYS
    parser = argparse.ArgumentParser(
        description="Import real Supercharger costs from Tesla API into TeslaMate",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run — show what would change without writing anything
  python importer.py --dry-run

  # Import from a saved JSON file (useful for testing)
  python importer.py --input history.json --dry-run

  # Historical import of the last 365 days
  python importer.py --lookback 365
        """,
    )
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Preview changes without writing to the database")
    parser.add_argument("--input", "-i", metavar="FILE",
                        help="Load sessions from a JSON file instead of the Tesla API")
    parser.add_argument("--lookback", type=int, metavar="DAYS",
                        help=f"How many days back to process (default: {LOOKBACK_DAYS})")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show debug messages (skipped/free sessions)")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.lookback:
        LOOKBACK_DAYS = args.lookback

    log.info("=" * 55)
    log.info("  TeslaMate Supercharger Cost Importer")
    log.info(f"  DB:        {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
    log.info(f"  Lookback:  {LOOKBACK_DAYS} days  |  Tolerance: {TIME_TOLERANCE_S}s")
    log.info(f"  Overwrite: {OVERWRITE_EXISTING}")
    if args.dry_run:
        log.info("  Mode:      DRY-RUN (no writes)")
    log.info("=" * 55)

    # Load sessions
    if args.input:
        log.info(f"Loading sessions from file: {args.input}")
        with open(args.input) as f:
            data = json.load(f)
        sessions = data["data"] if isinstance(data, dict) and "data" in data else data
        log.info(f"Loaded {len(sessions)} sessions")
    else:
        sessions = fetch_charging_sessions()

    if not sessions:
        log.warning("No sessions to process.")
        return

    stats = import_to_teslamate(sessions, args.dry_run)
    log_summary(stats, args.dry_run)


if __name__ == "__main__":
    main()
