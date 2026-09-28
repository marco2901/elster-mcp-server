"""Belegnachreichung: Formular füllen, Anhänge hochladen, Prüfung, Entwurf → PAUSE → (Freigabe) → Versenden.

Nur für Belege, die das Finanzamt angefordert hat – zur UStVA selbst werden keine Belege eingereicht.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path

from playwright.async_api import Page

from ..config import ElsterConfig
from ..filelinks import resolve_download
from ..models import BelegeRequest, check_birth_date
from ..security import AuditLog, confirmation_code, new_nonce
from ..sessions import Session, sessions
from ..taxnumber import validate_tax_id
from .base import ElsterPortal, PortalError
from .drafts import JS_CLICK_EXACT_BUTTON, JS_PRUEF_RESULT, DraftMixin

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


def parse_error_list(text: str) -> list[str]:
    """„Ihre Angaben sind leider nicht korrekt: <Meldung> <Seite>" → ["<Seite>: <Meldung>", …]."""
    flat = " ".join(text.split())
    parts = flat.split("Ihre Angaben sind leider nicht korrekt:")[1:]
    out = []
    for part in parts:
        m = re.match(r"\s*(.+?[.!])\s*(\d+ - [^.]+?)?\s*$", part.strip())
        out.append(f"{m.group(2).strip()}: {m.group(1)}" if m and m.group(2) else part.strip()[:200])
    return out[:10]


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
        if not self.cfg.taxpayer.tax_id:
            raise ValueError("ELSTER_TAX_ID (Steuer-Identifikationsnummer) fehlt – ELSTER verlangt sie "
                             "bei der Belegnachreichung für natürliche Personen.")
        validate_tax_id(self.cfg.taxpayer.tax_id)
        if not self.cfg.taxpayer.birth_date:
            raise ValueError("ELSTER_BIRTH_DATE (Geburtsdatum TT.MM.JJJJ) fehlt – ELSTER verlangt es "
                             "bei der Belegnachreichung für natürliche Personen.")
        check_birth_date(self.cfg.taxpayer.birth_date)
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
        # „Angaben noch nicht vollständig" betrifft beim Ausfüllen meist Pflichtfelder späterer Seiten –
        # weitermachen; die ELSTER-Prüfung am Ende meldet, was wirklich fehlt, und bricht dann ab.
        later = page.locator("#correctlater")
        if await later.count() and await later.first.is_visible():
            await later.first.click()
            await self.sleep(1.5)

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
        fields = (("Person_AIdentifikationsnummer)", validate_tax_id(tp.tax_id)),
                  ("Person_AVorname)", tp.first_name), ("Person_AName)", tp.name),
                  ("Person_AGeburtsdatum)", check_birth_date(tp.birth_date)))
        for suffix, value in fields:
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

    async def _visible(self, page: Page, selector: str):  # noqa: ANN202
        loc = page.locator(selector)
        for i in range(await loc.count() - 1, -1, -1):
            if await loc.nth(i).is_visible():
                return loc.nth(i)
        raise PortalError(f"Feld {selector} nicht gefunden.")

    async def _upload(self, page: Page, files: list[dict]) -> None:
        """Je Anhang: Bezeichnung, Datei, „Eintrag übernehmen" – ELSTER verlangt alle Angaben pro Eintrag."""
        for n, f in enumerate(files):
            if n:
                await page.locator('button[id^="AddMzbItem/"][id*="/Anhaenge"]').first.click()
                await self._settle(page)
            desc = await self._visible(page, 'input[id$="AnhangDateibezeichnung)"]')
            await desc.fill(Path(f["name"]).stem[:100])
            await page.keyboard.press("Tab")
            upload = page.locator('input[type="file"][id$="AnhangDateiname)"]').last
            await upload.set_input_files(f["path"])
            await self._settle(page)
            await page.locator('button[id^="CreateMzbItem/"][id*="/Anhaenge"]').first.click()
            await self._settle(page)

    async def _pruefen(self, page: Page, s: Session) -> None:
        await page.locator("#SwitchModusPruefen").first.click()
        await self._settle(page)
        res = await page.evaluate(JS_PRUEF_RESULT)
        if res["hasErrors"] and not res["noErrors"]:
            errors = res["errTexts"] or await self._error_list(page)
            s.screenshot_path = await self.screenshot(page, f"belege_pruefung_fehler_{s.id}")
            raise PortalError("ELSTER-Prüfung meldet Fehler: " + ("; ".join(errors) or "siehe Screenshot"))
        # Weiter zur Übersicht – der Screenshot für die Freigabe zeigt dann Steuerart, Zeitraum und Anhänge.
        if await page.evaluate(JS_CLICK_EXACT_BUTTON, "Weiter"):
            await self._settle(page)

    async def _error_list(self, page: Page) -> list[str]:
        """Einträge der ELSTER-„Fehlerliste" im Navigationsbereich."""
        link = page.locator("#fehlerliste_link")
        if not await link.count():
            return []
        await link.first.click()
        await self.sleep(2)
        text = await page.evaluate("() => (document.querySelector('.page-form__treeTop') || document.body).innerText")
        return parse_error_list(text)
