#!/usr/bin/env python3
"""
teslamate-supercharger-costs
============================
Fetches real Supercharger session costs from the Tesla ownership API
and writes them into TeslaMate's PostgreSQL database automatically.

Optionally converts all costs to a target currency using live exchange
rates from the European Central Bank (no API key required).

Configuration is done via environment variables or a .env file.
See .env.example for all available options.
"""

import os
import sys
import time
import json
import logging
import argparse
import urllib.request
import xml.etree.ElementTree as ET
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

    formatter = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Use server local time for log timestamps rather than UTC (logging's default)
    formatter.converter = time.localtime

    for handler in handlers:
        handler.setFormatter(formatter)

    logging.basicConfig(level=logging.INFO, handlers=handlers)
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
TESLA_VIN          = _cfg("TESLA_VIN")  # FIX: read at module level so it's always available

DB_HOST            = _cfg("TESLAMATE_DB_HOST",  "database")
DB_PORT            = _cfg("TESLAMATE_DB_PORT",  "5432")
DB_NAME            = _cfg("TESLAMATE_DB_NAME",  "teslamate")
DB_USER            = _cfg("TESLAMATE_DB_USER",  "teslamate")
DB_PASS            = _cfg("TESLAMATE_DB_PASS",  required=True)

LOOKBACK_DAYS      = int(_cfg("LOOKBACK_DAYS",      "9999"))
TIME_TOLERANCE_S   = int(_cfg("TIME_TOLERANCE_S",   "120"))
OVERWRITE_EXISTING = _cfg("OVERWRITE_EXISTING", "false").lower() == "true"
LOG_FILE           = _cfg("LOG_FILE",           "/logs/importer.log")

# Tesla API request timeout in seconds. Without this the API call can hang forever.
TESLA_API_TIMEOUT  = int(_cfg("TESLA_API_TIMEOUT", "30"))

# Currency conversion — set TARGET_CURRENCY to convert all costs to one currency.
# Uses live rates from the European Central Bank (updated daily, no API key needed).
# Leave empty to store costs in their original currency (no conversion).
TARGET_CURRENCY    = (_cfg("TARGET_CURRENCY", "") or "").upper().strip()

log = setup_logging(LOG_FILE)


# ── Currency conversion (ECB) ─────────────────────────────────────────────────

# ECB publishes daily rates vs EUR. We fetch once per run and cache in memory.
_ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
_fx_rates: dict[str, float] = {}   # currency -> rate vs EUR (e.g. CZK: 25.3)
_fx_date:  str = ""


def load_ecb_rates() -> None:
    """
    Fetch today's exchange rates from the European Central Bank.
    Rates are vs EUR (e.g. USD: 1.08 means 1 EUR = 1.08 USD).
    EUR itself is always 1.0. CHF, CZK, GBP etc. are all included.
    """
    global _fx_rates, _fx_date

    log.info("Fetching exchange rates from European Central Bank...")
    try:
        with urllib.request.urlopen(_ECB_URL, timeout=10) as resp:
            xml_data = resp.read()
    except Exception as e:
        log.error(f"Failed to fetch ECB rates: {e}")
        log.error("Costs will be stored in their original currency (no conversion).")
        return

    try:
        root = ET.fromstring(xml_data)
        ns = {"ecb": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}

        cube_time = root.find(".//ecb:Cube/ecb:Cube[@time]", ns)
        if cube_time is not None:
            _fx_date = cube_time.attrib.get("time", "")

        _fx_rates["EUR"] = 1.0  # base currency
        for cube in root.findall(".//ecb:Cube[@currency]", ns):
            currency = cube.attrib["currency"]
            rate     = float(cube.attrib["rate"])
            _fx_rates[currency] = rate

    except Exception as e:
        log.error(f"Failed to parse ECB rates: {e}")
        return

    log.info(f"  ECB rates loaded ({_fx_date}): {len(_fx_rates)} currencies")
    if TARGET_CURRENCY in _fx_rates:
        log.info(f"  Target currency: {TARGET_CURRENCY} "
                 f"(1 EUR = {_fx_rates[TARGET_CURRENCY]} {TARGET_CURRENCY})")
    elif TARGET_CURRENCY:
        log.warning(f"  Target currency '{TARGET_CURRENCY}' not found in ECB rates! "
                    f"No conversion will be applied.")


def convert_currency(amount: float, from_currency: str) -> tuple[float, str]:
    """
    Convert amount from from_currency to TARGET_CURRENCY.

    ECB rates are all vs EUR, so:
      amount_in_EUR = amount / rate[from_currency]
      result        = amount_in_EUR * rate[TARGET_CURRENCY]

    Returns (converted_amount, TARGET_CURRENCY).
    If conversion is not possible, returns the original amount and currency.
    """
    if not TARGET_CURRENCY:
        return amount, from_currency

    if from_currency == TARGET_CURRENCY:
        return round(amount, 4), TARGET_CURRENCY

    if not _fx_rates:
        return amount, from_currency

    if from_currency not in _fx_rates:
        log.warning(f"  Currency '{from_currency}' not in ECB rates — skipping conversion")
        return amount, from_currency

    if TARGET_CURRENCY not in _fx_rates:
        return amount, from_currency

    amount_in_eur = amount / _fx_rates[from_currency]
    converted     = amount_in_eur * _fx_rates[TARGET_CURRENCY]
    return round(converted, 4), TARGET_CURRENCY


# ── Tesla API ─────────────────────────────────────────────────────────────────

OWNERSHIP_API_URL = "https://ownership.tesla.com/mobile-app/charging/history"


def fetch_charging_sessions(lookback_days: int) -> list[dict]:
    """
    Authenticate with the Tesla ownership API and return all charging
    sessions within the lookback_days window.

    The Tesla API returns at most 25 sessions per page, so we paginate
    through all pages until we either run out of results or reach sessions
    older than lookback_days.
    """
    log.info(f"Connecting to Tesla API as {TESLA_EMAIL}")

    cache_path = Path(TESLA_CACHE_FILE)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    with teslapy.Tesla(TESLA_EMAIL, cache_file=str(cache_path)) as tesla:
        # FIX: set a timeout so the API call cannot hang forever
        tesla.timeout = TESLA_API_TIMEOUT

        if not tesla.authorized:
            log.info("No cached token found — starting interactive authorisation")
            log.info("This only happens ONCE. The token is saved to cache afterwards.")
            refresh_token = _interactive_auth(tesla)
            tesla.refresh_token(refresh_token=refresh_token)

        vehicles = tesla.vehicle_list()
        if not vehicles:
            log.error("No vehicles found in your Tesla account.")
            sys.exit(1)

        # FIX: honour TESLA_VIN to select the correct vehicle; validate it exists
        target_vin = TESLA_VIN or vehicles[0]["vin"]
        vehicle = next((v for v in vehicles if v["vin"] == target_vin), None)
        if vehicle is None:
            log.error(
                f"VIN '{target_vin}' not found in your Tesla account. "
                f"Available VINs: {[v['vin'] for v in vehicles]}"
            )
            sys.exit(1)

        log.info(f"Vehicle: {vehicle.get('display_name', 'N/A')} (VIN: {target_vin})")

        if len(vehicles) > 1 and not TESLA_VIN:
            log.info(
                f"  Note: {len(vehicles)} vehicles found. Using the first one. "
                f"Set TESLA_VIN to select a specific vehicle."
            )

        log.info(
            f"Fetching charging history for the last {lookback_days} days "
            f"(since {cutoff:%Y-%m-%d})..."
        )

        all_sessions: list[dict] = []
        page = 1
        page_size = 25  # Tesla API maximum per page
        last_page_first_session: str | None = None  # FIX: track to detect repeated pages

        while True:
            try:
                # teslapy returns a JsonDict (already parsed) -- NOT a requests.Response.
                # Do NOT call .raise_for_status() or .json() on the result.
                response = tesla.get(OWNERSHIP_API_URL, params={
                    "vin":            target_vin,
                    "deviceLanguage": "en",
                    "deviceCountry":  "US",
                    "operationName":  "getChargingHistoryV2",
                    "pageNo":         page,
                    "pageSize":       page_size,
                })
            except Exception as e:
                log.error(f"Tesla API request failed (page {page}): {e}")
                break

            # Normalise varying response structures across API versions
            if isinstance(response, dict):
                page_sessions = (
                    response.get("data") or
                    response.get("response") or
                    response.get("charging_history") or
                    []
                )
                # warn when none of the known keys matched so API changes are visible
                if not page_sessions and response:
                    log.debug(
                        f"  Page {page}: response had unexpected structure. "
                        f"Top-level keys: {list(response.keys())}"
                    )
            elif isinstance(response, list):
                page_sessions = response
            else:
                page_sessions = []

            if not page_sessions:
                log.debug(f"  Page {page}: empty -- stopping pagination")
                break

            # FIX: detect repeated pages — some Tesla API versions return the last
            # real page over and over instead of returning an empty page to signal
            # the end, causing the loop to run forever and accumulate duplicates.
            first_session_ts = page_sessions[0].get("chargeStartDateTime")
            if first_session_ts and first_session_ts == last_page_first_session:
                log.debug(f"  Page {page}: identical to previous page — stopping pagination")
                break
            last_page_first_session = first_session_ts

            log.debug(f"  Page {page}: {len(page_sessions)} sessions")

            # Sessions are returned newest-first; last item on the page is oldest.
            # If oldest is beyond our cutoff, we can stop after adding this page.
            oldest_str = page_sessions[-1].get("chargeStartDateTime", "")
            reached_cutoff = False
            if oldest_str:
                try:
                    oldest_dt = dateparser.parse(oldest_str).astimezone(timezone.utc)
                    if oldest_dt < cutoff:
                        reached_cutoff = True
                except Exception:
                    pass

            all_sessions.extend(page_sessions)

            if reached_cutoff:
                log.debug(f"  Page {page}: reached lookback cutoff -- stopping")
                break

            # Fewer results than page_size means this was the last page
            if len(page_sessions) < page_size:
                log.debug(f"  Page {page}: last page ({len(page_sessions)} results)")
                break

            page += 1

        log.info(
            f"Retrieved {len(all_sessions)} sessions from Tesla API "
            f"({page} page(s) fetched)"
        )
        return all_sessions


def _interactive_auth(tesla: teslapy.Tesla) -> str:
    """
    Guide the user through one-time OAuth authorisation.
    Returns a refresh_token string.
    """
    print()
    print("=" * 65)
    print("  FIRST RUN - Tesla API authorisation required")
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

    Both are included in the total. If TARGET_CURRENCY is set, the total
    is converted using live ECB rates.
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

    original_total    = round(charging_due + congestion_due, 2)
    original_currency = currency or "?"

    if original_total == 0.0:
        return None  # Free Supercharging or genuinely zero charge

    # Convert to target currency if configured
    converted_total, final_currency = convert_currency(original_total, original_currency)

    return {
        "charging":          charging_due,
        "congestion":        congestion_due,
        "original_total":    original_total,
        "original_currency": original_currency,
        "total":             converted_total,
        "currency":          final_currency,
        "converted":         final_currency != original_currency,
        "kwh":               _extract_kwh(fees),
        "rate":              _extract_rate(fees),
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


def _detect_start_date_timezone(cur) -> str:
    """
    Detect how TeslaMate's charging_processes.start_date is stored and return
    the timezone string to use when comparing against UTC timestamps from the
    Tesla API.

    Two cases:
      - TIMESTAMP WITH TIME ZONE: PostgreSQL stores as UTC internally and
        handles conversion automatically. We use 'UTC' as a no-op cast since
        the comparison will already be timezone-aware and correct.
      - TIMESTAMP WITHOUT TIME ZONE: The column has no timezone metadata.
        TeslaMate always writes UTC values here regardless of the server's
        locale, so we cast with 'UTC' to tell PostgreSQL what the values mean.
        However, if the DB server timezone is already UTC, the cast is still
        correct. We detect this case and log it clearly so users can verify.

    In both cases the cast expression is `start_date AT TIME ZONE <tz>`, and
    in both cases the right answer for a standard TeslaMate installation is
    'UTC'. We detect and log the column type so that non-standard setups are
    visible and easy to override via the TESLAMATE_DB_TIMEZONE env var.
    """
    # Allow explicit override via environment variable
    override = (_cfg("TESLAMATE_DB_TIMEZONE") or "").strip()
    if override:
        log.info(f"  DB timezone: using override '{override}' (TESLAMATE_DB_TIMEZONE)")
        return override

    # Detect the column data type
    cur.execute("""
        SELECT data_type
        FROM   information_schema.columns
        WHERE  table_name  = 'charging_processes'
          AND  column_name = 'start_date'
    """)
    row = cur.fetchone()
    col_type = row[0] if row else "unknown"

    # Also read the database server's timezone setting
    cur.execute("SHOW TimeZone")
    db_tz = cur.fetchone()[0]

    if col_type == "timestamp with time zone":
        # Timezone-aware column: PostgreSQL handles UTC conversion automatically.
        # The AT TIME ZONE cast is a no-op here but keeps the query consistent.
        log.info(f"  DB timezone: column is timestamptz — comparisons are automatic (DB tz: {db_tz})")
        return "UTC"
    else:
        # Timezone-naive column (standard TeslaMate schema).
        # TeslaMate always writes UTC into this column, so we tell PostgreSQL
        # to interpret the stored values as UTC regardless of the server's tz.
        if db_tz.upper() not in ("UTC", "ETC/UTC", "GMT"):
            log.warning(
                f"  DB timezone: column is timestamp (no tz), DB server tz is '{db_tz}'. "
                f"TeslaMate stores UTC in this column, so casting as UTC. "
                f"If matches are wrong, set TESLAMATE_DB_TIMEZONE in your .env."
            )
        else:
            log.info(f"  DB timezone: column is timestamp (no tz), DB server tz is UTC — OK")
        return "UTC"


def import_to_teslamate(sessions: list[dict], dry_run: bool, lookback_days: int) -> dict:
    """
    Match each Tesla session against TeslaMate's charging_processes table
    by start timestamp (within TIME_TOLERANCE_S seconds) and write the cost.

    Each successful UPDATE is committed immediately so that a DB error on one
    row does not roll back previously written costs.

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

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    # use try/finally to guarantee the connection is always closed
    conn = _get_db_connection()
    try:
        cur = conn.cursor()

        # Detect how start_date is stored so the timestamp comparison is correct
        # regardless of the DB server's timezone setting or column type.
        db_tz = _detect_start_date_timezone(cur)

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

            # Find the closest matching record in TeslaMate.
            # start_date is cast using the detected timezone so that the
            # comparison against the Tesla API's UTC timestamp is always correct,
            # regardless of whether the column is timestamptz or plain timestamp,
            # and regardless of the DB server's locale/timezone setting.
            try:
                cur.execute(f"""
                    SELECT id, start_date, cost
                    FROM   charging_processes
                    WHERE  ABS(EXTRACT(EPOCH FROM (
                               (start_date AT TIME ZONE %s) - %s::timestamptz
                           ))) < %s
                    ORDER BY ABS(EXTRACT(EPOCH FROM (
                               (start_date AT TIME ZONE %s) - %s::timestamptz
                           )))
                    LIMIT 1
                """, (db_tz, start_dt, TIME_TOLERANCE_S, db_tz, start_dt))
            except Exception as e:
                log.error(f"DB error during lookup: {e}")
                conn.rollback()
                stats["errors"] += 1
                continue

            row = cur.fetchone()

            if row is None:
                log.warning(
                    f"  NOT FOUND  {start_dt:%Y-%m-%d %H:%M}  {location}  "
                    f"{cost_info['original_total']:.2f} {cost_info['original_currency']}"
                )
                stats["not_found"] += 1
                continue

            tm_id, tm_start, tm_cost = row

            if tm_cost is not None and not OVERWRITE_EXISTING:
                log.debug(
                    f"  skipped    #{tm_id}  {tm_start:%Y-%m-%d %H:%M}  "
                    f"{location}  (already set: {tm_cost:.4f})"
                )
                stats["already_set"] += 1
                continue

            # Build log line
            kwh_str = (f"  [{cost_info['kwh']:.3f} kWh @ "
                       f"{cost_info['rate']} {cost_info['original_currency']}/kWh]"
                       if cost_info["kwh"] else "")

            if cost_info["converted"]:
                cost_str = (f"{cost_info['original_total']:.2f} {cost_info['original_currency']}"
                            f" -> {cost_info['total']:.4f} {cost_info['currency']}")
            else:
                cost_str = f"{cost_info['total']:.2f} {cost_info['currency']}"

            if cost_info["congestion"] > 0:
                cost_str += f"  (charging: {cost_info['charging']:.2f} + idle: {cost_info['congestion']:.2f})"

            if dry_run:
                log.info(f"  DRY-RUN    #{tm_id}  {tm_start:%Y-%m-%d %H:%M}  "
                         f"{location}  {cost_str}{kwh_str}")
                stats["would_update"] += 1
                continue

            try:
                cur.execute(
                    "UPDATE charging_processes SET cost = %s WHERE id = %s",
                    (cost_info["total"], tm_id),
                )
                # FIX: commit after each successful update so a later DB error
                # cannot roll back costs that have already been written
                conn.commit()
                log.info(f"  UPDATED    #{tm_id}  {tm_start:%Y-%m-%d %H:%M}  "
                         f"{location}  {cost_str}{kwh_str}")
                stats["updated"] += 1
            except Exception as e:
                log.error(f"DB error updating #{tm_id}: {e}")
                conn.rollback()
                stats["errors"] += 1

        cur.close()
    finally:
        conn.close()

    return stats


# ── Summary ───────────────────────────────────────────────────────────────────

def log_summary(stats: dict, dry_run: bool) -> None:
    mode = "DRY-RUN SUMMARY" if dry_run else "SUMMARY"
    log.info("-" * 55)
    log.info(f"  {mode}")
    log.info(f"  Sessions from Tesla API:    {stats['total']}")
    log.info(f"  Free / zero cost:           {stats['no_cost']}")
    log.info(f"  Older than lookback window: {stats['too_old']}")
    log.info(f"  Not found in TeslaMate:     {stats['not_found']}")
    log.info(f"  Already had cost (skipped): {stats['already_set']}")
    if dry_run:
        log.info(f"  Would be updated:           {stats['would_update']}")
    else:
        log.info(f"  Updated:                    {stats['updated']}")
    if stats["errors"]:
        log.warning(f"  Errors:                     {stats['errors']}")
    log.info("-" * 55)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    # FIX: use a local variable instead of mutating the global LOOKBACK_DAYS,
    # then pass it explicitly to functions that need it
    lookback_days = LOOKBACK_DAYS

    parser = argparse.ArgumentParser(
        description="Import real Supercharger costs from Tesla API into TeslaMate",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run - show what would change without writing anything
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
                        help=f"How many days back to process (default: {lookback_days})")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show debug messages (skipped/free sessions)")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.lookback:
        lookback_days = args.lookback

    log.info("=" * 55)
    log.info("  TeslaMate Supercharger Cost Importer")
    log.info(f"  DB:        {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
    log.info(f"  Lookback:  {lookback_days} days  |  Tolerance: {TIME_TOLERANCE_S}s")
    log.info(f"  Overwrite: {OVERWRITE_EXISTING}")
    if TARGET_CURRENCY:
        log.info(f"  Currency:  converting all costs -> {TARGET_CURRENCY} (ECB live rates)")
    else:
        log.info(f"  Currency:  storing in original currency (no conversion)")
    if args.dry_run:
        log.info("  Mode:      DRY-RUN (no writes)")
    log.info("=" * 55)

    # Load ECB exchange rates if currency conversion is enabled
    if TARGET_CURRENCY:
        load_ecb_rates()

    # Load sessions
    if args.input:
        log.info(f"Loading sessions from file: {args.input}")
        with open(args.input) as f:
            data = json.load(f)
        sessions = data["data"] if isinstance(data, dict) and "data" in data else data
        log.info(f"Loaded {len(sessions)} sessions")
    else:
        sessions = fetch_charging_sessions(lookback_days)

    if not sessions:
        log.warning("No sessions to process.")
        return

    stats = import_to_teslamate(sessions, args.dry_run, lookback_days)
    log_summary(stats, args.dry_run)


if __name__ == "__main__":
    main()
