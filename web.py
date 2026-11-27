"""
Web UI for the TeslaMate Supercharger Cost Importer.

Serves a small management page under ROOT_PATH (default ``/supercharger``)
and runs the importer every night on a schedule.

Environment (besides everything importer.py reads):
    ROOT_PATH       URL prefix (default: /supercharger)
    PORT            Listen port (default: 4002)
    WEB_USER        Login user name (default: admin)
    WEB_PASSWORD    Login password; without it the UI is open to anyone who
                    can reach the port (a warning is shown)
    SECRET_KEY      Session signing key (default: random per start)
"""

import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import psycopg2
import requests
from flask import (Blueprint, Flask, Response, abort, flash, redirect, render_template,
                   request, session, url_for)
from flask_babel import Babel, get_locale

import i18n
import importer
from i18n import _

log = logging.getLogger(__name__)

COMMON_CURRENCIES = ["CZK", "EUR", "CHF", "PLN", "HUF", "GBP", "USD", "SEK", "NOK", "DKK"]
PUBLIC_KEY_PATH = "/.well-known/appspecific/com.tesla.3p.public-key.pem"
_CONTENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_VIN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

# Supercharger sessions in TeslaMate: DC charges reported by the car as Tesla.
_SUPERCHARGER = """
    EXISTS (SELECT 1 FROM charges ch
            WHERE ch.charging_process_id = cp.id
              AND ch.fast_charger_present
              AND (ch.fast_charger_brand ILIKE 'tesla%%' OR ch.fast_charger_type ILIKE '%%supercharger%%'))
"""

CHARGES_QUERY = f"""
SELECT cp.id, c.name, c.vin, cp.start_date, cp.end_date,
       cp.charge_energy_added, cp.cost,
       COALESCE(g.name, a.name, a.display_name) AS location
FROM charging_processes cp
JOIN cars c ON c.id = cp.car_id
LEFT JOIN geofences g ON g.id = cp.geofence_id
LEFT JOIN addresses a ON a.id = cp.address_id
WHERE cp.end_date IS NOT NULL AND {_SUPERCHARGER}
  AND (%s = FALSE OR cp.cost IS NULL)
ORDER BY cp.start_date DESC
LIMIT %s
"""

MISSING_QUERY = f"""
SELECT COUNT(*) FROM charging_processes cp
WHERE cp.end_date IS NOT NULL AND cp.cost IS NULL AND {_SUPERCHARGER}
"""

CARS_QUERY = "SELECT vin FROM cars WHERE vin IS NOT NULL"


class _ListHandler(logging.Handler):
    """Collects the importer's log lines during a run (shown on the result page)."""

    def __init__(self, limit: int = 1000):
        super().__init__(logging.INFO)
        self.lines: list[str] = []
        self.limit = limit
        self.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))

    def emit(self, record):
        if len(self.lines) < self.limit:
            self.lines.append(self.format(record))


# ─── Runner & nightly schedule ────────────────────────────────────────────────

