import asyncio
import json

import pytest
from pydantic import ValidationError

from elster_mcp.config import get_config
from elster_mcp.models import EstData, EurData, UstvaReport, normalize_period
from elster_mcp.security import (
    AuditLog,
    BearerAuthMiddleware,
    codes_match,
    confirmation_code,
    host_allowed,
    safe_child,
)
from elster_mcp.xml import detect_reverse_charge, generate_ustva_xml

# ----------------------------- Validierung ----------------------------- #

@pytest.mark.parametrize("raw,expected", [(1, "01"), ("12", "12"), ("Q1", "41"), ("q4", "44")])
def test_normalize_period(raw, expected):
    assert normalize_period(raw) == expected


@pytest.mark.parametrize("bad", [0, 13, "Q5", "Januar", ""])
def test_normalize_period_rejects(bad):
    with pytest.raises(ValueError):
        normalize_period(bad)


def test_ustva_normalizes_keys():
    u = UstvaReport(year=2025, period="Q1", report={"Kz81": 1000.004, "066": 190})
    assert u.report == {"81": 1000.0, "66": 190.0}


@pytest.mark.parametrize("report", [
    {"99": 1},                 # unbekannte Kennziffer
    {"66": -10},               # negative Vorsteuer
    {"81": float("inf")},      # keine endliche Zahl
    {"81": 1e12},              # unplausibel groß
    {},                        # leer
])
def test_ustva_rejects(report):
    with pytest.raises(ValidationError):
        UstvaReport(year=2025, period=1, report=report)


def test_ustva_rejects_year():
    with pytest.raises(ValidationError):
        UstvaReport(year=1999, period=1, report={"81": 1})


def test_eur_rejects_unknown_field():
    with pytest.raises(ValidationError):
        EurData(year=2025, data={"hackerfeld": 1})
    assert EurData(year=2025, data={"afa": 800}).data == {"afa": 800.0}


def test_est_rejects_injection_in_hint():
    with pytest.raises(ValidationError):
        EstData(year=2025, data={"'); alert(1); //": "x"})


# ----------------------------- XML ----------------------------- #

def test_xml(monkeypatch):
    monkeypatch.setenv("ELSTER_TAX_NUMBER", "012/345/67890")
    xml = generate_ustva_xml(get_config(), UstvaReport(year=2025, period="Q2", report={"81": 1000, "66": 0, "86": 50.5}))
    assert "<Zeitraum>42</Zeitraum>" in xml
    assert "<Kz81>1000.00</Kz81>" in xml
    assert "<Kz86>50.50</Kz86>" in xml
    assert "Kz66" not in xml
    assert "<Steuernummer>01234567890</Steuernummer>" in xml


def test_reverse_charge(write_config):
    write_config({"ustva": {"reverseChargeSuppliers": [{"pattern": "google\\s+ireland", "region": "EU", "name": "Google IE"}]}})
    cfg = get_config()
    assert detect_reverse_charge(cfg, "Google Ireland Ltd", "") == {"region": "EU", "supplier": "Google IE"}
    assert detect_reverse_charge(cfg, "Anthropic PBC", "Reverse Charge") == {"region": "NON_EU", "supplier": "Anthropic PBC"}
    assert detect_reverse_charge(cfg, "Bäcker Müller", "Brötchen") is None


# ----------------------------- Sicherheit ----------------------------- #

def test_confirmation_code_bound_to_payload():
    nonce = "n" * 32
    a = confirmation_code("s1", {"81": 100}, nonce)
    assert a == confirmation_code("s1", {"81": 100}, nonce)
    assert a != confirmation_code("s1", {"81": 101}, nonce)
    assert a != confirmation_code("s2", {"81": 100}, nonce)
    assert a != confirmation_code("s1", {"81": 100}, "x" * 32)
    assert codes_match(a, a.lower())
    assert not codes_match(a, None)
    assert not codes_match(a, "")


def test_host_allowlist():
    hosts = ["elster.de"]
    assert host_allowed("https://www.elster.de/eportal/start", hosts)
    assert host_allowed("https://elster.de/x", hosts)
    assert not host_allowed("http://www.elster.de/", hosts)          # kein TLS
    assert not host_allowed("https://elster.de.evil.com/", hosts)
    assert not host_allowed("https://evilelster.de/", hosts)
    assert not host_allowed("https://tracker.example.com/pixel", hosts)
    assert host_allowed("data:image/png;base64,AAA", hosts)


def test_safe_child_prevents_traversal(tmp_path):
    p = safe_child(tmp_path, "../../etc/passwd.pdf")
    assert p.parent == tmp_path.resolve()
    assert p.suffix == ".pdf"


def test_audit_log_redacts_and_restricts(tmp_path, monkeypatch):
    monkeypatch.setenv("ELSTER_PASSWORD", "sehr-geheim-99")
    get_config()
    audit = AuditLog(tmp_path / "a" / "audit.jsonl")
    audit.write("test", note="pw=sehr-geheim-99")
    line = (tmp_path / "a" / "audit.jsonl").read_text()
    assert "sehr-geheim-99" not in line
    assert json.loads(line)["event"] == "test"
    assert oct((tmp_path / "a" / "audit.jsonl").stat().st_mode & 0o777) == "0o600"


def _run_asgi(mw, headers):
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw.app = app

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request"}

    asyncio.run(mw({"type": "http", "headers": headers, "client": ("1.2.3.4", 1)}, receive, send))
    return sent[0]["status"]


def test_bearer_middleware():
    token = "t" * 40
    mw = BearerAuthMiddleware(None, token)
    assert _run_asgi(mw, []) == 401
    assert _run_asgi(mw, [(b"authorization", b"Bearer falsch")]) == 401
    assert _run_asgi(mw, [(b"authorization", f"Bearer {token}".encode())]) == 200
    with pytest.raises(ValueError):
        BearerAuthMiddleware(None, "kurz")
