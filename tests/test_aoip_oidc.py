"""OIDC front door — verify id_token chuẩn RS256 + PKCE flow, offline (không cần IdP live).

Tự sinh RSA keypair, ký id_token, dựng JWKS → chứng minh verify_id_token đúng chuẩn:
chữ ký, issuer, audience, exp, nonce. Roundtrip begin_login → consume_flow → issue_session.
"""
from __future__ import annotations

import base64
import json
import time

import fakeredis.aioredis as aioredis
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes

from aoip.console import identity, oidc

CFG = oidc.OIDCConfig(issuer="https://op.example", client_id="aoip-provider",
                      client_secret="s3cret", authorize_url="https://op.example/auth",
                      token_url="https://op.example/token", jwks_url="https://op.example/jwks",
                      redirect_uri="https://provider.aoip/callback")


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_numbers()
    jwks = {"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256",
                      "n": _b64u(pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")),
                      "e": _b64u(pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big"))}]}
    return key, jwks


def _sign(key, claims: dict) -> str:
    header = {"alg": "RS256", "typ": "JWT", "kid": "k1"}
    seg = _b64u(json.dumps(header).encode()) + "." + _b64u(json.dumps(claims).encode())
    sig = key.sign(seg.encode(), padding.PKCS1v15(), hashes.SHA256())
    return seg + "." + _b64u(sig)


def _claims(now, nonce, **over):
    c = {"iss": CFG.issuer, "aud": CFG.client_id, "sub": "owner@aoip",
         "email": "owner@aoip", "exp": now + 300, "nonce": nonce}
    c.update(over)
    return c


def test_verify_valid_id_token():
    key, jwks = _keypair(); now = 1000.0
    tok = _sign(key, _claims(now, "n1"))
    claims = oidc.verify_id_token(tok, jwks, CFG, nonce="n1", now=now)
    assert claims["sub"] == "owner@aoip"


@pytest.mark.parametrize("bad,val", [("nonce", "wrong"), ("iss", "https://evil"),
                                     ("aud", "someone-else")])
def test_verify_rejects_tampered_claims(bad, val):
    key, jwks = _keypair(); now = 1000.0
    over = {bad: val} if bad != "nonce" else {}
    tok = _sign(key, _claims(now, "n1", **over))
    expected_nonce = "n1" if bad != "nonce" else "mismatch"
    with pytest.raises(ValueError):
        oidc.verify_id_token(tok, jwks, CFG, nonce=expected_nonce, now=now)


def test_verify_rejects_expired():
    key, jwks = _keypair(); now = 1000.0
    tok = _sign(key, _claims(now, "n1", exp=now - 1))
    with pytest.raises(ValueError):
        oidc.verify_id_token(tok, jwks, CFG, nonce="n1", now=now)


def test_verify_rejects_wrong_key():
    key, _ = _keypair(); _, other_jwks = _keypair(); now = 1000.0
    tok = _sign(key, _claims(now, "n1"))
    with pytest.raises(Exception):
        oidc.verify_id_token(tok, other_jwks, CFG, nonce="n1", now=now)


async def test_flow_roundtrip_issues_session():
    r = aioredis.FakeRedis(decode_responses=True)
    await identity.upsert_user(r, subject="owner@aoip", email="owner@aoip")
    await identity.grant_provider_role(r, subject="owner@aoip", role="platform_owner")
    key, jwks = _keypair(); now = time.time()

    url = await oidc.begin_login(r, CFG, kind="provider", tenant=None,
                                 state_seed=b"state-seed-32-bytes-padding-xx",
                                 verifier_seed=b"verifier-seed-32-bytes-pad-xxx")
    state = url.split("state=", 1)[1].split("&", 1)[0]
    flow = await oidc.consume_flow(r, state)
    assert flow is not None and flow["kind"] == "provider"
    # OP trả id_token với nonce của flow → verify → resolve → session
    tok = _sign(key, _claims(now, flow["nonce"]))
    claims = oidc.verify_id_token(tok, jwks, CFG, nonce=flow["nonce"], now=now)
    p = await identity.resolve_provider_principal(r, claims["sub"])
    s = await identity.issue_session(r, principal=p, now=now)
    loaded = await identity.load_session(r, s.sid, now)
    assert loaded and loaded.principal.kind == "provider"
    # flow là one-time
    assert await oidc.consume_flow(r, state) is None


async def test_http_login_callback_sets_cookie_and_authenticates(monkeypatch):
    import httpx
    from aoip.console.app import create_provider_app, SESSION_COOKIE

    r = aioredis.FakeRedis(decode_responses=True)
    await identity.upsert_user(r, subject="owner@aoip", email="owner@aoip")
    await identity.grant_provider_role(r, subject="owner@aoip", role="platform_owner")
    key, jwks = _keypair()

    for k, v in {"ISSUER": CFG.issuer, "CLIENT_ID": CFG.client_id, "CLIENT_SECRET": CFG.client_secret,
                 "AUTHORIZE_URL": CFG.authorize_url, "TOKEN_URL": CFG.token_url,
                 "JWKS_URL": CFG.jwks_url,
                 "REDIRECT_URI": "http://provider.aoip/callback"}.items():  # http → cookie gửi lại được trên test client
        monkeypatch.setenv("AOIP_OIDC_PROVIDER_" + k, v)

    # http_json inject: token endpoint trả id_token ký bằng nonce của flow (đọc từ redis).
    async def fake_http(method, url, *, data=None, auth=None):
        if method == "GET":
            return jwks
        state = data["code_verifier"]  # không dùng; nonce lấy theo state thực ở dưới
        return {"id_token": fake_http.token}
    fake_http.token = None

    app = create_provider_app(r, oidc_http=fake_http)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://c") as c:
        login = await c.get("/auth/login")
        assert login.status_code == 302
        state = login.headers["location"].split("state=", 1)[1].split("&", 1)[0]
        flow = await r.hgetall(oidc._FLOW + state)
        fake_http.token = _sign(key, _claims(time.time(), flow["nonce"]))
        cb = await c.get(f"/auth/callback?code=abc&state={state}")
        assert cb.status_code == 302
        assert SESSION_COOKIE in cb.headers.get("set-cookie", "")
        me = await c.get("/api/provider/v1/me")  # cookie tự gửi lại
        assert me.status_code == 200 and me.json()["subject"] == "owner@aoip"
