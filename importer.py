#!/usr/bin/env python3
"""
teslamate-supercharger-costs
============================
Fetches real Supercharger session costs from Tesla (official Fleet API,
or the legacy ownership API) and writes them into TeslaMate's PostgreSQL
database automatically.

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
from contextlib import contextmanager
from urllib.parse import parse_qs, urlencode, urlparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import psycopg2
    import requests
    from dateutil import parser as dateparser
except ImportError as e:
    print(f"ERROR: Missing dependency: {e}")
    print("Install with: pip install -r requirements.txt")
    sys.exit(1)

try:
    import fcntl  # file lock for the token (not available on Windows)
except ImportError:
    fcntl = None

try:
    import teslapy  # only needed for the legacy ownership API mode
except ImportError:
    teslapy = None


# ── Logging ──────────────────────────────────────────────────────────────────

log = logging.getLogger("teslamate_suc")


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

    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    return log


# ── Configuration ─────────────────────────────────────────────────────────────
#
# Settings come from environment variables. Settings saved in the web UI
# (DATA_DIR/settings.json) take precedence over them, so the web UI and the
# CLI / cron job always use the same values.

class ConfigError(Exception):
    """Invalid or missing configuration."""


class ImporterError(Exception):
    """A run cannot continue (Tesla API or account problem)."""


DATA_DIR      = os.getenv("DATA_DIR", "/data")
SETTINGS_FILE = os.getenv("SETTINGS_FILE", os.path.join(DATA_DIR, "settings.json"))
LAST_RUN_FILE = os.getenv("LAST_RUN_FILE", os.path.join(DATA_DIR, "last_run.json"))

# Settings that can be changed in the web UI (stored in SETTINGS_FILE).
WEB_SETTINGS = (
    "TARGET_CURRENCY", "LOOKBACK_DAYS", "TIME_TOLERANCE_S", "TESLA_VIN",
    "TESLA_FLEET_REGION", "SCHEDULE_TIME", "TIMEZONE",
)

# Set by main(): the CLI may ask for the Tesla login on the terminal.
INTERACTIVE = False


def load_web_settings(path: str | None = None) -> dict:
    try:
        with open(path or SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if k in WEB_SETTINGS} if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning(f"Cannot read {path or SETTINGS_FILE}: {e}")
        return {}


def save_web_settings(settings: dict, path: str | None = None) -> None:
    path = Path(path or SETTINGS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in settings.items() if k in WEB_SETTINGS}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def configure(overrides: dict | None = None, environ=None, use_file: bool = True) -> None:
    """
    (Re)load the configuration into the module-level settings below.

    Order of precedence: *overrides* > settings.json (web UI) > environment.
    Raises ConfigError for invalid values; nothing is changed in that case.
    """
    values = {k: v for k, v in (os.environ if environ is None else environ).items()}
    if use_file:
        values.update({k: str(v) for k, v in load_web_settings().items() if v is not None})
    values.update({k: str(v) for k, v in (overrides or {}).items() if v is not None})

    def get(key, default=None):
        v = values.get(key)
        return v.strip() if isinstance(v, str) and v.strip() else default

    def integer(key, default):
        try:
            return int(get(key, default))
        except ValueError:
            raise ConfigError(f"{key} must be a whole number, got '{get(key)}'")

    # Which Tesla API to use:
    #   fleet     — official Tesla Fleet API (recommended, needs a developer app)
    #   ownership — legacy unofficial endpoint used by the mobile app
    # Defaults to "fleet" when TESLA_CLIENT_ID is set, otherwise "ownership".
    mode = (get("TESLA_API_MODE") or ("fleet" if get("TESLA_CLIENT_ID") else "ownership")).lower()
    if mode not in ("fleet", "ownership"):
        raise ConfigError(f"TESLA_API_MODE must be 'fleet' or 'ownership', got '{mode}'")

    region = (get("TESLA_FLEET_REGION", "eu")).lower()
    if region not in FLEET_API_URLS and not get("TESLA_FLEET_API_URL"):
        raise ConfigError(f"Unknown TESLA_FLEET_REGION '{region}'. Use one of: {list(FLEET_API_URLS)}")

    schedule = get("SCHEDULE_TIME", "03:00")
    if schedule.lower() in ("off", "none", "-"):
        schedule = ""
    if schedule:
        try:
            datetime.strptime(schedule, "%H:%M")
        except ValueError:
            raise ConfigError(f"SCHEDULE_TIME must be HH:MM (or 'off'), got '{schedule}'")

    # "all" / "none" let the web UI override a value set in the environment.
    vin = get("TESLA_VIN", "")
    if vin.lower() == "all":
        vin = ""
    currency = get("TARGET_CURRENCY", "").upper()
    if currency in ("NONE", "-"):
        currency = ""
    new = {
        "TESLA_API_MODE":         mode,
        "TESLA_EMAIL":            get("TESLA_EMAIL"),
        "TESLA_CACHE_FILE":       get("TESLA_CACHE_FILE", os.path.join(DATA_DIR, "tesla_cache.json")),
        # Optional: limit the import to specific vehicles (comma-separated VINs).
        # Empty = all vehicles on the account.
        "TESLA_VIN":              vin,
        "TESLA_VINS":             [v.strip().upper() for v in vin.split(",") if v.strip()],
        "TESLA_CLIENT_ID":        get("TESLA_CLIENT_ID"),
        "TESLA_CLIENT_SECRET":    get("TESLA_CLIENT_SECRET"),
        "TESLA_REDIRECT_URI":     get("TESLA_REDIRECT_URI", "https://localhost/callback"),
        "TESLA_FLEET_REGION":     region,
        "TESLA_FLEET_API_URL":    get("TESLA_FLEET_API_URL"),
        "TESLA_FLEET_TOKEN_FILE": get("TESLA_FLEET_TOKEN_FILE", os.path.join(DATA_DIR, "fleet_token.json")),
        "FLEET_TOKEN_URL":        get("TESLA_FLEET_TOKEN_URL",
                                      "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"),
        "DB_HOST":                get("TESLAMATE_DB_HOST", "database"),
        "DB_PORT":                get("TESLAMATE_DB_PORT", "5432"),
        "DB_NAME":                get("TESLAMATE_DB_NAME", "teslamate"),
        "DB_USER":                get("TESLAMATE_DB_USER", "teslamate"),
        "DB_PASS":                get("TESLAMATE_DB_PASS"),
        "DB_TIMEZONE_OVERRIDE":   get("TESLAMATE_DB_TIMEZONE", ""),
        "LOOKBACK_DAYS":          integer("LOOKBACK_DAYS", "9999"),
        "TIME_TOLERANCE_S":       integer("TIME_TOLERANCE_S", "120"),
        "OVERWRITE_EXISTING":     get("OVERWRITE_EXISTING", "false").lower() == "true",
        "LOG_FILE":               get("LOG_FILE", "/logs/importer.log"),
        # Tesla API request timeout in seconds. Without this the API call can hang forever.
        "TESLA_API_TIMEOUT":      integer("TESLA_API_TIMEOUT", "30"),
        # Currency conversion — convert all costs to one currency using live ECB
        # rates (no API key needed). Empty = store costs in their original currency.
        "TARGET_CURRENCY":        currency,
        # Web UI: daily run at this local time (HH:MM), empty = no automatic runs.
        "SCHEDULE_TIME":          schedule,
        "TIMEZONE":               get("TIMEZONE") or get("TZ") or "UTC",
    }
    if new["TARGET_CURRENCY"] and (len(new["TARGET_CURRENCY"]) != 3 or not new["TARGET_CURRENCY"].isalpha()):
        raise ConfigError(f"TARGET_CURRENCY must be a 3-letter code, got '{new['TARGET_CURRENCY']}'")
    globals().update(new)


def validate_config() -> None:
    """Check the settings a real run needs."""
    if not DB_PASS:
        raise ConfigError("Required environment variable not set: TESLAMATE_DB_PASS")
    if TESLA_API_MODE == "ownership" and not TESLA_EMAIL:
        raise ConfigError("Required environment variable not set: TESLA_EMAIL "
                          "(or set TESLA_CLIENT_ID to use the Fleet API)")
    if TESLA_API_MODE == "fleet" and not TESLA_CLIENT_ID:
        raise ConfigError("TESLA_CLIENT_ID is required for TESLA_API_MODE=fleet.")


FLEET_API_URLS = {
    "na": "https://fleet-api.prd.na.vn.cloud.tesla.com",
    "eu": "https://fleet-api.prd.eu.vn.cloud.tesla.com",
    "cn": "https://fleet-api.prd.cn.vn.cloud.tesla.cn",
}

# Defaults until configure() is called with the real environment (main / web).
configure(environ={}, use_file=False)


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


# ── Tesla API (shared helpers) ────────────────────────────────────────────────

PAGE_SIZE = 25  # Tesla charging history maximum per page


def _select_vehicles(vehicles: list[dict]) -> list[dict]:
    """
    Pick the vehicles to import. With TESLA_VIN set, only those VINs
    (comma-separated) are used; otherwise every vehicle on the account.
    """
    if not vehicles:
        raise ImporterError("No vehicles found in your Tesla account.")

    available = [v.get("vin") for v in vehicles]
    if not TESLA_VINS:
        selected = vehicles
    else:
        missing = [vin for vin in TESLA_VINS if vin not in available]
        if missing:
            raise ImporterError(f"VIN(s) {missing} not found in your Tesla account. "
                                f"Available VINs: {available}")
        selected = [v for v in vehicles if v.get("vin") in TESLA_VINS]

    for v in selected:
        log.info(f"Vehicle: {v.get('display_name') or 'N/A'} (VIN: {v.get('vin')})")
    if len(vehicles) > 1 and not TESLA_VINS:
        log.info(f"  {len(vehicles)} vehicles found, importing all of them. "
                 f"Set TESLA_VIN to limit the import (comma-separated).")
    return selected


def _extract_page_sessions(response, page: int) -> list[dict]:
    """Normalise the varying response structures across API versions."""
    if isinstance(response, list):
        return response
    if not isinstance(response, dict):
        return []

    inner = response.get("response")
    if isinstance(inner, dict):  # Fleet API sometimes wraps: {"response": {"data": [...]}}
        response = inner

    sessions = (
        response.get("data") or
        (inner if isinstance(inner, list) else None) or
        response.get("charging_history") or
        []
    )
    if not sessions and response:
        log.debug(f"  Page {page}: response had unexpected structure. "
                  f"Top-level keys: {list(response.keys())}")
    return sessions


def _paginate_history(fetch_page, cutoff: datetime) -> list[dict]:
    """
    Call fetch_page(page_no) until all sessions back to `cutoff` are collected.
    Sessions are returned newest-first, PAGE_SIZE per page.
    """
    all_sessions: list[dict] = []
    page = 1
    last_page_first_session: str | None = None

    while True:
        try:
            response = fetch_page(page)
        except Exception as e:
            if page == 1:
                raise  # nothing fetched — report the error instead of "0 sessions"
            log.error(f"Tesla API request failed (page {page}): {e}")
            break

        page_sessions = _extract_page_sessions(response, page)
        if not page_sessions:
            log.debug(f"  Page {page}: empty -- stopping pagination")
            break

        # Some API versions return the last real page over and over instead of
        # an empty page — stop when a page repeats to avoid an endless loop.
        first_session_ts = page_sessions[0].get("chargeStartDateTime")
        if first_session_ts and first_session_ts == last_page_first_session:
            log.debug(f"  Page {page}: identical to previous page — stopping pagination")
            break
        last_page_first_session = first_session_ts

        log.debug(f"  Page {page}: {len(page_sessions)} sessions")
        all_sessions.extend(page_sessions)

        oldest_str = page_sessions[-1].get("chargeStartDateTime", "")
        if oldest_str:
            try:
                if dateparser.parse(oldest_str).astimezone(timezone.utc) < cutoff:
                    log.debug(f"  Page {page}: reached lookback cutoff -- stopping")
                    break
            except Exception:
                pass

        if len(page_sessions) < PAGE_SIZE:
            log.debug(f"  Page {page}: last page ({len(page_sessions)} results)")
            break

        page += 1

    log.info(f"  Retrieved {len(all_sessions)} sessions ({page} page(s) fetched)")
    return all_sessions


def fetch_charging_sessions(lookback_days: int) -> list[dict]:
    """Return charging sessions for all selected vehicles, using the configured API."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    if TESLA_API_MODE == "fleet":
        return _fetch_fleet(cutoff, lookback_days)
    return _fetch_ownership(cutoff, lookback_days)


