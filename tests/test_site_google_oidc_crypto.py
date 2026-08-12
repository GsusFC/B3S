from __future__ import annotations

import asyncio
import hashlib
import time

import pytest
from authlib.integrations.starlette_client import OAuth
from cryptography.hazmat.primitives.asymmetric import rsa
from joserfc import jwt
from joserfc.jwk import RSAKey


CLIENT_ID = "1234567890-example.apps.googleusercontent.com"
ISSUER = "https://accounts.google.com"


def _client_and_key():
    key = RSAKey.import_key(
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    public = key.as_dict(private=False)
    public["kid"] = "test-key"
    oauth = OAuth()
    client = oauth.register(
        "google-test",
        client_id=CLIENT_ID,
        client_secret="test-client-secret",
        server_metadata_url="https://accounts.google.test/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email", "code_challenge_method": "S256"},
    )

    async def metadata():
        return {
            "issuer": ISSUER,
            "id_token_signing_alg_values_supported": ["RS256"],
        }

    async def jwks(force=False):
        del force
        return {"keys": [public]}

    client.load_server_metadata = metadata
    client.fetch_jwk_set = jwks
    return client, key


def _token(key, *, audience=CLIENT_ID, expires_in=300, nonce="exact-nonce"):
    now = int(time.time())
    return jwt.encode(
        {"alg": "RS256", "kid": "test-key"},
        {
            "iss": ISSUER,
            "sub": "google-subject",
            "aud": audience,
            "exp": now + expires_in,
            "iat": now,
            "nonce": nonce,
            "email": "jesus@wearefloc.com",
            "email_verified": True,
            "hd": "wearefloc.com",
        },
        key,
    )


def _parse(client, token, nonce="exact-nonce"):
    return asyncio.run(
        client.parse_id_token(
            {"id_token": token},
            nonce=nonce,
            claims_options={
                "iss": {"values": [ISSUER]},
                "email": {"essential": True},
                "email_verified": {"essential": True},
                "hd": {"essential": True, "value": "wearefloc.com"},
                "sub": {"essential": True},
            },
            leeway=0,
        )
    )


def test_authlib_accepts_valid_google_id_token_contract():
    client, key = _client_and_key()
    userinfo = _parse(client, _token(key))
    assert userinfo["email"] == "jesus@wearefloc.com"


@pytest.mark.parametrize(
    ("mutation", "nonce"),
    [
        ({"audience": "wrong-client"}, "exact-nonce"),
        ({"expires_in": -60}, "exact-nonce"),
        ({"nonce": "wrong-nonce"}, "exact-nonce"),
    ],
)
def test_authlib_rejects_wrong_audience_expiry_and_nonce(mutation, nonce):
    client, key = _client_and_key()
    with pytest.raises(Exception):
        _parse(client, _token(key, **mutation), nonce=nonce)


def test_authlib_rejects_bad_signature():
    client, _trusted_key = _client_and_key()
    attacker_key = RSAKey.import_key(
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    with pytest.raises(Exception):
        _parse(client, _token(attacker_key))


def test_authlib_emits_s256_challenge_matching_the_stored_verifier():
    client, _key = _client_and_key()
    verifier = "v" * 64

    async def metadata():
        return {"authorization_endpoint": "https://accounts.google.test/auth"}

    client.load_server_metadata = metadata
    authorization = asyncio.run(
        client.create_authorization_url(
            "https://b3s-pr71-vault.fly.dev/auth/google/callback",
            state="state",
            nonce="nonce",
            code_verifier=verifier,
        )
    )
    from urllib.parse import parse_qs, urlsplit
    import base64

    query = parse_qs(urlsplit(authorization["url"]).query)
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [expected]
    assert authorization["code_verifier"] == verifier
