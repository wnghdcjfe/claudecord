import os
import unittest
from pathlib import Path
from unittest import mock

from src.errors import MAX_ERROR_CHARS, redact_paths, safe_error_text

# Path.home() reads HOME on POSIX, so patching it pins the "running user's own
# home" rule to a fixed value instead of whichever account runs the suite.
FAKE_HOME = "/Users/testuser"


class RedactHomeDirectoryTests(unittest.TestCase):
    def test_the_running_users_home_collapses_to_a_tilde(self):
        with mock.patch.dict(os.environ, {"HOME": FAKE_HOME}, clear=False):
            redacted = redact_paths(f"{FAKE_HOME}/Desktop/develop/app.py")

        self.assertEqual(redacted, "~/Desktop/develop/app.py")
        self.assertNotIn("testuser", redacted)

    def test_a_bare_home_with_no_trailing_path_collapses_too(self):
        with mock.patch.dict(os.environ, {"HOME": FAKE_HOME}, clear=False):
            self.assertEqual(redact_paths(f"cwd: {FAKE_HOME}"), "cwd: ~")

    def test_any_macos_home_collapses_not_just_the_running_users(self):
        # The path in an exception need not belong to whoever started the bot
        # -- an @workdir pointing at another account still names that account.
        with mock.patch.dict(os.environ, {"HOME": FAKE_HOME}, clear=False):
            redacted = redact_paths("/Users/someone-else/private/notes.md")

        self.assertNotIn("someone-else", redacted)
        self.assertEqual(redacted, "~/private/notes.md")

    def test_a_linux_home_collapses_the_same_way(self):
        with mock.patch.dict(os.environ, {"HOME": FAKE_HOME}, clear=False):
            redacted = redact_paths("/home/jane/projects/api/main.py")

        self.assertNotIn("jane", redacted)
        self.assertEqual(redacted, "~/projects/api/main.py")

    def test_a_windows_home_collapses_and_keeps_backslash_separators(self):
        with mock.patch.dict(os.environ, {"HOME": FAKE_HOME}, clear=False):
            redacted = redact_paths(r"C:\Users\jane\Desktop\app.py")

        self.assertNotIn("jane", redacted)
        self.assertEqual(redacted, r"~\Desktop\app.py")

    def test_an_already_redacted_path_is_not_clipped_a_second_time(self):
        # Regression guard for the lookbehind in _ABSOLUTE_PATH: once a home
        # has become "~/...", the tail must survive intact rather than being
        # reduced to ".../x" by the generic absolute-path rule.
        with mock.patch.dict(os.environ, {"HOME": FAKE_HOME}, clear=False):
            self.assertEqual(redact_paths("~/Desktop/develop/app.py"), "~/Desktop/develop/app.py")

    def test_a_bare_users_directory_names_nobody_and_is_left_alone(self):
        with mock.patch.dict(os.environ, {"HOME": FAKE_HOME}, clear=False):
            self.assertEqual(redact_paths("/tmp"), "/tmp")


class RedactAbsolutePathTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"HOME": FAKE_HOME}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_non_home_absolute_path_keeps_only_its_last_segment(self):
        redacted = redact_paths("/opt/secret-client/build/out.txt")

        self.assertEqual(redacted, ".../out.txt")
        self.assertNotIn("secret-client", redacted)

    def test_the_directories_above_the_file_are_dropped_mid_sentence(self):
        redacted = redact_paths("could not read /etc/nginx/nginx.conf, giving up")

        self.assertEqual(redacted, "could not read .../nginx.conf, giving up")
        self.assertNotIn("/etc", redacted)

    def test_several_paths_in_one_message_are_all_redacted(self):
        redacted = redact_paths(f"{FAKE_HOME}/a/b.py -> /opt/deploy/c/d.py")

        self.assertEqual(redacted, "~/a/b.py -> .../d.py")
        self.assertNotIn("testuser", redacted)
        self.assertNotIn("deploy", redacted)


