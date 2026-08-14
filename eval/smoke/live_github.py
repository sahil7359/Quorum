"""One real connection to the official GitHub MCP server, over real Docker + stdio.

This is a **smoke run, not an evaluation**, mirroring ``eval/smoke/live_review.py``. It exists
to close HANDOFF.md's risk #1: the four read paths on ``GitHubMcpClient`` were written against
GitHub's *documented* MCP tool schema and had never talked to the real
``ghcr.io/github/github-mcp-server``. Argument names (``pullNumber``, ``owner``) and response
shapes (``head.sha``, ``user.login``) were assumed. This script calls all four and prints what
came back, so a human (or the next Claude Code turn) can compare it against what
``github_client.py`` and ``tests/support/fake_github_mcp_server.py`` assume.

Needs ``QUORUM_GITHUB_TOKEN`` set and the Docker daemon up -- the client launches
``docker run -i --rm -e GITHUB_PERSONAL_ACCESS_TOKEN ghcr.io/github/github-mcp-server`` as a
subprocess and speaks MCP to it over its stdio.

Run: ``uv run python -m eval.smoke.live_github --repo pallets/click --pr 1``
(pick a real, merged PR number on a real public repo -- any small one will do)
"""

from __future__ import annotations

import argparse
import asyncio

from app.domain.entities import RepoRef
from app.infrastructure.config import Settings
from app.infrastructure.mcp.github_client import GitHubMcpClient
from tests.support.fakes import RecordingLogger


async def main(repo_spec: str, pr_number: int, file_path: str | None) -> None:
    settings = Settings()
    if not settings.github_token:
        raise SystemExit("QUORUM_GITHUB_TOKEN is not set -- add it to .env first")

    repo = RepoRef.parse(repo_spec)
    logger = RecordingLogger()

    print(f"connecting to {settings.github_mcp_command} {' '.join(settings.github_mcp_argv)}...")
    async with GitHubMcpClient(
        command=settings.github_mcp_command,
        args=settings.github_mcp_argv,
        token=settings.github_token,
        logger=logger,
    ) as client:
        print("connected. advertised-tool inspection passed (see log lines below).\n")

        print(f"--- get_pull_request({repo}, {pr_number}) ---")
        pr = await client.get_pull_request(repo, pr_number)
        print(f"  title      : {pr.title[:80]!r}")
        print(f"  author     : {pr.author!r}")
        print(f"  base_sha   : {pr.base_sha}")
        print(f"  head_sha   : {pr.head_sha}")
        print(f"  url        : {pr.url}")

        print(f"\n--- get_diff({repo}, {pr_number}) ---")
        diff = await client.get_diff(repo, pr_number, max_lines=5000)
        print(f"  files      : {len(diff.files)}")
        print(f"  truncated  : {diff.truncated}")
        if diff.files:
            first = diff.files[0]
            print(
                f"  first file : {first.file_path} ({first.status}, +{first.additions}/-{first.deletions})"
            )

        print(f"\n--- list_changed_files({repo}, {pr_number}) ---")
        changed = await client.list_changed_files(repo, pr_number)
        print(f"  count      : {len(changed)}")
        for f in changed[:5]:
            print(f"  - {f.file_path} ({f.status}, +{f.additions}/-{f.deletions})")

        target_path = file_path or (changed[0].file_path if changed else None)
        if target_path:
            print(f"\n--- get_file({repo}, {target_path!r}, ref={pr.head_sha}) ---")
            content = await client.get_file(repo, target_path, ref=pr.head_sha)
            print(f"  length     : {len(content)} chars")
            print(f"  looks like base64 (undecoded)?  {_looks_base64(content)}")
            print(f"  first 200  : {content[:200]!r}")
        else:
            print("\n--- get_file: skipped, no changed files to fetch ---")

    print("\n--- log events emitted ---")
    for line in logger.lines:
        print(f"  [{line.level}] {line.event} {line.fields}")

    print(
        "\nCompare the shapes above against app/infrastructure/mcp/github_client.py and "
        "tests/support/fake_github_mcp_server.py. If anything differs -- especially whether "
        "get_file returned raw text or base64 -- fix both in tandem and add a regression test."
    )


def _looks_base64(text: str) -> bool:
    """Rough heuristic: base64 has no whitespace and only [A-Za-z0-9+/=] characters."""
    if not text or any(c.isspace() for c in text[:500]):
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
    return all(c in allowed for c in text[:500])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify GitHubMcpClient against the real server")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", type=int, required=True, help="a real, merged PR number")
    parser.add_argument("--file", default=None, help="specific file path to fetch (optional)")
    args = parser.parse_args()
    asyncio.run(main(args.repo, args.pr, args.file))
