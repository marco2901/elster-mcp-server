"""Konfiguration: ``config.json`` + Umgebungsvariablen (Env gewinnt).

Das JSON-Format ist kompatibel zur ursprünglichen TypeScript-Version, ergänzt um
die Blöcke ``security`` und ``http``.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr

from .secrets import redactor, resolve_secret

log = logging.getLogger("elster_mcp.config")


class ReverseChargeSupplier(BaseModel):
    pattern: str
    region: Literal["EU", "NON_EU"]
    name: str


class AuthConfig(BaseModel):
    pfx_path: str = ""
    password: SecretStr | None = None
    password_source: str = "unset"


class TaxpayerConfig(BaseModel):
    tax_number: str = ""
    #: Steuer-Identifikationsnummer (11 Ziffern) – ELSTER verlangt sie z. B. bei der Belegnachreichung.
    tax_id: str = ""
    state_code: str = ""
    name: str = ""
    first_name: str = ""
    street: str = ""
    house_number: str = ""
    zip: str = ""
    city: str = ""
    country: str = "DE"


class RuntimeConfig(BaseModel):
    download_dir: Path = Path("./downloads")
    screenshot_dir: Path = Path("./screenshots")
    headless: bool = True
    browser_args: list[str] = Field(
        default_factory=lambda: ["--disable-dev-shm-usage", "--disable-gpu", "--window-size=1280,1024"]
    )
    #: Tippverzögerung in ms – ELSTER reagiert empfindlich auf zu schnelle Eingaben.
    type_delay_ms: int = 30
    #: Chromium-Sandbox aktiv lassen. Nur in Containern ohne User-Namespaces abschalten.
    chromium_sandbox: bool = True
    #: Optional eigenes Chrome/Chromium statt des von Playwright installierten.
    executable_path: str | None = None


class SecurityConfig(BaseModel):
    #: Ohne explizite Freigabe kann KEINE Übermittlung ans Finanzamt stattfinden.
    allow_submit: bool = False
    #: Freigabe muss zusätzlich per MCP-Elicitation direkt vom Menschen kommen.
    require_elicitation: bool = False
    #: Minuten, die eine geprüfte UStVA auf die Freigabe wartet.
    confirm_timeout_minutes: int = 15
    #: Screenshots enthalten Steuerdaten → abschaltbar.
    screenshots: bool = True
    #: Unsichere Dateirechte (Zertifikat/Secret-Dateien) sind ein harter Fehler.
    strict_permissions: bool = False
    #: Revisionsprotokoll (JSONL, ohne Geheimnisse).
    audit_log: Path = Path("./audit/elster-audit.jsonl")
    #: Erlaubte Browser-Ziele – alle anderen Requests werden blockiert.
    allowed_hosts: list[str] = Field(default_factory=lambda: ["elster.de", "www.elster.de"])


class HttpConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/mcp"
    #: Statischer Bearer-Token (MCP_API_KEY) für CLI/Skripte.
    token: SecretStr | None = None
    token_source: str = "unset"
    #: Öffentliche Basis-URL, z. B. https://elster-mcp.biegel24.de (aus MCP_DOMAIN).
    public_url: str | None = None
    #: Authelia als OAuth-Server für Claude.ai.
    oidc_issuer_url: str | None = None
    oidc_introspection_url: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    oidc_allowed_users: list[str] = Field(default_factory=list)
    oidc_required_scopes: list[str] = Field(default_factory=list)


class UstvaConfig(BaseModel):
    reverse_charge_suppliers: list[ReverseChargeSupplier] = Field(default_factory=list)


class EstConfig(BaseModel):
    skip_eur_pre_hook: bool = False


class ElsterConfig(BaseModel):
    auth: AuthConfig = Field(default_factory=AuthConfig)
    taxpayer: TaxpayerConfig = Field(default_factory=TaxpayerConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    ustva: UstvaConfig = Field(default_factory=UstvaConfig)
    est: EstConfig = Field(default_factory=EstConfig)
    config_path: str | None = None

    def redacted(self) -> dict[str, Any]:
        """Konfiguration für ``elster_config_show`` – ohne Geheimnisse."""
        data = self.model_dump(mode="json", exclude={"auth", "http"})
        tn = self.taxpayer.tax_number
        data["taxpayer"]["tax_number"] = f"***{tn[-3:]}" if len(tn) > 3 else ("<set>" if tn else "<empty>")
        ti = self.taxpayer.tax_id
        data["taxpayer"]["tax_id"] = f"***{ti[-3:]}" if len(ti) > 3 else ("<set>" if ti else "<empty>")
        data["auth"] = {
            "pfx_path": self.auth.pfx_path or "<empty>",
            "password": "<set>" if self.auth.password else "<empty>",
            "password_source": self.auth.password_source,
        }
        data["http"] = {
            "host": self.http.host,
            "port": self.http.port,
            "path": self.http.path,
            "token": "<set>" if self.http.token else "<empty>",
            "token_source": self.http.token_source,
            "public_url": self.http.public_url,
            "oidc_issuer_url": self.http.oidc_issuer_url,
            "oidc_introspection_url": self.http.oidc_introspection_url,
            "oidc_client_id": self.http.oidc_client_id,
            "oidc_client_secret": "<set>" if self.http.oidc_client_secret else "<empty>",
            "oidc_allowed_users": self.http.oidc_allowed_users,
        }
        return data


def _env(key: str, fallback: Any) -> Any:
    v = os.environ.get(key)
    return v if v else fallback


def _env_bool(key: str, fallback: bool) -> bool:
    v = os.environ.get(key)
    if v is None or v == "":
        return fallback
    return v.strip().lower() in {"1", "true", "yes", "ja", "on"}


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").replace(" ", ",").split(",") if v.strip()]


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"config.json ist kein gültiges JSON: {exc}") from exc


def _secure_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            path.chmod(0o700)
        except OSError:
            pass


def load_config() -> ElsterConfig:
    config_path = Path(_env("ELSTER_CONFIG_PATH", "./config.json")).expanduser()
    f = _read_json(config_path)
    fa, ft, fr = f.get("auth", {}), f.get("taxpayer", {}), f.get("runtime", {})
    fs, fh = f.get("security", {}), f.get("http", {})

    strict = _env_bool("ELSTER_STRICT_PERMISSIONS", fs.get("strictPermissions", False))

    password, password_source = resolve_secret(
        "ELSTER_PASSWORD", keyring_key="cert-password", fallback=fa.get("password"), strict=strict
    )
    token, token_source = resolve_secret("MCP_API_KEY", strict=strict)
    if token is None:  # älterer Name
        token, token_source = resolve_secret(
            "ELSTER_MCP_TOKEN", keyring_key="http-token", fallback=fh.get("token"), strict=strict
        )
    oidc_secret, _ = resolve_secret("OIDC_CLIENT_SECRET", strict=strict)
    domain = _env("MCP_DOMAIN", fh.get("domain", ""))

    cfg = ElsterConfig(
        config_path=str(config_path) if config_path.is_file() else None,
        auth=AuthConfig(
            pfx_path=_env("ELSTER_PFX_PATH", fa.get("pfxPath", "")),
            password=password,
            password_source=password_source,
        ),
        taxpayer=TaxpayerConfig(
            tax_number=_env("ELSTER_TAX_NUMBER", ft.get("taxNumber", "")),
            tax_id=_env("ELSTER_TAX_ID", ft.get("taxId", "")).replace(" ", ""),
            state_code=_env("ELSTER_STATE_CODE", ft.get("stateCode", "")),
            name=_env("ELSTER_NAME", ft.get("name", "")),
            first_name=_env("ELSTER_FIRST_NAME", ft.get("firstName", "")),
            street=_env("ELSTER_STREET", ft.get("street", "")),
            house_number=_env("ELSTER_HOUSE_NUMBER", ft.get("houseNumber", "")),
            zip=_env("ELSTER_ZIP", ft.get("zip", "")),
            city=_env("ELSTER_CITY", ft.get("city", "")),
            country=_env("ELSTER_COUNTRY", ft.get("country", "DE")),
        ),
        runtime=RuntimeConfig(
            download_dir=Path(_env("ELSTER_DOWNLOAD_DIR", fr.get("downloadDir", "./downloads"))),
            screenshot_dir=Path(_env("ELSTER_SCREENSHOT_DIR", fr.get("screenshotDir", "./screenshots"))),
            headless=_env_bool("ELSTER_HEADLESS", fr.get("headless", True)),
            browser_args=fr.get("browserArgs", RuntimeConfig().browser_args),
            executable_path=_env("ELSTER_CHROMIUM_PATH", fr.get("executablePath")) or None,
            chromium_sandbox=_env_bool("ELSTER_CHROMIUM_SANDBOX", fr.get("chromiumSandbox", True)),
        ),
        security=SecurityConfig(
            allow_submit=_env_bool("ELSTER_ALLOW_SUBMIT", fs.get("allowSubmit", False)),
            require_elicitation=_env_bool("ELSTER_REQUIRE_ELICITATION", fs.get("requireElicitation", False)),
            confirm_timeout_minutes=int(_env("ELSTER_CONFIRM_TIMEOUT_MINUTES", fs.get("confirmTimeoutMinutes", 15))),
            screenshots=_env_bool("ELSTER_SCREENSHOTS", fs.get("screenshots", True)),
            strict_permissions=strict,
            audit_log=Path(_env("ELSTER_AUDIT_LOG", fs.get("auditLog", "./audit/elster-audit.jsonl"))),
            allowed_hosts=fs.get("allowedHosts", SecurityConfig().allowed_hosts),
        ),
        http=HttpConfig(
            host=_env("ELSTER_MCP_HOST", fh.get("host", "127.0.0.1")),
            port=int(_env("ELSTER_MCP_PORT", fh.get("port", 8765))),
            path=_env("ELSTER_MCP_PATH", fh.get("path", "/mcp")),
            token=token,
            token_source=token_source,
            public_url=_env("MCP_PUBLIC_URL", f"https://{domain}" if domain else None),
            oidc_issuer_url=_env("OIDC_ISSUER_URL", fh.get("oidcIssuerUrl")) or None,
            oidc_introspection_url=_env("OIDC_INTROSPECTION_URL", fh.get("oidcIntrospectionUrl")) or None,
            oidc_client_id=_env("OIDC_CLIENT_ID", fh.get("oidcClientId")) or None,
            oidc_client_secret=oidc_secret,
            oidc_allowed_users=_split(_env("OIDC_ALLOWED_USERS", ",".join(fh.get("oidcAllowedUsers", [])))),
            oidc_required_scopes=_split(_env("OIDC_REQUIRED_SCOPES", ",".join(fh.get("oidcRequiredScopes", [])))),
        ),
        ustva=UstvaConfig(
            reverse_charge_suppliers=[
                ReverseChargeSupplier(**s) for s in f.get("ustva", {}).get("reverseChargeSuppliers", [])
            ]
        ),
        est=EstConfig(
            skip_eur_pre_hook=_env_bool("ELSTER_EST_SKIP_EUR", f.get("est", {}).get("skipEurPreHook", False))
        ),
    )

    # Alles, was nie im Log landen darf, beim Redaktor registrieren.
    redactor.register(
        cfg.auth.password.get_secret_value() if cfg.auth.password else None,
        cfg.http.token.get_secret_value() if cfg.http.token else None,
        cfg.http.oidc_client_secret.get_secret_value() if cfg.http.oidc_client_secret else None,
        cfg.taxpayer.tax_number,
        cfg.taxpayer.tax_id,
    )

    for d in (cfg.runtime.download_dir, cfg.runtime.screenshot_dir, cfg.security.audit_log.parent):
        try:
            _secure_mkdir(d.expanduser().resolve())
        except OSError as exc:
            log.warning("Verzeichnis %s konnte nicht angelegt werden: %s", d, exc)

    return cfg


@lru_cache(maxsize=1)
def get_config() -> ElsterConfig:
    return load_config()


def reset_config_cache() -> None:
    get_config.cache_clear()
