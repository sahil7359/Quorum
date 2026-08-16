"""GitHub App authentication: mint short-lived installation access tokens.

The read path (Phase 2 onward) authenticates the GitHub MCP server with a personal access
token. The **write** path -- posting review comments -- should not run on a human's PAT: a PAT
carries that person's full account access, and a review bot posting under someone's own identity
with their own broad scopes is exactly the blast radius a GitHub App exists to shrink. An App
installed on a repo gets *only* the permissions granted to that installation (here: pull
requests read/write, issues read/write, contents read), scoped to *only* the repos it's
installed on, and authenticates with a token that **expires in an hour** rather than living
until someone remembers to rotate it.

The token dance, per GitHub's documented flow:

1. Sign a JWT with the App's private key (RS256), `iss` = the App ID, short expiry.
2. Exchange that JWT for an *installation* access token via the REST API.
3. Use that installation token exactly like a PAT -- including as the GitHub MCP server's
   ``GITHUB_PERSONAL_ACCESS_TOKEN``, since an installation token is a valid GitHub token.

The private key never leaves this process and never reaches argv: it's read from a file whose
path is configured, and the file itself is gitignored (``secrets/``). The minted token is what
travels onward, and it is short-lived by construction.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt

from app.domain.errors import CodeHostError


@dataclass(frozen=True)
class InstallationToken:
    token: str
    expires_at: str


class GitHubAppAuth:
    """Mints installation access tokens for one App installation.

    Holds the private key in memory (read once at construction) rather than re-reading the
    file per mint -- the key does not change over a process's life, and keeping the file handle
    out of the hot path means a token refresh is one HTTPS call, not a disk read plus a call.
    """

    def __init__(
        self,
        *,
        app_id: str,
        installation_id: str,
        private_key_pem: str,
        base_url: str = "https://api.github.com",
    ) -> None:
        if not app_id or not installation_id or not private_key_pem:
            raise CodeHostError(
                "GitHubAppAuth requires app_id, installation_id and a private key -- one was "
                "empty. Set QUORUM_GITHUB_APP_ID, QUORUM_GITHUB_APP_INSTALLATION_ID and "
                "QUORUM_GITHUB_APP_PRIVATE_KEY_PATH."
            )
        self._app_id = app_id
        self._installation_id = installation_id
        self._private_key_pem = private_key_pem
        self._base_url = base_url.rstrip("/")

    @classmethod
    def from_key_file(
        cls,
        *,
        app_id: str,
        installation_id: str,
        private_key_path: str | Path,
        base_url: str = "https://api.github.com",
    ) -> GitHubAppAuth:
        path = Path(private_key_path)
        if not path.is_file():
            raise CodeHostError(
                f"GitHub App private key not found at {path!s}. Save the App's .pem there, or "
                "point QUORUM_GITHUB_APP_PRIVATE_KEY_PATH at it."
            )
        return cls(
            app_id=app_id,
            installation_id=installation_id,
            private_key_pem=path.read_text(encoding="utf-8"),
            base_url=base_url,
        )

    def _app_jwt(self) -> str:
        # why iat backdated 60s: GitHub rejects a JWT whose iat is in the future by even a
        #      second of clock skew between this host and theirs. exp capped well under the
        #      10-minute maximum GitHub allows. This JWT authenticates *as the App*, only long
        #      enough to exchange it for an installation token below.
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 540, "iss": self._app_id}
        return jwt.encode(payload, self._private_key_pem, algorithm="RS256")

    async def installation_token(
        self, client: httpx.AsyncClient | None = None
    ) -> InstallationToken:
        """Exchange an App JWT for an installation access token (valid ~1 hour)."""
        url = f"{self._base_url}/app/installations/{self._installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {self._app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        owns_client = client is None
        client = client or httpx.AsyncClient(timeout=30.0)
        try:
            response = await client.post(url, headers=headers)
        except httpx.HTTPError as exc:
            raise CodeHostError(
                f"could not reach GitHub to mint installation token: {exc}"
            ) from exc
        finally:
            if owns_client:
                await client.aclose()

        if response.status_code != 201:
            # why not response.text unbounded: GitHub's error body can be large and this reaches
            #      a log line; the status plus a bounded snippet is enough to tell "bad key" from
            #      "wrong installation id" without dumping a page into the logs.
            raise CodeHostError(
                f"minting installation token failed: {response.status_code}: {response.text[:200]}"
            )
        data: dict[str, Any] = response.json()
        return InstallationToken(
            token=str(data["token"]), expires_at=str(data.get("expires_at", ""))
        )
