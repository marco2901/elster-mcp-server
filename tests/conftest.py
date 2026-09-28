import json

import pytest

from elster_mcp.config import reset_config_cache

ENV_KEYS = [
    "ELSTER_CONFIG_PATH", "ELSTER_PFX_PATH", "ELSTER_PASSWORD", "ELSTER_PASSWORD_FILE",
    "ELSTER_TAX_NUMBER", "ELSTER_TAX_ID", "ELSTER_BIRTH_DATE", "ELSTER_IMPORT_HOSTS", "ELSTER_STATE_CODE", "ELSTER_ALLOW_SUBMIT", "ELSTER_MCP_TOKEN",
    "ELSTER_MCP_TOKEN_FILE", "ELSTER_STRICT_PERMISSIONS", "ELSTER_AUDIT_LOG",
    "ELSTER_DOWNLOAD_DIR", "ELSTER_SCREENSHOT_DIR", "ELSTER_REQUIRE_ELICITATION",
    "MCP_API_KEY", "MCP_API_KEY_FILE", "MCP_DOMAIN", "MCP_PUBLIC_URL", "OIDC_ISSUER_URL",
    "OIDC_INTROSPECTION_URL", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_CLIENT_SECRET_FILE",
    "OIDC_ALLOWED_USERS", "OIDC_REQUIRED_SCOPES",
]


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ELSTER_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setenv("ELSTER_AUDIT_LOG", str(tmp_path / "audit" / "audit.jsonl"))
    monkeypatch.setenv("ELSTER_DOWNLOAD_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("ELSTER_SCREENSHOT_DIR", str(tmp_path / "screenshots"))
    # Keyring aus Tests heraushalten
    monkeypatch.setattr("elster_mcp.secrets.keyring_get", lambda key: None)
    reset_config_cache()
    yield
    reset_config_cache()


@pytest.fixture
def cert(tmp_path):
    p = tmp_path / "zertifikat.pfx"
    p.write_bytes(b"\x30\x82" + b"\x00" * 2000)
    p.chmod(0o600)
    return p


@pytest.fixture
def write_config(tmp_path):
    def _write(data: dict):
        (tmp_path / "config.json").write_text(json.dumps(data), encoding="utf-8")
        reset_config_cache()

    return _write
