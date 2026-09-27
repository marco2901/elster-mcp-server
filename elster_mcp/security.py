"""Schutzmechanismen: Freigabecodes, Revisionsprotokoll, HTTP-Token, sichere Dateien."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .secrets import redactor

log = logging.getLogger("elster_mcp.security")


# --------------------------------------------------------------------------- #
# Freigabecode für Übermittlungen
# --------------------------------------------------------------------------- #

def confirmation_code(session_id: str, payload: dict[str, Any], nonce: str) -> str:
    """Kurzer Code, der genau an diese Sitzung + diese Beträge gebunden ist.

    Ändert sich irgendetwas am Inhalt, passt der Code nicht mehr.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hmac.new(nonce.encode(), f"{session_id}|{canonical}".encode(), hashlib.sha256).hexdigest()
    return digest[:8].upper()


def new_nonce() -> str:
    return secrets.token_hex(16)


def codes_match(expected: str, given: str | None) -> bool:
    return bool(given) and hmac.compare_digest(expected.upper(), given.strip().upper())


# --------------------------------------------------------------------------- #
# Revisionsprotokoll
# --------------------------------------------------------------------------- #

class AuditLog:
    """Append-only JSONL-Protokoll aller sicherheitsrelevanten Aktionen."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def write(self, event: str, **fields: Any) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        line = redactor.redact(json.dumps(record, ensure_ascii=False, default=str))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            log.error("Audit-Log konnte nicht geschrieben werden: %s", exc)


# --------------------------------------------------------------------------- #
# Dateien mit Steuerdaten
# --------------------------------------------------------------------------- #

def restrict_file(path: Path) -> None:
    """Setzt 0600 auf Dateien mit personenbezogenen Daten (Screenshots, PDFs)."""
    if os.name == "posix" and path.exists():
        try:
            path.chmod(0o600)
        except OSError:
            pass


def safe_filename(name: str, max_len: int = 60) -> str:
    keep = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)
    return keep[:max_len] or "file"


def safe_child(base: Path, filename: str) -> Path:
    """Verhindert Path-Traversal beim Schreiben in Download-/Screenshot-Ordner."""
    base = base.expanduser().resolve()
    target = (base / safe_filename(Path(filename).stem, 120)).with_suffix(Path(filename).suffix)
    if base not in target.resolve().parents:
        raise ValueError(f"Unzulässiger Dateiname: {filename}")
    return target


# --------------------------------------------------------------------------- #
# Browser-Zielbeschränkung
# --------------------------------------------------------------------------- #

def host_allowed(url: str, allowed_hosts: list[str]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme in {"data", "blob", "about"}:
        return True
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in allowed_hosts)


# --------------------------------------------------------------------------- #
# Bearer-Token für den HTTP-Transport
# --------------------------------------------------------------------------- #

class BearerAuthMiddleware:
    """Reine ASGI-Middleware: jeder HTTP-Request braucht ``Authorization: Bearer <token>``."""

    def __init__(self, app: Any, token: str, public_prefixes: tuple[str, ...] = ()) -> None:
        if len(token) < 32:
            raise ValueError("ELSTER_MCP_TOKEN muss mindestens 32 Zeichen lang sein.")
        self.app = app
        self._token = token.encode()
        # Pfade mit eigener Absicherung (z. B. Einmal-Download-Links) ohne Bearer durchlassen.
        self._public_prefixes = public_prefixes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or any(scope.get("path", "").startswith(p) for p in self._public_prefixes):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"")
        scheme, _, given = auth.partition(b" ")
        if scheme.lower() != b"bearer" or not hmac.compare_digest(given.strip(), self._token):
            client = scope.get("client") or ("?", 0)
            log.warning("Abgelehnter HTTP-Zugriff von %s", client[0])
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
            })
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return
        await self.app(scope, receive, send)
