"""MCP-Server: Tool-Definitionen."""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, ValidationError

from . import __version__
from .config import get_config
from .constants import EUR_FIELDS, KENNZIFFERN
from .filelinks import DEFAULT_TTL_SECONDS, file_links, resolve_download
from .models import EstData, EurData, UstvaReport
from .portal.base import ElsterPortal
from .portal.est import EstFlow
from .portal.eur import EurFlow
from .portal.sync import SyncFlow
from .portal.ustva import UstvaFlow
from .secrets import SecretError, insecure_permissions, validate_certificate
from .security import AuditLog, codes_match
from .sessions import sessions
from .xml import detect_reverse_charge, generate_ustva_xml

log = logging.getLogger("elster_mcp.server")

INSTRUCTIONS = """\
ELSTER-Automatisierung über das Web-Portal mit dem Zertifikat des Nutzers.
Regeln:
- Nur elster_ustva_confirm übermittelt Daten ans Finanzamt. Vorher IMMER Zusammenfassung
  und Screenshot aus elster_session_status dem Menschen zeigen und seine ausdrückliche
  Zustimmung einholen. Niemals eigenmächtig bestätigen.
- EÜR und ESt werden nie übermittelt, nur vorbereitet.
- Keine Steuerberatung (§ 2 StBerG).
"""

mcp = MCPServer(name="elster-mcp", version=__version__, instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
LOCAL_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
PREPARE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
SUBMIT = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)


def _audit() -> AuditLog:
    return AuditLog(get_config().security.audit_log)


def _validation_error(exc: ValidationError) -> dict[str, Any]:
    return {"error": "Ungültige Eingabe", "details": [e["msg"] for e in exc.errors()]}


# --------------------------------------------------------------------------- #
# Konfiguration & Sicherheit
# --------------------------------------------------------------------------- #

@mcp.tool(annotations=LOCAL_ONLY)
def elster_config_show() -> dict[str, Any]:
    """Zeigt die geladene Konfiguration (Geheimnisse und Steuernummer maskiert)."""
    return get_config().redacted()


@mcp.tool(annotations=LOCAL_ONLY)
def elster_security_check() -> dict[str, Any]:
    """Prüft lokal die Sicherheitsmerkmale (Zertifikat, Passwortquelle, Dateirechte, Freigaben) ohne Login."""
    cfg = get_config()
    checks: dict[str, Any] = {}
    try:
        pfx = validate_certificate(cfg.auth.pfx_path, strict=cfg.security.strict_permissions)
        checks["certificate"] = {"ok": True, "path": str(pfx), "insecurePermissions": insecure_permissions(pfx)}
    except SecretError as exc:
        checks["certificate"] = {"ok": False, "error": str(exc)}
    checks["password"] = {"ok": cfg.auth.password is not None, "source": cfg.auth.password_source}
    if cfg.auth.password_source == "config.json":
        checks["password"]["warning"] = "Passwort liegt im Klartext in config.json – besser ELSTER_PASSWORD_FILE oder Keyring."
    checks["taxNumber"] = {"ok": bool(cfg.taxpayer.tax_number), "stateCode": cfg.taxpayer.state_code or None}
    checks["submission"] = {
        "allowSubmit": cfg.security.allow_submit,
        "requireElicitation": cfg.security.require_elicitation,
        "confirmTimeoutMinutes": cfg.security.confirm_timeout_minutes,
    }
    checks["browser"] = {"allowedHosts": cfg.security.allowed_hosts, "chromiumSandbox": cfg.runtime.chromium_sandbox}
    checks["auditLog"] = str(cfg.security.audit_log.expanduser().resolve())
    checks["screenshots"] = cfg.security.screenshots
    checks["ok"] = checks["certificate"]["ok"] and checks["password"]["ok"] and checks["taxNumber"]["ok"]
    return checks


