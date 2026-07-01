"""OIDC front door — Authorization Code + PKCE, standards-based.

KHÔNG phụ thuộc claim riêng Keycloak: chỉ dùng `sub` (subject) + `email` chuẩn OIDC.
Keycloak/Dex/bất kỳ OP nào cũng chạy. Verify id_token qua JWKS (RS256).

Luồng:
  login   → tạo state+PKCE (lưu server-side Redis, TTL ngắn) → redirect tới OP authorize.
  callback→ đổi code lấy id_token (token endpoint) → verify JWKS → sub/email
          → resolve Principal (provider|tenant) server-side → issue_session → set cookie HttpOnly.

Không tin client: state chống CSRF, PKCE chống code interception, nonce chống replay.
Access/refresh token OP KHÔNG gửi ra browser — chỉ opaque session sid.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass

_FLOW = "portal:oidc:flow:"     # hash sid_state → {verifier, nonce, kind, tenant, redirect}
_FLOW_TTL_S = 600


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    jwks_url: str
    redirect_uri: str

    @classmethod
    def from_env(cls, prefix: str) -> "OIDCConfig":
        """prefix = AOIP_OIDC_PROVIDER_ hoặc AOIP_OIDC_TENANT_ (2 portal có thể khác realm)."""
        g = lambda k: os.environ[prefix + k]  # noqa: E731 — fail-fast nếu thiếu config
        return cls(issuer=g("ISSUER"), client_id=g("CLIENT_ID"), client_secret=g("CLIENT_SECRET"),
                   authorize_url=g("AUTHORIZE_URL"), token_url=g("TOKEN_URL"),
                   jwks_url=g("JWKS_URL"), redirect_uri=g("REDIRECT_URI"))


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _pkce_pair(seed: bytes) -> tuple[str, str]:
    verifier = _b64url(seed)
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


async def begin_login(redis, cfg: OIDCConfig, *, kind: str, tenant: str | None,
                      state_seed: bytes, verifier_seed: bytes) -> str:
    """Tạo state+PKCE server-side, trả authorize URL. Seeds do lớp trên cấp (os.urandom)."""
    from urllib.parse import urlencode
    state = _b64url(state_seed)
    verifier, challenge = _pkce_pair(verifier_seed)
    nonce = _b64url(hashlib.sha256(state_seed + verifier_seed).digest())
    await redis.hset(_FLOW + state, mapping={
        "verifier": verifier, "nonce": nonce, "kind": kind, "tenant": tenant or "",
    })
    await redis.expire(_FLOW + state, _FLOW_TTL_S)
    q = urlencode({
        "response_type": "code", "client_id": cfg.client_id, "redirect_uri": cfg.redirect_uri,
        "scope": "openid email profile", "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    return f"{cfg.authorize_url}?{q}"


def _jwt_segments(id_token: str) -> tuple[dict, dict, bytes, bytes]:
    h_b64, p_b64, s_b64 = id_token.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    header = json.loads(base64.urlsafe_b64decode(pad(h_b64)))
    payload = json.loads(base64.urlsafe_b64decode(pad(p_b64)))
    signing_input = f"{h_b64}.{p_b64}".encode()
    signature = base64.urlsafe_b64decode(pad(s_b64))
    return header, payload, signing_input, signature


def verify_id_token(id_token: str, jwks: dict, cfg: OIDCConfig, *, nonce: str, now: float) -> dict:
    """Verify RS256 id_token qua JWKS. Trả claims nếu hợp lệ; raise nếu không."""
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.utils import Prehashed  # noqa: F401

    header, claims, signing_input, signature = _jwt_segments(id_token)
    if header.get("alg") != "RS256":
        raise ValueError("chỉ chấp nhận RS256")
    key = next((k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid")), None)
    if key is None:
        raise ValueError("kid không có trong JWKS")
    n = int.from_bytes(base64.urlsafe_b64decode(key["n"] + "=" * (-len(key["n"]) % 4)), "big")
    e = int.from_bytes(base64.urlsafe_b64decode(key["e"] + "=" * (-len(key["e"]) % 4)), "big")
    pub = rsa.RSAPublicNumbers(e, n).public_key()
    pub.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())  # raise nếu sai

    if claims.get("iss") != cfg.issuer:
        raise ValueError("issuer mismatch")
    aud = claims.get("aud")
    if cfg.client_id not in (aud if isinstance(aud, list) else [aud]):
        raise ValueError("audience mismatch")
    if float(claims.get("exp", 0)) <= now:
        raise ValueError("id_token hết hạn")
    if claims.get("nonce") != nonce:
        raise ValueError("nonce mismatch (replay?)")
    if not claims.get("sub"):
        raise ValueError("thiếu sub")
    return claims


async def consume_flow(redis, state: str) -> dict | None:
    """Lấy + xoá flow (one-time). None nếu state không tồn tại/đã dùng/hết hạn."""
    f = await redis.hgetall(_FLOW + state)
    if not f:
        return None
    await redis.delete(_FLOW + state)
    return f
