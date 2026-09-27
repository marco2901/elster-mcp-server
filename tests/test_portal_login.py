"""Login-Zustandserkennung ohne echten Browser."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from elster_mcp import server
from elster_mcp.config import get_config
from elster_mcp.portal.base import ElsterPortal

PENDING_URL = "https://www.elster.de/eportal/temporaereaufgaben"


class FakePage:
    def __init__(self, url, titles=None):
        self.url = url
        self._titles = titles or []

    async def evaluate(self, script, *args):
        return self._titles


def test_pending_tasks_page_counts_as_logged_in():
    portal = ElsterPortal(get_config())
    assert portal._is_logged_in(PENDING_URL)
    assert portal._is_logged_in("https://www.elster.de/eportal/mein-elster/startseite")
    assert not portal._is_logged_in("https://www.elster.de/eportal/login/zertifikat")


def test_note_pending_tasks_collects_titles():
    portal = ElsterPortal(get_config())
    asyncio.run(portal._note_pending_tasks(FakePage(PENDING_URL, ["E-Mail-Adresse bestätigen"])))
    assert portal.pending_tasks == ["E-Mail-Adresse bestätigen"]


def test_note_pending_tasks_ignores_normal_pages():
    portal = ElsterPortal(get_config())
    asyncio.run(portal._note_pending_tasks(FakePage("https://www.elster.de/eportal/mein-elster/startseite")))
    assert portal.pending_tasks is None


def test_login_test_reports_pending_tasks(monkeypatch):
    @asynccontextmanager
    async def fake_open(self):
        yield FakePage(PENDING_URL)

    async def fake_login(self, page):
        self.pending_tasks = ["E-Mail-Adresse bestätigen"]

    monkeypatch.setattr(ElsterPortal, "open", fake_open)
    monkeypatch.setattr(ElsterPortal, "login", fake_login)
    res = asyncio.run(server.elster_login_test())
    assert res["ok"] is True
    assert res["pendingTasks"] == ["E-Mail-Adresse bestätigen"]
    assert "temporaereaufgaben" in res["hint"]


def test_login_test_without_pending_tasks(monkeypatch):
    @asynccontextmanager
    async def fake_open(self):
        yield SimpleNamespace(url="https://www.elster.de/eportal/mein-elster/startseite")

    async def fake_login(self, page):
        return None

    monkeypatch.setattr(ElsterPortal, "open", fake_open)
    monkeypatch.setattr(ElsterPortal, "login", fake_login)
    res = asyncio.run(server.elster_login_test())
    assert res == {"ok": True, "finalUrl": "https://www.elster.de/eportal/mein-elster/startseite"}