@mcp.tool(annotations=READ_ONLY)
async def elster_login_test() -> dict[str, Any]:
    """Testet, ob Zertifikat + Passwort den Login ins ELSTER-Portal schaffen. Übermittelt nichts."""
    portal = ElsterPortal(get_config())
    try:
        async with sessions.browser_slots, portal.open() as page:
            await portal.login(page)
            url = page.url
        result: dict[str, Any] = {"ok": True, "finalUrl": url}
        if portal.pending_tasks is not None:
            result["pendingTasks"] = portal.pending_tasks
            result["hint"] = (
                "Login erfolgreich, aber ELSTER zeigt offene Aufgaben (temporaereaufgaben). "
                "Bitte einmal im ELSTER-Portal anmelden und erledigen – der Server klickt dort nichts."
            )
        _audit().write("login_test", ok=True, pending_tasks=portal.pending_tasks is not None)
        return result
    except Exception as exc:
        _audit().write("login_test", ok=False, error=str(exc))
        return {"ok": False, "error": str(exc)}


# --------------------------------------------------------------------------- #
# UStVA
# --------------------------------------------------------------------------- #

@mcp.tool(annotations=LOCAL_ONLY)
def elster_kennziffern_list() -> dict[str, Any]:
    """Unterstützte UStVA-Kennziffern mit Beschreibung und Typ (NET = Bemessungsgrundlage, TAX = Steuerbetrag)."""
    return dict(KENNZIFFERN)


@mcp.tool(annotations=LOCAL_ONLY)
def elster_ustva_generate_xml(year: int, period: int | str, report: dict[str, float]) -> dict[str, Any]:
    """Validiert eine UStVA und erzeugt einen XML-Snapshot zur Archivierung. Übermittelt NICHT.

    Args:
        year: Steuerjahr, z. B. 2026
        period: Monat 1-12 oder "Q1".."Q4"
        report: Kennziffer → Betrag in EUR, z. B. {"81": 12000, "66": 1845.30}
    """
    try:
        u = UstvaReport(year=year, period=period, report=report)
    except ValidationError as exc:
        return _validation_error(exc)
    return {"xml": generate_ustva_xml(get_config(), u), "normalizedReport": u.report}


@mcp.tool(annotations=LOCAL_ONLY)
def elster_ustva_detect_reverse_charge(contactName: str = "", description: str = "") -> dict[str, Any] | None:
    """Prüft anhand der konfigurierten Lieferantenmuster, ob ein Beleg §13b-Reverse-Charge ist (EU / NON_EU)."""
    return detect_reverse_charge(get_config(), contactName, description)


@mcp.tool(annotations=PREPARE)
async def elster_ustva_start(year: int, period: int | str, report: dict[str, float]) -> dict[str, Any]:
    """Startet eine UStVA: Login, Formular füllen, ELSTER-Prüfung – dann PAUSE bei AWAITING_CONFIRM.

    Es wird noch nichts übermittelt. Status mit elster_session_status abfragen, dem Menschen
    Zusammenfassung + Screenshot zeigen und erst nach seiner Zustimmung elster_ustva_confirm aufrufen.

    Args:
        year: Steuerjahr
        period: Monat 1-12 oder "Q1".."Q4"
        report: Kennziffer → Betrag in EUR (nur Kennziffern aus elster_kennziffern_list)
    """
    try:
        u = UstvaReport(year=year, period=period, report=report)
    except ValidationError as exc:
        return _validation_error(exc)
    s = UstvaFlow(get_config(), _audit()).start(u)
    return {"sessionId": s.id, "summary": s.summary, "submitEnabled": get_config().security.allow_submit}


class SubmitApproval(BaseModel):
    code: str = Field(description="Bestätigungscode aus der Nachricht zur Übermittlung abtippen")


