"""UStVA: Entwurf speichern, Freigabe, Absenden aus dem gespeicherten Entwurf (ohne echten Browser)."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from mcp.server.mcpserver import Image

from elster_mcp import server
from elster_mcp.config import get_config
from elster_mcp.models import UstvaReport
from elster_mcp.portal import ustva as ustva_mod
from elster_mcp.portal.drafts import pick_draft
from elster_mcp.portal.ustva import UstvaFlow, parse_amount, parse_summary, verify_summary
from elster_mcp.security import AuditLog
from elster_mcp.sessions import sessions

NAME = "UStVA 2026 - III. Kalendervierteljahr"
ROWS_OK = [
    ["Kennzahl", "Wert"],
    ["Jahr", "", "2026"],
    ["Zeitraum", "", "III. Kalendervierteljahr"],
    ["Steuernummer", "", "030 806 31273"],
    ["Name", "", "Biegel"],
    ["Sonstige Leistungen … (Bemessungsgrundlage)", "46", "214 €"],
    ["Sonstige Leistungen … (Steuer)", "47", "40,66 €"],
    ["Verbleibende Umsatzsteuer-Vorauszahlung", "83", "40,66 €"],
]
REPORT = {"46": 214, "47": 40.66}


def test_pick_draft_newest_exact_name():
    rows = [{"id": "643507787", "name": NAME}, {"id": "643507999", "name": NAME},
            {"id": "700000000", "name": "UStVA 2026 - II. Kalendervierteljahr"}]
    assert pick_draft(rows, NAME) == "643507999"
    assert pick_draft(rows, "UStVA 2025 - III. Kalendervierteljahr") is None


@pytest.mark.parametrize(("text", "value"), [("214 €", 214.0), ("40,66 €", 40.66), ("1.234,56 €", 1234.56),
                                             ("-12,00 €", -12.0), ("030 806 31273", None), ("", None)])
def test_parse_amount(text, value):
    assert parse_amount(text) == value


def test_parse_summary():
    general, amounts = parse_summary(ROWS_OK)
    assert general["Jahr"] == "2026" and general["Zeitraum"] == "III. Kalendervierteljahr"
    assert amounts == {"46": 214.0, "47": 40.66, "83": 40.66}


def test_verify_summary_ok():
    assert verify_summary(ROWS_OK, REPORT, 2026, "III. Kalendervierteljahr") == []


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda r: r.__setitem__(6, ["x", "47", "40,67 €"]), "Kz47"),
        (lambda r: r.append(["Vorsteuer", "66", "10,00 €"]), "Kz66"),
        (lambda r: r.__setitem__(2, ["Zeitraum", "", "II. Kalendervierteljahr"]), "Zeitraum"),
        (lambda r: r.__setitem__(1, ["Jahr", "", "2025"]), "Jahr"),
        (lambda r: r.pop(5), "Kz46 fehlt"),
    ],
)
def test_verify_summary_detects_mismatch(mutate, needle):
    rows = [list(r) for r in ROWS_OK]
    mutate(rows)
    problems = verify_summary(rows, REPORT, 2026, "III. Kalendervierteljahr")
    assert any(needle in p for p in problems), problems


class FakePage:
    def __init__(self, rows):
        self.rows = rows
        self.url = "https://www.elster.de/eportal/formulare-leistungen/alleformulare/ustvaeru"

    async def evaluate(self, script, *args):
        return self.rows


def _patch_flow(monkeypatch, rows, calls):
    open_now = {"n": 0}

    @asynccontextmanager
    async def fake_open(self):
        open_now["n"] += 1
        calls.append("open")
        try:
            yield FakePage(rows)
        finally:
            open_now["n"] -= 1

    async def noop(self, *a, **k):
        return None

    async def walk(self, *a, **k):
        return []

    async def save_draft(self, page):
        calls.append("save_draft")
        return "643507787", NAME

    async def open_draft(self, page, draft_id):
        calls.append(f"open_draft:{draft_id}")

    async def submit(self, page):
        calls.append("submit")
        return {"ticket": "T-1", "auftrag": ""}

    for name, fn in {"login": noop, "_open_form": noop, "_select_period": noop, "_walk_pages": walk,
                     "_run_pruefung": noop, "screenshot": noop, "_save_draft": save_draft,
                     "_open_draft": open_draft, "_goto_send_page": noop, "_submit": submit}.items():
        monkeypatch.setattr(UstvaFlow, name, fn)
    monkeypatch.setattr(UstvaFlow, "open", fake_open)
    return open_now


async def _drive(rows, monkeypatch, calls):
    open_now = _patch_flow(monkeypatch, rows, calls)
    cfg = get_config()
    flow = UstvaFlow(cfg, AuditLog(cfg.security.audit_log))
    s = flow.start(UstvaReport(year=2026, period="Q3", report=REPORT))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if s.status == "AWAITING_CONFIRM":
            break
    assert s.status == "AWAITING_CONFIRM"
    assert s.draft == {"id": "643507787", "name": NAME}
    assert s.summary["draftId"] == "643507787"
    assert s.confirmation_code
    assert open_now["n"] == 0, "Browser darf während der Freigabe nicht offen sein"
    s.confirm_event.set()
    await asyncio.wait_for(s.done_event.wait(), 2)
    return s


def test_confirm_submits_saved_draft(monkeypatch):
    calls = []
    s = asyncio.run(_drive(ROWS_OK, monkeypatch, calls))
    assert calls == ["open", "save_draft", "open", "open_draft:643507787", "submit"]
    assert s.status == "DONE"
    assert s.result["draftId"] == "643507787"


def test_confirm_aborts_when_draft_differs(monkeypatch):
    calls = []
    rows = [list(r) for r in ROWS_OK]
    rows[6] = ["x", "47", "99,00 €"]
    s = asyncio.run(_drive(rows, monkeypatch, calls))
    assert "submit" not in calls
    assert s.status == "ERROR"
    assert "nichts übermittelt" in s.errors[-1]


def test_session_status_returns_screenshot(tmp_path):
    s = sessions.create("USTVA")
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
    s.screenshot_path = str(png)
    s.draft = {"id": "643507787", "name": NAME}

    s.status = "FILLING_PAGES"
    out = asyncio.run(server.elster_session_status(s.id))
    assert len(out) == 1

    s.status = "AWAITING_CONFIRM"
    out = asyncio.run(server.elster_session_status(s.id))
    assert json.loads(out[0].text)["draft"]["id"] == "643507787"
    assert isinstance(out[1], Image)
    assert len(asyncio.run(server.elster_session_status(s.id, includeScreenshot=False))) == 1


def test_helpers_exported():
    assert "83" in ustva_mod.COMPUTED_KZ
