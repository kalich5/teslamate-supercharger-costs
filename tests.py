"""
Tests for importer.py (no network, no database).

Run with:
    pytest tests.py tests_web.py -v
"""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

import importer


def iso(days_ago: float, hour: int = 12) -> str:
    dt = (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(
        hour=hour, minute=0, second=0, microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def session(vin="VIN1", days_ago=1, total=250.0, currency="CZK", idle=0.0, **extra):
    fees = [{"feeType": "CHARGING", "currencyCode": currency, "pricingType": "PAYMENT",
             "totalDue": total, "usageBase": 25.0, "uom": "kwh", "rateBase": 10.0}]
    if idle:
        fees.append({"feeType": "CONGESTION", "currencyCode": currency, "pricingType": "PAYMENT",
                     "totalDue": idle})
    return {"vin": vin, "sessionId": 1, "chargeStartDateTime": iso(days_ago),
            "siteLocationName": "Humpolec", "fees": fees, **extra}


# ── Fake TeslaMate database ──────────────────────────────────────────────────

class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = 0
        self._one = None
        self._all = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        q = " ".join(query.split())
        self.db.queries.append((q, params))
        self._one, self._all = None, []
        if "information_schema" in q:
            self._one = ("timestamp without time zone",)
        elif q.startswith("SHOW TimeZone"):
            self._one = ("UTC",)
        elif q.startswith("SELECT id, vin FROM cars"):
            self._all = list(self.db.cars.items())
        elif "FROM charging_processes" in q and "LIMIT 1" in q:
            start, car_id = params[1], params[3]
            for cp in self.db.processes:
                if (car_id is None or cp["car_id"] == car_id) and \
                        abs((cp["start"] - start).total_seconds()) < params[2]:
                    self._one = (cp["id"], cp["start"].replace(tzinfo=None), cp["cost"])
                    break
        elif q.startswith("UPDATE charging_processes"):
            cost, cp_id = params
            for cp in self.db.processes:
                if cp["id"] == cp_id:
                    cp["cost"] = cost
                    self.rowcount = 1
        else:
            self._all = self.db.rows_for(q, params)

    def fetchone(self):
        return self._one if self._one is not None else (self._all[0] if self._all else None)

    def fetchall(self):
        return self._all

    def close(self):
        pass


class FakeDB:
    def __init__(self, processes=None, cars=None):
        self.processes = processes or []
        self.cars = cars if cars is not None else {1: "VIN1", 2: "VIN2"}
        self.queries = []
        self.commits = 0

    def rows_for(self, query, params):
        return []

    # connection API
    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


def process(cp_id, car_id, days_ago, cost=None, offset_s=30):
    start = datetime.fromisoformat(iso(days_ago).replace("Z", "+00:00")) + timedelta(seconds=offset_s)
    return {"id": cp_id, "car_id": car_id, "start": start, "cost": cost}


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    """Clean configuration for each test, data files in tmp_path."""
    monkeypatch.setattr(importer, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(importer, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(importer, "LAST_RUN_FILE", str(tmp_path / "last_run.json"))
    env = {"TESLAMATE_DB_PASS": "x", "TESLA_CLIENT_ID": "cid", "TESLA_CLIENT_SECRET": "sec",
           "TESLA_FLEET_TOKEN_FILE": str(tmp_path / "fleet_token.json"), "LOG_FILE": ""}
    monkeypatch.setattr(importer.os, "environ", env)
    importer.configure()
    yield env
    importer.configure(environ={}, use_file=False)


# ── Configuration ────────────────────────────────────────────────────────────

class TestConfigure:
    def test_fleet_mode_when_client_id_set(self):
        assert importer.TESLA_API_MODE == "fleet"

    def test_ownership_without_client_id(self, config):
        del config["TESLA_CLIENT_ID"]
        importer.configure()
        assert importer.TESLA_API_MODE == "ownership"
        with pytest.raises(importer.ConfigError, match="TESLA_EMAIL"):
            importer.validate_config()

    def test_web_settings_override_environment(self, config):
        config["LOOKBACK_DAYS"] = "30"
        importer.save_web_settings({"LOOKBACK_DAYS": "7", "TESLA_VIN": "vin1, vin2",
                                    "NOT_ALLOWED": "x"})
        importer.configure()
        assert importer.LOOKBACK_DAYS == 7
        assert importer.TESLA_VINS == ["VIN1", "VIN2"]
        assert "NOT_ALLOWED" not in json.loads(open(importer.SETTINGS_FILE).read())

    def test_all_and_none_override_environment(self, config):
        config.update(TESLA_VIN="VIN1", TARGET_CURRENCY="EUR", SCHEDULE_TIME="02:00")
        importer.save_web_settings({"TESLA_VIN": "all", "TARGET_CURRENCY": "NONE",
                                    "SCHEDULE_TIME": "off"})
        importer.configure()
        assert importer.TESLA_VINS == [] and importer.TARGET_CURRENCY == "" \
            and importer.SCHEDULE_TIME == ""

    @pytest.mark.parametrize("key,value", [("LOOKBACK_DAYS", "abc"), ("SCHEDULE_TIME", "25:99"),
                                           ("TESLA_API_MODE", "x"), ("TARGET_CURRENCY", "EURO"),
                                           ("TESLA_FLEET_REGION", "mars")])
    def test_invalid_values_rejected_and_nothing_changed(self, key, value):
        before = importer.LOOKBACK_DAYS
        with pytest.raises(importer.ConfigError):
            importer.configure(overrides={key: value, "LOOKBACK_DAYS": "5"}
                               if key != "LOOKBACK_DAYS" else {key: value})
        assert importer.LOOKBACK_DAYS == before

    def test_missing_db_password(self, config):
        del config["TESLAMATE_DB_PASS"]
        importer.configure()
        with pytest.raises(importer.ConfigError, match="TESLAMATE_DB_PASS"):
            importer.validate_config()


# ── Cost extraction ──────────────────────────────────────────────────────────

def test_extract_cost_with_idle_fee():
    cost = importer.extract_cost(session(total=200, idle=50))
    assert cost["original_total"] == 250 and cost["congestion"] == 50 and cost["kwh"] == 25


def test_free_session_has_no_cost():
    s = session()
    s["fees"][0]["pricingType"] = "NO_CHARGE"
    assert importer.extract_cost(s) is None


# ── Matching & writing ───────────────────────────────────────────────────────

class TestImport:
    def test_writes_cost_to_matching_car_only(self):
        # Two cars charged at the same time: each cost goes to its own car.
        db = FakeDB([process(10, 1, 1), process(20, 2, 1)])
        stats = importer.import_to_teslamate(
            [session("VIN1", total=100), session("VIN2", total=200)], False, 30, conn=db)
        assert stats["updated"] == 2
        assert {cp["id"]: cp["cost"] for cp in db.processes} == {10: 100, 20: 200}
        assert [r["status"] for r in stats["results"]] == ["updated", "updated"]

    def test_existing_cost_is_kept(self):
        db = FakeDB([process(10, 1, 1, cost=5)])
        stats = importer.import_to_teslamate([session()], False, 30, conn=db)
        assert stats["already_set"] == 1 and db.processes[0]["cost"] == 5
        assert stats["results"][0]["tm_cost"] == 5

    def test_dry_run_writes_nothing(self):
        db = FakeDB([process(10, 1, 1)])
        stats = importer.import_to_teslamate([session()], True, 30, conn=db)
        assert stats["would_update"] == 1 and db.processes[0]["cost"] is None and db.commits == 0

    def test_not_found_and_too_old(self):
        db = FakeDB([process(10, 1, 1, offset_s=900)])
        stats = importer.import_to_teslamate([session(), session(days_ago=40)], False, 30, conn=db)
        assert stats["not_found"] == 1 and stats["too_old"] == 1
        assert stats["results"][0]["status"] == "not_found"

    def test_invoices_in_results(self):
        db = FakeDB([process(10, 1, 1)])
        s = session(invoices=[{"contentId": "abc-123", "fileName": "x.pdf", "invoiceType": "IMMEDIATE"}])
        stats = importer.import_to_teslamate([s], True, 30, conn=db)
        assert stats["results"][0]["invoices"] == [{"id": "abc-123", "name": "x.pdf", "type": "IMMEDIATE"}]


# ── Fleet API ────────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status, body=None, content=b""):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body) if body is not None else ""
        self.content = content

    def json(self):
        return self._body


class FakeHttp:
    """Stands in for requests.Session; *history* maps VIN -> list of sessions."""

    def __init__(self, history=None, vehicles=("VIN1", "VIN2")):
        self.history = history or {}
        self.vehicles = vehicles
        self.calls = []
        self.refreshes = 0

    def post(self, url, data=None, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, data))
        if data and data.get("grant_type") == "refresh_token":
            self.refreshes += 1
        return FakeResponse(200, {"access_token": f"at{len(self.calls)}", "refresh_token": "rt-new",
                                  "expires_in": 28800})

    def get(self, url, params=None, headers=None, timeout=None):
        path = url.split(".com", 1)[1]
        self.calls.append(("GET", path, params))
        if path == "/api/1/vehicles":
            return FakeResponse(200, {"response": [{"vin": v, "display_name": v} for v in self.vehicles]})
        if path == "/api/1/dx/charging/history":
            items = self.history.get(params["vin"], [])
            page = params["pageNo"]
            return FakeResponse(200, {"data": items[(page - 1) * 25: page * 25],
                                      "totalResults": len(items)})
        return FakeResponse(404, {"error": "not found"})


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    token = {"refresh_token": "rt", "access_token": "at", "expires_at": int(time.time()) + 3600}
    (tmp_path / "fleet_token.json").write_text(json.dumps(token))
    http = FakeHttp()
    original = importer.FleetClient.__init__

    def init(self):
        original(self)
        self.http = http

    monkeypatch.setattr(importer.FleetClient, "__init__", init)
    return http


class TestFleet:
    def test_paginates_all_cars_with_server_side_filter(self, fleet):
        fleet.history = {"VIN1": [session("VIN1", days_ago=d / 10) for d in range(28)],
                         "VIN2": [session("VIN2")]}
        sessions = importer.fetch_charging_sessions(7)
        assert len(sessions) == 29
        history_calls = [c for c in fleet.calls if c[1] == "/api/1/dx/charging/history"]
        assert [(c[2]["vin"], c[2]["pageNo"]) for c in history_calls] == \
            [("VIN1", 1), ("VIN1", 2), ("VIN2", 1)]
        assert all("startTime" in c[2] for c in history_calls)

    def test_vin_filter(self, fleet):
        importer.configure(overrides={"TESLA_VIN": "VIN2"})
        fleet.history = {"VIN2": [session("VIN2")]}
        assert len(importer.fetch_charging_sessions(7)) == 1
        assert not any(c[2] and c[2].get("vin") == "VIN1" for c in fleet.calls)

    def test_unknown_vin_is_an_error(self, fleet):
        importer.configure(overrides={"TESLA_VIN": "VIN9"})
        with pytest.raises(importer.ImporterError, match="VIN9"):
            importer.fetch_charging_sessions(7)

    def test_expired_token_is_refreshed_and_rotated_token_saved(self, fleet, tmp_path):
        path = tmp_path / "fleet_token.json"
        path.write_text(json.dumps({"refresh_token": "rt", "access_token": "old", "expires_at": 0}))
        importer.fetch_charging_sessions(7)
        assert fleet.refreshes == 1
        saved = json.loads(path.read_text())
        assert saved["refresh_token"] == "rt-new" and saved["expires_at"] > time.time()
        assert oct(path.stat().st_mode & 0o777) == "0o600"

    def test_not_logged_in(self, fleet, tmp_path):
        (tmp_path / "fleet_token.json").unlink()
        with pytest.raises(importer.NotLoggedIn):
            importer.fetch_charging_sessions(7)

    def test_login_with_callback_url_checks_state(self, fleet, tmp_path):
        (tmp_path / "fleet_token.json").unlink()
        client = importer.FleetClient()
        url = client.authorize_url("s1")
        assert "state=s1" in url and "vehicle_charging_cmds" in url
        with pytest.raises(importer.FleetApiError, match="expired"):
            client.complete_login("https://localhost/callback?code=C&state=other", expected_state="s1")
        client.complete_login("https://localhost/callback?code=C&state=s1", expected_state="s1")
        exchange = fleet.calls[-1][2]
        assert exchange["grant_type"] == "authorization_code" and exchange["code"] == "C"
        assert importer.FleetClient().logged_in

    def test_run_reports_error_instead_of_crashing(self, fleet, tmp_path):
        (tmp_path / "fleet_token.json").unlink()
        result = importer.run(conn=FakeDB())
        assert "Not logged in" in result["error"]
        assert importer.load_last_run()["error"] == result["error"]

    def test_run_saves_report(self, fleet):
        fleet.history = {"VIN1": [session("VIN1")]}
        db = FakeDB([process(10, 1, 1)])
        result = importer.run(lookback_days=7, conn=db)
        assert result["updated"] == 1 and "error" not in result
        last = importer.load_last_run()
        assert last["updated"] == 1 and isinstance(last["results"][0]["start"], datetime)
