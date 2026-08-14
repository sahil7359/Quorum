from __future__ import annotations

from app.infrastructure.mcp.diff_parser import parse_unified_diff

TWO_FILES = """diff --git a/app/auth/login.py b/app/auth/login.py
index 1111111..2222222 100644
--- a/app/auth/login.py
+++ b/app/auth/login.py
@@ -10,6 +10,8 @@ def authenticate(user, password):
     if not user:
         return None
-    return check(password)
+    token = issue_token(user)
+    return token
diff --git a/tests/test_login.py b/tests/test_login.py
new file mode 100644
--- /dev/null
+++ b/tests/test_login.py
@@ -0,0 +1,2 @@
+def test_authenticate():
+    assert authenticate(None, None) is None
"""


class TestStructure:
    def test_files_are_separated(self) -> None:
        diff = parse_unified_diff(TWO_FILES, max_lines=1000)
        assert diff.touched_paths == ("app/auth/login.py", "tests/test_login.py")

    def test_new_file_status_is_detected(self) -> None:
        diff = parse_unified_diff(TWO_FILES, max_lines=1000)
        assert diff.files[1].status == "added"

    def test_hunk_ranges_are_parsed(self) -> None:
        diff = parse_unified_diff(TWO_FILES, max_lines=1000)
        hunk = diff.files[0].hunks[0]
        assert (hunk.old_start, hunk.old_lines, hunk.new_start, hunk.new_lines) == (10, 6, 10, 8)

    def test_hunk_without_a_line_count_defaults_to_one(self) -> None:
        """``@@ -1 +1 @@`` is legal and means one line. Defaulting to 0 loses the hunk."""
        raw = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
        hunk = parse_unified_diff(raw, max_lines=100).files[0].hunks[0]
        assert (hunk.old_lines, hunk.new_lines) == (1, 1)

    def test_a_chunk_never_spans_files(self) -> None:
        diff = parse_unified_diff(TWO_FILES, max_lines=1000)
        for changed in diff.files:
            assert all(h.file_path == changed.file_path for h in changed.hunks)


class TestCounting:
    def test_file_headers_are_not_counted_as_changes(self) -> None:
        """``+++``/``---`` sit before the first ``@@`` and are outside every hunk."""
        diff = parse_unified_diff(TWO_FILES, max_lines=1000)
        login = diff.files[0]

        assert login.additions == 2
        assert login.deletions == 1

    def test_added_content_beginning_with_plus_signs_is_still_counted(self) -> None:
        """An added line whose *content* starts with ``++`` arrives as ``+++...``.

        This is not hypothetical for Quorum: the corpus is documentation, and a
        CONTRIBUTING.md explaining how to read a patch will contain exactly this. An earlier
        version of the parser excluded any line starting with ``+++`` as a "file header" and
        silently undercounted these. That exclusion was also dead code -- headers never reach
        the counter -- which is why breaking it deliberately did not turn any test red, and
        why this test now exists.
        """
        raw = (
            "diff --git a/docs/patch-guide.md b/docs/patch-guide.md\n"
            "--- a/docs/patch-guide.md\n"
            "+++ b/docs/patch-guide.md\n"
            "@@ -1,1 +1,3 @@\n"
            " # Patch guide\n"
            "+A unified diff header looks like:\n"
            "+++ b/example.py\n"
        )
        changed = parse_unified_diff(raw, max_lines=100).files[0]

        assert changed.additions == 2

    def test_removed_content_beginning_with_minus_signs_is_still_counted(self) -> None:
        raw = (
            "diff --git a/docs/patch-guide.md b/docs/patch-guide.md\n"
            "--- a/docs/patch-guide.md\n"
            "+++ b/docs/patch-guide.md\n"
            "@@ -1,3 +1,1 @@\n"
            " # Patch guide\n"
            "-An old line\n"
            "--- a/example.py\n"
        )
        changed = parse_unified_diff(raw, max_lines=100).files[0]

        assert changed.deletions == 2

    def test_added_lines_aggregate(self) -> None:
        assert parse_unified_diff(TWO_FILES, max_lines=1000).added_lines == 4

    def test_test_changes_are_detected(self) -> None:
        diff = parse_unified_diff(TWO_FILES, max_lines=1000)
        assert diff.has_test_changes
        assert diff.has_source_changes


class TestTruncation:
    def test_under_the_cap_is_untouched(self) -> None:
        diff = parse_unified_diff(TWO_FILES, max_lines=1000)
        assert not diff.truncated
        assert diff.truncated_at_line is None

    def test_over_the_cap_is_flagged(self) -> None:
        diff = parse_unified_diff(TWO_FILES, max_lines=6)
        assert diff.truncated
        assert diff.truncated_at_line == 6

    def test_truncation_still_yields_usable_files(self) -> None:
        """A truncated review is still a review -- it just has to say what it did not see."""
        diff = parse_unified_diff(TWO_FILES, max_lines=8)
        assert diff.files
        assert diff.files[0].file_path == "app/auth/login.py"


class TestEdgeCases:
    def test_empty_diff(self) -> None:
        diff = parse_unified_diff("", max_lines=100)
        assert diff.files == ()
        assert not diff.truncated

    def test_content_before_any_file_header_is_ignored(self) -> None:
        raw = "some preamble\nnoise\ndiff --git a/a.py b/a.py\n@@ -1 +1 @@\n+x\n"
        diff = parse_unified_diff(raw, max_lines=100)
        assert diff.touched_paths == ("a.py",)

    def test_a_line_that_looks_like_a_hunk_inside_content_does_not_split(self) -> None:
        """Diff content can legitimately contain text resembling a hunk header."""
        raw = (
            "diff --git a/a.md b/a.md\n"
            "--- a/a.md\n"
            "+++ b/a.md\n"
            "@@ -1,2 +1,3 @@\n"
            " intro\n"
            "+Explaining the @@ -1,2 +1,3 @@ syntax in prose.\n"
        )
        diff = parse_unified_diff(raw, max_lines=100)
        assert len(diff.files[0].hunks) == 1
        assert diff.files[0].additions == 1

    def test_renamed_path_uses_the_new_name(self) -> None:
        raw = "diff --git a/old.py b/new.py\n@@ -1 +1 @@\n+x\n"
        assert parse_unified_diff(raw, max_lines=100).touched_paths == ("new.py",)
