"""Tests that keep the documentation honest.

docs/privileges.md exists so somebody can decide whether to give this tool
root on their NAS. It makes two promises that are only worth anything if they
are actually true:

  1. every command the tool can run is listed there
  2. only host.py can execute anything

Documentation drifts. Someone adds a command, the table does not get updated,
and a page that people are relying on to make a security decision becomes
quietly wrong. These tests fail the build when that happens.

The link and secret checks are here for the same reason: they are things
that get verified by hand once and then silently rot.

Run with:  python3 tests/test_docs.py
"""

import ast
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(ROOT, "drive_anchor")
PRIVILEGES = os.path.join(ROOT, "docs", "privileges.md")

# `sh` is not documented as a command in its own right, deliberately. It is
# the wrapper used to read /proc/mounts and to test whether a directory is
# empty, and privileges.md explains that under its verification section
# rather than listing a shell as though it were a capability.
WRAPPER = "sh"


def python_files():
    for name in sorted(os.listdir(PACKAGE)):
        if name.endswith(".py"):
            yield os.path.join(PACKAGE, name)


def _module_constants(tree):
    """Module-level string assignments, so SYNOPKG resolves to its path."""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value.value
    return out


def _first_word(text):
    text = text.strip()
    return text.split()[0] if text else ""


def executables_invoked():
    """Every program the package can actually run, by basename.

    Read out of the syntax tree rather than by grepping, so a command hidden
    behind a constant or spread across lines is still found.
    """
    found = set()
    for path in python_files():
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        constants = _module_constants(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = None
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name not in ("run_on_host", "_sh") or not node.args:
                continue

            first = node.args[0]

            # run_on_host(["mount", "--bind", ...])
            if isinstance(first, ast.List) and first.elts:
                head = first.elts[0]
                if isinstance(head, ast.Constant) and isinstance(head.value, str):
                    found.add(os.path.basename(head.value))
                elif isinstance(head, ast.Name) and head.id in constants:
                    found.add(os.path.basename(constants[head.id]))

            # _sh("cat /proc/mounts")  and  _sh(f"ls -A {path} ...")
            elif isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.add(_first_word(first.value))
            elif isinstance(first, ast.JoinedStr):
                for part in first.values:
                    if isinstance(part, ast.Constant) and part.value.strip():
                        found.add(_first_word(part.value))
                        break
    return found


def executables_documented():
    """Program names from the command table in docs/privileges.md."""
    with open(PRIVILEGES, "r", encoding="utf-8") as fh:
        text = fh.read()
    documented = set()
    for line in text.splitlines():
        if not line.startswith("| `"):
            continue
        match = re.match(r"^\|\s*`([^`]+)`", line)
        if match:
            # "synopkg stop\|start <pkg>" -> synopkg
            documented.add(os.path.basename(_first_word(match.group(1))))
    return documented


class DocumentedCommands(unittest.TestCase):
    """The promise: every command it can run is on that page."""

    def test_the_table_was_found_at_all(self):
        """Guards the other tests: a renamed heading or reformatted table
        would otherwise make them pass by comparing two empty sets."""
        self.assertGreaterEqual(len(executables_documented()), 8)

    def test_every_command_the_code_runs_is_documented(self):
        undocumented = executables_invoked() - executables_documented() - {WRAPPER}
        self.assertEqual(
            undocumented, set(),
            f"these run as root but are missing from docs/privileges.md: "
            f"{sorted(undocumented)}. Someone deciding whether to trust this "
            f"tool would be reading an incomplete list.")

    def test_no_stale_entries_in_the_table(self):
        """A command listed but no longer used is its own problem: it makes
        the tool look like it does more than it does, and it suggests the
        page is not maintained."""
        stale = executables_documented() - executables_invoked()
        self.assertEqual(
            stale, set(),
            f"documented in docs/privileges.md but never invoked: "
            f"{sorted(stale)}")


class OnlyHostShellsOut(unittest.TestCase):
    """The promise that makes the audit tractable: one file to read."""

    def test_subprocess_is_confined_to_host_py(self):
        offenders = []
        for path in python_files():
            with open(path, "r", encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            uses = any(
                (isinstance(n, ast.Import)
                 and any(a.name == "subprocess" for a in n.names))
                or (isinstance(n, ast.ImportFrom) and n.module == "subprocess")
                for n in ast.walk(tree))
            if uses and os.path.basename(path) != "host.py":
                offenders.append(os.path.basename(path))
        self.assertEqual(
            offenders, [],
            f"{offenders} import subprocess. README.md and "
            f"docs/privileges.md both tell readers that host.py is the only "
            f"file that can execute anything, and that it is the one file "
            f"worth auditing. That is now untrue.")

    def test_host_py_really_does_use_subprocess(self):
        """Otherwise the test above passes for the wrong reason."""
        with open(os.path.join(PACKAGE, "host.py"), "r", encoding="utf-8") as fh:
            self.assertIn("import subprocess", fh.read())


class MarkdownLinks(unittest.TestCase):
    """Relative links rot quietly; a 404 in the security page is a bad look."""

    def _markdown_files(self):
        for path in (os.path.join(ROOT, "README.md"),
                     os.path.join(ROOT, "docs", "privileges.md"),
                     os.path.join(ROOT, "docs", "drives.md")):
            yield path

    def test_relative_links_point_at_files_that_exist(self):
        broken = []
        for path in self._markdown_files():
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
            for target in re.findall(r"\]\(([^)]+)\)", text):
                if target.startswith(("http://", "https://", "#", "mailto:")):
                    continue
                target = target.split("#")[0]
                if not target:
                    continue
                resolved = os.path.normpath(
                    os.path.join(os.path.dirname(path), target))
                if not os.path.exists(resolved):
                    broken.append(f"{os.path.basename(path)} -> {target}")
        self.assertEqual(broken, [], f"broken relative links: {broken}")


class NoPrivateDetails(unittest.TestCase):
    """The repo is public. These are the things that must never be in it.

    Checked here rather than by hand because a hand check happens once and
    the repo keeps changing.
    """

    PATTERNS = {
        "private IP address": r"\b192\.168\.\d{1,3}\.\d{1,3}\b",
        "MAC address": r"\bACEBE6[0-9A-F]{6}\b",
        "NAS login": r"\bzap@",
        "SSH key path": r"id_" + "ed25519",
        "a filesystem UUID": r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                             r"[0-9a-f]{4}-[0-9a-f]{12}\b",
    }

    # UUIDs that appear on purpose, in the example config and the docs.
    # Listed explicitly rather than guessed at with a "looks fake enough"
    # heuristic, so adding a new example is a deliberate act rather than
    # something that slips past a pattern.
    EXAMPLE_UUIDS = {
        "00000000-0000-0000-0000-000000000000",
        "11111111-1111-1111-1111-111111111111",
        "a1b2c3d4-5678-90ab-cdef-1234567890ab",
    }

    def _tracked_text_files(self):
        for base, dirs, names in os.walk(ROOT):
            dirs[:] = [d for d in dirs
                       if d not in (".git", "__pycache__", ".venv", "state")]
            for name in names:
                # This file is skipped for the obvious reason: it has to
                # contain the very patterns it is searching for.
                if name == os.path.basename(__file__):
                    continue
                if name.endswith((".py", ".md", ".yaml", ".yml", ".sh")) \
                        or name in ("Dockerfile", "drive-anchor"):
                    yield os.path.join(base, name)

    def test_no_private_details_anywhere(self):
        hits = []
        for path in self._tracked_text_files():
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            for label, pattern in self.PATTERNS.items():
                for match in re.findall(pattern, text):
                    if match in self.EXAMPLE_UUIDS:
                        continue
                    hits.append(f"{os.path.relpath(path, ROOT)}: {label} "
                                f"({match})")
        self.assertEqual(hits, [], f"private details in a public repo: {hits}")

    def test_the_scan_actually_catches_something(self):
        """Guards the test above. A pattern that stopped matching, or a file
        walk that stopped finding files, would otherwise look like success."""
        sample = ("nas at 192.168.1.50, key id_" + "ed25519, "
                  "uuid 1b933f7a-2753-486e-b392-62985cf45b8d")
        matched = [label for label, pattern in self.PATTERNS.items()
                   if re.search(pattern, sample)]
        self.assertEqual(sorted(matched),
                         ["SSH key path", "a filesystem UUID",
                          "private IP address"])
        self.assertGreater(len(list(self._tracked_text_files())), 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