# ── Tesla Fleet API (official) ────────────────────────────────────────────────

FLEET_AUTH_URL  = "https://auth.tesla.com/oauth2/v3/authorize"
FLEET_SCOPES    = "openid offline_access vehicle_device_data vehicle_charging_cmds"


class FleetApiError(ImporterError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class NotLoggedIn(FleetApiError):
    """No Fleet API token yet — the one-time login is needed."""


class FleetClient:
    """
    Minimal Tesla Fleet API client: OAuth token handling + GET requests.

    Tesla rotates refresh tokens — every refresh returns a new one that must
    be saved, otherwise the next run cannot log in. The token file is
    therefore rewritten after each refresh, under a file lock so that the web
    UI and a cron run never refresh at the same time.
    """

    def __init__(self) -> None:
        if not TESLA_CLIENT_ID:
            raise ConfigError("TESLA_CLIENT_ID is required for TESLA_API_MODE=fleet.")
        self.base_url = (TESLA_FLEET_API_URL or FLEET_API_URLS[TESLA_FLEET_REGION]).rstrip("/")
        self.token_path = Path(TESLA_FLEET_TOKEN_FILE)
        self.http = requests.Session()
        self.token: dict = self._load_token()

    # -- token storage --------------------------------------------------------

    def _load_token(self) -> dict:
        try:
            return json.loads(self.token_path.read_text())
        except FileNotFoundError:
            return {}
        except Exception as e:
            log.warning(f"Cannot read token file {self.token_path}: {e}")
            return {}

    def _save_token(self, data: dict, replace: bool = False) -> None:
        token = {} if replace else dict(self.token)
        token.update({k: v for k, v in data.items() if v})
        if "expires_in" in data:
            token["expires_at"] = int(time.time()) + int(data["expires_in"])
        self.token = token
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(token, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.token_path)

    @contextmanager
    def _locked(self):
        """Exclusive lock around token refreshes (shared by all processes)."""
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.token_path) + ".lock", "w") as lock:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def _token_request(self, data: dict) -> dict:
        try:
            r = self.http.post(FLEET_TOKEN_URL, data=data, timeout=TESLA_API_TIMEOUT)
        except requests.RequestException as e:
            raise FleetApiError(f"Token request failed: {e}") from e
        if r.status_code != 200:
            raise FleetApiError(f"Token request failed: HTTP {r.status_code} {r.text[:300]}",
                                r.status_code)
        return r.json()

    # -- authentication -------------------------------------------------------

    @property
    def logged_in(self) -> bool:
        return bool(self.token.get("refresh_token"))

    def status(self) -> dict:
        """Token state for display (never contains the tokens themselves)."""
        self.token = self._load_token()
        expires_at = self.token.get("expires_at")
        return {
            "logged_in":  self.logged_in,
            "expires_at": datetime.fromtimestamp(expires_at, timezone.utc) if expires_at else None,
            "updated_at": (datetime.fromtimestamp(self.token_path.stat().st_mtime, timezone.utc)
                           if self.token_path.exists() else None),
        }

    def refresh(self, force: bool = False) -> None:
        with self._locked():
            # Another process may have refreshed meanwhile — use its token.
            current = self._load_token()
            if current.get("refresh_token"):
                self.token = current
            if not self.token.get("refresh_token"):
                raise NotLoggedIn("Not logged in to the Tesla Fleet API.")
            if not force and self.token.get("access_token") and \
                    self.token.get("expires_at", 0) - 60 > time.time():
                return
            log.debug("Refreshing Fleet API access token")
            self._save_token(self._token_request({
                "grant_type":    "refresh_token",
                "client_id":     TESLA_CLIENT_ID,
                "refresh_token": self.token["refresh_token"],
            }))

    def ensure_token(self) -> None:
        if not self.logged_in:
            self.token = self._load_token()
        if not self.logged_in:
            if not INTERACTIVE or not sys.stdin.isatty():
                raise NotLoggedIn(
                    "Not logged in to the Tesla Fleet API. Log in on the Tesla page of the "
                    "web UI, or run once:  docker compose run --rm importer python importer.py --auth")
            log.info("No Fleet API token found — starting interactive authorisation")
            log.info("This only happens ONCE. The token is saved afterwards.")
            self.interactive_auth()
        if not self.token.get("access_token") or self.token.get("expires_at", 0) - 60 < time.time():
            self.refresh()

    def authorize_url(self, state: str = "teslamate-suc", redirect_uri: str | None = None) -> str:
        return FLEET_AUTH_URL + "?" + urlencode({
            "response_type":         "code",
            "client_id":             TESLA_CLIENT_ID,
            "redirect_uri":          redirect_uri or TESLA_REDIRECT_URI,
            "scope":                 FLEET_SCOPES,
            "state":                 state,
            "prompt_missing_scopes": "true",
        })

    def complete_login(self, value: str, expected_state: str | None = None,
                       redirect_uri: str | None = None) -> None:
        """
        Finish the login with either the callback URL Tesla redirected to
        (contains ?code=…) or an existing refresh token.
        """
        value = (value or "").strip()
        if not value:
            raise FleetApiError("Nothing entered.")

        if not value.startswith("http"):
            self._save_token({"refresh_token": value}, replace=True)
            try:
                self.refresh(force=True)
            except FleetApiError:
                self.logout()
                raise
            log.info(f"Fleet API refresh token validated and saved to {self.token_path}")
            return

        qs = parse_qs(urlparse(value).query)
        if "error" in qs:
            raise FleetApiError(f"Authorisation failed: {qs.get('error_description', qs['error'])[0]}")
        code = (qs.get("code") or [None])[0]
        if not code:
            raise FleetApiError("No ?code= parameter found in the pasted URL.")
        if expected_state and (qs.get("state") or [None])[0] != expected_state:
            raise FleetApiError("The login link has expired or belongs to another login attempt — "
                                "start the login again.")
        if not TESLA_CLIENT_SECRET:
            raise FleetApiError("TESLA_CLIENT_SECRET is required to exchange the authorisation code.")
        token = self._token_request({
            "grant_type":    "authorization_code",
            "client_id":     TESLA_CLIENT_ID,
            "client_secret": TESLA_CLIENT_SECRET,
            "code":          code,
            "audience":      self.base_url,
            "redirect_uri":  redirect_uri or TESLA_REDIRECT_URI,
        })
        with self._locked():
            self._save_token(token, replace=True)
        log.info(f"Fleet API token obtained and saved to {self.token_path}")

    def logout(self) -> None:
        self.token = {}
        try:
            self.token_path.unlink()
        except FileNotFoundError:
            pass

    def interactive_auth(self) -> None:
        print()
        print("=" * 65)
        print("  FIRST RUN - Tesla Fleet API authorisation required")
        print("=" * 65)
        print()
        print("  1. Open this URL in your browser and log in with your Tesla account:")
        print()
        print(f"     {self.authorize_url()}")
        print()
        print("  2. Approve access. You are redirected to your TESLA_REDIRECT_URI.")
        print("     The page itself may show an error — that is fine.")
        print("  3. Copy the FULL URL from the address bar (it contains ?code=...)")
        print()
        print("  Alternatively paste an existing Fleet API refresh_token.")
        print()
        self.complete_login(input("  Paste callback URL or refresh_token: "))

    def register_partner(self, domain: str) -> dict:
        """One-time registration of the developer app in the region (POST /api/1/partner_accounts)."""
        domain = (domain or "").strip().removeprefix("https://").removeprefix("http://").strip("/")
        if not domain:
            raise FleetApiError("Enter the domain that hosts your public key.")
        if not TESLA_CLIENT_SECRET:
            raise FleetApiError("TESLA_CLIENT_SECRET is required for partner registration.")
        partner = self._token_request({
            "grant_type":    "client_credentials",
            "client_id":     TESLA_CLIENT_ID,
            "client_secret": TESLA_CLIENT_SECRET,
            "scope":         FLEET_SCOPES,
            "audience":      self.base_url,
        })
        try:
            r = self.http.post(
                f"{self.base_url}/api/1/partner_accounts",
                json={"domain": domain},
                headers={"Authorization": f"Bearer {partner['access_token']}"},
                timeout=TESLA_API_TIMEOUT,
            )
        except requests.RequestException as e:
            raise FleetApiError(f"Partner registration failed: {e}") from e
        if r.status_code != 200:
            raise FleetApiError(f"Partner registration failed: HTTP {r.status_code} {r.text[:500]}",
                                r.status_code)
        return r.json()

    # -- requests -------------------------------------------------------------

    HINTS = {
        401: "Token rejected. Log in again (web UI → Tesla, or --auth).",
        402: "Payment required — check billing in the Tesla developer dashboard.",
        403: "Missing scope. Make sure 'vehicle_charging_cmds' is enabled for your app "
             "and was granted during login (log in again).",
        412: "App not registered in this region. Register your domain (web UI → Tesla, "
             "or --register-domain <your-domain>).",
        421: "Wrong region for this account. Change TESLA_FLEET_REGION (eu / na).",
        429: "Rate limited by Tesla. Try again later.",
    }

    def request(self, path: str, params: dict | None = None) -> requests.Response:
        self.ensure_token()
        r = None
        for attempt in (1, 2):
            try:
                r = self.http.get(
                    f"{self.base_url}{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {self.token['access_token']}"},
                    timeout=TESLA_API_TIMEOUT,
                )
            except requests.RequestException as e:
                raise FleetApiError(f"GET {path} failed: {e}") from e
            if r.status_code == 401 and attempt == 1:
                self.refresh(force=True)  # token revoked/expired early — refresh once and retry
                continue
            break

        if r.status_code == 200:
            return r
        hint = self.HINTS.get(r.status_code, "")
        raise FleetApiError(f"GET {path}: HTTP {r.status_code} {r.text[:300]} {hint}".strip(),
                            r.status_code)

    def get(self, path: str, params: dict | None = None):
        return self.request(path, params).json()

    def vehicles(self) -> list[dict]:
        resp = self.get("/api/1/vehicles")
        return (resp.get("response") or []) if isinstance(resp, dict) else []

    def invoice(self, content_id: str, vin: str | None = None) -> bytes:
        """PDF of a charging invoice (contentId from a session's ``invoices``)."""
        params = {"vin": vin} if vin else None
        return self.request(f"/api/1/dx/charging/invoice/{content_id}", params).content


