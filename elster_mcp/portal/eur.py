"""Anlage EÜR: füllt bis zur Prüfung und speichert als Entwurf. Übermittelt NIE."""

from __future__ import annotations

import asyncio
import logging

from playwright.async_api import Page

from ..constants import EUR_FIELD_MAP, PORTAL_URLS
from ..models import EurData
from ..security import AuditLog
from ..sessions import Session, sessions
from .base import ElsterPortal

log = logging.getLogger("elster_mcp.eur")

_FILL_PAGE_HINTS = (
    "einnahm", "ausgab", "betriebs", "abschreib", "afa", "iab", "raumkost",
    "arbeitszimmer", "homeoffice", "gewinn", "ergebnis", "wareneink",
)

JS_FOCUS_BY_ID = """
(pattern) => {
  for (const inp of Array.from(document.querySelectorAll('input, textarea'))) {
    const id = inp.id || inp.name || '';
    if (id.includes(pattern) && !inp.disabled && !inp.readOnly && inp.type !== 'hidden') return inp;
  }
  return null;
}
"""

JS_SAVE_TRIGGER = """
() => {
  const lower = s => (s || '').toLowerCase().replace(/\\s+/g, ' ').trim();
  const els = Array.from(document.querySelectorAll('button, a, input[type="button"], input[type="submit"]'));
  const el = els.find(e => e.offsetParent !== null && lower(e.textContent || e.value).includes('speichern und formular verlassen'));
  if (el) { el.click(); return true; }
  return false;
}
"""

JS_SAVE_CONFIRM = """
() => {
  const lower = s => (s || '').toLowerCase().replace(/\\s+/g, ' ').trim();
  const vis = Array.from(document.querySelectorAll('button, a, input[type="button"], input[type="submit"]'))
    .filter(e => e.offsetParent !== null);
  const btn = vis.find(e => lower(e.textContent || e.value) === 'speichern und verlassen')
    || vis.find(e => { const t = lower(e.textContent || e.value); return t.includes('speichern und verlassen') && !t.includes('ohne'); });
  if (btn) { btn.click(); return true; }
  return false;
}
"""

JS_SAVED = """
() => {
  const b = (document.body ? document.body.innerText : '').toLowerCase();
  return b.includes('meine formulare') || b.includes('mein elster') || b.includes('entwurf gespeichert') || b.includes('erfolgreich gespeichert');
}
"""


