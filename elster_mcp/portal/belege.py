"""Belegnachreichung: Formular füllen, Anhänge hochladen, Prüfung, Entwurf → PAUSE → (Freigabe) → Versenden.

Nur für Belege, die das Finanzamt angefordert hat – zur UStVA selbst werden keine Belege eingereicht.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from playwright.async_api import Page

from ..config import ElsterConfig
from ..filelinks import resolve_download
from ..models import BelegeRequest
from ..security import AuditLog, confirmation_code, new_nonce
from ..sessions import Session, sessions
from .base import ElsterPortal, PortalError
from .drafts import JS_PRUEF_RESULT, DraftMixin

log = logging.getLogger("elster_mcp.belege")

FORM_URL = "https://www.elster.de/eportal/formulare-leistungen/alleformulare/belegnachreichung"
MAX_FILE_BYTES = 10 * 1024 * 1024
PAGE_ORDER = ("Startseite", "PersonA", "PersonB", "AbweichendeAdresse", "Belege", "Anhaenge")


def prepare_files(download_dir: Path, names: list[str]) -> list[dict]:
    """Dateien aus dem Download-Ordner prüfen (Name, Größe, Typ) und mit SHA-256 für den Freigabecode versehen."""
    files = []
    for name in names:
        path = resolve_download(download_dir, name)
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ValueError(f"{name}: {size / 1024 / 1024:.1f} MB – ELSTER erlaubt höchstens 10 MB je Datei")
        head = path.read_bytes()[:5]
        if path.suffix.lower() == ".pdf" and not head.startswith(b"%PDF"):
            raise ValueError(f"{name}: keine gültige PDF-Datei")
        files.append({"name": path.name, "path": str(path), "size": size,
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return files


def verify_send_page(text: str, req: BelegeRequest, files: list[dict]) -> list[str]:
    """Prüft die Versand-Übersicht gegen die Freigabe (leer = alles passt)."""
    flat = " ".join(text.split())
    problems = []
    for label, value in (("Steuerart", req.steuerart), ("Jahr", str(req.year)), ("Zeitraum", req.zeitraum)):
        if value and value not in flat:
            problems.append(f"{label} „{value}“ fehlt in der Übersicht")
    for f in files:
        if f["name"] not in flat:
            problems.append(f"Anhang {f['name']} fehlt in der Übersicht")
    return problems


class BelegeFlow(DraftMixin, ElsterPortal):
    def __init__(self, cfg: ElsterConfig, audit: AuditLog) -> None:
        super().__init__(cfg)
        self.audit = audit

    def start(self, req: BelegeRequest) -> Session:
        files = prepare_files(self.cfg.runtime.download_dir, req.files)
        s = sessions.create("BELEG")
        s.summary = {
            "year": req.year,
            "steuerart": req.steuerart,
            "zeitraum": req.zeitraum,
            "text": req.text,
            "anhaenge": [{"name": f["name"], "size": f["size"], "sha256": f["sha256"]} for f in files],
        }
        self.audit.write("belege_start", session=s.id, year=req.year, steuerart=req.steuerart,
                         zeitraum=req.zeitraum, files=[f["name"] for f in files])
        s.task = asyncio.create_task(self._run(s, req, files))
        return s

    async def _run(self, s: Session, req: BelegeRequest, files: list[dict]) -> None:
        nonce = new_nonce()
        try:
            # Phase 1: ausfüllen, hochladen, prüfen, als Entwurf speichern – dann Browser schließen.
            async with sessions.browser_slots, self.open() as page:
                s.status = "LOGGING_IN"
                s.log("Belegnachreichung: Login …")
                await self.login(page)

                s.status = "OPENING_FORM"
                s.log("Öffne Formular „Belegnachreichung“ …")
                await self._open_form(page)

                s.status = "FILLING_PAGES"
                await self._fill_startseite(page)
                s.log("[Startseite] Steuernummer eingetragen")
                await self._goto(page, "PersonA")
                await self._fill_person(page)
                s.log("[PersonA] Name eingetragen")
                await self._goto(page, "Belege")
                await self._fill_belege(page, req)
                s.log(f"[Belege] {req.steuerart} {req.year} {req.zeitraum or ''}".rstrip())
                await self._goto(page, "Anhaenge")
                await self._upload(page, files)
                s.log(f"[Anhaenge] {len(files)} Datei(en) hochgeladen")

                s.status = "PRUEFUNG"
                s.log('ELSTER-"Prüfung" …')
                await self._pruefen(page, s)
                s.screenshot_path = await self.screenshot(page, f"belege_pruefung_{s.id}")

                s.status = "SAVING"
                draft_id, draft_name = await self._save_draft(page)
                s.draft = {"id": draft_id, "name": draft_name}
                s.log(f"Entwurf gespeichert: {draft_name} (ID {draft_id})")
                self.audit.write("belege_draft_saved", session=s.id, draft=draft_id)

            assert s.summary is not None
            s.summary["draftId"] = draft_id
            s.confirmation_code = confirmation_code(s.id, s.summary, nonce)
            s.status = "AWAITING_CONFIRM"
            s.log("Prüfung bestanden, Entwurf gespeichert – warte auf ausdrückliche Freigabe.")
            self.audit.write("belege_awaiting_confirm", session=s.id, draft=draft_id)

            timeout = self.cfg.security.confirm_timeout_minutes * 60
            try:
                await asyncio.wait_for(s.confirm_event.wait(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise PortalError(
                    f"Keine Freigabe innerhalb von {timeout // 60} min – nichts übermittelt. "
                    f"Der Entwurf {draft_id} bleibt in „Meine Formulare“."
                ) from exc

            # Phase 2: gespeicherten Entwurf öffnen, Versand-Übersicht prüfen, versenden.
            async with sessions.browser_slots, self.open() as page:
                s.status = "SUBMITTING"
                s.log(f"Öffne Entwurf {draft_id} …")
                await self.login(page)
                await self._open_draft(page, draft_id)
                await self._goto_send_page(page)
                text = await page.evaluate("() => (document.querySelector('main') || document.body).innerText")
                problems = verify_send_page(text, req, files)
                s.screenshot_path = await self.screenshot(page, f"belege_versand_{s.id}")
                if problems:
                    self.audit.write("belege_draft_mismatch", session=s.id, draft=draft_id, problems=problems)
                    raise PortalError("Entwurf weicht von der Freigabe ab – nichts übermittelt: " + "; ".join(problems))

                s.log("Sende an ELSTER …")
                self.audit.write("belege_submitting", session=s.id, draft=draft_id)
                ticket = await self._submit(page)
                s.screenshot_path = await self.screenshot(page, f"belege_submitted_{s.id}")
                s.result = {"success": True, "draftId": draft_id, **ticket}
                s.status = "DONE"
                s.log(f"Übermittelt. Transferticket: {ticket.get('ticket') or '?'}")
                self.audit.write("belege_submitted", session=s.id, draft=draft_id, **ticket)
        except asyncio.CancelledError:
            s.status = "CANCELLED"
            self.audit.write("belege_cancelled", session=s.id)
        except Exception as exc:
            log.error("Belege-Sitzung %s: %s", s.id, exc)
            s.fail(str(exc))
            s.result = {"success": False, "error": str(exc)}
            self.audit.write("belege_error", session=s.id, error=str(exc))
        finally:
            s.confirmation_code = None
            s.done_event.set()
            sessions.schedule_cleanup(s.id)

    # ------------------------------------------------------------------ #

    async def _settle(self, page: Page) -> None:
        for _ in range(10):
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
                break
            except Exception:
                await self.sleep(1)
        await self.sleep(1.5)
        await self.handle_modals(page)

    async def _open_form(self, page: Page) -> None:
        for _ in range(2):
            await page.goto(FORM_URL, wait_until="networkidle", timeout=60000)
            await self._settle(page)
            if not self._is_pending_tasks(page.url):
                break
            await self._discard_unsaved_restores(page)
        await page.locator("main button", has_text="Weiter").first.click()
        await self._settle(page)
        skip = page.locator("button", has_text="Ohne Datenübernahme fortfahren")
        if await skip.count():
            await skip.first.click()
            await self._settle(page)
        if self.page_name(page.url) != "Startseite":
            raise PortalError(f"Formular-Startseite nicht erreicht (Seite {self.page_name(page.url)}).")

    async def _goto(self, page: Page, target: str) -> None:
        """Mit „Nächste Seite" bis zur Zielseite blättern (Reihenfolge wie im Formular)."""
        for _ in range(len(PAGE_ORDER)):
            if self.page_name(page.url) == target:
                return
            await page.locator("#NextPage").first.click()
            await self._settle(page)
        if self.page_name(page.url) != target:
            raise PortalError(f"Seite {target} nicht erreicht (aktuell {self.page_name(page.url)}).")

    async def _fill_startseite(self, page: Page) -> None:
        radio = page.locator('main input[type="radio"][value="S"]')
        if await radio.count():
            await radio.first.check()
            await self.sleep(1)
        if not await self.fill_steuernummer(page):
            raise PortalError("Steuernummer ließ sich nicht eintragen – Abbruch.")

    async def _fill_person(self, page: Page) -> None:
        tp = self.cfg.taxpayer
        typ = page.locator('select[id$="Person_ASteuerpflichtigerTyp)"]')
        if await typ.count():
            await typ.first.select_option(label="natürliche Person")
            await self.sleep(1)
        for suffix, value in (("Person_AVorname)", tp.first_name), ("Person_AName)", tp.name)):
            if value:
                el = await page.locator(f'input[id$="{suffix}"]').first.element_handle()
                await self.type_into(page, el, value)

    async def _fill_belege(self, page: Page, req: BelegeRequest) -> None:
        await page.locator('select[id$="SteuerartenSteuerart)"]').first.select_option(label=req.steuerart)
        await self.sleep(1)
        take = page.locator('button[id^="CreateMzbItem/"][id*="/Steuerart"]')
        if await take.count() and await take.first.is_enabled():
            await take.first.click()
            await self._settle(page)
        await page.locator('select[id$="BelegeJahr)"]').first.select_option(label=str(req.year))
        await self.sleep(1)
        if req.zeitraum:
            await page.locator('select[id$="BelegeZeitraum)"]').first.select_option(label=req.zeitraum)
            await self.sleep(1)
        await page.locator('textarea[id$="BelegeText)"]').first.fill(req.text)
        await page.keyboard.press("Tab")
        await self.sleep(1)

    async def _upload(self, page: Page, files: list[dict]) -> None:
        await page.locator("#anhang_mzb_anhang_multiUpload").set_input_files([f["path"] for f in files])
        await self._settle(page)
        # Bezeichnung je Anhang: Dateiname ohne Endung, falls ELSTER sie nicht selbst setzt.
        names = page.locator('input[id$="AnhangDateibezeichnung)"]')
        for i in range(await names.count()):
            el = names.nth(i)
            if await el.is_visible() and not await el.input_value() and i < len(files):
                await el.fill(Path(files[i]["name"]).stem[:100])
        take = page.locator('button[id^="CreateMzbItem/"][id*="/Anhaenge"]')
        if await take.count() and await take.first.is_enabled():
            await take.first.click()
            await self._settle(page)

    async def _pruefen(self, page: Page, s: Session) -> None:
        await page.locator("#SwitchModusPruefen").first.click()
        await self._settle(page)
        res = await page.evaluate(JS_PRUEF_RESULT)
        if res["hasErrors"] and not res["noErrors"]:
            s.screenshot_path = await self.screenshot(page, f"belege_pruefung_fehler_{s.id}")
            raise PortalError("ELSTER-Prüfung meldet Fehler: " + ("; ".join(res["errTexts"]) or "siehe Screenshot"))
