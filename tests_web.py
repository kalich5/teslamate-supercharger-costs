"""
Tests for the web UI (web.py).

Run with:
    pytest tests.py tests_web.py -v
"""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

import importer
import web
from tests import FakeDB, FakeHttp, config, process, session  # noqa: F401 (config = autouse fixture)

AUTH = {"Authorization": "Basic YWRtaW46c2VjcmV0"}  # admin:secret
PREFIX = "/supercharger"


class WebDB(FakeDB):
    """FakeDB that also answers the web UI's own queries."""

    def __init__(self, *args, charges=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.charges = charges or []

    def rows_for(self, query, params):
        if query.startswith("SELECT COUNT(*)"):
            return [(sum(1 for c in self.charges if c[6] is None),)]
        if "FROM charging_processes cp JOIN cars" in query:
            only_missing = params[0]
            return [c for c in self.charges if not only_missing or c[6] is None]
        if query.startswith("SELECT vin FROM cars"):
            return [(vin,) for vin in self.cars.values()]
        return []


class FakeFleet:
    """Stands in for importer.FleetClient in the web UI."""

    instances = []

    def __init__(self):
        self.base_url = "https://fleet-api.prd.eu.vn.cloud.tesla.com"
        self.logins = []
        self.registered = []
        FakeFleet.instances.append(self)

    logged_in_flag = False

    def status(self):
        return {"logged_in": FakeFleet.logged_in_flag, "expires_at": None,
                "updated_at": datetime.now(timezone.utc) if FakeFleet.logged_in_flag else None}

    def authorize_url(self, state="x", redirect_uri=None):
        return f"https://auth.tesla.com/oauth2/v3/authorize?state={state}"

    def complete_login(self, value, expected_state=None, redirect_uri=None):
        if expected_state and f"state={expected_state}" not in value and value.startswith("http"):
            raise importer.FleetApiError("The login link has expired")
        self.logins.append(value)
        FakeFleet.logged_in_flag = True

    def logout(self):
        FakeFleet.logged_in_flag = False

    def vehicles(self):
        return [{"vin": "VIN1", "display_name": "Red"}, {"vin": "VIN3", "display_name": "Blue"}]

    def invoice(self, content_id, vin=None):
        return b"%PDF-1.4 fake"

    def register_partner(self, domain):
        self.registered.append(domain)
        return {"response": {"domain": domain}}


@pytest.fixture
def db():
    now = datetime.now(timezone.utc)
    return WebDB([process(10, 1, 1)], charges=[
        (10, "Red", "VIN1", now - timedelta(hours=5), now - timedelta(hours=4), 30.5, None, "Humpolec"),
        (11, "Red", "VIN1", now - timedelta(days=3), now - timedelta(days=3), 20.0, 150.0, "Brno"),
    ])


@pytest.fixture
def app(db, monkeypatch):
    FakeFleet.logged_in_flag = False
    FakeFleet.instances.clear()
    application = web.create_app(root_path=PREFIX, password="secret", user="admin",
                                 db_connector=lambda: db, fleet_factory=FakeFleet)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def csrf(client, url=PREFIX + "/"):
    html = client.get(url, headers=AUTH).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', html).group(1)


def text(response):
    return response.get_data(as_text=True)


class TestAccess:
    def test_login_required(self, client):
        assert client.get(PREFIX + "/").status_code == 401

    def test_health_is_public(self, client):
        assert client.get(PREFIX + "/health").status_code == 200

    def test_root_redirects_to_prefix(self, client):
        assert client.get("/").headers["Location"].endswith(PREFIX + "/")

    def test_post_without_csrf_rejected(self, client):
        assert client.post(PREFIX + "/run", headers=AUTH, data={"mode": "preview"}).status_code == 400

    def test_no_password_shows_warning(self, db):
        app = web.create_app(root_path=PREFIX, password="", db_connector=lambda: db,
                             fleet_factory=FakeFleet)
        assert "WEB_PASSWORD" in text(app.test_client().get(PREFIX + "/"))

    @pytest.mark.parametrize("page", ["/", "/charges", "/charges?missing=1", "/tesla", "/settings"])
    def test_pages_render(self, client, page):
        assert client.get(PREFIX + page, headers=AUTH).status_code == 200


class TestOverview:
    def test_shows_missing_count_and_login_state(self, client):
        html = text(client.get(PREFIX + "/", headers=AUTH))
        assert "Not logged in" in html
        assert re.search(r'title is-4[^"]*">1<', html)

    def test_db_down(self, monkeypatch):
        def broken():
            raise RuntimeError("db down")
        app = web.create_app(root_path=PREFIX, password="", db_connector=broken, fleet_factory=FakeFleet)
        assert "db down" in text(app.test_client().get(PREFIX + "/"))


class TestRun:
    @pytest.fixture
    def tesla(self, monkeypatch):
        http = FakeHttp(history={"VIN1": [session("VIN1", invoices=[{"contentId": "inv-1"}])]},
                        vehicles=("VIN1",))
        original = importer.FleetClient.__init__

        def init(self):
            original(self)
            self.http = http
        monkeypatch.setattr(importer.FleetClient, "__init__", init)
        return http

    def test_preview_then_save(self, client, db, tesla, tmp_path):
        (tmp_path / "fleet_token.json").write_text(json.dumps(
            {"refresh_token": "rt", "access_token": "at", "expires_at": 9999999999}))
        token = csrf(client)
        html = text(client.post(PREFIX + "/run", headers=AUTH,
                                data={"csrf_token": token, "mode": "preview", "lookback": "7"}))
        assert "Will be saved" in html and "250.00 CZK" in html
        assert "/invoice/inv-1?vin=VIN1" in html
        assert db.processes[0]["cost"] is None and importer.load_last_run() is None

        html = text(client.post(PREFIX + "/run", headers=AUTH,
                                data={"csrf_token": token, "mode": "write", "lookback": "7"}))
        assert db.processes[0]["cost"] == 250
        assert importer.load_last_run()["source"] == "web"
        assert "Humpolec" in text(client.get(PREFIX + "/runs/last", headers=AUTH))

    def test_not_logged_in_is_shown(self, client, tesla):
        html = text(client.post(PREFIX + "/run", headers=AUTH,
                                data={"csrf_token": csrf(client), "mode": "preview"}))
        assert "Not logged in" in html

    def test_invalid_lookback(self, client):
        r = client.post(PREFIX + "/run", headers=AUTH,
                        data={"csrf_token": csrf(client), "mode": "preview", "lookback": "x"})
        assert r.status_code == 400

    def test_only_one_run_at_a_time(self, app):
        runner = app.extensions["runner"]
        runner.lock.acquire()
        try:
            assert "in progress" in runner.run()["error"]
        finally:
            runner.lock.release()


class TestCharges:
    def test_lists_and_filters(self, client):
        html = text(client.get(PREFIX + "/charges", headers=AUTH))
        assert "Humpolec" in html and "Brno" in html
        html = text(client.get(PREFIX + "/charges?missing=1", headers=AUTH))
        assert "Humpolec" in html and "Brno" not in html

    def test_set_and_clear_cost(self, client, db):
        token = csrf(client)
        client.post(PREFIX + "/charges/10/cost", headers=AUTH, data={"csrf_token": token, "cost": "12,5"})
        assert db.processes[0]["cost"] == 12.5
        client.post(PREFIX + "/charges/10/cost", headers=AUTH, data={"csrf_token": token, "cost": ""})
        assert db.processes[0]["cost"] is None

    def test_invalid_cost(self, client, db):
        r = client.post(PREFIX + "/charges/10/cost", headers=AUTH, follow_redirects=True,
                        data={"csrf_token": csrf(client), "cost": "abc"})
        assert "not a valid cost" in text(r) and db.processes[0]["cost"] is None

    def test_unknown_charge(self, client):
        r = client.post(PREFIX + "/charges/999/cost", headers=AUTH,
                        data={"csrf_token": csrf(client), "cost": "1"})
        assert r.status_code == 404


class TestTesla:
    def test_login_with_pasted_url_checks_state(self, client):
        html = text(client.get(PREFIX + "/tesla", headers=AUTH))
        state = re.search(r"state=([\w-]+)", html).group(1)
        token = csrf(client)
        r = client.post(PREFIX + "/tesla/login", headers=AUTH, follow_redirects=True,
                        data={"csrf_token": token, "callback": "https://localhost/callback?code=1&state=bad"})
        assert "expired" in text(r) and not FakeFleet.logged_in_flag
        client.get(PREFIX + "/tesla", headers=AUTH)  # new attempt, new state
        with client.session_transaction(PREFIX + "/") as s:
            state = s["oauth_state"]
        r = client.post(PREFIX + "/tesla/login", headers=AUTH, follow_redirects=True,
                        data={"csrf_token": token,
                              "callback": f"https://localhost/callback?code=1&state={state}"})
        assert "Logged in to Tesla" in text(r) and FakeFleet.logged_in_flag

    def test_callback_route(self, client):
        client.get(PREFIX + "/tesla", headers=AUTH)
        with client.session_transaction(PREFIX + "/") as s:
            state = s["oauth_state"]
        r = client.get(f"{PREFIX}/tesla/callback?code=1&state={state}", headers=AUTH, follow_redirects=True)
        assert "Logged in to Tesla" in text(r)

    def test_vehicles_and_logout(self, client):
        FakeFleet.logged_in_flag = True
        token = csrf(client)
        html = text(client.post(PREFIX + "/tesla/vehicles", headers=AUTH, follow_redirects=True,
                                data={"csrf_token": token}))
        assert "VIN3" in html and "Blue" in html
        client.post(PREFIX + "/tesla/logout", headers=AUTH, data={"csrf_token": token})
        assert not FakeFleet.logged_in_flag

    def test_generate_and_serve_public_key(self, client, app):
        token = csrf(client)
        client.post(PREFIX + "/tesla/keys", headers=AUTH, data={"csrf_token": token})
        pem = client.get(web.PUBLIC_KEY_PATH)  # public, at the domain root
        assert pem.status_code == 200 and "BEGIN PUBLIC KEY" in text(pem)
        private, _ = web.key_paths()
        assert oct(private.stat().st_mode & 0o777) == "0o600"
        assert "BEGIN PUBLIC KEY" in text(client.get(PREFIX + "/tesla/public-key.pem", headers=AUTH))

    def test_no_key_yet(self, client):
        assert client.get(web.PUBLIC_KEY_PATH).status_code == 404

    def test_register(self, client, monkeypatch):
        monkeypatch.setattr(web, "check_published_key", lambda d: (True, "ok"))
        r = client.post(PREFIX + "/tesla/register", headers=AUTH, follow_redirects=True,
                        data={"csrf_token": csrf(client), "domain": "https://Me.github.io/"})
        assert "App registered for me.github.io" in text(r)
        assert web.load_partner()["domain"] == "me.github.io"

    def test_register_invalid_domain(self, client):
        r = client.post(PREFIX + "/tesla/register", headers=AUTH, follow_redirects=True,
                        data={"csrf_token": csrf(client), "domain": "not a domain"})
        assert "Enter a domain" in text(r)

    def test_invoice(self, client):
        r = client.get(PREFIX + "/invoice/abc-1?vin=5YJ3E7EB0KF000000", headers=AUTH)
        assert r.mimetype == "application/pdf"
        assert client.get(PREFIX + "/invoice/a..b", headers=AUTH).status_code == 404


class TestSettings:
    def form(self, token, **overrides):
        data = {"csrf_token": token, "schedule_time": "02:30", "timezone": "Europe/Prague",
                "lookback_days": "7", "target_currency": "eur", "tesla_vin": "",
                "time_tolerance_s": "300", "tesla_fleet_region": "eu"}
        data.update(overrides)
        return data

    def test_save_and_reschedule(self, client, app):
        r = client.post(PREFIX + "/settings", headers=AUTH, data=self.form(csrf(client)))
        assert r.status_code == 302
        saved = importer.load_web_settings()
        assert saved["TARGET_CURRENCY"] == "EUR" and saved["TESLA_VIN"] == "all"
        assert importer.TIME_TOLERANCE_S == 300 and importer.SCHEDULE_TIME == "02:30"
        next_run = app.extensions["runner"].next_run
        local = next_run.astimezone(web.ZoneInfo("Europe/Prague"))
        assert (local.hour, local.minute) == (2, 30)

    def test_empty_schedule_turns_it_off(self, client, app):
        client.post(PREFIX + "/settings", headers=AUTH, data=self.form(csrf(client), schedule_time=""))
        assert importer.SCHEDULE_TIME == "" and app.extensions["runner"].next_run is None

    @pytest.mark.parametrize("field,value,message", [
        ("timezone", "Mars/Base", "Unknown timezone"),
        ("tesla_vin", "123", "Invalid VIN"),
        ("lookback_days", "abc", "whole number"),
    ])
    def test_invalid_input_not_saved(self, client, field, value, message):
        r = client.post(PREFIX + "/settings", headers=AUTH, data=self.form(csrf(client), **{field: value}))
        assert r.status_code == 400 and message in text(r)
        assert importer.load_web_settings() == {}


def test_next_occurrence_handles_dst():
    # 2026-10-25 is the end of summer time in Europe/Prague.
    now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    nxt = web.Runner.next_occurrence("03:00", "Europe/Prague", now)
    assert nxt.astimezone(web.ZoneInfo("Europe/Prague")).hour == 3
    assert web.Runner.next_occurrence("", "UTC") is None


class TestTranslations:
    def test_czech_from_browser(self, client):
        html = text(client.get(PREFIX + "/", headers={**AUTH, "Accept-Language": "cs-CZ,cs;q=0.9"}))
        assert "Přehled" in html

    def test_switcher_is_remembered(self, client):
        client.get(PREFIX + "/settings?locale=cs", headers=AUTH)
        assert "Nastavení" in text(client.get(PREFIX + "/", headers=AUTH))

    def test_unsupported_language_falls_back_to_english(self, client):
        html = text(client.get(PREFIX + "/", headers={**AUTH, "Accept-Language": "ja"}))
        assert "Overview" in html


class TestCatalogs:
    """Every text in the templates/code is translated and the .mo files are compiled."""

    def catalogs(self):
        import glob
        from babel.messages.pofile import read_po
        for path in glob.glob("translations/*/LC_MESSAGES/messages.po"):
            with open(path, "rb") as f:
                yield path, read_po(f)

    def test_complete(self):
        from babel.messages.pofile import read_po
        with open("translations/messages.pot", "rb") as f:
            template = {m.id for m in read_po(f) if m.id}
        for path, catalog in self.catalogs():
            translated = {m.id for m in catalog if m.id and m.string}
            assert template - translated == set(), path

    def test_compiled_mo_is_up_to_date(self):
        from babel.messages.mofile import read_mo
        for path, catalog in self.catalogs():
            with open(path[:-3] + ".mo", "rb") as f:
                compiled = read_mo(f)
            assert {m.id: m.string for m in catalog if m.id} == \
                {m.id: m.string for m in compiled if m.id}, f"run: pybabel compile -d translations ({path})"