class EurFlow(ElsterPortal):
    def __init__(self, cfg, audit: AuditLog) -> None:  # noqa: ANN001
        super().__init__(cfg)
        self.audit = audit

    def start(self, eur: EurData) -> Session:
        s = sessions.create("EUR")
        self.audit.write("eur_start", session=s.id, year=eur.year, fields=sorted(eur.data))
        s.task = asyncio.create_task(self._run(s, eur))
        return s

    async def _run(self, s: Session, eur: EurData) -> None:
        try:
            async with sessions.browser_slots, self.open() as page:
                s.status = "LOGGING_IN"
                s.log(f"EÜR {eur.year}: Login …")
                await self.login(page)
                await self.sleep(3)
                await self.handle_modals(page)

                s.status = "OPENING_FORM"
                await self._open_form(page, eur.year)

                s.status = "FILLING_PAGES"
                await self._walk(page, eur.data, s)

                s.status = "PRUEFUNG"
                s.log('ELSTER-"Prüfung" …')
                await self.click_pruefen(page)
                await self.wait_nav(page, 30000)
                await self.sleep(8)
                s.screenshot_path = await self.screenshot(page, f"eur_pruefung_{s.id}")
                body = (await page.inner_text("body")).lower()
                if "keine fehler" in body:
                    s.log("Prüfung ohne Fehler.")
                elif "fehler" in body:
                    s.errors.append("Prüfung meldet Hinweise/Fehler – siehe Screenshot.")

                s.status = "SAVING"
                if await self._save_and_exit(page, s):
                    s.status = "SAVED"
                    s.log("Entwurf in ELSTER gespeichert. Bitte im Portal prüfen und selbst absenden.")
                else:
                    s.status = "AWAITING_REVIEW"
                    s.log("Speichern fehlgeschlagen – Browser bleibt 10 min offen (nur mit ELSTER_HEADLESS=false sichtbar).")
                    try:
                        await asyncio.wait_for(s.done_event.wait(), timeout=600)
                    except asyncio.TimeoutError:
                        pass
                    s.status = "DONE"
                self.audit.write("eur_finished", session=s.id, status=s.status)
        except asyncio.CancelledError:
            s.status = "CANCELLED"
        except Exception as exc:
            log.error("EÜR-Sitzung %s: %s", s.id, exc)
            s.fail(str(exc))
            self.audit.write("eur_error", session=s.id, error=str(exc))
        finally:
            s.done_event.set()
            sessions.schedule_cleanup(s.id)

    async def _open_form(self, page: Page, year: int) -> None:
        await page.goto(PORTAL_URLS["eur_form"], wait_until="networkidle", timeout=60000)
        await self.sleep(2)
        await self.handle_modals(page)
        if not await self.select_year(page, year):
            log.warning("EÜR: Jahresauswahl nicht gefunden.")
        await self.click_enter(page)
        await self.sleep(3)
        await self.handle_modals(page)
        await self.wait_nav(page, 10000)
        await self.skip_datenuebernahme(page)

    async def _walk(self, page: Page, data: dict[str, float], s: Session) -> None:
        done_pages: set[str] = set()
        last_url, same = "", 0
        for n in range(1, 41):
            await self.sleep(2)
            url = page.url
            name = self.page_name(url)
            if url == last_url:
                same += 1
                if same >= 3:
                    s.log(f'Hänge auf "{name}" – Abbruch.')
                    break
            else:
                same = 0
            last_url = url

            if name and name not in done_pages:
                done_pages.add(name)
                s.log(f"[Seite {n}] {name}")
                low = name.lower()
                if any(h in low for h in ("startseite", "steuernummer", "angaben")):
                    await self.fill_steuernummer(page)
                elif any(h in low for h in _FILL_PAGE_HINTS):
                    await self._fill_fields(page, data, s)

            await self.handle_modals(page)
            if not await self.has_next(page, allow_weiter=True):
                s.log('Kein "Nächste Seite" mehr – fertig.')
                break
            await self.click_next(page, allow_weiter=True)
            await self.sleep(0.5)
            await self.handle_modals(page)

    async def _fill_fields(self, page: Page, data: dict[str, float], s: Session) -> None:
        for spec in EUR_FIELD_MAP:
            value = data.get(spec["field"])
            if not value:
                continue
            text = str(round(value))
            done = False
            for kz in spec["kz_patterns"]:
                el = (await page.evaluate_handle(JS_FOCUS_BY_ID, kz)).as_element()
                if el:
                    await self.type_into(page, el, text)
                    s.log(f"  ✓ {spec['field']} = {text} ({kz})")
                    done = True
                    break
            if not done:
                for label in spec["labels"]:
                    if await self.fill_by_label(page, label, text, exact=False):
                        s.log(f"  ✓ {spec['field']} = {text} (Label: {label})")
                        break

    async def _save_and_exit(self, page: Page, s: Session) -> bool:
        if not await page.evaluate(JS_SAVE_TRIGGER):
            s.log('"Speichern und Formular verlassen" nicht gefunden.')
            return False
        await self.sleep(3)
        if not await page.evaluate(JS_SAVE_CONFIRM):
            s.log('Modal-Button "Speichern und Verlassen" nicht gefunden.')
            return False
        await self.wait_nav(page, 20000)
        await self.sleep(4)
        return bool(await page.evaluate(JS_SAVED))
