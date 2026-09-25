import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.transport_security import TransportSecuritySettings
from starlette.testclient import TestClient

from icampus.mcp_auth import AccessVerifier, AuthGate
from icampus.mcp_server import mcp

TEAM, AUD, EMAIL = "team.cloudflareaccess.com", "aud-tag", "me@example.com"
TOKEN = "b" * 24
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class StubJwks:
    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": KEY.public_key()})()


def access_jwt(**over):
    claims = {"aud": [AUD], "iss": f"https://{TEAM}", "email": EMAIL, "iat": int(time.time()),
              "exp": int(time.time()) + 300, **over}
    return jwt.encode(claims, KEY, algorithm="RS256")


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def client():
    app = mcp.streamable_http_app(stateless_http=True, json_response=True,
                                  transport_security=TransportSecuritySettings(allowed_hosts=["testserver"]))
    gate = AuthGate(app, tokens={TOKEN: "mac"},
                    access=AccessVerifier(TEAM, f"other-aud,{AUD}", {EMAIL}, jwks=StubJwks()))
    with TestClient(gate) as c:  # the context runs the session manager's lifespan
        yield c


def post(client, **headers):
    return client.post("/mcp", json=INIT, headers={**MCP_HEADERS, **headers})


def test_bearer_and_access_pass(client):
    assert post(client, Authorization=f"Bearer {TOKEN}").status_code == 200
    r = post(client, **{"Cf-Access-Jwt-Assertion": access_jwt()})
    assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "icampus"


@pytest.mark.parametrize("bad", [
    {"aud": ["other-app"]}, {"iss": "https://evil.cloudflareaccess.com"},
    {"exp": int(time.time()) - 3600}, {"email": "someone@else.com"},
])
def test_bad_access_jwt(client, bad):
    assert post(client, **{"Cf-Access-Jwt-Assertion": access_jwt(**bad)}).status_code == 401


def test_bad_jwt_does_not_fall_back_to_bearer(client):
    r = post(client, **{"Cf-Access-Jwt-Assertion": "x.y.z", "Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 401


def test_no_credentials_and_wrong_host(client):
    assert post(client).status_code == 401
    assert post(client, Authorization="Bearer nope").status_code == 401
    assert post(client, Authorization=f"Bearer {TOKEN}", Host="evil.example").status_code == 421
    assert client.get("/health").status_code == 200
