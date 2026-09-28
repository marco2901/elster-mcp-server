"""UStVA: Login → Formular → Kennziffern → Prüfung → PAUSE → (Freigabe) → Absenden."""

from __future__ import annotations

import asyncio
import logging
import re

from playwright.async_api import Page

from ..config import ElsterConfig
from ..constants import INPUT_TAX_KZ, KENNZIFFERN, PORTAL_URLS
from ..models import UstvaReport
from ..security import AuditLog, confirmation_code, new_nonce
from ..sessions import Session, sessions
from .base import ElsterPortal, PortalError

log = logging.getLogger("elster_mcp.ustva")

# Alle sichtbaren, editierbaren Kennziffer-Felder der aktuellen Seite. ELSTER benennt sie stabil,
# z. B. id="…_fields(eruAnmeldungssteuernSteuerfallUmsatzsteuervoranmeldungKz46)".
JS_LIST_KZ_INPUTS = """
() => Array.from(document.querySelectorAll('input:not([type="hidden"])'))
  .filter(e => e.offsetParent !== null && !e.readOnly && !e.disabled && /Kz\\d+/.test((e.id || '') + (e.name || '')))
  .map(e => ({ id: e.id || '', name: e.getAttribute('name') || '', placeholder: e.placeholder || '' }))
"""

_KZ_FIELD_RE = re.compile(r"Kz(\d+)[)\]]$")


def kz_of_field(field_id: str, name: str = "") -> str | None:
    """Kennziffer aus der Feld-ID bzw. dem Feldnamen, nur bei exaktem Ende (``Kz46)``/``Kz46]``)."""
    for candidate in (field_id, name):
        m = _KZ_FIELD_RE.search(candidate or "")
        if m:
            return str(int(m.group(1)))
    return None


def format_kz_value(value: float, placeholder: str) -> str:
    """„Euro, Cent“-Felder mit Komma und zwei Nachkommastellen, reine Euro-Felder ganzzahlig."""
    if "cent" in placeholder.lower():
        return f"{value:.2f}".replace(".", ",")
    return str(round(value))


JS_PRUEF_RESULT = """
() => {
  const body = document.body.innerText.toLowerCase();
  const hasErrors = body.includes('fehler vorhanden') || body.includes('sind noch fehler') || body.includes('fehlerliste');
  const noErrors = body.includes('keine fehler') || body.includes('keine pflichtfehler');
  const errTexts = Array.from(document.querySelectorAll('.alert-danger, .feedback--error, .validation-error'))
    .map(e => (e.textContent || '').trim()).filter(Boolean).slice(0, 10);
  return { hasErrors, noErrors, errTexts };
}
"""

JS_CLICK_EXACT_BUTTON = """
(label) => {
  const btn = Array.from(document.querySelectorAll('button')).find(b =>
    (b.textContent || '').trim() === label && b.offsetParent !== null && !b.disabled);
  if (btn) { btn.click(); return true; }
  return false;
}
"""

JS_EXTRACT_TICKET = """
() => {
  const body = document.body.innerText;
  const cells = Array.from(document.querySelectorAll('td, dt, dd, span, div, p'));
  let ticket = '', auftrag = '';
  for (let i = 0; i < cells.length - 1; i++) {
    const t = (cells[i].textContent || '').trim().toLowerCase();
    if (!ticket && t.includes('transferticket')) ticket = (cells[i + 1].textContent || '').trim();
    if (!auftrag && (t.includes('auftragsnummer') || t.includes('telenummer'))) auftrag = (cells[i + 1].textContent || '').trim();
  }
  if (!ticket) { const m = body.match(/Transferticket[:\\s]+([A-Z0-9-]+)/i); if (m) ticket = m[1]; }
  return { ticket: ticket.slice(0, 80), auftrag: auftrag.slice(0, 80) };
}
"""


# --------------------------------------------------------------------------- #
# Entwurf „Meine Formulare“ und Abgleich der Versand-Übersicht (ohne Browser testbar)
# --------------------------------------------------------------------------- #

JS_DRAFT_ROWS = """
() => Array.from(document.querySelectorAll('[id^="oeffneEntwurf_"]')).map(b => ({
  id: b.id.replace('oeffneEntwurf_', ''),
  name: (b.innerText || b.textContent || '').replace(/\\s+/g, ' ').trim(),
}))
"""

