"""Auth for the MCP HTTP endpoint.

Two ways in: a Cloudflare Access JWT (requests that came through the public hostname) or
a labelled bearer token (tailnet clients). The port is also reachable on the LAN, so Access
alone would not be enough. An invalid Access JWT never falls back to the bearer token.
"""

import hmac
import json
import logging

import anyio
import jwt

log = logging.getLogger(__name__)


class AccessVerifier:
    def __init__(self, team_domain: str, audience: str | list[str], emails: set[str],
                 jwks: jwt.PyJWKClient | None = None):
        self.issuer = f"https://{team_domain}"
        # several Access apps (e.g. an MCP-server entry and a self-hosted app) may front the same server
        self.audience = [a.strip() for a in audience.split(",") if a.strip()] if isinstance(audience, str) else audience
        self.emails = {e.lower() for e in emails}
        self.jwks = jwks or jwt.PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs", lifespan=3600)

    async def verify(self, token: str) -> dict | None:
        try:
            key = await anyio.to_thread.run_sync(self.jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=self.audience, issuer=self.issuer,
                                options={"require": ["exp", "iat", "iss", "aud"]}, leeway=30)
        except jwt.PyJWTError as exc:
            try:
                seen = jwt.decode(token, options={"verify_signature": False})
                log.warning("Access JWT rejected (%s); aud=%s iss=%s", exc, seen.get("aud"), seen.get("iss"))
            except jwt.PyJWTError:
                log.warning("Access JWT rejected (%s)", exc)
            return None
        if (claims.get("email") or "").lower() not in self.emails:
            log.warning("Access JWT for %s is not on the email allowlist", claims.get("email"))
            return None
        return claims


class AuthGate:
    """Pure ASGI middleware, so the MCP app's lifespan passes through untouched."""

    def __init__(self, app, *, tokens: dict[str, str], access: AccessVerifier | None,
                 open_paths: frozenset[str] = frozenset({"/health"})):
        self.app = app
        self.tokens = tokens
        self.access = access
        self.open_paths = open_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in self.open_paths:
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        assertion = headers.get("cf-access-jwt-assertion")
        if assertion:
            claims = await self.access.verify(assertion) if self.access else None
            if claims:
                log.info("MCP call via Cloudflare Access by %s", claims.get("email"))
                return await self.app(scope, receive, send)
            return await self._deny(send)
        auth = headers.get("authorization", "")
        given = auth[7:].strip().encode() if auth.startswith("Bearer ") else b""
        for token, label in self.tokens.items():
            if given and hmac.compare_digest(given, token.encode()):
                log.debug("MCP call by %s", label)
                return await self.app(scope, receive, send)
        return await self._deny(send)

    @staticmethod
    async def _deny(send) -> None:
        body = json.dumps({"error": "unauthorized"}).encode()
        await send({"type": "http.response.start", "status": 401, "headers": [
            (b"content-type", b"application/json"), (b"www-authenticate", b"Bearer"),
            (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
