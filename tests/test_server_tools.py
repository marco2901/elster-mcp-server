"""Tool-Ebene ohne echten Browser: Freigabelogik von elster_ustva_confirm."""

import asyncio
from types import SimpleNamespace

import pytest

from elster_mcp import server
from elster_mcp.config import reset_config_cache
from elster_mcp.sessions import sessions


class FakeCtx:
    def __init__(self, elicitation=False, answer=None):
        self.client_capabilities = SimpleNamespace(elicitation={} if elicitation else None)
        self._answer = answer
        self.elicited = None

    async def elicit(self, message, schema):
        self.elicited = message
        return self._answer


def _awaiting_session(code="ABCD1234"):
    s = sessions.create("USTVA")
    s.status = "AWAITING_CONFIRM"
    s.confirmation_code = code
    s.summary = {"year": 2025, "period": "Q1", "kennziffern": {"81": {"betrag": 100.0, "beschreibung": "x"}}}
    return s


async def _simulate_flow(s):
    await s.confirm_event.wait()
    s.status = "DONE"
    s.result = {"success": True, "ticket": "T-1"}
    s.done_event.set()


def test_tools_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"elster_ustva_start", "elster_ustva_confirm", "elster_security_check", "elster_sync_inbox"} <= names


def test_confirm_blocked_without_allow_submit():
    s = _awaiting_session()
    res = asyncio.run(server.elster_ustva_confirm(s.id, "ABCD1234", FakeCtx()))
    assert "gesperrt" in res["error"]
    assert not s.confirm_event.is_set()


def test_confirm_wrong_code(monkeypatch):
    monkeypatch.setenv("ELSTER_ALLOW_SUBMIT", "1")
    reset_config_cache()
    s = _awaiting_session()
    res = asyncio.run(server.elster_ustva_confirm(s.id, "FALSCH00", FakeCtx()))
    assert "passt nicht" in res["error"]
    assert not s.confirm_event.is_set()


def test_confirm_requires_elicitation_when_configured(monkeypatch):
    monkeypatch.setenv("ELSTER_ALLOW_SUBMIT", "1")
    monkeypatch.setenv("ELSTER_REQUIRE_ELICITATION", "1")
    reset_config_cache()
    s = _awaiting_session()
    res = asyncio.run(server.elster_ustva_confirm(s.id, "ABCD1234", FakeCtx(elicitation=False)))
    assert "Elicitation" in res["error"]


@pytest.mark.parametrize("answer,ok", [
    (SimpleNamespace(action="accept", data=SimpleNamespace(code="abcd1234")), True),
    (SimpleNamespace(action="accept", data=SimpleNamespace(code="00000000")), False),
    (SimpleNamespace(action="decline"), False),
])
def test_confirm_with_elicitation(monkeypatch, answer, ok):
    monkeypatch.setenv("ELSTER_ALLOW_SUBMIT", "1")
    reset_config_cache()

    async def run():
        s = _awaiting_session()
        flow = asyncio.create_task(_simulate_flow(s))
        ctx = FakeCtx(elicitation=True, answer=answer)
        res = await server.elster_ustva_confirm(s.id, "ABCD1234", ctx)
        if not ok:
            flow.cancel()
        return s, res, ctx

    s, res, ctx = asyncio.run(run())
    assert "ABCD1234" in ctx.elicited
    if ok:
        assert res["result"]["ticket"] == "T-1"
    else:
        assert "nicht bestätigt" in res["error"]
        assert s.status == "AWAITING_CONFIRM"


def test_start_rejects_invalid_report():
    res = asyncio.run(server.elster_ustva_start(2025, "Q1", {"99": 1}))
    assert res["error"] == "Ungültige Eingabe"


def test_start_via_mcp_creates_background_session(monkeypatch):
    """Über das echte MCP-Dispatching (nicht direkt) – der Hintergrund-Task muss anlaufen."""
    started = []

    async def fake_run(self, s, ustva):
        started.append(s.id)
        s.status = "AWAITING_CONFIRM"

    monkeypatch.setattr("elster_mcp.portal.ustva.UstvaFlow._run", fake_run)

    async def run():
        res = await server.mcp.call_tool("elster_ustva_start", {"year": 2025, "period": "Q1", "report": {"81": 100}})
        await asyncio.sleep(0)
        return res

    res = asyncio.run(run())
    assert not res.is_error, res
    assert len(started) == 1
    assert sessions.get(started[0]).status == "AWAITING_CONFIRM"


def test_security_check_reports_missing(monkeypatch, cert):
    monkeypatch.setenv("ELSTER_PFX_PATH", str(cert))
    reset_config_cache()
    res = server.elster_security_check()
    assert res["certificate"]["ok"] is True
    assert res["password"]["ok"] is False
    assert res["ok"] is False
