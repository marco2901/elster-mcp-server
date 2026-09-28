"""elster_downloads_import: Datei per Einmal-Link holen, mit Host-, Größen- und Inhaltsprüfung."""

import io
import urllib.error

import pytest

from elster_mcp import imports, server
from elster_mcp.config import reset_config_cache
from elster_mcp.imports import ImportError_, check_filename, check_source_url, import_file

HOSTS = ["mcp-eee.example.test"]
URL = "https://mcp-eee.example.test/files/abc"
PDF = b"%PDF-1.7\n" + b"x" * 100


class Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, data=PDF, exc=None):
        self.data, self.exc, self.calls = data, exc, 0

    def open(self, req, timeout):
        self.calls += 1
        if self.exc:
            raise self.exc
        return Resp(self.data)


@pytest.mark.parametrize(
    ("url", "needle"),
    [
        ("http://mcp-eee.example.test/files/abc", "https"),
        ("file:///etc/passwd", "https"),
        ("https://evil.example.test/files/abc", "nicht freigegeben"),
        ("https://mcp-eee.example.test.evil.test/x", "nicht freigegeben"),
        ("https://user:pw@mcp-eee.example.test/x", "Zugangsdaten"),
        ("https://mcp-eee.example.test:8443/x", "Port"),
    ],
)
def test_source_url_rules(url, needle):
    with pytest.raises(ImportError_, match=needle):
        check_source_url(url, HOSTS)


def test_import_disabled_without_hosts():
    with pytest.raises(ImportError_, match="deaktiviert"):
        check_source_url(URL, [])


@pytest.mark.parametrize("name", ["../x.pdf", ".hidden.pdf", "a/b.pdf", "rechnung.docx", "a b.pdf", ""])
def test_filename_rules(name):
    with pytest.raises(ImportError_):
        check_filename(name)


def test_import_ok_and_no_overwrite(tmp_path):
    info = import_file(URL, "2026-09-01_BMW_52039926.pdf", tmp_path, HOSTS, Opener())
    target = tmp_path / "2026-09-01_BMW_52039926.pdf"
    assert target.read_bytes() == PDF and info["size"] == len(PDF) and len(info["sha256"]) == 64
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(ImportError_, match="existiert bereits"):
        import_file(URL, "2026-09-01_BMW_52039926.pdf", tmp_path, HOSTS, Opener())


def test_import_rejects_wrong_content_and_size(tmp_path, monkeypatch):
    with pytest.raises(ImportError_, match="keine PDF"):
        import_file(URL, "a.pdf", tmp_path, HOSTS, Opener(b"<html>login</html>"))
    monkeypatch.setattr(imports, "MAX_IMPORT_BYTES", 10)
    with pytest.raises(ImportError_, match="10 MB"):
        import_file(URL, "b.pdf", tmp_path, HOSTS, Opener())
    assert list(tmp_path.iterdir()) == []


def test_import_http_error_and_redirect(tmp_path):
    err = urllib.error.HTTPError(URL, 404, "not found", {}, None)
    with pytest.raises(ImportError_, match="abgelaufen"):
        import_file(URL, "a.pdf", tmp_path, HOSTS, Opener(exc=err))
    handler = imports._NoRedirect()
    with pytest.raises(ImportError_, match="Weiterleitung"):
        handler.redirect_request(None, None, 302, "Found", {}, "https://evil.test/")


def test_tool_uses_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ELSTER_DOWNLOAD_DIR", str(tmp_path))
    reset_config_cache()
    assert "deaktiviert" in server.elster_downloads_import(URL, "a.pdf")["error"]
    monkeypatch.setenv("ELSTER_IMPORT_HOSTS", "MCP-EEE.example.test, andere.example.test")
    reset_config_cache()
    monkeypatch.setattr(imports.urllib.request, "build_opener", lambda *h: Opener())
    res = server.elster_downloads_import(URL, "rechnung.pdf")
    assert res["name"] == "rechnung.pdf" and (tmp_path / "rechnung.pdf").exists()