@mcp.tool(annotations=SUBMIT)
async def elster_ustva_confirm(sessionId: str, confirmationCode: str, ctx: Context) -> dict[str, Any]:
    """ÜBERMITTELT eine geprüfte UStVA verbindlich an das Finanzamt ("Absenden").

    Nur aufrufen, wenn der Mensch Zusammenfassung und Screenshot gesehen und ausdrücklich
    zugestimmt hat. confirmationCode steht in elster_session_status und ist an genau diese
    Beträge gebunden.
    """
    cfg = get_config()
    audit = _audit()
    s = sessions.get(sessionId)
    if not s or s.kind != "USTVA":
        return {"error": "UStVA-Sitzung nicht gefunden."}
    if not cfg.security.allow_submit:
        audit.write("ustva_confirm_blocked", session=sessionId, reason="allow_submit=false")
        return {
            "error": "Übermittlung ist gesperrt (ELSTER_ALLOW_SUBMIT ist nicht gesetzt). "
                     "Bitte im Portal selbst absenden oder die Sperre bewusst aufheben."
        }
    if s.status != "AWAITING_CONFIRM" or not s.confirmation_code:
        return {"error": f"Sitzung ist nicht freigabebereit (Status {s.status})."}
    if not codes_match(s.confirmation_code, confirmationCode):
        audit.write("ustva_confirm_rejected", session=sessionId, reason="code_mismatch")
        return {"error": "Bestätigungscode passt nicht zu dieser Sitzung."}

    caps = ctx.client_capabilities
    supports_elicitation = caps is not None and caps.elicitation is not None
    if supports_elicitation:
        lines = [f"Kz{k}: {v['betrag']:.2f} € ({v['beschreibung']})" for k, v in s.summary["kennziffern"].items()]
        msg = (
            f"UStVA {s.summary['year']} / {s.summary['period']} JETZT verbindlich an das Finanzamt übermitteln?\n"
            + "\n".join(lines)
            + f"\n\nZur Bestätigung den Code {s.confirmation_code} eingeben."
        )
        answer = await ctx.elicit(msg, SubmitApproval)
        if answer.action != "accept" or not codes_match(s.confirmation_code, answer.data.code):
            audit.write("ustva_confirm_rejected", session=sessionId, reason=f"elicitation_{answer.action}")
            return {"error": "Übermittlung vom Nutzer nicht bestätigt – nichts gesendet."}
    elif cfg.security.require_elicitation:
        audit.write("ustva_confirm_blocked", session=sessionId, reason="no_elicitation_support")
        return {"error": "Dieser MCP-Client unterstützt keine direkte Nutzerbestätigung (Elicitation); "
                         "ELSTER_REQUIRE_ELICITATION verbietet die Übermittlung."}

    audit.write("ustva_confirm_accepted", session=sessionId, via="elicitation" if supports_elicitation else "code")
    s.confirm_event.set()
    await s.done_event.wait()
    return {"status": s.status, "result": s.result, "errors": s.errors, "screenshotPath": s.screenshot_path}


# --------------------------------------------------------------------------- #
# EÜR / ESt
# --------------------------------------------------------------------------- #

@mcp.tool(annotations=PREPARE)
async def elster_eur_start(year: int, data: dict[str, float]) -> dict[str, Any]:
    """Bereitet die Anlage EÜR vor (füllen, Prüfung, als Entwurf speichern). Übermittelt NIE.

    Args:
        year: Wirtschaftsjahr
        data: Feldname → Betrag. Erlaubte Felder siehe Fehlermeldung bzw. README.
    """
    try:
        e = EurData(year=year, data=data)
    except ValidationError as exc:
        return {**_validation_error(exc), "allowedFields": sorted(EUR_FIELDS)}
    s = EurFlow(get_config(), _audit()).start(e)
    return {"sessionId": s.id}


@mcp.tool(annotations=PREPARE)
async def elster_est_start(year: int, data: dict[str, float | str] | None = None) -> dict[str, Any]:
    """Bereitet die ESt 1 A vor: Stammdaten aus der Config + optionale Feld-Hinweise (ID-Teilstring → Wert),
    Prüfung, danach 30 min Zeit zur Kontrolle im Portal. Übermittelt NIE."""
    try:
        e = EstData(year=year, data=data or {})
    except ValidationError as exc:
        return _validation_error(exc)
    s = EstFlow(get_config(), _audit()).start(e)
    return {"sessionId": s.id}


# --------------------------------------------------------------------------- #
# Sitzungen
# --------------------------------------------------------------------------- #

@mcp.tool(annotations=LOCAL_ONLY)
async def elster_session_status(sessionId: str) -> dict[str, Any]:
    """Status, Fortschritt, Zusammenfassung, Screenshot-Pfad und ggf. Bestätigungscode einer Sitzung."""
    s = sessions.get(sessionId)
    return s.view() if s else {"error": "Sitzung nicht gefunden."}


