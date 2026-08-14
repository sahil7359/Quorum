"""Assemble trajectory-eval golden-set fixtures from real merged pull requests.

Needs ``QUORUM_GITHUB_TOKEN`` (a fine-grained PAT, ``public_repo`` read scope) in the
environment or ``.env``. Talks to the GitHub REST API directly -- this is offline tooling to
build eval data, not part of Quorum's own MCP-mediated GitHub access, so it deliberately does
not go through ``GitHubMcpClient`` or the tool allowlist; those exist to constrain what the
*agent* can do at review time, not this one-off data-collection script.

**Unverified.** Written against GitHub's documented REST schema with no token available to
run it against. The same caveat HANDOFF.md records for ``github_client.py``: the shapes are
assumed, not confirmed. Run it once a token exists and fix whatever the real API disagrees on.

Selection: scans the most recently merged pull requests for a repo, ranks them by the count of
substantive review comments (non-bot authors, body at least ``MIN_COMMENT_LENGTH`` chars), and
writes the top ``--count`` as fixtures. That is a heuristic for "carries a real review", not a
guarantee -- read what gets written before trusting it as a label source.

Every written fixture has an **empty** ``expected_specialists``. That field is a hand label
(see ``eval/trajectory/goldenset/README.md``) and this script cannot honestly write it: routing
precision/recall for an unlabelled fixture score 0.0 (not a vacuous 1.0, since the heuristic
floor always includes ``correctness``), so a forgotten label makes the number look artificially
*bad*, never inflated.

Run: ``uv run python -m eval.trajectory.fetch_fixtures --repo pallets/click --count 5``
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from app.infrastructure.config import Settings

API = "https://api.github.com"
GOLDENSET_DIR = Path(__file__).resolve().parent / "goldenset"
MIN_COMMENT_LENGTH = 20
"""Below this, a review comment is almost always "nit", "lgtm", "+1" -- noise that would
dilute finding precision/recall without representing a real review judgement."""
CANDIDATE_MULTIPLIER = 6
"""How many recently-merged PRs to scan per repo before ranking. Most merged PRs carry no
substantive review at all, so scanning only ``count`` of them would usually yield nothing."""
MAX_PAGES = 10
DOC_EXTENSIONS = (".md", ".rst")
MAX_DOC_FILES = 15
MAX_FILE_BYTES = 40_000
"""Skip anything bigger -- a vendored lockfile or generated doc would dominate the retrieval
corpus and blow the fixture size for no benefit to the eval."""


class GitHubFetchError(RuntimeError):
    pass


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _get_json(client: httpx.AsyncClient, url: str, **params: Any) -> Any:
    response = await client.get(url, params=params)
    if response.status_code != 200:
        raise GitHubFetchError(f"GET {url} -> {response.status_code}: {response.text[:200]}")
    return response.json()


async def _get_diff(client: httpx.AsyncClient, owner: str, repo: str, number: int) -> str:
    url = f"{API}/repos/{owner}/{repo}/pulls/{number}"
    response = await client.get(url, headers={"Accept": "application/vnd.github.v3.diff"})
    if response.status_code != 200:
        raise GitHubFetchError(f"GET {url} (diff) -> {response.status_code}")
    return response.text


def _is_substantive(comment: dict[str, Any]) -> bool:
    return (
        comment.get("user", {}).get("type") != "Bot"
        and len(comment.get("body", "")) >= MIN_COMMENT_LENGTH
    )


async def _find_candidates(
    client: httpx.AsyncClient, owner: str, repo: str, *, count: int
) -> list[dict[str, Any]]:
    """Recently merged PRs, ranked by substantive review-comment count, most first."""
    scanned: list[dict[str, Any]] = []
    page = 1
    while len(scanned) < count * CANDIDATE_MULTIPLIER and page <= MAX_PAGES:
        prs = await _get_json(
            client,
            f"{API}/repos/{owner}/{repo}/pulls",
            state="closed",
            sort="updated",
            direction="desc",
            per_page=50,
            page=page,
        )
        if not prs:
            break
        scanned.extend(pr for pr in prs if pr.get("merged_at"))
        page += 1

    scored: list[tuple[int, dict[str, Any]]] = []
    for pr in scanned[: count * CANDIDATE_MULTIPLIER]:
        try:
            comments = await _get_json(
                client, f"{API}/repos/{owner}/{repo}/pulls/{pr['number']}/comments", per_page=100
            )
        except GitHubFetchError:
            continue
        substantive = sum(1 for c in comments if _is_substantive(c))
        if substantive:
            scored.append((substantive, pr))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [pr for _, pr in scored[:count]]


async def _fetch_text_file(
    client: httpx.AsyncClient, owner: str, repo: str, path: str, sha: str
) -> str | None:
    try:
        content = await _get_json(client, f"{API}/repos/{owner}/{repo}/contents/{path}", ref=sha)
    except GitHubFetchError:
        return None
    if content.get("encoding") != "base64":
        return None
    raw = base64.b64decode(content["content"])
    if len(raw) > MAX_FILE_BYTES:
        return None
    return raw.decode("utf-8", errors="replace")


async def _doc_corpus(
    client: httpx.AsyncClient, owner: str, repo: str, sha: str
) -> list[dict[str, str]]:
    tree = await _get_json(client, f"{API}/repos/{owner}/{repo}/git/trees/{sha}", recursive="1")
    doc_paths = [
        item["path"]
        for item in tree.get("tree", [])
        if item.get("type") == "blob" and item["path"].lower().endswith(DOC_EXTENSIONS)
    ][:MAX_DOC_FILES]

    docs: list[dict[str, str]] = []
    for path in doc_paths:
        text = await _fetch_text_file(client, owner, repo, path, sha)
        if text is not None:
            docs.append({"file_path": path, "content": text})
    return docs


async def _changed_files(
    client: httpx.AsyncClient, owner: str, repo: str, number: int, sha: str
) -> dict[str, str]:
    files = await _get_json(
        client, f"{API}/repos/{owner}/{repo}/pulls/{number}/files", per_page=100
    )
    out: dict[str, str] = {}
    for entry in files:
        path = entry["filename"]
        text = await _fetch_text_file(client, owner, repo, path, sha)
        if text is not None:
            out[path] = text
    return out


async def fetch_repo(client: httpx.AsyncClient, owner: str, repo: str, *, count: int) -> list[Path]:
    candidates = await _find_candidates(client, owner, repo, count=count)
    written: list[Path] = []

    for pr in candidates:
        number = pr["number"]
        try:
            detail = await _get_json(client, f"{API}/repos/{owner}/{repo}/pulls/{number}")
            base_sha = detail["base"]["sha"]
            head_sha = detail["head"]["sha"]

            diff = await _get_diff(client, owner, repo, number)
            comments = await _get_json(
                client, f"{API}/repos/{owner}/{repo}/pulls/{number}/comments", per_page=100
            )
            human_comments = [
                {
                    "file_path": c["path"],
                    "line": c.get("line") or c.get("original_line"),
                    "body": c["body"],
                    "author": c.get("user", {}).get("login", ""),
                }
                for c in comments
                if _is_substantive(c)
            ]
            changed_files = await _changed_files(client, owner, repo, number, head_sha)
            doc_corpus = await _doc_corpus(client, owner, repo, base_sha)
        except GitHubFetchError as exc:
            # why: scanning dozens of PRs sequentially means one transient GitHub API error
            #      (a 401/5xx on a single request, seen live on an otherwise-healthy token
            #      with rate limit to spare) must not discard every fixture already written
            #      in this run. Skipping one candidate and moving on mirrors the same
            #      one-broken-case-must-not-kill-the-run rule eval/trajectory/runner.py uses.
            print(f"  skipping PR #{number}: {exc}")
            continue

        fixture = {
            "repo": f"{owner}/{repo}",
            "pr_number": number,
            "url": detail.get("html_url", ""),
            "title": detail.get("title", ""),
            "body": detail.get("body") or "",
            "author": (detail.get("user") or {}).get("login", ""),
            "base_sha": base_sha,
            "head_sha": head_sha,
            "diff": diff,
            "changed_files": changed_files,
            "doc_corpus": doc_corpus,
            "human_comments": human_comments,
            "expected_specialists": [],
            "note": "expected_specialists is unlabelled -- fill in by hand before this fixture "
            "counts toward routing accuracy",
        }

        GOLDENSET_DIR.mkdir(parents=True, exist_ok=True)
        path = GOLDENSET_DIR / f"{owner}-{repo}-{number}.json"
        path.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
        written.append(path)
        print(
            f"  wrote {path.name}  ({len(human_comments)} human comments, "
            f"{len(doc_corpus)} docs, {len(changed_files)} changed files)"
        )

    return written


async def main_async(args: argparse.Namespace) -> int:
    # why: Settings() reads QUORUM_GITHUB_TOKEN from .env the same way the rest of the app
    #      does. A raw os.environ.get() here would silently miss a token that lives only in
    #      .env and never got exported to the shell -- which is the common case in dev.
    token = Settings().github_token
    if not token:
        print("QUORUM_GITHUB_TOKEN is not set. Add it to .env or the environment.", file=sys.stderr)
        return 2

    async with httpx.AsyncClient(headers=_headers(token), timeout=30.0) as client:
        for repo_spec in args.repo:
            owner, _, name = repo_spec.partition("/")
            if not name:
                print(f"skipping {repo_spec!r}: expected owner/name", file=sys.stderr)
                continue
            print(f"{repo_spec}: scanning merged PRs...")
            written = await fetch_repo(client, owner, name, count=args.count)
            print(f"{repo_spec}: wrote {len(written)} fixture(s)\n")

    print(
        "NOTE: every fixture's expected_specialists is empty. Hand-label it -- read the PR, "
        "decide which reviewer roles it genuinely warranted -- before running the eval. "
        "Left unlabelled, routing precision and recall score 0.0 for that fixture (the "
        "heuristic floor always includes 'correctness', so an empty expectation can never "
        "match), which understates rather than inflates the number."
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Assemble trajectory-eval golden-set fixtures")
    parser.add_argument("--repo", action="append", required=True, help="owner/name, repeatable")
    parser.add_argument("--count", type=int, default=5, help="fixtures to write per repo")
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
