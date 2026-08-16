"""Post a real, approved review comment to a real PR, via the GitHub App.

The end-to-end write path, exercised live for the first time: mint an App installation token,
connect the MCP client with it, and post an inline review comment plus a summary comment on a
real pull request in the throwaway test repo. Everything the read path already proved (the MCP
transport, the allowlist) plus the two things only a write can prove -- App authentication, and
the write-authorisation guard actually letting an approved finding through.

**The finding is hand-constructed, not produced by a review run.** That is deliberate. This
script tests the *write path and its authorisation guard*, not "can a review of an empty test
repo produce a grounded finding" (it cannot -- there are no docs to cite). So it builds a
``Finding`` as if a review had produced one, and an ``Approval`` that authorises exactly it,
and confirms ``post_review_comment``'s guard (``approval.authorises(finding)``) lets it reach
GitHub. The finding targets a line that actually exists in the PR's diff (``app.py`` line 13,
the ``logger.info`` that logs a card number -- see ``scripts/seed_test_pr.py``).

Nothing here can post without an approval: the write methods take the ``Approval`` as an
argument, so an unauthorised write is not something to forget -- it is unexpressible.

Usage: ``uv run python -m scripts.write_demo``  (after ``scripts/seed_test_pr`` has opened a PR)
"""

from __future__ import annotations

import asyncio

import httpx

from app.domain.entities import Approval, Citation, Finding, RepoRef
from app.domain.values import (
    ApprovalAction,
    ChunkId,
    ChunkLocator,
    FindingId,
    RunId,
    Severity,
    SpecialistKind,
)
from app.infrastructure.config import Settings
from app.infrastructure.github_app import GitHubAppAuth
from app.infrastructure.mcp.github_client import GitHubMcpClient
from app.infrastructure.observability.logging import StructlogLogger, configure_structlog

REPO = RepoRef.parse("sahil7359/test-repo")
BRANCH = "demo-review"
TARGET_FILE = "app.py"
TARGET_LINE = 13  # the `logger.info(... card ...)` line in the seeded PR's diff


def a_finding_and_approval(run_id: RunId) -> tuple[Finding, Approval]:
    """A finding as a review would have produced it, plus the approval that authorises it."""
    locator = ChunkLocator(
        repo=str(REPO),
        commit_sha="demo",
        file_path="SECURITY.md",
        section_path="Logging > Never log full card numbers",
        start_offset=0,
        end_offset=80,
    )
    citation = Citation(chunk_id=ChunkId.derive(locator), locator=locator)
    finding = Finding(
        finding_id=FindingId.new(),
        specialist=SpecialistKind.SECURITY,
        severity=Severity.HIGH,
        confidence=0.9,
        title="Card number written to the log",
        body=(
            "`refund` logs the full card number at INFO. Logs are retained and shipped to "
            "places a card number should never reach. Log a token or the last four digits."
        ),
        citation=citation,
        file_path=TARGET_FILE,
        line_start=TARGET_LINE,
    )
    approval = Approval(
        run_id=run_id,
        finding_id=finding.finding_id,
        action=ApprovalAction.APPROVED,
        actor="sahil (demo)",
        payload_hash=finding.payload_hash,
    )
    return finding, approval


async def main() -> None:
    settings = Settings()
    configure_structlog(log_level=settings.log_level, log_format="console")
    logger = StructlogLogger()

    if not settings.github_app_configured:
        raise SystemExit(
            "GitHub App not configured. Set QUORUM_GITHUB_APP_ID, "
            "QUORUM_GITHUB_APP_INSTALLATION_ID and QUORUM_GITHUB_APP_PRIVATE_KEY_PATH."
        )

    auth = GitHubAppAuth.from_key_file(
        app_id=settings.github_app_id,
        installation_id=settings.github_app_installation_id,
        private_key_path=settings.github_app_private_key_path,
    )
    installation = await auth.installation_token()
    print(f"minted installation token (expires {installation.expires_at})")

    # Find the open PR the seed script created.
    async with httpx.AsyncClient(timeout=30.0) as http:
        prs = (
            await http.get(
                f"https://api.github.com/repos/{REPO}/pulls",
                params={"state": "open", "head": f"{REPO.owner}:{BRANCH}"},
                headers={"Authorization": f"Bearer {installation.token}"},
            )
        ).json()
    if not prs:
        raise SystemExit(
            f"no open PR on {REPO}#{BRANCH}. Run `uv run python -m scripts.seed_test_pr` first."
        )
    pr_number = int(prs[0]["number"])
    print(f"target PR: {REPO}#{pr_number} — {prs[0]['html_url']}")

    run_id = RunId.new()
    finding, approval = a_finding_and_approval(run_id)

    async with GitHubMcpClient(
        command=settings.github_mcp_command,
        args=settings.github_mcp_argv,
        token=installation.token,
        logger=logger,
    ) as client:
        comment_id = await client.post_review_comment(REPO, pr_number, finding, approval=approval)
        print(f"posted inline review comment: id={comment_id}")

        summary_id = await client.post_summary_comment(
            REPO,
            pr_number,
            body=(
                "**Quorum review** — 1 finding.\n\n"
                "- HIGH · security: Card number written to the log (`app.py:13`)\n\n"
                "_Posted by the Quorum GitHub App after human approval._"
            ),
            approvals=[approval],
        )
        print(f"posted summary comment: id={summary_id}")

    print(f"\ndone — see {prs[0]['html_url']}")


if __name__ == "__main__":
    asyncio.run(main())
