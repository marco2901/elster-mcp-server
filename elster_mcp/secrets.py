"""Beschaffung und Schutz der ELSTER-Sicherheitsmerkmale.

Die Sicherheitsmerkmale (Zertifikatsdatei ``.pfx`` + Zertifikats-Passwort/PIN)
landen NIE im Repository und NIE im Klartext in Logs oder Tool-Antworten.

Reihenfolge der Quellen für Geheimnisse (erste Fundstelle gewinnt):

1. ``<NAME>_FILE``  – Pfad zu einer Datei mit dem Geheimnis (Docker-/Podman-Secrets,
   systemd-Credentials, Datei mit ``chmod 600``)
2. OS-Keyring      – nur wenn das Paket ``keyring`` installiert ist
   (Service ``elster-mcp``, siehe ``python -m elster_mcp store-secret``)
3. ``<NAME>``       – Umgebungsvariable
4. ``config.json``  – nur als Notlösung, erzeugt eine Warnung
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from pathlib import Path

from pydantic import SecretStr

log = logging.getLogger("elster_mcp.secrets")

KEYRING_SERVICE = "elster-mcp"

#: Maximale Größe einer Secret-Datei – schützt vor versehentlich falschem Pfad.
_MAX_SECRET_FILE_BYTES = 4096
#: ELSTER-Zertifikate sind wenige KB groß.
_MAX_PFX_BYTES = 256 * 1024


class SecretError(RuntimeError):
    """Ein Sicherheitsmerkmal ist nicht vorhanden oder unsicher abgelegt."""


def _is_posix() -> bool:
    return os.name == "posix"


def insecure_permissions(path: Path) -> bool:
    """True, wenn Gruppe oder andere Benutzer die Datei lesen/schreiben dürfen."""
    if not _is_posix():
        return False
    mode = path.stat().st_mode
    return bool(mode & (stat.S_IRWXG | stat.S_IRWXO))


def _check_permissions(path: Path, what: str, strict: bool) -> None:
    if not insecure_permissions(path):
        return
    msg = (
        f"{what} '{path}' ist für Gruppe/andere lesbar "
        f"(Modus {oct(path.stat().st_mode & 0o777)}). Empfohlen: chmod 600 '{path}'."
    )
    if strict:
        raise SecretError(msg + " (ELSTER_STRICT_PERMISSIONS=1)")
    log.warning(msg)


def read_secret_file(path: str | os.PathLike[str], *, strict: bool = False) -> SecretStr:
    p = Path(path).expanduser()
    if not p.is_file():
        raise SecretError(f"Secret-Datei nicht gefunden: {p}")
    if p.stat().st_size > _MAX_SECRET_FILE_BYTES:
        raise SecretError(f"Secret-Datei {p} ist unplausibel groß – falscher Pfad?")
    _check_permissions(p, "Secret-Datei", strict)
    # Nur den abschließenden Zeilenumbruch entfernen, Leerzeichen im Passwort bleiben erhalten.
    value = p.read_text(encoding="utf-8").rstrip("\r\n")
    return SecretStr(value)


def keyring_get(key: str) -> SecretStr | None:
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        value = keyring.get_password(KEYRING_SERVICE, key)
    except Exception as exc:  # Keyring-Backends werfen sehr unterschiedliche Fehler
        log.debug("Keyring nicht verfügbar: %s", exc)
        return None
    return SecretStr(value) if value else None


def keyring_set(key: str, value: str) -> None:
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SecretError("Paket 'keyring' ist nicht installiert: pip install 'elster-mcp[keyring]'") from exc
    keyring.set_password(KEYRING_SERVICE, key, value)


def resolve_secret(
    env_name: str,
    *,
    keyring_key: str | None = None,
    fallback: str | None = None,
    fallback_source: str = "config.json",
    strict: bool = False,
) -> tuple[SecretStr | None, str]:
    """Liefert ``(geheimnis, quelle)``. Die Quelle ist für Diagnosezwecke gedacht."""
    file_var = f"{env_name}_FILE"
    if os.environ.get(file_var):
        return read_secret_file(os.environ[file_var], strict=strict), f"file:${file_var}"

    if keyring_key:
        from_keyring = keyring_get(keyring_key)
        if from_keyring is not None:
            return from_keyring, f"keyring:{KEYRING_SERVICE}/{keyring_key}"

    if os.environ.get(env_name):
        return SecretStr(os.environ[env_name]), f"env:${env_name}"

    if fallback:
        log.warning(
            "Geheimnis für %s stammt aus %s. Besser: %s oder OS-Keyring verwenden.",
            env_name, fallback_source, file_var,
        )
        return SecretStr(fallback), fallback_source

    return None, "unset"


def validate_certificate(path: str, *, strict: bool = False) -> Path:
    """Prüft die Zertifikatsdatei, bevor sie in den Browser hochgeladen wird."""
    if not path:
        raise SecretError("Kein ELSTER-Zertifikat konfiguriert (ELSTER_PFX_PATH).")
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise SecretError(f"ELSTER-Zertifikat nicht gefunden: {p}")
    if p.suffix.lower() not in {".pfx", ".p12"}:
        raise SecretError(f"ELSTER-Zertifikat muss eine .pfx/.p12-Datei sein: {p.name}")
    size = p.stat().st_size
    if size == 0 or size > _MAX_PFX_BYTES:
        raise SecretError(f"ELSTER-Zertifikat hat eine unplausible Größe ({size} Bytes).")
    _check_permissions(p, "ELSTER-Zertifikat", strict)
    return p


class RedactingFilter(logging.Filter):
    """Ersetzt registrierte Geheimnisse in allen Log-Ausgaben durch ``***``."""

    def __init__(self) -> None:
        super().__init__()
        self._needles: set[str] = set()

    def register(self, *values: str | None) -> None:
        for v in values:
            if v and len(v) >= 3:
                self._needles.add(v)

    def redact(self, text: str) -> str:
        for needle in sorted(self._needles, key=len, reverse=True):
            text = text.replace(needle, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if self._needles:
            record.msg = self.redact(record.getMessage())
            record.args = None
        return True


redactor = RedactingFilter()


def setup_logging(level: str = "INFO") -> None:
    """Loggt ausschließlich nach stderr (stdout gehört dem MCP-Protokoll)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s"))
    handler.addFilter(redactor)
    root = logging.getLogger("elster_mcp")
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    root.propagate = False