JS_SUMMARY_ROWS = """
() => Array.from(document.querySelectorAll('table tr')).filter(r => r.offsetParent !== null)
  .map(r => Array.from(r.querySelectorAll('td, th')).map(c => (c.innerText || '').replace(/\\s+/g, ' ').trim()))
  .filter(c => c.length >= 2)
"""

#: Von ELSTER berechnete Kennziffern, die in der Versand-Übersicht zusätzlich erscheinen dürfen.
COMPUTED_KZ = frozenset({"83"})


def pick_draft(rows: list[dict], name: str) -> str | None:
    """Neuester Entwurf mit exakt dieser Bezeichnung (ELSTER vergibt aufsteigende IDs)."""
    ids = [int(r["id"]) for r in rows if r.get("name") == name and str(r.get("id", "")).isdigit()]
    return str(max(ids)) if ids else None


def parse_amount(text: str) -> float | None:
    """„1.234,56 €" → 1234.56; „214 €" → 214.0; sonst None."""
    m = re.fullmatch(r"(-?[\d.]+(?:,\d{1,2})?)\s*€?", text.strip())
    if not m:
        return None
    return float(m.group(1).replace(".", "").replace(",", "."))


def parse_summary(rows: list[list[str]]) -> tuple[dict[str, str], dict[str, float]]:
    """Versand-Übersicht → (Allgemein-Angaben, Kennziffer → Betrag)."""
    general: dict[str, str] = {}
    amounts: dict[str, float] = {}
    for cells in rows:
        kz_idx = next((i for i, c in enumerate(cells[1:], 1) if re.fullmatch(r"\d{1,3}", c)), None)
        if kz_idx is not None and kz_idx + 1 < len(cells):
            value = parse_amount(cells[kz_idx + 1])
            if value is not None:
                amounts[str(int(cells[kz_idx]))] = value
                continue
        if cells[0] and cells[-1] and cells[0] != "Kennzahl":
            general.setdefault(cells[0], cells[-1])
    return general, amounts


def verify_summary(rows: list[list[str]], report: dict[str, float], year: int, period_label: str) -> list[str]:
    """Abweichungen zwischen Versand-Übersicht und freigegebenen Beträgen (leer = alles passt)."""
    general, amounts = parse_summary(rows)
    problems: list[str] = []
    if general.get("Jahr") != str(year):
        problems.append(f"Jahr {general.get('Jahr')!r} statt {year}")
    if period_label and general.get("Zeitraum") != period_label:
        problems.append(f"Zeitraum {general.get('Zeitraum')!r} statt {period_label!r}")
    expected = {k: v for k, v in report.items() if v != 0}
    for kz, value in expected.items():
        shown = amounts.get(kz)
        if shown is None:
            problems.append(f"Kz{kz} fehlt")
        elif abs(shown - value) >= 0.005 and not (shown == int(shown) and abs(shown - round(value)) < 0.005):
            problems.append(f"Kz{kz} = {shown:.2f} statt {value:.2f}")
    for kz, shown in amounts.items():
        if kz not in expected and kz not in COMPUTED_KZ and shown != 0:
            problems.append(f"Kz{kz} = {shown:.2f} nicht freigegeben")
    if not amounts:
        problems.append("Keine Kennziffern in der Übersicht gefunden")
    return problems


