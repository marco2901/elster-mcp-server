"""Login-Zustandserkennung ohne echten Browser."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from elster_mcp import server
from elster_mcp.config import get_config
from elster_mcp.portal.base import ElsterPortal

PENDING_URL = "https://www.elster.de/eportal/temporaereaufgaben"


class _NoLoc:
    async def count(self):
        return 0


class FakePage:
    def __init__(self, url, titles=None):
        self.url = url
        self._titles = titles or []

    async def evaluate(self, script, *args):
        return self._titles

    def locator(self, selector):
        return _NoLoc()


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


RESTORE_TEXT = ("Formular wurde verlassen ohne zu Speichern Während Ihrer letzten Nutzung von Mein ELSTER haben Sie "
                "folgendes Formular bearbeitet: UStVA 2026 - III. Kalendervierteljahr (automatisch gespeichert am "
                "28.09.2026 um 16:11 Uhr) Sie haben die Bearbeitung nicht durch \"Speichern und Verlassen\" beendet. "
                "Nein Ja, letzten Stand der Bearbeitung speichern")


def test_parse_restore_prompt():
    from elster_mcp.portal.base import parse_restore_prompt
    assert parse_restore_prompt(RESTORE_TEXT) == {"form": "UStVA 2026 - III. Kalendervierteljahr",
                                                  "savedAt": "28.09.2026 um 16:11 Uhr"}
    assert parse_restore_prompt("irgendwas")["form"] == "unbekannt"


def test_restore_prompt_is_answered_with_nein(tmp_path):
    import json

    clicked = []

    class Loc:
        def __init__(self, page, sel):
            self.page, self.sel = page, sel

        @property
        def first(self):
            return self

        async def count(self):
            return 1 if self.sel in ("#temporaereaufgaben_nein_button", "main") and PENDING_URL in self.page.url else 0

        async def inner_text(self):
            return RESTORE_TEXT

        async def click(self):
            clicked.append(self.sel)
            self.page.url = "https://www.elster.de/eportal/meinelster"

    class Page(FakePage):
        def locator(self, selector):
            assert "ja" not in selector.lower(), "„Ja“ darf nie angeklickt werden"
            return Loc(self, selector)

    portal = ElsterPortal(get_config())

    async def no_wait(*a, **k):
        return None

    portal.wait_nav = no_wait
    portal.sleep = no_wait
    page = Page(PENDING_URL)
    asyncio.run(portal._note_pending_tasks(page))
    assert clicked == ["#temporaereaufgaben_nein_button"]
    assert portal.pending_tasks is None
    audit = [json.loads(line) for line in get_config().security.audit_log.read_text().splitlines()]
    assert audit[-1]["event"] == "restore_discarded"
    assert audit[-1]["form"] == "UStVA 2026 - III. Kalendervierteljahr"
