"""UStVA: Login → Formular → Kennziffern → Prüfung → PAUSE → (Freigabe) → Absenden."""

from __future__ import annotations

import asyncio
import logging
import re

from playwright.async_api import Page

from ..config import ElsterConfig
from ..constants import INPUT_TAX_KZ, KENNZIFFERN, PORTAL_URLS, USTVA_PAGE_KZ_MAP
from ..models import UstvaReport
from ..security import AuditLog, confirmation_code, new_nonce
from ..sessions import Session, sessions
from .base import ElsterPortal, PortalError

log = logging.getLogger("elster_mcp.ustva")

JS_FIND_KZ_BY_TEXT = """
(kz) => {
  for (const el of Array.from(document.querySelectorAll('*'))) {
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)) continue;
    const direct = Array.from(el.childNodes).filter(n => n.nodeType === 3)
      .map(n => (n.textContent || '').trim()).join('').trim();
    if (direct !== kz) continue;
    const r = document.evaluate("preceding::input[not(@type='hidden')][1]", el, null,
      XPathResult.FIRST_ORDERED_NODE_TYPE, null);
    const inp = r.singleNodeValue;
    if (inp && inp.offsetParent !== null) return inp;
    const parent = el.closest('div, tr, li, section');
    if (parent) {
      const inp2 = parent.querySelector('input:not([type="hidden"])');
      if (inp2 && inp2.offsetParent !== null) return inp2;
    }
  }
  return null;
}
"""

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

                s.confirmation_code = confirmation_code(s.id, s.summary or {}, nonce)
                s.status = "AWAITING_CONFIRM"
                s.log("Prüfung bestanden – warte auf ausdrückliche Freigabe.")
                self.audit.write("ustva_awaiting_confirm", session=s.id)

                timeout = self.cfg.security.confirm_timeout_minutes * 60
                try:
                    await asyncio.wait_for(s.confirm_event.wait(), timeout=timeout)
                except asyncio.TimeoutError as exc:
                    raise PortalError(f"Keine Freigabe innerhalb von {timeout // 60} min – nichts übermittelt.") from exc

                s.status = "SUBMITTING"
                s.log("Sende an ELSTER …")
                self.audit.write("ustva_submitting", session=s.id)
                ticket = await self._submit(page)
                s.screenshot_path = await self.screenshot(page, f"ustva_submitted_{s.id}")
                s.result = {"success": True, **ticket}
                s.status = "DONE"
                s.log(f"Übermittelt. Transferticket: {ticket.get('ticket') or '?'}")
                self.audit.write("ustva_submitted", session=s.id, **ticket)
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
            for kz in USTVA_PAGE_KZ_MAP.get(name, []):
                if kz in wanted and kz not in filled and await self._fill_kz(page, kz, report[kz]):
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

    async def _fill_kz(self, page: Page, kz: str, value: float) -> bool:
        if kz in INPUT_TAX_KZ and value < 0:
            raise PortalError(f"Vorsteuer Kz{kz} negativ ({value:.2f}) – Abbruch.")
        padded = kz.zfill(3)
        el = None
        for sel in (
            f'input[id*="Kz{kz}"]:not([type="hidden"]):not([id*="EOL"])',
            f'input[id*="Kz{padded}"]:not([type="hidden"]):not([id*="EOL"])',
            f'input[name*="Kz{kz}"]:not([type="hidden"])',
        ):
            cand = await page.query_selector(sel)
            if cand and await cand.is_visible():
                el = cand
                break
        if el is None:
            handle = await page.evaluate_handle(JS_FIND_KZ_BY_TEXT, kz)
            el = handle.as_element()
        if el is None:
            log.warning("Kz%s: Eingabefeld nicht gefunden.", kz)
            return False

        placeholder = (await el.get_attribute("placeholder") or "").lower()
        formatted = f"{value:.2f}".replace(".", ",") if "cent" in placeholder else str(round(value))
        await self.type_into(page, el, formatted)
        await self.sleep(1)
        return True

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
