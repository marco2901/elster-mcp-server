"""Nur lesend: „Übermittelte Formulare" und Posteingang, optional als PDF."""

from __future__ import annotations

import html
import logging
import re
from datetime import date

from playwright.async_api import Page

from ..constants import PORTAL_URLS
from ..security import restrict_file, safe_child, safe_filename
from ..sessions import sessions
from .base import ElsterPortal

log = logging.getLogger("elster_mcp.sync")

JS_EXTRACT_HISTORY = r"""
() => {
  const TYPE_MAP = [['Umsatzsteuer-Voranmeldung','USTVA'],['UStVA','USTVA'],['Einkommensteuer','EST'],['ESt','EST'],
    ['Körperschaftsteuer','KST'],['Einnahmenüberschussrechnung','EÜR'],['EÜR','EÜR'],['Gewerbesteuer','GEW'],
    ['Lohnsteuer','LST'],['Bescheid','BESCHEID']];
  const detectType = t => { for (const [k, v] of TYPE_MAP) if (t.includes(k)) return v; return 'OTHER'; };
  const detectMonth = t => {
    if (/\bIV\.\s*Kalendervierteljahr/.test(t)) return 12;
    if (/\bIII\.\s*Kalendervierteljahr/.test(t)) return 9;
    if (/\bII\.\s*Kalendervierteljahr/.test(t)) return 6;
    if (/\bI\.\s*Kalendervierteljahr/.test(t)) return 3;
    const q = t.match(/([1-4])\.\s*Quartal|Q([1-4])/i); if (q) return parseInt(q[1] || q[2]) * 3;
    const months = {Januar:1,Februar:2,'März':3,April:4,Mai:5,Juni:6,Juli:7,August:8,September:9,Oktober:10,November:11,Dezember:12};
    for (const [n, m] of Object.entries(months)) if (t.includes(n)) return m;
    return 0;
  };
  const out = [];
  for (const row of Array.from(document.querySelectorAll('table tr, .list-row, .table-row, [role="row"], .list-group-item'))) {
    if (row.closest('thead') || row.offsetParent === null) continue;
    const tds = Array.from(row.querySelectorAll('td, .cell, [role="gridcell"]')).map(c => (c.textContent || '').trim().replace(/\s+/g, ' '));
    const full = tds.join(' ');
    if (full.length < 5) continue;
    const btn = row.querySelector('[id^="showActions_"],[id^="showUebermitteltesFormular_"], button, a');
    if (!btn) continue;
    const d = full.match(/(\d{2})\.(\d{2})\.(\d{4})/);
    const iso = d ? `${d[3]}-${d[2]}-${d[1]}` : null;
    const y = full.match(/\b(20\d{2})\b/);
    out.push({ date: iso, type: detectType(full), year: y ? parseInt(y[1]) : (d ? parseInt(d[3]) : null),
      month: detectMonth(full), description: tds.slice(0, 6).join(' | ').slice(0, 250),
      elsterId: (btn.id || '').replace(/\D/g, '') });
  }
  return out;
}
"""

JS_EXTRACT_INBOX = r"""
() => Array.from(document.querySelectorAll('table tr')).map(row => {
  if (row.closest('thead')) return null;
  const v = row.querySelector('[id^="viewNachricht"],[onclick*="viewNachricht"]');
  if (!v) return null;
  let id = v.id;
  if (!id) { const m = (v.getAttribute('onclick') || '').match(/viewNachricht\('(\d+)'\)/); if (m) id = 'viewNachricht' + m[1]; }
  if (!id) return null;
  const subject = ((v.querySelector('.interactive-icon__text') || v).textContent || '').trim() || 'Ohne Betreff';
  const td = Array.from(row.querySelectorAll('td')).find(t => /\d{2}\.\d{2}\.\d{4}/.test(t.textContent || ''));
  const m = td ? td.textContent.match(/(\d{2})\.(\d{2})\.(\d{4})/) : null;
  return { id, elsterId: id.replace(/\D/g, ''), subject: subject.slice(0, 200), date: m ? `${m[3]}-${m[2]}-${m[1]}` : null };
}).filter(Boolean)
"""

JS_INBOX_MODAL_HTML = """
() => {
  const b = document.querySelector('.modal--openOnLoad');
  if (!b || b.getBoundingClientRect().height === 0) return '';
  const w = b.querySelector('.modal__wrapper') || b;
  return (w.textContent || '').trim().length > 100 ? w.innerHTML : '';
}
"""