@mcp.tool(annotations=LOCAL_ONLY)
async def elster_session_list() -> list[dict[str, Any]]:
    """Alle bekannten Sitzungen mit Status."""
    return sessions.list()


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
async def elster_session_cancel(sessionId: str) -> dict[str, Any]:
    """Bricht eine Sitzung ab und schließt den Browser. Bei UStVA wird dann nichts übermittelt."""
    ok = sessions.cancel(sessionId)
    if ok:
        _audit().write("session_cancel", session=sessionId)
    return {"ok": ok}


# --------------------------------------------------------------------------- #
# Lesen
# --------------------------------------------------------------------------- #

@mcp.tool(annotations=READ_ONLY)
async def elster_sync_history(years: list[int] | None = None) -> dict[str, Any]:
    """Liest „Übermittelte Formulare" aus Mein ELSTER (nur lesend)."""
    items = await SyncFlow(get_config()).history(years)
    return {"count": len(items), "items": items}


@mcp.tool(annotations=READ_ONLY)
async def elster_sync_inbox(downloadPdfs: bool = False, maxPages: int = 20) -> dict[str, Any]:
    """Liest den ELSTER-Posteingang (Bescheide, Nachrichten), nur lesend.

    Je Nachricht: subject, read (gelesen/ungelesen), hasAttachment, date. Mit downloadPdfs=true werden das
    offizielle Nachrichten-PDF (pdfPath) und alle Anhänge (attachments) über ELSTERs Zip-Export gespeichert,
    Dateinamen nach dem Schema JJJJ-MM-TT_ELSTER_<ID>_<Kurzbetreff>.pdf. Hinweis: Das Öffnen markiert
    ungelesene Nachrichten in ELSTER als gelesen.
    """
    items = await SyncFlow(get_config()).inbox(download_pdfs=downloadPdfs, max_pages=maxPages)
    return {"count": len(items), "items": items}


# --------------------------------------------------------------------------- #
# Heruntergeladene Dateien (Posteingang) weitergeben
# --------------------------------------------------------------------------- #

FILES_ROUTE = "/files/"


@mcp.tool(annotations=LOCAL_ONLY)
def elster_downloads_list() -> dict[str, Any]:
    """Listet die heruntergeladenen Dateien (Nachrichten-PDFs und Anhänge) auf dem Server."""
    base = get_config().runtime.download_dir.expanduser().resolve()
    files = sorted((p for p in base.glob("*") if p.is_file() and not p.name.startswith(".")), key=lambda p: p.name)
    return {"count": len(files), "files": [{"name": p.name, "size": p.stat().st_size} for p in files]}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False))
def elster_file_link(name: str) -> dict[str, Any]:
    """Erzeugt einen Einmal-Download-Link (10 Minuten) für eine Datei aus elster_downloads_list.

    Zum Ablegen in OneDrive den Link als ``sourceUrl`` an ``onedrive-upload`` übergeben; der OneDrive-Server
    holt die Datei dann selbst ab. Der Link funktioniert genau einmal – für einen zweiten Versuch neu erzeugen.
    """
    cfg = get_config()
    if not cfg.http.public_url:
        return {"error": "Kein öffentlicher Server-Name (MCP_DOMAIN/MCP_PUBLIC_URL) konfiguriert."}
    try:
        path = resolve_download(cfg.runtime.download_dir, name)
    except (ValueError, FileNotFoundError) as exc:
        return {"error": str(exc)}
    token = file_links.create(path)
    _audit().write("file_link", file=path.name)
    return {
        "name": path.name,
        "size": path.stat().st_size,
        "url": cfg.http.public_url.rstrip("/") + FILES_ROUTE + token,
        "expiresInSeconds": DEFAULT_TTL_SECONDS,
        "singleUse": True,
    }


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
def elster_downloads_delete(names: list[str]) -> dict[str, Any]:
    """Löscht Dateien aus dem Download-Ordner des Servers, z. B. nachdem sie in OneDrive abgelegt wurden.

    Betrifft nur die lokalen Kopien auf dem Server, nicht das ELSTER-Postfach.
    """
    base = get_config().runtime.download_dir
    deleted, errors = [], {}
    for name in names:
        try:
            resolve_download(base, name).unlink()
            deleted.append(name)
        except (ValueError, OSError) as exc:
            errors[name] = str(exc)
    _audit().write("downloads_delete", files=deleted)
    return {"deleted": deleted, "errors": errors}


