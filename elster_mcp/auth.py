"""Zugangsschutz für den HTTP-Transport – gleiches Muster wie die übrigen biegel24-MCP-Server.

* Claude.ai & Co. holen sich per OAuth ein Token bei Authelia (``OIDC_ISSUER_URL``).
  Der Server findet Authelia über ``/.well-known/oauth-protected-resource`` (RFC 9728)
  und prüft jedes Token per Introspection (RFC 7662) bei ``OIDC_INTROSPECTION_URL``.
* Für CLI/Skripte gilt zusätzlich ein statischer ``MCP_API_KEY`` als Bearer-Token.
* ``OIDC_ALLOWED_USERS`` beschränkt den Zugriff auf bestimmte Authelia-Benutzer –
  für ein Steuer-Werkzeug dringend empfohlen.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any

import httpx
from mcp.server.auth.provider import AccessToken

log = logging.getLogger("elster_mcp.auth")

_CACHE_TTL_S = 60
_MAX_CACHE = 256


class ElsterTokenVerifier:
    """API-Key **oder** Authelia-Introspection; Ergebnisse kurz zwischengespeichert."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        introspection_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        allowed_users: list[str] | None = None,
        required_scopes: list[str] | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        if api_key is not None and len(api_key) < 32:
            raise ValueError("MCP_API_KEY muss mindestens 32 Zeichen lang sein.")
        if introspection_url and not (client_id and client_secret):
            raise ValueError("OIDC_INTROSPECTION_URL braucht OIDC_CLIENT_ID und OIDC_CLIENT_SECRET.")
        if not api_key and not introspection_url:
            raise ValueError("Weder MCP_API_KEY noch OIDC_INTROSPECTION_URL gesetzt – HTTP wäre ungeschützt.")
        self._api_key = api_key.encode() if api_key else None
        self._introspection_url = introspection_url
        self._client = (client_id, client_secret) if client_id and client_secret else None
        self._allowed_users = {u.strip().lower() for u in (allowed_users or []) if u.strip()}
        self._required_scopes = set(required_scopes or [])
        self._http = http
        self._cache: dict[str, tuple[float, AccessToken | None]] = {}

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        if self._api_key and hmac.compare_digest(token.encode(), self._api_key):
            return AccessToken(token=token, client_id="api-key", scopes=[], subject="api-key")
        if not self._introspection_url:
            return None

        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        result = await self._introspect(token)
        if len(self._cache) >= _MAX_CACHE:
            self._cache.clear()
        self._cache[key] = (now + _CACHE_TTL_S, result)
        return result

    async def _introspect(self, token: str) -> AccessToken | None:
        client = self._http or httpx.AsyncClient(timeout=10)
        try:
            resp = await client.post(
                self._introspection_url,  # type: ignore[arg-type]
                data={"token": token, "token_type_hint": "access_token"},
                auth=self._client,  # type: ignore[arg-type]
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            log.error("Token-Introspection nicht erreichbar: %s", exc)
            return None
        finally:
            if self._http is None:
                await client.aclose()
        if resp.status_code != 200:
            log.warning("Token-Introspection antwortet mit HTTP %s", resp.status_code)
            return None
        data: dict[str, Any] = resp.json()
        if not data.get("active"):
            return None

        exp = data.get("exp")
        if isinstance(exp, (int, float)) and exp < time.time():
            return None
        scopes = str(data.get("scope") or "").split()
        if self._required_scopes and not self._required_scopes.issubset(scopes):
            log.warning("Token ohne erforderliche Scopes abgelehnt.")
            return None
        user = str(data.get("username") or data.get("preferred_username") or data.get("sub") or "")
        if self._allowed_users and user.lower() not in self._allowed_users:
            log.warning("Benutzer '%s' ist nicht in OIDC_ALLOWED_USERS – abgelehnt.", user)
            return None
        return AccessToken(
            token=token,
            client_id=str(data.get("client_id") or ""),
            scopes=scopes,
            expires_at=int(exp) if isinstance(exp, (int, float)) else None,
            subject=user or None,
            claims={k: data[k] for k in ("iss", "aud") if k in data},
        )
