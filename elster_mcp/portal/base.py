"""Gemeinsame Browser-Logik: Start, Login mit Zertifikat, Modals, Screenshots."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, Route, async_playwright

from ..config import ElsterConfig
from ..constants import PORTAL_URLS
from ..secrets import SecretError, validate_certificate
from ..security import host_allowed, restrict_file, safe_child

log = logging.getLogger("elster_mcp.portal")

LOGGED_IN_MARKERS = ("mein-elster/startseite", "eportal/mein-elster", "meinelster")

# --------------------------------------------------------------------------- #
# JavaScript-Helfer (1:1 aus der TypeScript-Version übernommen)
# --------------------------------------------------------------------------- #

JS_HANDLE_MODALS = """
() => {
  const btns = Array.from(document.querySelectorAll('button, a, .btn'));
  const body = document.body ? document.body.innerText : '';
  if (body.includes('Eingabefehler gefunden') || body.includes('In einem Feld ist ein Eingabefehler')) {
    const btn = btns.find(b => (b.textContent || '').trim().includes('Zum Fehler'));
    if (btn) { btn.click(); return 'input-error modal closed'; }
  }
  if (body.includes('Wiederaufnahme') || body.includes('wiederaufnehmen') ||
      body.includes('gespeicherten Stand') || body.includes('vorherigen Eingaben') ||
      body.includes('automatische Wiederherstellung') || body.includes('letzten Stand der Bearbeitung')) {
    const nein = btns.find(b => (b.textContent || '').trim() === 'Nein');
    if (nein) { nein.click(); return 'resume rejected'; }
  }
  if (body.includes('Möchten Sie das Formular verlassen') || body.includes('Temporäre Aufgaben')) {
    const cancel = btns.find(b => {
      const t = (b.textContent || '').toLowerCase();
      return t.includes('nein') || t.includes('abbrechen') || t.includes('bleiben');
    });
    if (cancel) { cancel.click(); return 'leave-form modal cancelled'; }
  }
  return null;
}
"""

JS_HAS_NEXT = """
(allowWeiter) => Array.from(document.querySelectorAll(allowWeiter ? 'button, a' : 'button')).some(b => {
  const t = (b.textContent || '').trim();
  return (t.includes('Nächste Seite') || (allowWeiter && t === 'Weiter')) && b.offsetParent !== null;
})
"""

JS_CLICK_NEXT = """
(allowWeiter) => {
  const btn = Array.from(document.querySelectorAll(allowWeiter ? 'button, a' : 'button')).find(b => {
    const t = (b.textContent || '').trim();
    return (t.includes('Nächste Seite') || (allowWeiter && t === 'Weiter')) && b.offsetParent !== null && !b.disabled;
  });
  if (btn) { btn.click(); return true; }
  return false;
}
"""

JS_CLICK_PRUEFEN = """
() => {
  const el = Array.from(document.querySelectorAll('a, button, li')).find(e => {
    const t = (e.textContent || '').replace(/\\s+/g, ' ').trim();
    return t.includes('Prüfen') && !t.includes('Absenden') && e.offsetParent !== null;
  });
  if (el) { el.click(); return true; }
  return false;
}
"""

JS_SKIP_DATENUEBERNAHME = """
() => {
  const body = document.body ? document.body.innerText : '';
  if (!body.includes('Datenübernahme')) return false;
  const btn = Array.from(document.querySelectorAll('button, a, input[type="button"]')).find(b => {
    const t = (b.textContent || '').trim() || b.value || '';
    return t.toLowerCase().includes('ohne datenübernahme');
  });
  if (btn) { btn.click(); return true; }
  return false;
}
"""

JS_SET_STATE_CODE = """
([sel, code]) => {
  const el = document.querySelector(sel);
  if (!el) return false;
  const opt = Array.from(el.options).find(o => o.value === code);
  if (!opt) return false;
  el.value = opt.value;
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
}
"""

JS_FIND_BY_LABEL = """
([text, exact]) => {
  const all = Array.from(document.querySelectorAll('label, th, span, div, td'));
  for (const el of all) {
    const t = (el.textContent || '').trim();
    const hit = exact ? (t === text || t.startsWith(text + ' ') || t.startsWith(text + ':')) : t.includes(text);
    if (!hit) continue;
    if (el.htmlFor) {
      const inp = document.getElementById(el.htmlFor);
      if (inp && !inp.readOnly && !inp.disabled && inp.offsetParent !== null) return inp;
    }
    const parent = el.closest('tr, .form-group, .field, .row, div');
    if (parent) {
      const inp = parent.querySelector('input:not([type="hidden"]):not([readonly]):not([disabled])');
      if (inp && inp.offsetParent !== null) return inp;
    }
  }
  return null;
}
"""


class PortalError(RuntimeError):
    pass


class ElsterPortal:
    """Ein Browser-Kontext pro Sitzung. Immer über ``open()`` verwenden."""

    def __init__(self, cfg: ElsterConfig) -> None:
        self.cfg = cfg
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    # ------------------------------------------------------------------ #
    # Lebenszyklus
    # ------------------------------------------------------------------ #

    @asynccontextmanager
    async def open(self) -> AsyncIterator[Page]:
        rt = self.cfg.runtime
        download_dir = rt.download_dir.expanduser().resolve()
        download_dir.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as pw:
            self.browser = await pw.chromium.launch(
                headless=rt.headless,
                args=rt.browser_args,
                chromium_sandbox=rt.chromium_sandbox,
                executable_path=rt.executable_path,
                downloads_path=str(download_dir),
            )
            try:
                # Frischer, isolierter Kontext: keine Cookies/Passwörter anderer Sitzungen.
                self.context = await self.browser.new_context(
                    viewport={"width": 1280, "height": 1024},
                    locale="de-DE",
                    accept_downloads=True,
                    service_workers="block",
                )
                await self.context.route("**/*", self._guard_route)
                self.page = await self.context.new_page()
                yield self.page
            finally:
                try:
                    if self.context:
                        await self.context.close()
                finally:
                    await self.browser.close()
                    self.browser = self.context = self.page = None

    async def _guard_route(self, route: Route) -> None:
        """Browser darf ausschließlich mit ELSTER sprechen (kein Datenabfluss an Dritte)."""
        url = route.request.url
        if host_allowed(url, self.cfg.security.allowed_hosts):
            await route.continue_()
        else:
            log.debug("Blockierter Request: %s", url.split("?")[0])
            await route.abort()

    # ------------------------------------------------------------------ #
    # Hilfsfunktionen
    # ------------------------------------------------------------------ #

    @staticmethod
    async def sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def wait_nav(self, page: Page, timeout_ms: int = 15000) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass

    async def screenshot(self, page: Page, name: str) -> str | None:
        if not self.cfg.security.screenshots:
            return None
        target = safe_child(self.cfg.runtime.screenshot_dir, f"{name}.png")
        try:
            await page.screenshot(path=str(target), full_page=True)
            restrict_file(target)
            return str(target)
        except Exception as exc:
            log.debug("Screenshot fehlgeschlagen: %s", exc)
            return None

    async def handle_modals(self, page: Page) -> None:
        try:
            result = await page.evaluate(JS_HANDLE_MODALS)
            if result:
                log.info("Modal behandelt: %s", result)
        except Exception:
            pass

    async def has_next(self, page: Page, allow_weiter: bool = False) -> bool:
        try:
            return bool(await page.evaluate(JS_HAS_NEXT, allow_weiter))
        except Exception:
            return False

    async def click_next(self, page: Page, allow_weiter: bool = False) -> None:
        if await page.evaluate(JS_CLICK_NEXT, allow_weiter):
            await self.wait_nav(page)

    async def click_pruefen(self, page: Page) -> bool:
        try:
            return bool(await page.evaluate(JS_CLICK_PRUEFEN))
        except Exception:
            return False

    async def skip_datenuebernahme(self, page: Page) -> None:
        try:
            if await page.evaluate(JS_SKIP_DATENUEBERNAHME):
                log.info('"Ohne Datenübernahme fortfahren" geklickt.')
                await self.wait_nav(page, 30000)
                await self.sleep(2)
                await self.handle_modals(page)
        except Exception:
            pass

    async def type_into(self, page: Page, element, value: str) -> None:  # noqa: ANN001
        await element.click(click_count=3)
        await self.sleep(0.1)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        await page.keyboard.type(value, delay=self.cfg.runtime.type_delay_ms)
        await page.keyboard.press("Tab")
        await self.sleep(0.5)

    async def fill_by_label(self, page: Page, label: str, value: str, *, exact: bool = True) -> bool:
        handle = await page.evaluate_handle(JS_FIND_BY_LABEL, [label, exact])
        el = handle.as_element()
        if not el:
            return False
        await self.type_into(page, el, value)
        return True

    async def select_year(self, page: Page, year: int) -> bool:
        try:
            await page.wait_for_selector("#zeitraumJahr", timeout=15000)
            await page.select_option("#zeitraumJahr", f"{year}-v1")
            return True
        except Exception:
            return False

    async def click_enter(self, page: Page) -> None:
        btn = await page.query_selector("#Enter")
        if btn:
            await btn.click()
            await self.wait_nav(page, 30000)

    async def fill_steuernummer(self, page: Page) -> bool:
        tp = self.cfg.taxpayer
        if not tp.tax_number:
            log.warning("Keine Steuernummer konfiguriert (ELSTER_TAX_NUMBER).")
            return False
        land_sel = 'select[id*="Steuernummer-country"], select[id*="Steuernummer"][id*="country"]'
        if tp.state_code and await page.query_selector(land_sel):
            if await page.evaluate(JS_SET_STATE_CODE, [land_sel, tp.state_code]):
                await self.sleep(1.5)
            else:
                log.warning("Bundesland-Code %s nicht im Auswahlfeld gefunden.", tp.state_code)
        stnr = await page.query_selector(
            'input[id*="Steuernummer-tax-number"], input[id*="Steuernummer"][id*="tax-number"]'
        )
        if not stnr:
            log.warning("Steuernummer-Feld nicht gefunden.")
            return False
        await self.type_into(page, stnr, tp.tax_number)
        await self.sleep(1.5)
        log.info("Steuernummer eingetragen.")  # Wert selbst wird nie geloggt
        return True

    @staticmethod
    def page_name(url: str) -> str:
        return url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]

    # ------------------------------------------------------------------ #
    # Login mit Zertifikatsdatei + Passwort
    # ------------------------------------------------------------------ #

    def _is_logged_in(self, url: str) -> bool:
        return any(m in url for m in LOGGED_IN_MARKERS)

    async def login(self, page: Page) -> None:
        auth = self.cfg.auth
        pfx: Path = validate_certificate(auth.pfx_path, strict=self.cfg.security.strict_permissions)
        if not auth.password:
            raise SecretError(
                "Kein Zertifikats-Passwort gefunden. Setze ELSTER_PASSWORD_FILE, den OS-Keyring "
                "(python -m elster_mcp store-secret cert-password) oder ELSTER_PASSWORD."
            )

        log.info("Öffne ELSTER-Startseite …")
        await page.goto(PORTAL_URLS["start"], wait_until="networkidle", timeout=60000)
        if self._is_logged_in(page.url):
            return

        login_link = await page.query_selector('a[href*="login"], button.btn-login')
        if login_link:
            await login_link.click()
            await self.wait_nav(page, 30000)

        try:
            cert_link = await page.wait_for_selector('a[href*="login/zertifikat"], #login-zertifikat', timeout=10000)
            if cert_link:
                await cert_link.click()
                await self.wait_nav(page, 30000)
        except Exception:
            log.info("Zertifikats-Login-Seite bereits geladen oder Selektor geändert.")

        upload = await page.wait_for_selector(
            'input[type="file"], #loginZertifikat-dateiauswahl', timeout=20000, state="attached"
        )
        if not upload:
            raise PortalError("Upload-Feld für das Zertifikat nicht gefunden.")
        await upload.set_input_files(str(pfx))
        log.info("Zertifikat ausgewählt.")

        await self.sleep(1)
        pw_field = await page.wait_for_selector('input[id*="passwort"], input[type="password"]', timeout=10000)
        await pw_field.type(auth.password.get_secret_value(), delay=50)

        submit = await page.query_selector('#loginZertifikat-login, button[type="submit"], button.btn-primary')
        if not submit:
            submit = page.get_by_role("button", name="Login").first
        await submit.click()
        await self.wait_nav(page, 30000)

        # Passwortfeld sofort leeren, falls die Seite stehen bleibt.
        try:
            await pw_field.fill("")
        except Exception:
            pass

        if self._is_logged_in(page.url):
            log.info("Login erfolgreich.")
            return

        err = await page.evaluate(
            "() => { const e = document.querySelector('.alert-danger, .error-message, .feedback--error');"
            " return e ? e.textContent.trim() : null; }"
        )
        raise PortalError(f"ELSTER-Login fehlgeschlagen: {err or 'unbekannter Zustand'} ({page.url})")