class UstvaFlow(ElsterPortal):
    def __init__(self, cfg: ElsterConfig, audit: AuditLog) -> None:
        super().__init__(cfg)
        self.audit = audit

    def start(self, ustva: UstvaReport) -> Session:
        s = sessions.create("USTVA")
        s.summary = {
            "year": ustva.year,
            "period": str(ustva.period),
            "elsterPeriod": ustva.elster_period,
            "kennziffern": {
                k: {"betrag": v, "beschreibung": KENNZIFFERN[k]["description"]}
                for k, v in sorted(ustva.report.items(), key=lambda kv: int(kv[0]))
            },
        }
        self.audit.write("ustva_start", session=s.id, year=ustva.year, period=str(ustva.period),
                         report=ustva.report)
        s.task = asyncio.create_task(self._run(s, ustva))
        return s

    async def _run(self, s: Session, ustva: UstvaReport) -> None:
        nonce = new_nonce()
        try:
            # Phase 1: ausfüllen, prüfen, als Entwurf in „Meine Formulare" speichern – dann Browser schließen.
            async with sessions.browser_slots, self.open() as page:
                s.status = "LOGGING_IN"
                s.log(f"UStVA {ustva.year} / {ustva.period}: Login …")
                await self.login(page)

                s.status = "OPENING_FORM"
                s.log("Öffne UStVA-Formular …")
                await self._open_form(page, ustva.year)
                await self._select_period(page, ustva.elster_period)

                s.status = "FILLING_PAGES"
                s.log("Fülle Formularseiten …")
                missing = await self._walk_pages(page, ustva.report, s)
                if missing:
                    raise PortalError(f"Kennziffern nicht im Formular gefunden: {', '.join(missing)} – Abbruch.")

                s.status = "PRUEFUNG"
                s.log('ELSTER-"Prüfung" …')
                await self._run_pruefung(page, s)
                s.screenshot_path = await self.screenshot(page, f"ustva_pruefung_{s.id}")

                s.status = "SAVING"
                s.log("Speichere Entwurf in „Meine Formulare“ …")
                draft_id, draft_name = await self._save_draft(page)
                s.draft = {"id": draft_id, "name": draft_name}
                s.log(f"Entwurf gespeichert: {draft_name} (ID {draft_id})")
                self.audit.write("ustva_draft_saved", session=s.id, draft=draft_id)

            # Freigabe-Code bindet Beträge UND Entwurf.
            assert s.summary is not None
            s.summary["draftId"] = draft_id
            s.confirmation_code = confirmation_code(s.id, s.summary, nonce)
            s.status = "AWAITING_CONFIRM"
            s.log("Prüfung bestanden, Entwurf gespeichert – warte auf ausdrückliche Freigabe.")
            self.audit.write("ustva_awaiting_confirm", session=s.id, draft=draft_id)

            timeout = self.cfg.security.confirm_timeout_minutes * 60
            try:
                await asyncio.wait_for(s.confirm_event.wait(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise PortalError(
                    f"Keine Freigabe innerhalb von {timeout // 60} min – nichts übermittelt. "
                    f"Der Entwurf {draft_id} bleibt in „Meine Formulare“."
                ) from exc

            # Phase 2: gespeicherten Entwurf öffnen, Übersicht gegen die Freigabe prüfen, absenden.
            async with sessions.browser_slots, self.open() as page:
                s.status = "SUBMITTING"
                s.log(f"Öffne Entwurf {draft_id} …")
                await self.login(page)
                await self._open_draft(page, draft_id)
                await self._goto_send_page(page)
                rows = await page.evaluate(JS_SUMMARY_ROWS)
                period_label = draft_name.split(" - ", 1)[1] if " - " in draft_name else ""
                problems = verify_summary(rows, ustva.report, ustva.year, period_label)
                s.screenshot_path = await self.screenshot(page, f"ustva_versand_{s.id}")
                if problems:
                    self.audit.write("ustva_draft_mismatch", session=s.id, draft=draft_id, problems=problems)
                    raise PortalError("Entwurf weicht von der Freigabe ab – nichts übermittelt: " + "; ".join(problems))

                s.log("Sende an ELSTER …")
                self.audit.write("ustva_submitting", session=s.id, draft=draft_id)
                ticket = await self._submit(page)
                s.screenshot_path = await self.screenshot(page, f"ustva_submitted_{s.id}")
                s.result = {"success": True, "draftId": draft_id, **ticket}
                s.status = "DONE"
                s.log(f"Übermittelt. Transferticket: {ticket.get('ticket') or '?'}")
                self.audit.write("ustva_submitted", session=s.id, draft=draft_id, **ticket)
        except asyncio.CancelledError:
            s.status = "CANCELLED"
            self.audit.write("ustva_cancelled", session=s.id)
        except Exception as exc:
            log.error("UStVA-Sitzung %s: %s", s.id, exc)
            s.fail(str(exc))
            s.result = {"success": False, "error": str(exc)}
            self.audit.write("ustva_error", session=s.id, error=str(exc))
        finally:
            s.confirmation_code = None
            s.done_event.set()
            sessions.schedule_cleanup(s.id)

    # ------------------------------------------------------------------ #

    async def _save_draft(self, page: Page) -> tuple[str, str]:
        """„Speichern und Formular verlassen" → Entwurf; liefert (Entwurfs-ID, Bezeichnung)."""
        await page.click("#verlassenModal")
        save = page.locator("#saveAufgabe")
        await save.wait_for(state="visible", timeout=15000)
        text = await page.locator(".modal").filter(has=save).first.inner_text()
        m = re.search(r"Bezeichnung gespeichert:\s*(.+?)\s*(?:Ordnungskriterium|Sie finden|$)", " ".join(text.split()))
        if not m:
            raise PortalError("Bezeichnung des Entwurfs nicht erkannt – Abbruch.")
        name = m.group(1).strip()
        async with page.expect_navigation(timeout=30000):
            await save.click()
        rows = await self._draft_rows(page)
        draft_id = pick_draft(rows, name)
        if not draft_id:
            raise PortalError(f"Gespeicherter Entwurf „{name}“ nicht in „Meine Formulare“ gefunden.")
        return draft_id, name

    async def _draft_rows(self, page: Page) -> list[dict]:
        await page.goto(PORTAL_URLS["meine_formulare"], wait_until="networkidle", timeout=30000)
        await self.sleep(2)
        await self.handle_modals(page)
        await page.click("#meineFormulare-entwuerfe_tab_desktop")
        await self.sleep(2)
        return await page.evaluate(JS_DRAFT_ROWS)

    async def _open_draft(self, page: Page, draft_id: str) -> None:
        if not draft_id.isdigit():
            raise PortalError("Ungültige Entwurfs-ID.")
        await self._draft_rows(page)
        btn = page.locator(f"#oeffneEntwurf_{draft_id}")
        if await btn.count() == 0:
            raise PortalError(f"Entwurf {draft_id} nicht mehr in „Meine Formulare“ – nichts übermittelt.")
        async with page.expect_navigation(timeout=30000):
            await btn.click()
        await self.sleep(3)
        await self.handle_modals(page)

    async def _goto_send_page(self, page: Page) -> None:
        """Wechselt in „Versenden des Formulars" (ELSTER prüft dabei erneut)."""
        async with page.expect_navigation(timeout=60000):
            await page.click("#SwitchModusSenden")
        await self.sleep(3)
        await self.handle_modals(page)
        res = await page.evaluate(JS_PRUEF_RESULT)
        if res["hasErrors"] and not res["noErrors"]:
            raise PortalError("ELSTER meldet Fehler im Entwurf – nichts übermittelt: " + "; ".join(res["errTexts"]))

    async def _open_form(self, page: Page, year: int) -> None:
        for attempt in range(2):
            await page.goto(PORTAL_URLS["ustva_form"], wait_until="networkidle", timeout=60000)
            if not await self.select_year(page, year):
                raise PortalError(f"Jahr {year} nicht im UStVA-Formular auswählbar.")
            await self.click_enter(page)
            await self.sleep(3)
            await self.handle_modals(page)
            await self.wait_nav(page, 8000)
            await self.sleep(2)
            if "mein-elster/startseite" not in page.url:
                break
            log.info("Modal hat zur Startseite umgeleitet – neuer Versuch (%d).", attempt + 1)
        await self.skip_datenuebernahme(page)

    async def _select_period(self, page: Page, period_value: str) -> None:
        selectors = [
            'select[id*="UmsatzsteuervoranmeldungZeitraum"]',
            'select[name*="Zeitraum"]',
            'select[id*="Zeitraum"]',
        ]
        for _attempt in range(3):
            for sel in selectors:
                if not await page.query_selector(sel):
                    continue
                has = await page.evaluate(
                    "([s, v]) => { const el = document.querySelector(s);"
                    " return el ? Array.from(el.options).some(o => o.value === v) : false; }",
                    [sel, period_value],
                )
                if has:
                    await page.select_option(sel, period_value)
                    await self.fill_steuernummer(page)
                    await self.sleep(1.5)
                    await self.click_next(page)
                    return
            await self.handle_modals(page)
            await self.sleep(3)
        raise PortalError(f'Zeitraum "{period_value}" nicht im Auswahlfeld gefunden.')

    async def _walk_pages(self, page: Page, report: dict[str, float], s: Session) -> list[str]:
        wanted = {k for k, v in report.items() if v != 0}
        filled: set[str] = set()
        last_url, same = "", 0
        for n in range(1, 16):
            await self.sleep(2)
            url = page.url
            if url == last_url:
                same += 1
                if same >= 3:
                    s.log(f"Hänge auf Seite {self.page_name(url)} – Abbruch der Seitenschleife.")
                    break
            else:
                same = 0
            last_url = url
            await self.handle_modals(page)

            name = self.page_name(url)
            s.log(f"[Seite {n}] {name}")
            if name == "AngabenUnternehmen":
                await self._fill_angaben_unternehmen(page)
            # Felder über ihre Kennziffer finden statt über Seitennamen – ELSTER verschiebt Kennziffern
            # zwischen Seiten (z. B. §13b: Kz 46/47/73 auf „LeistungsempfaengerAlsSteuerschuldner“).
            for field in await page.evaluate(JS_LIST_KZ_INPUTS):
                kz = kz_of_field(field["id"], field["name"])
                if kz in wanted and kz not in filled:
                    await self._fill_kz(page, field, kz, report[kz])
                    filled.add(kz)
                    s.log(f"  Kz{kz} = {report[kz]:.2f}")

            if not await self.has_next(page):
                break
            await self.click_next(page)
            await self.sleep(0.5)
            await self.handle_modals(page)
        return sorted(wanted - filled, key=int)

    async def _fill_angaben_unternehmen(self, page: Page) -> None:
        tp = self.cfg.taxpayer
        fields = [
            (["UnternehmerName", "Nachname"], ["Name", "Nachname"], tp.name),
            (["Vorname"], ["Vorname"], tp.first_name),
            (["UnternehmerStr", "Strasse", "AdresseStrasse"], ["Straße", "Strasse"], tp.street),
            (["UnternehmerHausnummer", "Hausnummer"], ["Hausnummer"], tp.house_number),
            (["UnternehmerPLZ", "PLZ", "Postleitzahl"], ["Postleitzahl", "PLZ"], tp.zip),
            (["UnternehmerOrt", "Ort", "Wohnort"], ["Ort", "Gemeinde"], tp.city),
            (["UnternehmerLand", "Land"], ["Land"], tp.country),
        ]
        for id_patterns, labels, value in fields:
            if not value:
                continue
            done = False
            for pat in id_patterns:
                el = await page.query_selector(
                    f'input[id*="{pat}"]:not([type="hidden"]):not([readonly]):not([disabled])'
                )
                if el and await el.is_visible():
                    await self.type_into(page, el, value)
                    done = True
                    break
            if not done:
                for label in labels:
                    if await self.fill_by_label(page, label, value):
                        break

    async def _fill_kz(self, page: Page, field: dict, kz: str, value: float) -> None:
        if kz in INPUT_TAX_KZ and value < 0:
            raise PortalError(f"Vorsteuer Kz{kz} negativ ({value:.2f}) – Abbruch.")
        el = page.locator(f'[id="{field["id"]}"]' if field["id"] else f'input[name="{field["name"]}"]').first
        formatted = format_kz_value(value, field["placeholder"])
        await self.type_into(page, await el.element_handle(), formatted)
        await self.sleep(1)
        # Kontrolle: ELSTER formatiert u. U. mit Tausenderpunkten – Ziffern und Komma vergleichen.
        shown = re.sub(r"[^\d,-]", "", await el.input_value())
        if shown != formatted:
            raise PortalError(f"Kz{kz}: eingetragen {formatted!r}, Formular zeigt {shown!r} – Abbruch.")

    async def _run_pruefung(self, page: Page, s: Session) -> None:
        if not await self.click_pruefen(page):
            raise PortalError('"Prüfen" nicht gefunden.')
        await self.sleep(5)
        res = await page.evaluate(JS_PRUEF_RESULT)
        if res["hasErrors"] and not res["noErrors"]:
            s.screenshot_path = await self.screenshot(page, f"ustva_pruefung_fehler_{s.id}")
            raise PortalError("ELSTER-Prüfung meldet Fehler: " + ("; ".join(res["errTexts"]) or "siehe Screenshot"))
        if not await page.evaluate(JS_CLICK_EXACT_BUTTON, "Weiter"):
            log.warning('"Weiter" nach der Prüfung nicht gefunden.')
        await self.sleep(3)

    async def _submit(self, page: Page) -> dict[str, str]:
        if not await page.evaluate(JS_CLICK_EXACT_BUTTON, "Absenden"):
            raise PortalError('"Absenden" nicht gefunden – nichts übermittelt.')
        await self.wait_nav(page, 60000)
        await self.sleep(3)
        ticket = await page.evaluate(JS_EXTRACT_TICKET)
        return {k: re.sub(r"[^\w\-/ ]", "", v) for k, v in ticket.items()}