class Runner:
    """
    Runs the importer (manually or on the nightly schedule), one run at a time.

    importer.py keeps its settings in module globals, so everything that
    reloads them (runs, Tesla account actions, saving settings) goes through
    ``self.lock``.
    """

    def __init__(self, connect=None, fleet_factory=None):
        self.connect = connect                  # optional DB connection factory (tests)
        self.fleet_factory = fleet_factory or importer.FleetClient
        self.lock = threading.Lock()
        self.next_run: datetime | None = None   # aware datetime (UTC)
        self.running = False
        self._stop = threading.Event()

    def run(self, dry_run: bool = False, lookback_days: int | None = None,
            source: str = "web") -> dict:
        if not self.lock.acquire(blocking=False):
            return {"error": _("Another run is in progress, try again in a moment."),
                    "dry_run": dry_run, "results": []}
        handler = _ListHandler()
        importer.log.addHandler(handler)
        self.running = True
        try:
            try:
                importer.configure()
                conn = self.connect() if self.connect else None
                try:
                    result = importer.run(dry_run=dry_run, lookback_days=lookback_days,
                                          conn=conn, source=source)
                finally:
                    if conn is not None:
                        conn.close()
            except importer.ConfigError as e:
                result = {"error": _("Invalid configuration: %(error)s", error=e)}
            except psycopg2.OperationalError as e:
                result = {"error": _("Cannot connect to the TeslaMate database: %(error)s", error=e)}
            except Exception as e:  # never let the scheduler thread die
                log.exception("Run failed")
                result = {"error": _("Unexpected error: %(error)s", error=e)}
            result.setdefault("dry_run", dry_run)
            result.setdefault("results", [])
            result["log"] = handler.lines
            return result
        finally:
            self.running = False
            importer.log.removeHandler(handler)
            self.lock.release()

    def fleet(self):
        """FleetClient with freshly loaded settings (call while holding the lock)."""
        importer.configure()
        if importer.TESLA_API_MODE != "fleet":
            raise importer.ConfigError(_("The web UI manages the Tesla Fleet API login. "
                                         "Set TESLA_CLIENT_ID (TESLA_API_MODE=fleet)."))
        return self.fleet_factory()

    # -- schedule -------------------------------------------------------------

    @staticmethod
    def next_occurrence(schedule: str, tz_name: str, now: datetime | None = None):
        """Next time HH:MM occurs in *tz_name* after *now*, as UTC — or None."""
        if not schedule:
            return None
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            tz = timezone.utc
        hour, minute = (int(x) for x in schedule.split(":"))
        local_now = (now or datetime.now(timezone.utc)).astimezone(tz)
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_now:
            candidate = (candidate + timedelta(days=1)).replace(hour=hour, minute=minute)
        return candidate.astimezone(timezone.utc)

    def plan(self):
        self.next_run = self.next_occurrence(importer.SCHEDULE_TIME, importer.TIMEZONE)

    def loop(self):
        while not self._stop.is_set():
            if self.next_run and datetime.now(timezone.utc) >= self.next_run:
                log.info("Scheduled run")
                self.run(source="schedule")
                with self.lock:
                    try:
                        importer.configure()
                    except importer.ConfigError:
                        pass
                    self.plan()
            self._stop.wait(30)

    def start(self):
        self.plan()
        threading.Thread(target=self.loop, name="scheduler", daemon=True).start()

    def stop(self):
        self._stop.set()


# ─── Public key for the Fleet API partner registration ────────────────────────

def key_paths() -> tuple[Path, Path]:
    keys = Path(importer.DATA_DIR) / "keys"
    return keys / "private-key.pem", keys / "public-key.pem"


def public_key() -> str | None:
    try:
        return key_paths()[1].read_text()
    except OSError:
        return None


def generate_keys() -> None:
    """Create the EC (prime256v1) key pair Tesla requires — once."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_path, public_path = key_paths()
    if public_path.exists():
        return
    private_path.parent.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    private_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                               serialization.PrivateFormat.TraditionalOpenSSL,
                                               serialization.NoEncryption()))
    os.chmod(private_path, 0o600)
    public_path.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))


def fingerprint(pem: str | None) -> str | None:
    if not pem:
        return None
    return hashlib.sha256(pem.strip().encode()).hexdigest()[:16]


def check_published_key(domain: str) -> tuple[bool, str]:
    """Is our public key reachable at https://<domain>/.well-known/…?"""
    url = f"https://{domain}{PUBLIC_KEY_PATH}"
    try:
        r = requests.get(url, timeout=10)
    except requests.RequestException as e:
        return False, _("%(url)s is not reachable: %(error)s", url=url, error=e)
    if r.status_code != 200:
        return False, _("%(url)s returned HTTP %(status)s.", url=url, status=r.status_code)
    mine = public_key()
    if mine and r.text.strip() != mine.strip():
        return False, _("%(url)s serves a different key than the one generated here.", url=url)
    return True, _("Public key found at %(url)s.", url=url)