def _fetch_fleet(cutoff: datetime, lookback_days: int) -> list[dict]:
    client = FleetClient()
    log.info(f"Connecting to Tesla Fleet API ({client.base_url})")
    selected = _select_vehicles(client.vehicles())

    log.info(f"Fetching charging history for the last {lookback_days} days (since {cutoff:%Y-%m-%d})...")
    all_sessions: list[dict] = []
    for vehicle in selected:
        vin = vehicle["vin"]
        params = {"vin": vin, "pageSize": PAGE_SIZE}
        # Server-side date filter keeps nightly runs to a single request.
        if lookback_days < 3650:
            params["startTime"] = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

        log.info(f"  VIN {vin}:")
        sessions = _paginate_history(
            lambda page: client.get("/api/1/dx/charging/history", {**params, "pageNo": page}),
            cutoff,
        )
        for s in sessions:
            s.setdefault("vin", vin)
        all_sessions.extend(sessions)
    return all_sessions


# ── Tesla ownership API (legacy, unofficial) ──────────────────────────────────

OWNERSHIP_API_URL = "https://ownership.tesla.com/mobile-app/charging/history"


def _fetch_ownership(cutoff: datetime, lookback_days: int) -> list[dict]:
    """
    Legacy mode: the unofficial endpoint used by the Tesla mobile app.
    Tesla appears to have stopped supporting it — prefer TESLA_API_MODE=fleet.
    """
    if teslapy is None:
        raise ConfigError("teslapy is not installed — required for TESLA_API_MODE=ownership.")

    log.info(f"Connecting to Tesla ownership API as {TESLA_EMAIL}")
    log.warning("ownership.tesla.com appears to be no longer supported by Tesla. "
                "Consider switching to TESLA_API_MODE=fleet (see README).")

    cache_path = Path(TESLA_CACHE_FILE)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    with teslapy.Tesla(TESLA_EMAIL, cache_file=str(cache_path)) as tesla:
        tesla.timeout = TESLA_API_TIMEOUT

        if not tesla.authorized:
            if not INTERACTIVE or not sys.stdin.isatty():
                raise ImporterError("Not logged in to the Tesla ownership API. Run once: "
                                    "docker compose run --rm importer python importer.py")
            log.info("No cached token found — starting interactive authorisation")
            log.info("This only happens ONCE. The token is saved to cache afterwards.")
            refresh_token = _interactive_auth(tesla)
            tesla.refresh_token(refresh_token=refresh_token)

        selected = _select_vehicles(tesla.vehicle_list())

        log.info(f"Fetching charging history for the last {lookback_days} days (since {cutoff:%Y-%m-%d})...")
        all_sessions: list[dict] = []
        for vehicle in selected:
            vin = vehicle["vin"]
            log.info(f"  VIN {vin}:")
            # teslapy returns a JsonDict (already parsed) -- NOT a requests.Response.
            sessions = _paginate_history(
                lambda page: tesla.get(OWNERSHIP_API_URL, params={
                    "vin":            vin,
                    "deviceLanguage": "en",
                    "deviceCountry":  "US",
                    "operationName":  "getChargingHistoryV2",
                    "pageNo":         page,
                    "pageSize":       PAGE_SIZE,
                }),
                cutoff,
            )
            for s in sessions:
                s.setdefault("vin", vin)
            all_sessions.extend(sessions)
        return all_sessions


