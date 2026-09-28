"""Belegnachreichung: Eingaben, Dateien, Abgleich und Zwei-Phasen-Ablauf (ohne echten Browser)."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from pydantic import ValidationError

from elster_mcp import server
from elster_mcp.config import get_config, reset_config_cache
from elster_mcp.models import BelegeRequest, zeitraum_label
from elster_mcp.portal import belege
from elster_mcp.portal.belege import BelegeFlow, prepare_files, verify_send_page
from elster_mcp.security import AuditLog
from elster_mcp.sessions import sessions

PDF = b"%PDF-1.4\n%test\n"


@pytest.fixture
def downloads(tmp_path, monkeypatch):
    d = tmp_path / "downloads"
    d.mkdir()
    (d / "2026-09-01_Google_Rechnung.pdf").write_bytes(PDF)
    (d / "kein_pdf.pdf").write_bytes(b"MZ....")
    monkeypatch.setenv("ELSTER_DOWNLOAD_DIR", str(d))
    reset_config_cache()
    return d


def _req(**kw):
    base = {"year": 2026, "zeitraum": "Q3", "text": "Angeforderte Rechnung, Schreiben vom 01.10.2026",
            "files": ["2026-09-01_Google_Rechnung.pdf"]}
    return BelegeRequest(**{**base, **kw})


@pytest.mark.parametrize(("z", "label"), [("Q3", "3. Kalendervierteljahr"), (9, "September"),
                                          ("01", "Januar"), ("Jahr", "Kalenderjahr")])
def test_zeitraum_label(z, label):
    assert zeitraum_label(z) == label


@pytest.mark.parametrize(
    ("kw", "needle"),
    [
        ({"zeitraum": None}, "Pflicht"),
        ({"text": "   "}, "leer"),
        ({"text": "x" * 15001}, "15.000"),
        ({"files": []}, "1 bis 20"),
        ({"files": [f"{i}.pdf" for i in range(21)]}, "1 bis 20"),
        ({"files": ["a.pdf", "a.pdf"]}, "doppelt"),
        ({"files": ["rechnung.docx"]}, ".pdf und .xml"),
        ({"steuerart": "Erbschaftsteuer"}, "steuerart"),
    ],
)
def test_request_validation(kw, needle):
    with pytest.raises(ValidationError, match=needle):
        _req(**kw)


def test_request_ok_without_zeitraum_for_est():
    r = _req(steuerart="Einkommensteuererklärung", zeitraum=None)
    assert r.zeitraum is None


def test_prepare_files(downloads, monkeypatch):
    files = prepare_files(downloads, ["2026-09-01_Google_Rechnung.pdf"])
    assert files[0]["size"] == len(PDF) and len(files[0]["sha256"]) == 64
    with pytest.raises(ValueError, match="keine gültige PDF"):
        prepare_files(downloads, ["kein_pdf.pdf"])
    with pytest.raises((ValueError, FileNotFoundError)):
        prepare_files(downloads, ["../geheim.pdf"])
    monkeypatch.setattr(belege, "MAX_FILE_BYTES", 5)
    with pytest.raises(ValueError, match="10 MB"):
        prepare_files(downloads, ["2026-09-01_Google_Rechnung.pdf"])


def test_verify_send_page():
    files = [{"name": "2026-09-01_Google_Rechnung.pdf"}]
    ok = "Belege Umsatzsteuer-Voranmeldung Jahr 2026 Zeitraum 3. Kalendervierteljahr Anhänge 2026-09-01_Google_Rechnung.pdf"
    assert verify_send_page(ok, _req(), files) == []
    problems = verify_send_page(ok.replace("3. Kalendervierteljahr", "2. Kalendervierteljahr"), _req(), files)
    assert any("Zeitraum" in p for p in problems)
    assert any("Anhang" in p for p in verify_send_page(ok.replace("Google", "X"), _req(), files))


def _patch(monkeypatch, send_text, calls):
    open_now = {"n": 0}

    class FakePage:
        url = "https://www.elster.de/x/Anhaenge"

        async def evaluate(self, script, *a):
            return send_text

    @asynccontextmanager
    async def fake_open(self):
        open_now["n"] += 1
        calls.append("open")
        try:
            yield FakePage()
        finally:
            open_now["n"] -= 1

    async def noop(self, *a, **k):
        return None

    async def save_draft(self, page):
        calls.append("save_draft")
        return "700000001", "Belegnachreichung 2026"

    async def upload(self, page, files):
        calls.append(f"upload:{len(files)}")

    async def open_draft(self, page, draft_id):
        calls.append(f"open_draft:{draft_id}")

    async def submit(self, page):
        calls.append("submit")
        return {"ticket": "T-9", "auftrag": ""}

    for name, fn in {"login": noop, "_open_form": noop, "_fill_startseite": noop, "_goto": noop,
                     "_fill_person": noop, "_fill_belege": noop, "_upload": upload, "_pruefen": noop,
                     "screenshot": noop, "_save_draft": save_draft, "_open_draft": open_draft,
                     "_goto_send_page": noop, "_submit": submit}.items():
        monkeypatch.setattr(BelegeFlow, name, fn)
    monkeypatch.setattr(BelegeFlow, "open", fake_open)
    return open_now


async def _drive(monkeypatch, send_text, calls):
    open_now = _patch(monkeypatch, send_text, calls)
    cfg = get_config()
    s = BelegeFlow(cfg, AuditLog(cfg.security.audit_log)).start(_req())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if s.status == "AWAITING_CONFIRM":
            break
    assert s.status == "AWAITING_CONFIRM"
    assert s.summary["draftId"] == "700000001" and s.summary["anhaenge"][0]["sha256"]
    assert open_now["n"] == 0
    s.confirm_event.set()
    await asyncio.wait_for(s.done_event.wait(), 2)
    return s


def test_flow_submits_verified_draft(downloads, monkeypatch):
    calls = []
    text = "Umsatzsteuer-Voranmeldung 2026 3. Kalendervierteljahr 2026-09-01_Google_Rechnung.pdf"
    s = asyncio.run(_drive(monkeypatch, text, calls))
    assert calls == ["open", "upload:1", "save_draft", "open", "open_draft:700000001", "submit"]
    assert s.status == "DONE"


def test_flow_aborts_on_mismatch(downloads, monkeypatch):
    calls = []
    s = asyncio.run(_drive(monkeypatch, "Umsatzsteuer-Voranmeldung 2026 3. Kalendervierteljahr", calls))
    assert "submit" not in calls
    assert s.status == "ERROR" and "nichts übermittelt" in s.errors[-1]


def test_tools_registered_and_confirm_blocked(downloads):
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"elster_belege_start", "elster_belege_confirm"} <= names
    s = sessions.create("BELEG")
    s.status = "AWAITING_CONFIRM"
    s.confirmation_code = "ABCD1234"

    class Ctx:
        client_capabilities = None

    res = asyncio.run(server.elster_belege_confirm(s.id, "ABCD1234", Ctx()))
    assert "gesperrt" in res["error"]
    assert not s.confirm_event.is_set()


def test_start_tool_reports_file_errors(downloads):
    res = asyncio.run(server.elster_belege_start(2026, "Anforderung", ["fehlt.pdf"], "Q3"))
    assert "nicht gefunden" in res["error"]
