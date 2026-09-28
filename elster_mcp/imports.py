"""Dateien per Einmal-Link (z. B. paperless_file_link) in den Download-Ordner holen.

Schutz gegen Missbrauch als Proxy: nur HTTPS, nur freigegebene Hosts (ELSTER_IMPORT_HOSTS), keine
Weiterleitungen, Größenlimit, Inhalt muss zur Endung passen (PDF/XML), vorhandene Dateien bleiben unberührt.
"""

from __future__ import annotations

import hashlib
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .security import restrict_file

MAX_IMPORT_BYTES = 10 * 1024 * 1024  # ELSTER nimmt je Anhang höchstens 10 MB an
ALLOWED_SUFFIXES = (".pdf", ".xml")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


class ImportError_(ValueError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        raise ImportError_(f"Weiterleitung ({code}) nicht erlaubt.")


def check_source_url(url: str, import_hosts: list[str]) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ImportError_("Nur https-Links sind erlaubt.")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ImportError_("Link mit Zugangsdaten oder abweichendem Port ist nicht erlaubt.")
    host = (parsed.hostname or "").lower()
    if not import_hosts:
        raise ImportError_("Import ist deaktiviert (ELSTER_IMPORT_HOSTS ist leer).")
    if host not in import_hosts:
        raise ImportError_(f"Host {host} ist nicht freigegeben (ELSTER_IMPORT_HOSTS).")
    return url


def check_filename(name: str) -> str:
    name = name.strip()
    if not _NAME_RE.fullmatch(name) or ".." in name:
        raise ImportError_("Dateiname: nur A-Z, a-z, 0-9, Punkt, Minus, Unterstrich (max. 120 Zeichen).")
    if not name.lower().endswith(ALLOWED_SUFFIXES):
        raise ImportError_("Dateiname muss auf .pdf oder .xml enden.")
    return name


def check_content(name: str, head: bytes) -> None:
    if name.lower().endswith(".pdf") and not head.startswith(b"%PDF"):
        raise ImportError_("Der Link liefert keine PDF-Datei.")
    if name.lower().endswith(".xml") and not head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):
        raise ImportError_("Der Link liefert keine XML-Datei.")


def import_file(url: str, filename: str, download_dir: Path, import_hosts: list[str],
                opener: urllib.request.OpenerDirector | None = None) -> dict:
    check_source_url(url, import_hosts)
    name = check_filename(filename)
    base = download_dir.expanduser().resolve()
    target = base / name
    if target.exists():
        raise ImportError_(f"{name} existiert bereits im Download-Ordner.")
    opener = opener or urllib.request.build_opener(_NoRedirect)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "elster-mcp-import"})  # noqa: S310 – https geprüft
        with opener.open(req, timeout=60) as resp:  # noqa: S310
            if resp.status != 200:
                raise ImportError_(f"Abruf fehlgeschlagen (HTTP {resp.status}).")
            data = resp.read(MAX_IMPORT_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise ImportError_(f"Abruf fehlgeschlagen (HTTP {exc.code}) – Einmal-Link abgelaufen oder schon benutzt?") from exc
    except urllib.error.URLError as exc:
        raise ImportError_(f"Abruf fehlgeschlagen: {exc.reason}") from exc
    if len(data) > MAX_IMPORT_BYTES:
        raise ImportError_("Datei ist größer als 10 MB – ELSTER nimmt sie nicht an.")
    check_content(name, data[:16])
    tmp = target.with_name(f".{name}.part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    tmp.rename(target)
    restrict_file(target)
    return {"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