class SyncFlow(ElsterPortal):
    async def history(self, years: list[int] | None = None) -> list[dict]:
        years = years or [date.today().year, date.today().year - 1]
        async with sessions.browser_slots, self.open() as page:
            await self.login(page)
            await self.handle_modals(page)
            await page.goto(PORTAL_URLS["meine_formulare"], wait_until="networkidle", timeout=30000)
            await self.sleep(3)
            await self.handle_modals(page)
            tab = page.get_by_text("Übermittelte Formulare", exact=False).first
            try:
                await tab.click(timeout=5000)
                await self.sleep(3)
            except Exception:
                await page.goto(f"{PORTAL_URLS['meine_formulare']}#meineFormulare-uebermittelt")
                await self.sleep(4)
            try:
                await page.wait_for_selector("table, .list-row, .table-row, [id*='Table']", timeout=15000)
            except Exception:
                log.warning("Keine Tabelle gefunden.")

            items: list[dict] = list(await page.evaluate(JS_EXTRACT_HISTORY))
            for y in years:
                switched = await page.evaluate(
                    """(y) => { for (const s of Array.from(document.querySelectorAll('select'))) {
                         const o = Array.from(s.options).find(o => o.text.includes(String(y)));
                         if (o) { s.value = o.value; s.dispatchEvent(new Event('change', {bubbles: true})); return true; } }
                       return false; }""",
                    y,
                )
                if switched:
                    await self.sleep(4)
                    items += await page.evaluate(JS_EXTRACT_HISTORY)

        unique: dict[str, dict] = {}
        for it in items:
            unique[it.get("elsterId") or f"{it['type']}_{it['date']}_{it['description'][:40]}"] = it
        return list(unique.values())

    async def inbox(self, download_pdfs: bool = False, max_pages: int = 20) -> list[dict]:
        collected: list[dict] = []
        async with sessions.browser_slots, self.open() as page:
            await self.login(page)
            await self.handle_modals(page)
            await page.goto(PORTAL_URLS["posteingang"], wait_until="networkidle", timeout=30000)
            await self.sleep(2)
            await self.handle_modals(page)

            for _ in range(max(1, min(max_pages, 50))):
                await self.sleep(1.5)
                messages = await page.evaluate(JS_EXTRACT_INBOX)
                if not messages:
                    break
                for msg in messages:
                    if download_pdfs:
                        try:
                            msg["pdfPath"] = await self._inbox_pdf(page, msg)
                        except Exception as exc:
                            log.warning("PDF für Nachricht %s fehlgeschlagen: %s", msg["elsterId"], exc)
                    collected.append(msg)

                first_before = messages[0]["id"]
                nxt = await page.query_selector("#MeinPosteingangTable_pagination_next_page")
                if not nxt or not await nxt.is_enabled() or not await nxt.is_visible():
                    break
                await nxt.click()
                advanced = False
                for _ in range(5):
                    await self.sleep(1)
                    first = await page.evaluate(
                        "() => (document.querySelector('[id^=\"viewNachricht\"]') || {}).id || ''"
                    )
                    if first and first != first_before:
                        advanced = True
                        break
                if not advanced:
                    break
        return collected

    async def _inbox_pdf(self, page: Page, msg: dict) -> str | None:
        target = safe_child(
            self.cfg.runtime.download_dir,
            f"elster_inbox_{msg['elsterId']}_{safe_filename(msg['subject'])}.pdf",
        )
        if not re.fullmatch(r"viewNachricht\d+", msg["id"]):
            return None
        el = await page.query_selector(f"#{msg['id']}")
        if not el:
            return None
        await el.scroll_into_view_if_needed()
        await el.click()
        body_html = ""
        for _ in range(15):
            await self.sleep(1)
            body_html = await page.evaluate(JS_INBOX_MODAL_HTML)
            if body_html:
                break
        if not body_html:
            return None

        title = html.escape(msg["subject"])
        doc = (
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            "<style>body{font-family:Arial,sans-serif;padding:30px;font-size:14px;line-height:1.6}"
            "h1{font-size:20px;border-bottom:2px solid #333;padding-bottom:10px}"
            "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:6px}"
            "button,.btn{display:none!important}</style></head><body>"
            f"<h1>{title}</h1><p style='color:#666;font-size:12px'>Datum: {html.escape(msg.get('date') or '')}</p>"
            f"{body_html}</body></html>"
        )
        # Isolierter Render-Kontext: kein JavaScript, keine Netzwerkzugriffe.
        assert self.browser is not None
        ctx = await self.browser.new_context(java_script_enabled=False)
        try:
            await ctx.route("**/*", lambda route: route.abort())
            render = await ctx.new_page()
            await render.set_content(doc, wait_until="domcontentloaded")
            await render.pdf(path=str(target), format="A4",
                             margin={"top": "20mm", "bottom": "20mm", "left": "20mm", "right": "20mm"})
        finally:
            await ctx.close()
        await page.keyboard.press("Escape")
        await self.sleep(0.8)
        if target.exists() and target.stat().st_size > 500:
            restrict_file(target)
            return str(target)
        return None
