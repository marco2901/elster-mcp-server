"""Nur lesend: „Übermittelte Formulare" und Posteingang, optional als PDF."""

from __future__ import annotations

import html
import io
import logging
import re
import unicodedata
import zipfile
from datetime import date
from pathlib import Path, PurePosixPath

from playwright.async_api import Page

from ..constants import PORTAL_URLS
from ..security import restrict_file, safe_child
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
  // Der Lesestatus steckt als unsichtbarer Text („gelesen"/„ungelesen") im Betreff-Button.
  const textEl = (v.querySelector('.interactive-icon__text') || v).cloneNode(true);
  const status = Array.from(textEl.querySelectorAll('._helper-invisible')).map(e => (e.textContent || '').trim()).join(' ');
  textEl.querySelectorAll('._helper-invisible').forEach(e => e.remove());
  const subject = (textEl.textContent || '').replace(/\s+/g, ' ').trim() || 'Ohne Betreff';
  const iconTitle = ((v.querySelector('[title]') || {}).title || '').trim();
  const s = (status || iconTitle).toLowerCase();
  const read = s.includes('ungelesen') ? false : (s.includes('gelesen') ? true : null);
  const hasAttachment = !!row.querySelector('[class*="svg-paperclip"]');
  const td = Array.from(row.querySelectorAll('td')).find(t => /\d{2}\.\d{2}\.\d{4}/.test(t.textContent || ''));
  const m = td ? td.textContent.match(/(\d{2})\.(\d{2})\.(\d{4})/) : null;
  return { id, elsterId: id.replace(/\D/g, ''), subject: subject.slice(0, 200), read, hasAttachment,
           date: m ? `${m[3]}-${m[2]}-${m[1]}` : null };
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


# --------------------------------------------------------------------------- #
# Dateinamen und Zip-Auswertung (ohne Browser testbar)
# --------------------------------------------------------------------------- #

ZIP_MAX_ENTRIES = 50
ZIP_MAX_TOTAL_BYTES = 100 * 1024 * 1024
ATTACHMENT_SUFFIXES = {".pdf", ".xml", ".txt", ".csv", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}
_STOPWORDS = {"in", "im", "ihr", "ihre", "ihrem", "ihren", "ihrer", "zu", "zum", "zur", "fuer", "der", "die",
              "das", "den", "dem", "des", "und", "von", "vom", "mit", "an", "am", "auf", "ein", "eine", "einer"}


def ascii_slug(text: str, sep: str = "-") -> str:
    """Umlaute transliterieren, alles außer [A-Za-z0-9] zu einem Trenner zusammenfassen."""
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss")):
        text = text.replace(a, b)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", sep, text).strip(sep)


def short_subject(subject: str, max_words: int = 2) -> str:
    """„Energieberatung für Wohngebäude (EBW): Neue Dokumente …" → „EBW-Neue-Dokumente"."""
    abbr = re.search(r"\(([^()]{1,15})\)", subject)
    rest = subject.split(":", 1)[1] if ":" in subject else re.sub(r"\([^()]*\)", " ", subject)
    words = [w for w in ascii_slug(rest, " ").split() if w.lower() not in _STOPWORDS][:max_words]
    parts = ([ascii_slug(abbr.group(1))] if abbr else []) + words
    return "-".join(p for p in parts if p)[:60] or "Nachricht"


def inbox_basename(msg: dict) -> str:
    return f"{msg.get('date') or 'undatiert'}_ELSTER_{msg['elsterId']}_{short_subject(msg['subject'])}"


def extract_inbox_zip(data: bytes, target_dir: Path, msg: dict) -> tuple[str | None, list[dict]]:
    """Speichert das Nachrichten-PDF und alle Anhänge aus dem ELSTER-Zip.

    Zip-Pfade werden nie übernommen (nur der bereinigte Dateiname), Größe und Anzahl sind begrenzt.
    """
    base = inbox_basename(msg)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        entries = [i for i in z.infolist() if not i.is_dir()]
        if len(entries) > ZIP_MAX_ENTRIES or sum(i.file_size for i in entries) > ZIP_MAX_TOTAL_BYTES:
            raise ValueError("ELSTER-Zip überschreitet die zulässige Größe.")
        # Die Nachricht selbst liegt als .html + .pdf mit gleichem Namen bei; alles andere sind Anhänge.
        msg_stems = {PurePosixPath(i.filename).stem for i in entries if i.filename.lower().endswith(".html")}
        message_pdf: str | None = None
        attachments: list[dict] = []
        for info in entries:
            name = PurePosixPath(info.filename)
            suffix = name.suffix.lower()
            if name.stem in msg_stems:
                if suffix != ".pdf":
                    continue
                target = safe_child(target_dir, f"{base}.pdf")
            elif suffix in ATTACHMENT_SUFFIXES:
                target = safe_child(target_dir, f"{base}_{ascii_slug(name.stem, '_')[:80] or 'Anhang'}{suffix}")
            else:
                log.info("Anhang mit Endung %s übersprungen.", suffix or "(keine)")
                continue
            target.write_bytes(z.read(info))
            restrict_file(target)
            if name.stem in msg_stems:
                message_pdf = str(target)
            else:
                attachments.append({"name": name.name, "path": str(target), "size": info.file_size})
    return message_pdf, attachments


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
                            msg["pdfPath"], msg["attachments"] = await self._inbox_download(page, msg)
                        except Exception as exc:
                            log.warning("Download für Nachricht %s fehlgeschlagen: %s", msg["elsterId"], exc)
                            try:
                                await page.keyboard.press("Escape")
                                await self.sleep(0.8)
                                msg["pdfPath"] = await self._inbox_pdf(page, msg)
                                msg["attachments"] = []
                            except Exception as exc2:
                                log.warning("PDF-Fallback für Nachricht %s fehlgeschlagen: %s", msg["elsterId"], exc2)
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

    async def _inbox_download(self, page: Page, msg: dict) -> tuple[str | None, list[dict]]:
        """Lädt über „Mit Anhängen (Zip)" das offizielle Nachrichten-PDF samt aller Anhänge."""
        if not re.fullmatch(r"viewNachricht\d+", msg["id"]):
            raise ValueError("Unerwartete Nachrichten-ID.")
        el = await page.query_selector(f"#{msg['id']}")
        if not el:
            raise ValueError("Nachricht nicht in der Liste gefunden.")
        await el.scroll_into_view_if_needed()
        await el.click()
        btn = page.locator("#downloadNachrichtMitAnhangButton")
        await btn.wait_for(state="attached", timeout=20000)
        if not await btn.is_visible():
            await page.locator("#nachrichtHerunterladenForm button").first.click()
            await self.sleep(0.5)
        async with page.expect_download(timeout=60000) as dl_info:
            await btn.click()
        dl = await dl_info.value
        try:
            data = Path(await dl.path()).read_bytes()
        finally:
            await dl.delete()
        await page.keyboard.press("Escape")
        await self.sleep(0.8)
        return extract_inbox_zip(data, self.cfg.runtime.download_dir.expanduser().resolve(), msg)

    async def _inbox_pdf(self, page: Page, msg: dict) -> str | None:
        """Fallback: Nachrichtentext aus dem Dialog selbst als PDF rendern (ohne Anhänge)."""
        target = safe_child(self.cfg.runtime.download_dir, f"{inbox_basename(msg)}.pdf")
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
