"""Einmal-Download-Links für den Download-Ordner."""

import pytest
from starlette.testclient import TestClient

from elster_mcp import filelinks, server
from elster_mcp.config import reset_config_cache
from elster_mcp.filelinks import FileLinkStore, resolve_download

API_KEY = "k" * 40


@pytest.fixture
def downloads(tmp_path, monkeypatch):
    d = tmp_path / "downloads"
    d.mkdir()
    (d / "2026-08-18_ELSTER_1_EBW-Neue-Dokumente.pdf").write_bytes(b"%PDF-1")
    (tmp_path / "geheim.txt").write_text("x")
    monkeypatch.setenv("ELSTER_DOWNLOAD_DIR", str(d))
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://elster.example.test")
    monkeypatch.setenv("MCP_API_KEY", API_KEY)
    monkeypatch.setattr(server, "file_links", FileLinkStore())
    reset_config_cache()
    return d


def test_store_is_single_use(tmp_path):
    store = FileLinkStore()
    token = store.create(tmp_path)
    assert len(token) >= 40
    assert store.consume(token) == tmp_path
    assert store.consume(token) is None
    assert store.consume("unbekannt") is None


def test_store_expires(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(filelinks.time, "monotonic", lambda: now[0])
    store = FileLinkStore()
    token = store.create(tmp_path, ttl=60)
    now[0] += 61
    assert store.consume(token) is None


def test_store_limits_open_links(tmp_path, monkeypatch):
    monkeypatch.setattr(filelinks, "MAX_OPEN_LINKS", 2)
    store = FileLinkStore()
    store.create(tmp_path)
    store.create(tmp_path)
    with pytest.raises(RuntimeError):
        store.create(tmp_path)


@pytest.mark.parametrize("name", ["../geheim.txt", "sub/x.pdf", "", ".hidden", "/etc/passwd"])
def test_resolve_download_rejects_paths(downloads, name):
    with pytest.raises((ValueError, FileNotFoundError)):
        resolve_download(downloads, name)


def test_resolve_download_rejects_symlink_escape(downloads):
    (downloads / "link.pdf").symlink_to(downloads.parent / "geheim.txt")
    with pytest.raises(FileNotFoundError):
        resolve_download(downloads, "link.pdf")


def test_list_link_fetch_once_and_delete(downloads):
    name = "2026-08-18_ELSTER_1_EBW-Neue-Dokumente.pdf"
    assert server.elster_downloads_list()["files"] == [{"name": name, "size": 6}]

    link = server.elster_file_link(name)
    assert link["url"].startswith("https://elster.example.test/files/")
    assert link["singleUse"] is True
    path = "/files/" + link["url"].rsplit("/", 1)[1]

    client = TestClient(server.http_app())
    # Ohne Bearer: MCP-Endpunkt gesperrt, Einmal-Link erreichbar – genau einmal.
    assert client.post("/mcp").status_code == 401
    first = client.get(path)
    assert first.status_code == 200
    assert first.content == b"%PDF-1"
    assert first.headers["cache-control"] == "no-store"
    assert client.get(path).status_code == 404
    assert client.get("/files/geraten").status_code == 404

    assert server.elster_file_link("../geheim.txt")["error"]
    assert server.elster_downloads_delete([name, "../geheim.txt"])["deleted"] == [name]
    assert not (downloads / name).exists()
    assert (downloads.parent / "geheim.txt").exists()