def _interactive_auth(tesla: "teslapy.Tesla") -> str:
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
    override = DB_TIMEZONE_OVERRIDE
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


def _load_car_ids(cur) -> dict[str, int]:
    """Return {VIN: car_id} from TeslaMate's cars table (empty on failure)."""
    try:
        cur.execute("SELECT id, vin FROM cars WHERE vin IS NOT NULL")
        return {vin.upper(): car_id for car_id, vin in cur.fetchall()}
    except Exception as e:
        log.warning(f"  Cannot read cars table ({e}) — matching without VIN filter")
        cur.connection.rollback()
        return {}


def _session_result(session: dict, start_dt: datetime, location: str, cost_info: dict | None) -> dict:
    """One row of the run report (shown in the web UI and stored in last_run.json)."""
    return {
        "start":      start_dt,
        "location":   location,
        "vin":        session.get("vin"),
        "session_id": session.get("sessionId"),
        "invoices":   [
            {"id": inv.get("contentId"), "name": inv.get("fileName"), "type": inv.get("invoiceType")}
            for inv in (session.get("invoices") or []) if inv.get("contentId")
        ],
        "cost":       cost_info,
        "tm_id":      None,
        "tm_cost":    None,
        "status":     None,
        "message":    None,
    }


def import_to_teslamate(sessions: list[dict], dry_run: bool, lookback_days: int,
                        conn=None) -> dict:
    """
    Match each Tesla session against TeslaMate's charging_processes table
    by start timestamp (within TIME_TOLERANCE_S seconds) and write the cost.

    Each successful UPDATE is committed immediately so that a DB error on one
    row does not roll back previously written costs.

    Returns a summary dict with counts of outcomes and, under "results",
    one entry per session (status: free, not_found, already_set,
    would_update, updated or error).
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
        "results":      [],
    }

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    # use try/finally to guarantee the connection is always closed
    own_conn = conn is None
    conn = conn or _get_db_connection()
    try:
        cur = conn.cursor()

        # Detect how start_date is stored so the timestamp comparison is correct
        # regardless of the DB server's timezone setting or column type.
        db_tz = _detect_start_date_timezone(cur)

        # Map VIN -> TeslaMate car_id so that, with several cars on one account,
        # a session is only matched against charging processes of the same car.
        car_ids = _load_car_ids(cur)

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
            result = _session_result(session, start_dt, location, cost_info)
            stats["results"].append(result)
            if cost_info is None:
                log.debug(f"  free/zero  {start_dt:%Y-%m-%d %H:%M}  {location}")
                stats["no_cost"] += 1
                result["status"] = "free"
                continue

            car_id = car_ids.get((session.get("vin") or "").upper())
            if session.get("vin") and car_id is None and len(car_ids) > 1:
                log.warning(f"  VIN {session['vin']} not found in TeslaMate cars table — "
                            f"matching against all cars")

            # Find the closest matching record in TeslaMate.
            # start_date is cast using the detected timezone so that the
            # comparison against the Tesla API's UTC timestamp is always correct,
            # regardless of whether the column is timestamptz or plain timestamp,
            # and regardless of the DB server's locale/timezone setting.
            try:
                cur.execute("""
                    SELECT id, start_date, cost
                    FROM   charging_processes
                    WHERE  ABS(EXTRACT(EPOCH FROM (
                               (start_date AT TIME ZONE %s) - %s::timestamptz
                           ))) < %s
                      AND  (%s::int IS NULL OR car_id = %s::int)
                    ORDER BY ABS(EXTRACT(EPOCH FROM (
                               (start_date AT TIME ZONE %s) - %s::timestamptz
                           )))
                    LIMIT 1
                """, (db_tz, start_dt, TIME_TOLERANCE_S, car_id, car_id, db_tz, start_dt))
            except Exception as e:
                log.error(f"DB error during lookup: {e}")
                conn.rollback()
                stats["errors"] += 1
                result.update(status="error", message=str(e))
                continue

            row = cur.fetchone()

            if row is None:
                log.warning(
                    f"  NOT FOUND  {start_dt:%Y-%m-%d %H:%M}  {location}  "
                    f"{cost_info['original_total']:.2f} {cost_info['original_currency']}"
                )
                stats["not_found"] += 1
                result["status"] = "not_found"
                continue

            tm_id, tm_start, tm_cost = row
            result.update(tm_id=tm_id, tm_cost=float(tm_cost) if tm_cost is not None else None)

            if tm_cost is not None and not OVERWRITE_EXISTING:
                log.debug(
                    f"  skipped    #{tm_id}  {tm_start:%Y-%m-%d %H:%M}  "
                    f"{location}  (already set: {tm_cost:.4f})"
                )
                stats["already_set"] += 1
                result["status"] = "already_set"
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
                result["status"] = "would_update"
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
                result["status"] = "updated"
            except Exception as e:
                log.error(f"DB error updating #{tm_id}: {e}")
                conn.rollback()
                stats["errors"] += 1
                result.update(status="error", message=str(e))

        cur.close()
    finally:
        if own_conn:
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


# ── Run report (shared with the web UI) ───────────────────────────────────────

def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def save_last_run(result: dict, path: str | None = None) -> None:
    """Store the report of a real (non dry-run) run so the web UI can show it."""
    try:
        path = Path(path or LAST_RUN_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, default=_json_default, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.warning(f"Cannot save run report: {e}")


def load_last_run(path: str | None = None) -> dict | None:
    try:
        data = json.loads(Path(path or LAST_RUN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for key in ("started", "finished"):
        if data.get(key):
            data[key] = dateparser.parse(data[key])
    for row in data.get("results") or []:
        if row.get("start"):
            row["start"] = dateparser.parse(row["start"])
    return data


def run(dry_run: bool = False, lookback_days: int | None = None,
        sessions: list[dict] | None = None, conn=None, source: str = "cli") -> dict:
    """
    One complete import: fetch sessions from Tesla (unless *sessions* is
    given), match them against TeslaMate and write the costs.

    Returns the stats from import_to_teslamate() plus "started", "finished",
    "dry_run", "lookback_days", "source" and, if the run failed, "error".
    Configuration errors are raised as ConfigError before anything happens.
    """
    validate_config()
    lookback_days = lookback_days or LOOKBACK_DAYS
    started = datetime.now(timezone.utc)

    log.info("=" * 55)
    log.info("  TeslaMate Supercharger Cost Importer")
    log.info(f"  Tesla API: {TESLA_API_MODE}")
    log.info(f"  DB:        {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
    log.info(f"  Lookback:  {lookback_days} days  |  Tolerance: {TIME_TOLERANCE_S}s")
    log.info(f"  Overwrite: {OVERWRITE_EXISTING}")
    if TARGET_CURRENCY:
        log.info(f"  Currency:  converting all costs -> {TARGET_CURRENCY} (ECB live rates)")
    else:
        log.info(f"  Currency:  storing in original currency (no conversion)")
    if dry_run:
        log.info("  Mode:      DRY-RUN (no writes)")
    log.info("=" * 55)

    result = {"total": 0, "no_cost": 0, "too_old": 0, "not_found": 0, "already_set": 0,
              "updated": 0, "would_update": 0, "errors": 0, "results": []}
    try:
        # Load ECB exchange rates if currency conversion is enabled
        if TARGET_CURRENCY:
            load_ecb_rates()
        if sessions is None:
            sessions = fetch_charging_sessions(lookback_days)
        if not sessions:
            log.warning("No sessions to process.")
        else:
            result = import_to_teslamate(sessions, dry_run, lookback_days, conn=conn)
            log_summary(result, dry_run)
    except (ImporterError, psycopg2.Error) as e:
        log.error(str(e).strip())
        result["error"] = str(e).strip()

    result.update(started=started, finished=datetime.now(timezone.utc), dry_run=dry_run,
                  lookback_days=lookback_days, source=source)
    if not dry_run:
        save_last_run(result)
    return result


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    global INTERACTIVE
    INTERACTIVE = True

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

  # Fleet API one-time setup (also possible in the web UI)
  python importer.py --register-domain your-name.github.io
  python importer.py --auth
        """,
    )
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Preview changes without writing to the database")
    parser.add_argument("--input", "-i", metavar="FILE",
                        help="Load sessions from a JSON file instead of the Tesla API")
    parser.add_argument("--lookback", type=int, metavar="DAYS",
                        help="How many days back to process (default: LOOKBACK_DAYS)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show debug messages (skipped/free sessions)")
    parser.add_argument("--auth", action="store_true",
                        help="Fleet API: (re)run the one-time interactive login and exit")
    parser.add_argument("--register-domain", metavar="DOMAIN",
                        help="Fleet API: one-time registration of your app in the region "
                             "(domain hosting the public key) and exit")
    args = parser.parse_args()

    try:
        configure()
    except ConfigError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    setup_logging(LOG_FILE)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        if args.auth or args.register_domain:
            if TESLA_API_MODE != "fleet":
                raise ConfigError("--auth / --register-domain need TESLA_API_MODE=fleet "
                                  "(set TESLA_CLIENT_ID).")
            client = FleetClient()
            if args.register_domain:
                result = client.register_partner(args.register_domain)
                log.info(f"Partner registration OK ({client.base_url}): {json.dumps(result)}")
            if args.auth:
                client.interactive_auth()
                client.ensure_token()
                log.info("Fleet API login OK.")
            return

        sessions = None
        if args.input:
            log.info(f"Loading sessions from file: {args.input}")
            with open(args.input) as f:
                data = json.load(f)
            sessions = data["data"] if isinstance(data, dict) and "data" in data else data
            log.info(f"Loaded {len(sessions)} sessions")

        result = run(dry_run=args.dry_run, lookback_days=args.lookback, sessions=sessions)
    except (ConfigError, ImporterError) as e:
        log.error(str(e))
        sys.exit(1)
    if result.get("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