class NonPathTextIsPreservedTests(unittest.TestCase):
    """A slash is not a path. Mangling ordinary prose would make every error
    message worse in exchange for redacting nothing."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"HOME": FAKE_HOME}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_prose_with_no_slashes_is_returned_unchanged(self):
        text = "예상치 못한 응답 형식입니다. 다시 시도해 주세요."
        self.assertEqual(redact_paths(text), text)

    def test_a_fraction_survives(self):
        self.assertEqual(redact_paths("ratio 3/4"), "ratio 3/4")

    def test_a_relative_path_survives(self):
        self.assertEqual(redact_paths("a/b/c"), "a/b/c")

    def test_a_date_survives(self):
        self.assertEqual(redact_paths("expired 2024/01/02"), "expired 2024/01/02")

    def test_an_and_or_construction_survives(self):
        self.assertEqual(redact_paths("read and/or write"), "read and/or write")

    def test_spaced_slashes_survive(self):
        self.assertEqual(redact_paths("input / output / error"), "input / output / error")

    def test_empty_text_is_returned_unchanged(self):
        self.assertEqual(redact_paths(""), "")


class SafeErrorTextTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"HOME": FAKE_HOME}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_exception_type_name_is_kept(self):
        # The type is the part that tells the operator what went wrong, and it
        # can never contain user data.
        self.assertEqual(
            safe_error_text(ValueError("작업 디렉터리가 없습니다")),
            "ValueError: 작업 디렉터리가 없습니다",
        )

    def test_a_real_oserror_has_its_path_redacted(self):
        try:
            Path(f"{FAKE_HOME}/no-such-dir/missing.txt").read_text(encoding="utf-8")
        except OSError as exc:
            raw = str(exc)
            rendered = safe_error_text(exc)
        else:  # pragma: no cover - the path is guaranteed absent
            self.fail("expected the open() to fail")

        self.assertTrue(rendered.startswith("FileNotFoundError:"))
        self.assertNotIn("testuser", rendered)
        self.assertNotIn(FAKE_HOME, rendered)
        # The tail comes from the exception's own text, not from the literal
        # typed above: str(OSError) renders the filename with repr(), so on
        # Windows the same path arrives as `\\Users\\testuser\\...` -- flipped
        # separators, and doubled by the escaping.
        self.assertIn("~" + raw.split("testuser", 1)[1], rendered)

    def test_an_exception_with_no_message_renders_as_the_bare_type_name(self):
        self.assertEqual(safe_error_text(TimeoutError()), "TimeoutError")
        self.assertEqual(safe_error_text(ValueError("   ")), "ValueError")

    def test_a_long_message_is_truncated_to_the_limit_with_an_ellipsis(self):
        rendered = safe_error_text(RuntimeError("x" * 5000))

        self.assertEqual(len(rendered), MAX_ERROR_CHARS)
        self.assertTrue(rendered.endswith("…"))
        self.assertTrue(rendered.startswith("RuntimeError: "))

    def test_the_truncation_limit_is_configurable(self):
        rendered = safe_error_text(RuntimeError("abcdefghij"), limit=20)

        self.assertEqual(len(rendered), 20)
        self.assertEqual(rendered, "RuntimeError: abcde…")

    def test_a_message_that_fits_is_left_whole(self):
        rendered = safe_error_text(RuntimeError("짧은 오류"))

        self.assertEqual(rendered, "RuntimeError: 짧은 오류")
        self.assertNotIn("…", rendered)

    def test_a_stack_trace_shaped_message_is_redacted_before_truncation(self):
        # Redaction must not be defeated by the message being long: the path
        # sits inside the part that survives truncation.
        rendered = safe_error_text(
            RuntimeError(f"failed at {FAKE_HOME}/proj/run.py " + "detail " * 200)
        )

        self.assertNotIn("testuser", rendered)
        self.assertIn("~/proj/run.py", rendered)


class BareHomeDirectoryTests(unittest.TestCase):
    """A home directory with nothing after it used to leak the account name.

    The home pattern once required a trailing separator, so "/Users/jane" fell
    through to the generic last-segment rule -- whose "last segment" *is* the
    account name, publishing it as ".../jane". Only reachable for a home other
    than the running user's own (that one is substituted first), but
    `@/Users/someone` as a workdir produces exactly this shape.
    """

    def test_a_bare_home_directory_does_not_leak_the_account(self):
        with mock.patch.dict(os.environ, {"HOME": FAKE_HOME}, clear=False):
            redacted = redact_paths("작업 디렉터리가 없습니다: /Users/jane")

        self.assertNotIn("jane", redacted)
        self.assertEqual("작업 디렉터리가 없습니다: ~", redacted)

    def test_a_bare_home_directory_of_another_shape(self):
        with mock.patch.dict(os.environ, {"HOME": FAKE_HOME}, clear=False):
            self.assertNotIn("bob", redact_paths("/home/bob"))


class UrlPreservationTests(unittest.TestCase):
    """URLs are not filesystem paths and are now returned untouched.

    They used to be mangled -- `https://example.com/a/b` came back as
    `https:/.../b` -- which leaked nothing but destroyed the one part of an
    error message a user could act on.
    """

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"HOME": FAKE_HOME}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_https_url_survives_intact(self):
        text = "요청이 거부되었습니다: https://discord.com/developers/docs/topics/rate-limits"
        self.assertEqual(redact_paths(text), text)

    def test_a_non_http_scheme_survives_too(self):
        self.assertEqual(redact_paths("s3://bucket/key/name"), "s3://bucket/key/name")

    def test_a_url_and_a_real_path_in_one_message_are_handled_separately(self):
        redacted = redact_paths(f"see https://example.com/a/b after {FAKE_HOME}/x/y.py")

        self.assertIn("https://example.com/a/b", redacted)
        self.assertIn("~/x/y.py", redacted)
        self.assertNotIn("testuser", redacted)


class IdempotenceTests(unittest.TestCase):
    """main.py redacts what it sends, and orchestrator.py already redacted the
    message it raised -- so redaction runs twice on the same text and must not
    degrade it the second time."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"HOME": FAKE_HOME}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_redacting_twice_gives_the_same_answer_as_once(self):
        for text in (
            f"{FAKE_HOME}/Desktop/app.py",
            "/Users/jane/private/notes.md",
            "/opt/deploy/build/out.txt",
            "작업 디렉터리가 없습니다: /Users/jane",
            r"C:\Users\jane\Desktop\app.py",
            "see https://example.com/a/b",
            "ratio 3/4",
        ):
            once = redact_paths(text)
            self.assertEqual(redact_paths(once), once, f"not idempotent: {text!r}")


