"""Verwaltung laufender Browser-Sitzungen (UStVA / EÜR / ESt)."""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal

SessionKind = Literal["USTVA", "EUR", "EST"]
SessionStatus = Literal[
    "STARTING", "LOGGING_IN", "OPENING_FORM", "FILLING_PAGES", "PRUEFUNG",
    "AWAITING_CONFIRM", "AWAITING_REVIEW", "SUBMITTING", "SAVING", "SAVED",
    "DONE", "ERROR", "CANCELLED",
]

TERMINAL: frozenset[str] = frozenset({"DONE", "ERROR", "CANCELLED", "SAVED"})


@dataclass
class Session:
    id: str
    kind: SessionKind
    status: SessionStatus = "STARTING"
    progress: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    screenshot_path: str | None = None
    result: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    #: Zusammenfassung dessen, was übermittelt würde (UStVA).
    summary: dict[str, Any] | None = None
    # interne Felder
    confirmation_code: str | None = None
    confirm_event: asyncio.Event = field(default_factory=asyncio.Event)
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[Any] | None = None
    cancelled: bool = False

    def log(self, msg: str) -> None:
        self.progress.append(msg)

    def fail(self, msg: str) -> None:
        self.errors.append(msg)
        if not self.cancelled:
            self.status = "ERROR"

    def view(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress": list(self.progress),
            "errors": list(self.errors),
            "screenshotPath": self.screenshot_path,
            "result": self.result,
            "createdAt": self.created_at,
        }
        if self.summary is not None:
            out["summary"] = self.summary
        if self.status == "AWAITING_CONFIRM" and self.confirmation_code:
            out["confirmationCode"] = self.confirmation_code
            out["hint"] = (
                "Bitte Screenshot und Zusammenfassung prüfen. Übermittlung nur mit "
                "elster_ustva_confirm(sessionId, confirmationCode)."
            )
        return out


class SessionManager:
    def __init__(self, max_parallel: int = 2) -> None:
        self._sessions: dict[str, Session] = {}
        #: Begrenzt parallele Browser – schützt vor Ressourcenerschöpfung und Portalsperren.
        self.browser_slots = asyncio.Semaphore(max_parallel)

    def create(self, kind: SessionKind) -> Session:
        sid = f"{kind.lower()}-{int(time.time())}-{secrets.token_hex(4)}"
        s = Session(id=sid, kind=kind)
        self._sessions[sid] = s
        return s

    def get(self, sid: str) -> Session | None:
        return self._sessions.get(sid)

    def list(self) -> list[dict[str, Any]]:
        return [s.view() for s in self._sessions.values()]

    def cancel(self, sid: str) -> bool:
        s = self._sessions.get(sid)
        if not s:
            return False
        s.cancelled = True
        s.status = "CANCELLED"
        s.done_event.set()
        if s.task and not s.task.done():
            s.task.cancel()
        return True

    def schedule_cleanup(self, sid: str, delay_s: float = 3600) -> None:
        loop = asyncio.get_running_loop()
        loop.call_later(delay_s, self._sessions.pop, sid, None)


sessions = SessionManager()
