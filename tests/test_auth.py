import asyncio

import httpx
import pytest
from starlette.testclient import TestClient

from elster_mcp import server
from elster_mcp.auth import ElsterTokenVerifier
from elster_mcp.config import reset_config_cache

API_KEY = "k" * 40
INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _introspection(active=True, username="marco", scope="openid", client_id="elster-mcp", aud=None):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"].startswith("Basic ")
        if b"token=good" not in request.content:
            return httpx.Response(200, json={"active": False})
        return httpx.Response(200, json={"active": active, "username": username, "scope": scope,
                                         "client_id": client_id, "aud": aud or [], "exp": 4_000_000_000})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


def _verifier(http, **kw):
    kw.setdefault("allowed_users", ["marco"])
    return ElsterTokenVerifier(api_key=API_KEY, introspection_url="https://auth/introspect",
                               client_id="elster-mcp", client_secret="s", http=http, **kw)


def test_api_key_and_introspection():
    http, calls = _introspection()
    v = _verifier(http)
    assert asyncio.run(v.verify_token(API_KEY)).client_id == "api-key"
    assert asyncio.run(v.verify_token("good")).subject == "marco"
    assert asyncio.run(v.verify_token("bad")) is None
    asyncio.run(v.verify_token("good"))  # aus dem Cache
    assert len(calls) == 2


def test_token_of_other_client_rejected():
    http, _ = _introspection(client_id="outlook-mcp", aud=["https://outlook-mcp.example.de"])
    assert asyncio.run(_verifier(http).verify_token("good")) is None
    http, _ = _introspection(username="nobody")
    assert asyncio.run(_verifier(http, allowed_users=[]).verify_token("good")) is None  # sperrt im Zweifel
    http, _ = _introspection(username="nobody")
    assert asyncio.run(_verifier(http, allowed_users=["*"]).verify_token("good"))
    http, _ = _introspection(client_id="claude-dyn", aud=["https://elster-mcp.example.de/"])
    assert asyncio.run(_verifier(http, audience="https://elster-mcp.example.de").verify_token("good"))


def test_allowed_users_and_scopes():
    http, _ = _introspection(username="eve")
    assert asyncio.run(_verifier(http, allowed_users=["marco"]).verify_token("good")) is None
    http, _ = _introspection(scope="openid")
    assert asyncio.run(_verifier(http, required_scopes=["elster"]).verify_token("good")) is None
    http, _ = _introspection(username="Marco", scope="openid elster")
    assert asyncio.run(_verifier(http, allowed_users=["marco"], required_scopes=["elster"]).verify_token("good"))


def test_verifier_refuses_unprotected_setup():
    with pytest.raises(ValueError):
        ElsterTokenVerifier()
    with pytest.raises(ValueError):
        ElsterTokenVerifier(api_key="kurz")
    with pytest.raises(ValueError):
        ElsterTokenVerifier(introspection_url="https://auth/introspect")


def test_http_app_refuses_without_protection():
    with pytest.raises(Exception, match="ohne Schutz"):
        server.http_app()


def test_http_app_api_key_only(monkeypatch):
    monkeypatch.setenv("MCP_API_KEY", API_KEY)
    reset_config_cache()
    with TestClient(server.http_app(), base_url="http://127.0.0.1:8765") as c:
        assert c.post("/mcp", json=INIT, headers=HEADERS).status_code == 401
        ok = c.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": f"Bearer {API_KEY}"})
        assert ok.status_code == 200


def test_http_app_oidc(monkeypatch):
    monkeypatch.setenv("MCP_API_KEY", API_KEY)
    monkeypatch.setenv("MCP_DOMAIN", "elster-mcp.example.de")
    monkeypatch.setenv("OIDC_ISSUER_URL", "https://authelia.example.de")
    monkeypatch.setenv("OIDC_INTROSPECTION_URL", "https://authelia.example.de/api/oidc/introspection")
    monkeypatch.setenv("OIDC_CLIENT_ID", "elster-mcp")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "geheim-geheim")
    reset_config_cache()
    try:
        app = server.http_app()
        with TestClient(app, base_url="https://elster-mcp.example.de") as c:
            meta = c.get("/.well-known/oauth-protected-resource/mcp")
            assert meta.status_code == 200
            assert meta.json()["authorization_servers"] == ["https://authelia.example.de"]
            denied = c.post("/mcp", json=INIT, headers=HEADERS)
            assert denied.status_code == 401
            assert "resource_metadata" in denied.headers["www-authenticate"]
            ok = c.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": f"Bearer {API_KEY}"})
            assert ok.status_code == 200
            evil = c.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": f"Bearer {API_KEY}",
                                                        "Host": "evil.example.com"})
            assert evil.status_code == 421
    finally:
        server.mcp.settings.auth = None
        server.mcp._token_verifier = None


def test_username_via_userinfo_when_introspection_has_only_sub():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/userinfo"):
            assert request.headers["authorization"] == "Bearer good"
            return httpx.Response(200, json={"preferred_username": "marco", "sub": "uuid-1"})
        return httpx.Response(200, json={"active": True, "sub": "uuid-1", "client_id": "elster-mcp",
                                         "scope": "openid profile", "exp": 4_000_000_000})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    v = ElsterTokenVerifier(api_key=API_KEY, introspection_url="https://auth/api/oidc/introspection",
                            client_id="elster-mcp", client_secret="s", http=http, allowed_users=["marco"])
    tok = asyncio.run(v.verify_token("good"))
    assert tok and tok.subject == "marco"
