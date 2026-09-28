"""Gemeinsame ELSTER-Formularlogik: Prüfen, Entwurf in „Meine Formulare", Entwurf öffnen, Versenden."""

from __future__ import annotations

import re

from playwright.async_api import Page

from ..constants import PORTAL_URLS
from .base import PortalError

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


def pick_draft(rows: list[dict], name: str) -> str | None:
    """Neuester Entwurf mit exakt dieser Bezeichnung (ELSTER vergibt aufsteigende IDs)."""
    ids = [int(r["id"]) for r in rows if r.get("name") == name and str(r.get("id", "")).isdigit()]
    return str(max(ids)) if ids else None


class DraftMixin:
    """Für Flows auf ``ElsterPortal``-Basis (braucht ``sleep``, ``handle_modals``, ``wait_nav``)."""

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

    async def _submit(self, page: Page) -> dict[str, str]:
        if not await page.evaluate(JS_CLICK_EXACT_BUTTON, "Absenden"):
            raise PortalError('"Absenden" nicht gefunden – nichts übermittelt.')
        await self.wait_nav(page, 60000)
        await self.sleep(3)
        ticket = await page.evaluate(JS_EXTRACT_TICKET)
        return {k: re.sub(r"[^\w\-/ ]", "", v) for k, v in ticket.items()}