class TruncationBoundaryTests(unittest.TestCase):
    def test_a_non_positive_limit_yields_nothing_rather_than_almost_everything(self):
        # `rendered[: limit - 1]` with limit=0 is `rendered[:-1]`, which used
        # to hand back all but the last character of a message the caller had
        # asked to suppress entirely.
        for limit in (0, -1, -400):
            self.assertEqual(safe_error_text(RuntimeError("abcdefghij"), limit=limit), "")

    def test_a_limit_of_one_is_just_the_ellipsis(self):
        self.assertEqual(safe_error_text(RuntimeError("abcdefghij"), limit=1), "…")

    def test_the_result_never_exceeds_the_limit(self):
        for limit in range(1, 40):
            rendered = safe_error_text(RuntimeError("x" * 500), limit=limit)
            self.assertLessEqual(len(rendered), limit)


class UrlsThatAreReallyPathsTests(unittest.TestCase):
    """The URL carve-out must not become a way to smuggle a path through.

    URLs are returned verbatim so an actionable endpoint survives redaction,
    but two shapes are paths wearing a scheme: `file://` is an absolute path
    outright, and any URL can spell one inside its query string.
    """

    def test_a_file_url_is_redacted_like_the_path_it_is(self):
        with mock.patch.dict(os.environ, {"HOME": FAKE_HOME}, clear=False):
            redacted = redact_paths("could not open file:///Users/jane/Desktop/secret.md")

        self.assertNotIn("jane", redacted)
        self.assertEqual("could not open file://~/Desktop/secret.md", redacted)

    def test_a_file_url_is_recognised_whatever_its_case(self):
        with mock.patch.dict(os.environ, {"HOME": FAKE_HOME}, clear=False):
            self.assertNotIn("jane", redact_paths("FILE:///Users/jane/x.md"))

    def test_a_path_in_a_url_query_string_is_redacted(self):
        with mock.patch.dict(os.environ, {"HOME": FAKE_HOME}, clear=False):
            redacted = redact_paths("GET http://localhost/x?path=/Users/jane/secret")

        self.assertNotIn("jane", redacted)
        # The endpoint itself is still readable -- that is the whole point of
        # exempting URLs.
        self.assertIn("http://localhost/x", redacted)

    def test_a_path_in_a_url_fragment_is_redacted(self):
        with mock.patch.dict(os.environ, {"HOME": FAKE_HOME}, clear=False):
            self.assertNotIn(
                "jane", redact_paths("see https://api.example.com/v1#/Users/jane/frag")
            )

    def test_redacting_a_file_url_twice_changes_nothing(self):
        # orchestrator.py redacts when it raises and main.py redacts again
        # when it sends, so a second pass has to be a no-op.
        with mock.patch.dict(os.environ, {"HOME": FAKE_HOME}, clear=False):
            once = redact_paths("could not open file:///Users/jane/Desktop/secret.md")
            self.assertEqual(once, redact_paths(once))


if __name__ == "__main__":
    unittest.main()
