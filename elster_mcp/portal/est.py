"""ESt 1 A: öffnet das Formular, füllt Stammdaten + Feld-Hinweise, Prüfung. Übermittelt NIE."""

from __future__ import annotations

import asyncio
import logging

from playwright.async_api import Page

from ..constants import PORTAL_URLS
from ..models import EstData
from ..security import AuditLog
from ..sessions import Session, sessions
from .base import ElsterPortal

log = logging.getLogger("elster_mcp.est")

JS_FILL_BY_ID = """
([hint, val]) => {
  for (const inp of Array.from(document.querySelectorAll('input, textarea'))) {
    if (inp.disabled || inp.readOnly || inp.type === 'hidden') continue;
    const key = inp.id || inp.name;
    if (!key || !key.includes(hint)) continue;
    inp.focus();
    inp.value = val;
    inp.dispatchEvent(new Event('input', { bubbles: true }));
    inp.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  }
  return false;
}
"""

JS_SELECT_YEAR_FALLBACK = """
(y) => {
  for (const sel of Array.from(document.querySelectorAll('select'))) {
    const opt = Array.from(sel.options).find(o => o.value.includes(String(y)));
    if (opt) { sel.value = opt.value; sel.dispatchEvent(new Event('change', { bubbles: true })); return true; }
  }
  return false;
}
"""

REVIEW_MINUTES = 30


class EstFlow(ElsterPortal):
    def __init__(self, cfg, audit: AuditLog) -> None:  # noqa: ANN001
        super().__init__(cfg)
        self.audit = audit

    def start(self, est: EstData) -> Session:
        s = sessions.create("EST")
        self.audit.write("est_start", session=s.id, year=est.year, fields=sorted(est.data))
        s.task = asyncio.create_task(self._run(s, est))
        return s

    async def _run(self, s: Session, est: EstData) -> None:
        try:
            async with sessions.browser_slots, self.open() as page:
                s.status = "LOGGING_IN"
                s.log(f"ESt {est.year}: Login …")
                await self.login(page)
                await self.sleep(3)
                await self.handle_modals(page)

                s.status = "OPENING_FORM"
                await page.goto(PORTAL_URLS["est_form"], wait_until="networkidle", timeout=60000)
                await self.sleep(2)
                if not await self.select_year(page, est.year) and not await page.evaluate(
                    JS_SELECT_YEAR_FALLBACK, est.year
                ):
                    log.warning("ESt: Jahresauswahl nicht gefunden.")
                await self.click_enter(page)
                await self.sleep(3)
                await self.handle_modals(page)

                s.status = "FILLING_PAGES"
                await self._walk(page, est.data, s)

                s.status = "PRUEFUNG"
                s.log('ELSTER-"Prüfung" …')
                await self.click_pruefen(page)
                await self.sleep(8)
                s.screenshot_path = await self.screenshot(page, f"est_pruefung_{s.id}")

                s.status = "AWAITING_REVIEW"
                s.log(f"Prüfung erledigt. Browser bleibt {REVIEW_MINUTES} min offen. NICHT übermittelt.")
                try:
                    await asyncio.wait_for(s.done_event.wait(), timeout=REVIEW_MINUTES * 60)
                except asyncio.TimeoutError:
                    pass
                s.status = "DONE"
        except asyncio.CancelledError:
            s.status = "CANCELLED"
        except Exception as exc:
            log.error("ESt-Sitzung %s: %s", s.id, exc)
            s.fail(str(exc))
            self.audit.write("est_error", session=s.id, error=str(exc))
        finally:
            s.done_event.set()
            sessions.schedule_cleanup(s.id)

    async def _walk(self, page: Page, data: dict[str, float | str], s: Session) -> None:
        tp = self.cfg.taxpayer
        basics = [
            (tp.name, ["Name", "Nachname"]),
            (tp.first_name, ["Vorname"]),
            (tp.street, ["Straße", "Strasse"]),
            (tp.house_number, ["Hausnummer"]),
            (tp.zip, ["Postleitzahl", "PLZ"]),
            (tp.city, ["Ort", "Wohnort"]),
        ]
        last_url, same = "", 0
        for n in range(1, 51):
            await self.sleep(2)
            url = page.url
            if url == last_url:
                same += 1
                if same >= 3:
                    s.log(f'Hänge auf "{self.page_name(url)}" – Abbruch.')
                    break
            else:
                same = 0
            last_url = url
            s.log(f"[Seite {n}] {self.page_name(url)}")

            for value, labels in basics:
                if not value:
                    continue
                for label in labels:
                    if await self.fill_by_label(page, label, value):
                        break
            for hint, value in data.items():
                if value in (None, ""):
                    continue
                if await page.evaluate(JS_FILL_BY_ID, [hint, str(value)]):
                    s.log(f"  {hint} gesetzt (ID-Treffer)")

            await self.handle_modals(page)
            if not await self.has_next(page, allow_weiter=True):
                s.log('Kein "Nächste Seite" mehr – fertig.')
                break
            await self.click_next(page, allow_weiter=True)
            await self.sleep(0.5)
            await self.handle_modals(page)
