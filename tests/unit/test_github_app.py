"""``GitHubAppAuth`` -- JWT minting and the token exchange, with a throwaway RSA key and a
mocked GitHub. No real App, no network: the parts that could actually be wrong (the JWT claims,
the request shape, error handling) are exercised; GitHub itself is not under test.
"""

from __future__ import annotations

import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.domain.errors import CodeHostError
from app.infrastructure.github_app import GitHubAppAuth


def a_test_key() -> tuple[str, str]:
    """A throwaway RSA keypair, PEM-encoded. Not GitHub's, not anyone's -- generated per test."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return private_pem, public_pem


class TestConstruction:
    def test_missing_credentials_refuse(self) -> None:
        private_pem, _ = a_test_key()
        with pytest.raises(CodeHostError, match="requires"):
            GitHubAppAuth(app_id="", installation_id="42", private_key_pem=private_pem)
        with pytest.raises(CodeHostError, match="requires"):
            GitHubAppAuth(app_id="1", installation_id="", private_key_pem=private_pem)
        with pytest.raises(CodeHostError, match="requires"):
            GitHubAppAuth(app_id="1", installation_id="42", private_key_pem="")

    def test_from_key_file_missing_file_is_a_clear_error(self, tmp_path: object) -> None:
        with pytest.raises(CodeHostError, match="private key not found"):
            GitHubAppAuth.from_key_file(
                app_id="1",
                installation_id="42",
                private_key_path="/no/such/key.pem",
            )


class TestJwt:
    def test_the_app_jwt_is_signed_and_carries_the_expected_claims(self) -> None:
        private_pem, public_pem = a_test_key()
        auth = GitHubAppAuth(app_id="123456", installation_id="42", private_key_pem=private_pem)

        token = auth._app_jwt()
        # Verify with the *public* key -- proves it was signed by the private one.
        claims = jwt.decode(token, public_pem, algorithms=["RS256"])

        assert claims["iss"] == "123456"
        now = int(time.time())
        # iat backdated (clock-skew tolerance), exp within GitHub's 10-minute ceiling.
        assert claims["iat"] <= now
        assert now < claims["exp"] <= now + 600

    def test_a_wrong_key_cannot_forge_the_jwt(self) -> None:
        private_pem, _ = a_test_key()
        _, other_public_pem = a_test_key()
        auth = GitHubAppAuth(app_id="1", installation_id="42", private_key_pem=private_pem)

        token = auth._app_jwt()
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(token, other_public_pem, algorithms=["RS256"])


class TestInstallationToken:
    async def test_a_201_returns_the_token(self) -> None:
        private_pem, _ = a_test_key()
        auth = GitHubAppAuth(app_id="1", installation_id="42", private_key_pem=private_pem)

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/app/installations/42/access_tokens"
            assert request.headers["Authorization"].startswith("Bearer ")
            return httpx.Response(
                201, json={"token": "ghs_faketoken", "expires_at": "2026-01-01T00:00:00Z"}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            token = await auth.installation_token(client)

        assert token.token == "ghs_faketoken"
        assert token.expires_at == "2026-01-01T00:00:00Z"

    async def test_a_non_201_is_a_domain_error_with_a_bounded_snippet(self) -> None:
        private_pem, _ = a_test_key()
        auth = GitHubAppAuth(app_id="1", installation_id="42", private_key_pem=private_pem)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CodeHostError, match="minting installation token failed: 404"):
                await auth.installation_token(client)

    async def test_a_transport_error_is_a_domain_error(self) -> None:
        private_pem, _ = a_test_key()
        auth = GitHubAppAuth(app_id="1", installation_id="42", private_key_pem=private_pem)

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CodeHostError, match="could not reach GitHub"):
                await auth.installation_token(client)
