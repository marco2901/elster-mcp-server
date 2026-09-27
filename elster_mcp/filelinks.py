"""Kurzlebige Einmal-Links auf Dateien im Download-Ordner.

Damit kann ein anderer Server (z. B. der OneDrive-Upload des M365-MCP per ``sourceUrl``) eine Datei
abholen, ohne dass die Bytes durch den MCP-Aufruf laufen. Tokens sind zufällig (256 Bit), gelten
wenige Minuten und nur für genau einen Abruf. Sie liegen nur im Speicher, ein Neustart verwirft sie.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TTL_SECONDS = 600
MAX_OPEN_LINKS = 200


@dataclass(frozen=True)
class FileLink:
    path: Path
    expires_at: float


class FileLinkStore:
    def __init__(self) -> None:
        self._links: dict[str, FileLink] = {}
        self._lock = threading.Lock()

    def _purge(self, now: float) -> None:
        for token in [t for t, link in self._links.items() if link.expires_at <= now]:
            del self._links[token]

    def create(self, path: Path, ttl: int = DEFAULT_TTL_SECONDS) -> str:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            if len(self._links) >= MAX_OPEN_LINKS:
                raise RuntimeError("Zu viele offene Download-Links. Bitte einige Minuten warten.")
            token = secrets.token_urlsafe(32)
            self._links[token] = FileLink(path, now + ttl)
            return token

    def consume(self, token: str) -> Path | None:
        """Gibt den Pfad genau einmal zurück; abgelaufene oder unbekannte Tokens liefern None."""
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            link = self._links.pop(token, None)
        return link.path if link else None


file_links = FileLinkStore()


def resolve_download(download_dir: Path, name: str) -> Path:
    """Nur einfache Dateinamen direkt im Download-Ordner, keine Pfade, keine Symlinks nach außen."""
    if not name or name != Path(name).name or name.startswith("."):
        raise ValueError(f"Ungültiger Dateiname: {name!r}")
    base = download_dir.expanduser().resolve()
    target = (base / name).resolve()
    if target.parent != base or not target.is_file():
        raise FileNotFoundError(f"Datei nicht gefunden: {name}")
    return target
