"""Black-box acceptance tests for the codex-agents-local startup hook.

These tests deliberately invoke the hook as Codex does: as a separate Python
process that receives one JSON object on standard input.
"""

import ast
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK = PROJECT_ROOT / "plugins" / "codex-agents-local" / "src" / "hook.py"
SOURCE_LINE = re.compile(r"^## Source: (.+)$", re.MULTILINE)


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        # /var may itself be a symlink on macOS; the hook reports resolved paths.
        self.base = Path(self.temporary_directory.name).resolve()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def make_directory(self, relative_path):
        path = self.base / relative_path
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_local(self, directory, contents, binary=False):
        path = directory / "AGENTS.local.md"
        if binary:
            path.write_bytes(contents)
        else:
            path.write_text(contents, encoding="utf-8")
        return path

    def mark_git_root(self, directory, as_file=False):
        git_path = directory / ".git"
        if as_file:
            git_path.write_text("gitdir: /not/a/repository\n", encoding="utf-8")
        else:
            git_path.mkdir()

    def run_hook(self, cwd, event_name="SessionStart", raw_input=None):
        if raw_input is None:
            raw_input = json.dumps(
                {"cwd": str(cwd), "hook_event_name": event_name}
            ).encode("utf-8")
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=raw_input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            timeout=5,
            check=False,
        )

    def context_for(self, cwd, event_name="SessionStart"):
        completed = self.run_hook(cwd, event_name)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
        self.assertEqual(completed.stderr, b"")
        self.assertNotEqual(completed.stdout, b"")
        response = json.loads(completed.stdout.decode("utf-8"))
        output = response["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], event_name)
        return output["additionalContext"]

    def source_paths(self, context):
        return [json.loads(line) for line in SOURCE_LINE.findall(context)]

    def test_root_and_nested_files_are_loaded_in_order_and_siblings_are_excluded(self):
        repository = self.make_directory("repository")
        self.mark_git_root(repository)
        root_file = self.write_local(repository, "root instructions")
        nested = self.make_directory("repository/project/component")
        nested_file = self.write_local(nested, "nested instructions")
        sibling = self.make_directory("repository/project/sibling")
        sibling_file = self.write_local(sibling, "sibling instructions")

        context = self.context_for(nested)

        self.assertEqual(self.source_paths(context), [str(root_file), str(nested_file)])
        self.assertLess(context.index("root instructions"), context.index("nested instructions"))
        self.assertNotIn(str(sibling_file), context)
        self.assertNotIn("sibling instructions", context)

    def test_files_above_the_git_root_are_excluded(self):
        outside = self.make_directory("outside")
        outside_file = self.write_local(outside, "outside instructions")
        repository = self.make_directory("outside/repository")
        self.mark_git_root(repository)
        root_file = self.write_local(repository, "repository instructions")
        cwd = self.make_directory("outside/repository/child")

        context = self.context_for(cwd)

        self.assertEqual(self.source_paths(context), [str(root_file)])
        self.assertNotIn(str(outside_file), context)
        self.assertNotIn("outside instructions", context)

    def test_without_a_git_root_only_the_current_directory_is_considered(self):
        parent = self.make_directory("not-a-repository")
        parent_file = self.write_local(parent, "parent instructions")
        cwd = self.make_directory("not-a-repository/child")
        cwd_file = self.write_local(cwd, "cwd instructions")

        context = self.context_for(cwd)

        self.assertEqual(self.source_paths(context), [str(cwd_file)])
        self.assertNotIn(str(parent_file), context)

    def test_symlinked_local_file_is_rejected(self):
        repository = self.make_directory("repository")
        self.mark_git_root(repository)
        root_file = self.write_local(repository, "real instructions")
        cwd = self.make_directory("repository/child")
        target = self.base / "outside-local.md"
        target.write_text("must not be read", encoding="utf-8")
        link = cwd / "AGENTS.local.md"
        try:
            link.symlink_to(target)
        except (NotImplementedError, OSError) as error:
            self.skipTest("symlinks unavailable: {0}".format(error))

        context = self.context_for(cwd)

        self.assertEqual(self.source_paths(context), [str(root_file)])
        self.assertNotIn(str(link), context)
        self.assertNotIn("must not be read", context)

    def test_linked_worktree_git_file_is_a_root_without_inspecting_its_target(self):
        main_repository = self.make_directory("main-repository")
        self.mark_git_root(main_repository)
        self.write_local(main_repository, "main repository instructions")
        worktree = self.make_directory("linked-worktree")
        (worktree / ".git").write_text(
            "gitdir: {0}\n".format(main_repository / ".git"), encoding="utf-8"
        )
        worktree_file = self.write_local(worktree, "worktree instructions")
        cwd = self.make_directory("linked-worktree/child")
        child_file = self.write_local(cwd, "child instructions")

        context = self.context_for(cwd)

        self.assertEqual(self.source_paths(context), [str(worktree_file), str(child_file)])
        self.assertNotIn("main repository instructions", context)

    def test_symlinked_git_marker_is_not_a_repository_boundary(self):
        repository = self.make_directory("repository")
        self.mark_git_root(repository)
        root_file = self.write_local(repository, "repository instructions")
        nested = self.make_directory("repository/nested")
        nested_file = self.write_local(nested, "nested instructions")
        marker_target = self.base / "external-git-marker"
        marker_target.write_text("not a git marker", encoding="utf-8")
        try:
            (nested / ".git").symlink_to(marker_target)
        except (NotImplementedError, OSError) as error:
            self.skipTest("symlinks unavailable: {0}".format(error))
        cwd = self.make_directory("repository/nested/child")

        context = self.context_for(cwd)

        self.assertEqual(self.source_paths(context), [str(root_file), str(nested_file)])

    def test_symlinked_cwd_is_resolved_before_discovery(self):
        repository = self.make_directory("repository")
        self.mark_git_root(repository)
        outside = self.make_directory("outside/child")
        self.write_local(outside, "resolved cwd instructions")
        link = repository / "linked"
        try:
            link.symlink_to(outside.parent, target_is_directory=True)
        except (NotImplementedError, OSError) as error:
            self.skipTest("symlinks unavailable: {0}".format(error))

        context = self.context_for(link / "child")

        self.assertEqual(
            self.source_paths(context),
            [str(outside / "AGENTS.local.md")],
        )
        self.assertIn("resolved cwd instructions", context)

    def test_only_the_first_sixteen_accepted_files_are_loaded(self):
        repository = self.make_directory("repository")
        self.mark_git_root(repository)
        expected = [self.write_local(repository, "file 0")]
        directory = repository
        for index in range(1, 18):
            directory = directory / "level-{0}".format(index)
            directory.mkdir()
            local_file = self.write_local(directory, "file {0}".format(index))
            if index < 16:
                expected.append(local_file)

        context = self.context_for(directory)

        self.assertEqual(self.source_paths(context), [str(path) for path in expected])
        self.assertNotIn("file 16", context)
        self.assertNotIn("file 17", context)

    def test_per_file_size_limit_accepts_16kib_and_rejects_larger_files(self):
        repository = self.make_directory("repository")
        self.mark_git_root(repository)
        root_file = self.write_local(repository, "a" * (16 * 1024))
        too_large_directory = self.make_directory("repository/too-large")
        too_large_file = self.write_local(too_large_directory, "b" * (16 * 1024 + 1))
        cwd = self.make_directory("repository/too-large/child")
        cwd_file = self.write_local(cwd, "final instructions")

        context = self.context_for(cwd)

        self.assertEqual(self.source_paths(context), [str(root_file), str(cwd_file)])
        self.assertNotIn(str(too_large_file), context)
        self.assertNotIn("b" * 32, context)

    def test_combined_size_limit_skips_overflow_and_continues_to_later_files(self):
        repository = self.make_directory("repository")
        self.mark_git_root(repository)
        first = self.write_local(repository, "a" * (12 * 1024))
        second_directory = self.make_directory("repository/second")
        second = self.write_local(second_directory, "b" * (12 * 1024))
        overflow_directory = self.make_directory("repository/second/overflow")
        overflow = self.write_local(overflow_directory, "c" * (10 * 1024))
        cwd = self.make_directory("repository/second/overflow/final")
        final = self.write_local(cwd, "d" * (8 * 1024))

        context = self.context_for(cwd)

        self.assertEqual(
            self.source_paths(context), [str(first), str(second), str(final)]
        )
        self.assertNotIn(str(overflow), context)
        self.assertNotIn("c" * 32, context)

    def test_malformed_json_is_safe_quiet_on_stdout_and_has_short_stderr(self):
        cwd = self.make_directory("directory")

        completed = self.run_hook(cwd, raw_input=b"{not valid json")

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertTrue(completed.stderr)
        self.assertLessEqual(len(completed.stderr), 200)

    def test_invalid_hook_event_is_safe_quiet_on_stdout_and_has_short_stderr(self):
        repository = self.make_directory("repository")
        self.mark_git_root(repository)
        self.write_local(repository, "must not be emitted")

        completed = self.run_hook(repository, event_name="PreToolUse")

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertTrue(completed.stderr)
        self.assertLessEqual(len(completed.stderr), 200)
        self.assertNotIn(b"must not be emitted", completed.stderr)

    def test_malformed_utf8_is_replaced(self):
        repository = self.make_directory("repository")
        self.mark_git_root(repository)
        self.write_local(repository, b"before\xffafter", binary=True)

        context = self.context_for(repository)

        self.assertIn("before\ufffdafter", context)

    def test_both_start_event_names_are_preserved_in_hook_output(self):
        repository = self.make_directory("repository")
        self.mark_git_root(repository)
        self.write_local(repository, "instructions")

        for event_name in ("SessionStart", "SubagentStart"):
            with self.subTest(event_name=event_name):
                context = self.context_for(repository, event_name)
                self.assertIn("instructions", context)

    def test_no_output_is_produced_when_no_usable_file_exists(self):
        repository = self.make_directory("repository")
        self.mark_git_root(repository)

        completed = self.run_hook(repository)

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr, b"")

    def test_hook_source_parses_using_python_39_grammar(self):
        source = HOOK.read_text(encoding="utf-8")
        try:
            ast.parse(source, filename=str(HOOK), feature_version=(3, 9))
        except TypeError:
            # Python 3.9 accepts an integer feature version rather than a tuple.
            ast.parse(source, filename=str(HOOK), feature_version=9)


if __name__ == "__main__":
    unittest.main()