@mcp.custom_route(FILES_ROUTE + "{token}", methods=["GET"])
async def download_file(request: Any) -> Any:
    """Öffentliche Route für Einmal-Links; ohne gültiges Token nur 404."""
    from starlette.responses import FileResponse, Response

    path = file_links.consume(request.path_params.get("token", ""))
    if not path or not path.is_file():
        return Response("not found", status_code=404, headers={"Cache-Control": "no-store"})
    _audit().write("file_fetched", file=path.name)
    media = "application/pdf" if path.suffix.lower() == ".pdf" else "application/octet-stream"
    return FileResponse(path, media_type=media, filename=path.name,
                        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


def http_app() -> Any:
    """Streamable-HTTP-App.

    * Mit ``OIDC_ISSUER_URL`` + ``MCP_DOMAIN``: OAuth-Resource-Server für Claude.ai
      (Authelia-Introspection, zusätzlich ``MCP_API_KEY`` als Bearer).
    * Nur ``MCP_API_KEY``: einfacher Bearer-Schutz.
    * Nichts gesetzt: Start wird verweigert.
    """
    from mcp.server.auth.settings import AuthSettings
    from mcp.server.transport_security import TransportSecuritySettings

    from .auth import ElsterTokenVerifier
    from .security import BearerAuthMiddleware

    cfg = get_config()
    h = cfg.http
    api_key = h.token.get_secret_value() if h.token else None

    # Schutz gegen DNS-Rebinding: nur die öffentliche Domain und localhost als Host-Header.
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*"]
    if h.public_url:
        public_host = urlparse(h.public_url).netloc
        hosts += [public_host, f"{public_host}:*"]
        origins += [h.public_url.rstrip("/"), "https://claude.ai"]
    security = TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)

    if h.oidc_issuer_url:
        if not h.public_url:
            raise SecretError("OIDC benötigt MCP_DOMAIN (bzw. MCP_PUBLIC_URL) als öffentliche Adresse.")
        verifier = ElsterTokenVerifier(
            api_key=api_key,
            introspection_url=h.oidc_introspection_url,
            client_id=h.oidc_client_id,
            client_secret=h.oidc_client_secret.get_secret_value() if h.oidc_client_secret else None,
            allowed_users=h.oidc_allowed_users,
            required_scopes=h.oidc_required_scopes,
            audience=h.public_url,
        )
        # Das SDK liest Auth-Einstellungen beim Bauen der App; die Tools hängen am Modul-Server.
        mcp.settings.auth = AuthSettings(
            issuer_url=h.oidc_issuer_url,
            resource_server_url=h.public_url.rstrip("/") + h.path,
            required_scopes=h.oidc_required_scopes or None,
            validate_token_resource=False,
        )
        mcp._token_verifier = verifier
        log.info("HTTP-Auth: OAuth via %s%s", h.oidc_issuer_url, " + MCP_API_KEY" if api_key else "")
        return mcp.streamable_http_app(streamable_http_path=h.path, transport_security=security)

    if not api_key:
        raise SecretError(
            "HTTP-Transport ohne Schutz ist nicht erlaubt. Setze OIDC_* (Authelia) und/oder "
            "MCP_API_KEY_FILE / MCP_API_KEY (mind. 32 Zeichen, `python -m elster_mcp gen-token`)."
        )
    log.info("HTTP-Auth: nur MCP_API_KEY")
    return BearerAuthMiddleware(
        mcp.streamable_http_app(streamable_http_path=h.path, transport_security=security), api_key,
        public_prefixes=(FILES_ROUTE,),
    )


def run(transport: str = "stdio") -> None:
    cfg = get_config()
    if transport == "stdio":
        mcp.run("stdio")
        return
    import uvicorn

    if cfg.http.host not in {"127.0.0.1", "localhost", "::1"}:
        log.warning("HTTP-Server lauscht auf %s – nur hinter TLS-Reverse-Proxy betreiben!", cfg.http.host)
    uvicorn.run(http_app(), host=cfg.http.host, port=cfg.http.port, log_level="warning",
                proxy_headers=bool(os.environ.get("ELSTER_MCP_BEHIND_PROXY")))

