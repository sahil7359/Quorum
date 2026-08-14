# Trajectory eval golden set

Empty as of this commit -- no `QUORUM_GITHUB_TOKEN` was available when this harness was built,
so no fixtures could be assembled. `eval.trajectory.runner` reports `TODO: not yet run` rather
than a fabricated number when this directory is empty. That is a deliberate design choice, not
an oversight: see `HANDOFF.md`, Phase 7's "what Phase 6 starts with" section.

## Assembling fixtures

```bash
uv run python -m eval.trajectory.fetch_fixtures --repo pallets/click --count 5
```

Needs `QUORUM_GITHUB_TOKEN` (a fine-grained PAT, `public_repo` read scope) set in `.env` or the
environment. See `eval/trajectory/fetch_fixtures.py` for what it selects and why.

## Fixture shape

One JSON file per PR, named `<owner>-<repo>-<pr>.json`:

```json
{
  "repo": "owner/name",
  "pr_number": 123,
  "url": "https://github.com/owner/name/pull/123",
  "title": "...",
  "body": "...",
  "author": "...",
  "base_sha": "...",
  "head_sha": "...",
  "diff": "<unified diff text>",
  "changed_files": {"path/to/file.py": "<full file content at head_sha>"},
  "doc_corpus": [{"file_path": "CONTRIBUTING.md", "content": "..."}],
  "human_comments": [{"file_path": "path/to/file.py", "line": 42, "body": "...", "author": "..."}],
  "expected_specialists": ["correctness", "security"],
  "note": "why this PR was chosen, or anything unusual about the labelling"
}
```

`expected_specialists` is hand-labelled by whoever curates the fixture -- read the PR, decide
which reviewer roles it genuinely warranted, same trade-off `eval/retrieval/goldenset.py`
makes for relevance labels. `human_comments` should be the *substantive* review comments only;
drop "LGTM" and CI-bot noise, or routing/finding recall gets diluted by comments that were
never going to be matched by anything.
