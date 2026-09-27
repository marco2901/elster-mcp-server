import logging

import pytest

from elster_mcp.config import get_config
from elster_mcp.secrets import SecretError, redactor, resolve_secret, setup_logging, validate_certificate


def test_password_file_wins_over_env(tmp_path, monkeypatch):
    f = tmp_path / "pw"
    f.write_text("aus-datei\n")
    f.chmod(0o600)
    monkeypatch.setenv("ELSTER_PASSWORD_FILE", str(f))
    monkeypatch.setenv("ELSTER_PASSWORD", "aus-env")
    cfg = get_config()
    assert cfg.auth.password.get_secret_value() == "aus-datei"
    assert cfg.auth.password_source == "file:$ELSTER_PASSWORD_FILE"


def test_keyring_before_env(monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr("elster_mcp.secrets.keyring_get", lambda key: SecretStr("aus-keyring"))
    monkeypatch.setenv("ELSTER_PASSWORD", "aus-env")
    value, source = resolve_secret("ELSTER_PASSWORD", keyring_key="cert-password")
    assert value.get_secret_value() == "aus-keyring"
    assert source.startswith("keyring:")


def test_config_json_fallback_warns(write_config, caplog):
    write_config({"auth": {"password": "klartext"}})
    with caplog.at_level(logging.WARNING):
        cfg = get_config()
    assert cfg.auth.password_source == "config.json"


def test_strict_permissions_rejects_world_readable(tmp_path, monkeypatch):
    f = tmp_path / "pw"
    f.write_text("geheim")
    f.chmod(0o644)
    monkeypatch.setenv("ELSTER_PASSWORD_FILE", str(f))
    monkeypatch.setenv("ELSTER_STRICT_PERMISSIONS", "1")
    with pytest.raises(SecretError):
        get_config()


def test_redacted_config_hides_secrets(monkeypatch, cert):
    monkeypatch.setenv("ELSTER_PASSWORD", "supergeheim123")
    monkeypatch.setenv("ELSTER_TAX_NUMBER", "012/345/67890")
    monkeypatch.setenv("ELSTER_PFX_PATH", str(cert))
    text = str(get_config().redacted())
    assert "supergeheim123" not in text
    assert "012/345/67890" not in text
    assert "***890" in text


def test_logging_redacts_registered_secrets(monkeypatch, capsys):
    monkeypatch.setenv("ELSTER_PASSWORD", "pin-4711-geheim")
    get_config()
    setup_logging("INFO")
    logging.getLogger("elster_mcp.test").info("Passwort ist %s", "pin-4711-geheim")
    err = capsys.readouterr().err
    assert "pin-4711-geheim" not in err
    assert "***" in err
    assert redactor.redact("x pin-4711-geheim y") == "x *** y"


def test_certificate_validation(cert, tmp_path):
    assert validate_certificate(str(cert)) == cert.resolve()
    with pytest.raises(SecretError):
        validate_certificate("")
    with pytest.raises(SecretError):
        validate_certificate(str(tmp_path / "fehlt.pfx"))
    wrong = tmp_path / "zert.txt"
    wrong.write_text("x")
    with pytest.raises(SecretError):
        validate_certificate(str(wrong))
    cert.chmod(0o644)
    with pytest.raises(SecretError):
        validate_certificate(str(cert), strict=True)


def test_submit_disabled_by_default():
    assert get_config().security.allow_submit is False


def test_missing_secret_file_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("ELSTER_PASSWORD_FILE", str(tmp_path / "fehlt"))
    cfg = get_config()
    assert cfg.auth.password is None
    assert cfg.auth.password_source == "missing:$ELSTER_PASSWORD_FILE"