def _partner_file() -> Path:
    return Path(importer.DATA_DIR) / "fleet_partner.json"


def load_partner() -> dict:
    try:
        import json
        return json.loads(_partner_file().read_text())
    except (OSError, ValueError):
        return {}


def save_partner(data: dict) -> None:
    import json
    path = _partner_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


# ─── App ──────────────────────────────────────────────────────────────────────

def create_app(root_path=None, password=None, user=None, start_scheduler=False,
               db_connector=None, fleet_factory=None) -> Flask:
    root_path = "/" + (root_path if root_path is not None
                       else os.environ.get("ROOT_PATH", "/supercharger")).strip("/")
    root_path = "" if root_path == "/" else root_path
    password = password if password is not None else os.environ.get("WEB_PASSWORD", "")
    user = user or os.environ.get("WEB_USER", "admin")

    try:
        importer.configure()
    except importer.ConfigError as e:
        log.error("Configuration problem: %s", e)

    def connect():
        return db_connector() if db_connector else importer._get_db_connection()

    app = Flask(__name__, static_folder=None)
    app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_PATH=root_path or "/")
    runner = Runner(db_connector, fleet_factory)
    app.extensions["runner"] = runner

    # Language like TeslaMate: ?locale= → session → browser → English.
    Babel(app, default_locale=i18n.DEFAULT_LOCALE,
          default_translation_directories=os.path.join(os.path.dirname(__file__), "translations"),
          locale_selector=lambda: i18n.select_locale(
              request.args.get("locale"), session, request.accept_languages.values()))

    bp = Blueprint("ui", __name__, url_prefix=root_path or None,
                   static_folder="static", template_folder="templates")

    # ── auth & CSRF ──
    def check_auth():
        if not password:
            return True
        auth = request.authorization
        return bool(auth and hmac.compare_digest(auth.username or "", user)
                    and hmac.compare_digest(auth.password or "", password))

    @bp.before_request
    def guard():
        if request.endpoint == "ui.health":
            return None
        if not check_auth():
            return Response(_("Login required"), 401,
                            {"WWW-Authenticate": 'Basic realm="Supercharger costs"'})
        if request.method == "POST":
            token = request.form.get("csrf_token", "")
            expected = session.get("csrf_token", "")
            if not expected or not hmac.compare_digest(token, expected):
                abort(400, _("Invalid or expired form token — reload the page and try again."))
        return None

    @bp.app_context_processor
    def inject():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(16)
        return {"csrf_token": session["csrf_token"], "auth_enabled": bool(password),
                "runner": runner, "locale": str(get_locale()), "cfg": importer,
                "languages": i18n.language_options(), "STATUS": status_labels()}

    def status_labels():
        return {
            "updated":      (_("Saved"), "is-success"),
            "would_update": (_("Will be saved"), "is-info"),
            "already_set":  (_("Already had a cost"), "is-light"),
            "not_found":    (_("Not found in TeslaMate"), "is-warning"),
            "free":         (_("Free"), "is-light"),
            "error":        (_("Error"), "is-danger"),
        }

    def busy():
        flash(_("A run is in progress, try again in a moment."), "warning")
        return redirect(request.referrer or url_for("ui.index"))

    def db_query(query, params=None):
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchall()
        finally:
            conn.close()

    def fleet_status():
        """Login state shown on the overview and Tesla pages (no API request)."""
        if importer.TESLA_API_MODE != "fleet":
            return {"mode": importer.TESLA_API_MODE, "configured": False}
        try:
            client = runner.fleet_factory()
        except importer.ConfigError as e:
            return {"mode": "fleet", "configured": False, "error": str(e)}
        return {"mode": "fleet", "configured": True, "base_url": client.base_url,
                **client.status()}

    # ── pages ──
    @bp.route("/health")
    def health():
        return {"status": "ok"}

    @bp.route("/")
    def index():
        missing, db_error = None, None
        try:
            missing = db_query(MISSING_QUERY)[0][0]
        except Exception as e:
            db_error = _("Cannot read the TeslaMate database: %(error)s", error=e)
        return render_template("index.html", tesla=fleet_status(), last_run=importer.load_last_run(),
                               missing=missing, db_error=db_error)

    @bp.route("/run", methods=["POST"])
    def run():
        dry_run = request.form.get("mode") != "write"
        try:
            lookback = int(request.form.get("lookback") or 0) or None
        except ValueError:
            abort(400, _("Days must be a whole number."))
        if lookback is not None and not 1 <= lookback <= 9999:
            abort(400, _("Days must be between 1 and 9999."))
        result = runner.run(dry_run=dry_run, lookback_days=lookback)
        return render_template("run.html", result=result, title=_("Preview") if dry_run
                               else _("Import finished"))

    @bp.route("/runs/last")
    def last_run():
        result = importer.load_last_run()
        if not result:
            flash(_("No import has run yet."), "warning")
            return redirect(url_for("ui.index"))
        return render_template("run.html", result=result, title=_("Last import"))

    @bp.route("/charges")
    def charges():
        only_missing = request.args.get("missing") == "1"
        rows, error = [], None
        try:
            for cid, car, vin, start, end, kwh, cost, location in db_query(CHARGES_QUERY,
                                                                          (only_missing, 200)):
                kwh = float(kwh) if kwh is not None else None
                cost = float(cost) if cost is not None else None
                rows.append({"id": cid, "car": car or vin, "start": start, "end": end,
                             "kwh": kwh, "cost": cost, "location": location})
        except Exception as e:
            error = _("Cannot read charges from the TeslaMate database: %(error)s", error=e)
        return render_template("charges.html", rows=rows, error=error, only_missing=only_missing)

    @bp.route("/charges/<int:charge_id>/cost", methods=["POST"])
    def set_cost(charge_id):
        raw = (request.form.get("cost") or "").strip().replace(",", ".")
        try:
            cost = float(raw) if raw else None
            if cost is not None and cost < 0:
                raise ValueError
        except ValueError:
            flash(_("'%(value)s' is not a valid cost.", value=raw), "danger")
            return redirect(url_for("ui.charges", missing=request.form.get("missing")))
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE charging_processes SET cost = %s WHERE id = %s", (cost, charge_id))
                if cur.rowcount != 1:
                    conn.rollback()
                    abort(404)
            conn.commit()
        finally:
            conn.close()
        flash(_("Cost of charge #%(id)s saved.", id=charge_id) if cost is not None
              else _("Cost of charge #%(id)s removed.", id=charge_id))
        return redirect(url_for("ui.charges", missing=request.form.get("missing")))

    @bp.route("/invoice/<content_id>")
    def invoice(content_id):
        if not _CONTENT_ID.match(content_id):
            abort(404)
        vin = request.args.get("vin") or None
        if vin and not _VIN.match(vin):
            abort(400)
        try:
            pdf = runner.fleet().invoice(content_id, vin)
        except (importer.ConfigError, importer.ImporterError) as e:
            return Response(str(e), 502, mimetype="text/plain")
        return Response(pdf, mimetype="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="invoice-{content_id}.pdf"'})

    # ── Tesla account ──
    def redirect_uri_is_ours() -> bool:
        return importer.TESLA_REDIRECT_URI.rstrip("/").endswith(f"{root_path}/tesla/callback")

    @bp.route("/tesla")
    def tesla():
        pem = public_key()
        login_url = None
        status = fleet_status()
        if status.get("configured"):
            state = session.get("oauth_state") or secrets.token_urlsafe(16)
            session["oauth_state"] = state
            login_url = runner.fleet_factory().authorize_url(state)
        return render_template(
            "tesla.html", tesla=status, login_url=login_url, partner=load_partner(),
            public_key=pem, key_fingerprint=fingerprint(pem),
            key_url=request.host_url.rstrip("/") + PUBLIC_KEY_PATH,
            callback_url=request.host_url.rstrip("/") + url_for("ui.tesla_callback"),
            redirect_uri_is_ours=redirect_uri_is_ours(),
            vehicles=session.pop("vehicles", None),
        )

    def fleet_action(action, success):
        if not runner.lock.acquire(timeout=10):
            return busy()
        try:
            action(runner.fleet())
            flash(success)
        except (importer.ConfigError, importer.ImporterError) as e:
            flash(str(e), "danger")
        finally:
            runner.lock.release()
        return redirect(url_for("ui.tesla"))

    @bp.route("/tesla/login", methods=["POST"])
    def tesla_login():
        value = request.form.get("callback", "")
        state = session.get("oauth_state")
        session.pop("oauth_state", None)
        return fleet_action(lambda c: c.complete_login(value, expected_state=state),
                            _("Logged in to Tesla. The token is renewed automatically."))

    @bp.route("/tesla/callback")
    def tesla_callback():
        state = session.pop("oauth_state", None)
        return fleet_action(lambda c: c.complete_login(request.url, expected_state=state),
                            _("Logged in to Tesla. The token is renewed automatically."))

    @bp.route("/tesla/logout", methods=["POST"])
    def tesla_logout():
        return fleet_action(lambda c: c.logout(), _("Tesla login removed."))

    @bp.route("/tesla/vehicles", methods=["POST"])
    def tesla_vehicles():
        def load(client):
            vehicles = client.vehicles()
            try:
                known = {r[0].upper() for r in db_query(CARS_QUERY)}
            except Exception:
                known = set()
            session["vehicles"] = [
                {"vin": v.get("vin"), "name": v.get("display_name"),
                 "in_teslamate": (v.get("vin") or "").upper() in known,
                 "imported": not importer.TESLA_VINS or v.get("vin") in importer.TESLA_VINS}
                for v in vehicles]
        return fleet_action(load, _("Vehicles loaded from Tesla."))

    @bp.route("/tesla/keys", methods=["POST"])
    def tesla_keys():
        generate_keys()
        flash(_("Key pair generated."))
        return redirect(url_for("ui.tesla"))

    @bp.route("/tesla/public-key.pem")
    def tesla_public_key():
        pem = public_key()
        if not pem:
            abort(404)
        return Response(pem, mimetype="application/x-pem-file",
                        headers={"Content-Disposition": 'attachment; filename="com.tesla.3p.public-key.pem"'})

    @bp.route("/tesla/register", methods=["POST"])
    def tesla_register():
        domain = (request.form.get("domain") or "").strip().lower()
        domain = domain.removeprefix("https://").removeprefix("http://").split("/")[0]
        if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", domain):
            flash(_("Enter a domain such as your-name.github.io."), "danger")
            return redirect(url_for("ui.tesla"))
        ok, message = check_published_key(domain)
        if not ok:
            flash(message, "warning")

        def register(client):
            client.register_partner(domain)
            save_partner({"domain": domain, "region": importer.TESLA_FLEET_REGION,
                          "registered_at": datetime.now(timezone.utc).isoformat()})
        return fleet_action(register, _("App registered for %(domain)s.", domain=domain))

    # ── settings ──
    @bp.route("/settings", methods=["GET", "POST"])
    def settings():
        stored = importer.load_web_settings()
        error = None
        locked = importer.ENV_LOCKED
        if request.method == "POST":
            # Values set in Docker cannot be changed here (fields are disabled).
            editable = [key for key in importer.WEB_SETTINGS if key not in locked]
            form = {key: (request.form.get(key.lower()) or "").strip() for key in editable}
            if "TARGET_CURRENCY" in form:
                form["TARGET_CURRENCY"] = form["TARGET_CURRENCY"].upper()
            if "TESLA_VIN" in form:
                form["TESLA_VIN"] = ",".join(v.strip().upper() for v in form["TESLA_VIN"].split(",")
                                             if v.strip())
            if "SCHEDULE_TIME" in form:
                form["SCHEDULE_TIME"] = form["SCHEDULE_TIME"] or "off"  # empty = no nightly import
            form = {k: v for k, v in form.items() if v}  # empty = default
            if form.get("TIMEZONE") and form["TIMEZONE"] not in available_timezones():
                error = _("Unknown timezone '%(tz)s'.", tz=form["TIMEZONE"])
            bad_vins = [v for v in form.get("TESLA_VIN", "").split(",")
                        if v.upper() not in ("", "ALL") and not _VIN.match(v)]
            if bad_vins and not error:
                error = _("Invalid VIN: %(vins)s", vins=", ".join(bad_vins))
            if not error:
                if not runner.lock.acquire(timeout=10):
                    return busy()
                try:
                    importer.configure(overrides=form)  # validates; raises before saving
                    # Keep web values of settings now fixed in Docker, in case they are removed there.
                    importer.save_web_settings({**{k: v for k, v in stored.items() if k in locked}, **form})
                    importer.configure()
                    runner.plan()
                    flash(_("Settings saved."))
                    return redirect(url_for("ui.settings"))
                except importer.ConfigError as e:
                    error = str(e)
                    importer.configure()
                finally:
                    runner.lock.release()
            values = {key: getattr(importer, key) if key in locked else request.form.get(key.lower(), "")
                      for key in importer.WEB_SETTINGS}
        else:
            # Show the effective values (Docker over web settings over defaults).
            values = {key: getattr(importer, key) for key in importer.WEB_SETTINGS}
        return render_template("settings.html", values=values, error=error, stored=stored,
                               locked=locked,
                               currencies=COMMON_CURRENCIES, timezones=sorted(available_timezones()),
                               regions=list(importer.FLEET_API_URLS)), 400 if error else 200

    app.register_blueprint(bp)

    # Tesla fetches the public key from the domain root, outside ROOT_PATH.
    @app.route(PUBLIC_KEY_PATH)
    def well_known_public_key():
        pem = public_key()
        if not pem:
            abort(404)
        return Response(pem, mimetype="application/x-pem-file")

    if root_path:
        @app.route("/")
        def to_root():
            return redirect(root_path + "/")

    @app.template_filter("dt")
    def fmt_dt(value):
        if not value:
            return ""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        try:
            value = value.astimezone(ZoneInfo(importer.TIMEZONE))
        except (ZoneInfoNotFoundError, ValueError):
            pass
        return value.strftime("%Y-%m-%d %H:%M")

    @app.template_filter("money")
    def fmt_money(value, currency=""):
        if value is None:
            return ""
        return f"{value:,.2f}".replace(",", " ") + (f" {currency}" if currency else "")

    if start_scheduler:
        runner.start()
    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    try:
        importer.configure()
    except importer.ConfigError as e:
        log.error("Configuration problem: %s", e)
    if importer.LOG_FILE:
        try:
            Path(importer.LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(importer.LOG_FILE, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                                                   "%Y-%m-%d %H:%M:%S"))
            logging.getLogger().addHandler(handler)
        except OSError as e:
            log.warning("Cannot write log file %s: %s", importer.LOG_FILE, e)

    app = create_app(start_scheduler=True)
    port = int(os.environ.get("PORT", "4002"))
    if not os.environ.get("WEB_PASSWORD"):
        log.warning("WEB_PASSWORD is not set — the UI is open to anyone who can reach port %d.", port)
    from waitress import serve
    log.info("Serving on http://0.0.0.0:%d%s/", port,
             os.environ.get("ROOT_PATH", "/supercharger").rstrip("/"))
    serve(app, host="0.0.0.0", port=port, threads=4)


if __name__ == "__main__":
    main()
