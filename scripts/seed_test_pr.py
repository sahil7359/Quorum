"""Seed the throwaway test repo with a demo pull request, using the human PAT.

The write-path demo (``scripts/write_demo.py``) needs a real PR to post a review comment on.
The GitHub App deliberately has *contents: read-only* -- a review bot comments, it does not
push code -- so it cannot create the PR itself, which is correct: in reality a human opens the
PR and the bot reviews it. This script plays the human, using ``QUORUM_GITHUB_TOKEN`` (a PAT
with push access to the user's own throwaway repo), and only ever touches the one repo named
below.

Idempotent-ish: if an open PR already exists it does nothing. Re-runnable against a repo that
already has the base files.

Usage: ``uv run python -m scripts.seed_test_pr``
"""

from __future__ import annotations

import httpx

from app.infrastructure.config import Settings

REPO = "sahil7359/test-repo"
BRANCH = "demo-review"
API = "https://api.github.com"

BASE_APP = '''"""A tiny payments module, for demoing a code review."""


def charge(amount: int, card: str) -> str:
    return f"charged {amount} to {card}"
'''

# The branch version adds a refund path that logs the full card number -- a plausible thing a
# reviewer would flag. The demo's *finding* is hand-written (see write_demo.py); this just needs
# to be a real diff on a real line.
BRANCH_APP = '''"""A tiny payments module, for demoing a code review."""

import logging

logger = logging.getLogger(__name__)


def charge(amount: int, card: str) -> str:
    return f"charged {amount} to {card}"


def refund(amount: int, card: str) -> str:
    logger.info("refunding %s to card %s", amount, card)
    return f"refunded {amount} to {card}"
'''


def _put_file(
    client: httpx.Client,
    headers: dict[str, str],
    path: str,
    content: str,
    message: str,
    *,
    branch: str,
    sha: str | None = None,
) -> dict[str, object]:
    import base64

    body: dict[str, object] = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha is not None:
        body["sha"] = sha
    r = client.put(f"{API}/repos/{REPO}/contents/{path}", headers=headers, json=body)
    r.raise_for_status()
    result: dict[str, object] = r.json()
    return result


def main() -> None:
    settings = Settings()
    headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with httpx.Client(timeout=30.0) as client:
        existing = client.get(
            f"{API}/repos/{REPO}/pulls?state=open&head=sahil7359:{BRANCH}", headers=headers
        ).json()
        if existing:
            print(f"open PR already exists: #{existing[0]['number']} {existing[0]['html_url']}")
            return

        # 1. Base file on main (initial commit if the repo is empty; a no-op update otherwise).
        head_main = client.get(f"{API}/repos/{REPO}/contents/app.py?ref=main", headers=headers)
        base_sha = head_main.json().get("sha") if head_main.status_code == 200 else None
        if base_sha is None:
            _put_file(client, headers, "app.py", BASE_APP, "add payments module", branch="main")
            print("seeded app.py on main")
        else:
            print("app.py already on main")

        # 2. Branch off main's current tip.
        main_ref = client.get(f"{API}/repos/{REPO}/git/ref/heads/main", headers=headers).json()
        main_sha = main_ref["object"]["sha"]
        make_branch = client.post(
            f"{API}/repos/{REPO}/git/refs",
            headers=headers,
            json={"ref": f"refs/heads/{BRANCH}", "sha": main_sha},
        )
        if make_branch.status_code not in (201, 422):  # 422 == branch already exists
            make_branch.raise_for_status()
        print(f"branch {BRANCH} ready")

        # 3. The change, on the branch.
        on_branch = client.get(f"{API}/repos/{REPO}/contents/app.py?ref={BRANCH}", headers=headers)
        _put_file(
            client,
            headers,
            "app.py",
            BRANCH_APP,
            "add refund path",
            branch=BRANCH,
            sha=on_branch.json()["sha"],
        )
        print("committed the change on the branch")

        # 4. Open the PR.
        pr = client.post(
            f"{API}/repos/{REPO}/pulls",
            headers=headers,
            json={
                "title": "Add refund path to payments",
                "head": BRANCH,
                "base": "main",
                "body": "Adds a refund function. Opened to demo Quorum's review + write path.",
            },
        )
        pr.raise_for_status()
        data = pr.json()
        print(f"opened PR #{data['number']}: {data['html_url']}")


if __name__ == "__main__":
    main()
