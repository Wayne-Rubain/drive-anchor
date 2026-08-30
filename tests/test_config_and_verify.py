"""Tests for the logic that does not need a NAS to exercise.

Deliberately focused on the rules that keep the tool safe rather than on
coverage for its own sake. Each test below corresponds to a way this could
quietly do the wrong thing.

Run with:  python3 -m pytest tests/ -v
       or: python3 tests/test_config_and_verify.py
"""

import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drive_anchor import config as config_mod          # noqa: E402
from drive_anchor.config import ConfigError            # noqa: E402
from drive_anchor.verify import (                      # noqa: E402
    DEVICE_ABSENT, EMPTY_STUB, NOT_MOUNTED, Problem)
from drive_anchor.config import Drive                  # noqa: E402


def write_config(text):
    fh = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                     encoding="utf-8")
    fh.write(text)
    fh.close()
    return fh.name


VALID = """
dry_run: true
drives:
  - name: media
    uuid: "aaaa-1111"
    path: /volume1/USB_Media
    share: USB_Media
"""


class ConfigValidation(unittest.TestCase):
    """A malformed config must stop the run, never be silently skipped.

    Skipping a drive means a detach reports success while that drive is
    still mounted and spinning -- the worst outcome this tool has.
    """

    def test_valid_config_loads(self):
        cfg = config_mod.load(write_config(VALID))
        self.assertEqual(len(cfg.drives), 1)
        self.assertEqual(cfg.drives[0].name, "media")
        self.assertEqual(cfg.drives[0].path, "/volume1/USB_Media")

    def test_dry_run_defaults_to_true_when_absent(self):
        cfg = config_mod.load(write_config(
            'drives:\n  - uuid: "a"\n    path: /volume1/x\n'))
        self.assertTrue(cfg.dry_run, "dry_run must default to true")

    def test_missing_uuid_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            config_mod.load(write_config(
                'drives:\n  - name: x\n    path: /volume1/x\n'))
        self.assertIn("uuid", str(ctx.exception))

    def test_missing_path_is_rejected(self):
        with self.assertRaises(ConfigError):
            config_mod.load(write_config('drives:\n  - uuid: "a"\n'))

    def test_relative_path_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            config_mod.load(write_config(
                'drives:\n  - uuid: "a"\n    path: volume1/x\n'))
        self.assertIn("absolute", str(ctx.exception))

    def test_duplicate_path_is_rejected(self):
        """Two drives on one path means one silently shadows the other."""
        with self.assertRaises(ConfigError) as ctx:
            config_mod.load(write_config(
                'drives:\n'
                '  - uuid: "a"\n    path: /volume1/same\n'
                '  - uuid: "b"\n    path: /volume1/same\n'))
        self.assertIn("another drive", str(ctx.exception))

    def test_duplicate_uuid_is_rejected(self):
        """One UUID on two paths means a bind that flip-flops."""
        with self.assertRaises(ConfigError):
            config_mod.load(write_config(
                'drives:\n'
                '  - uuid: "same"\n    path: /volume1/a\n'
                '  - uuid: "same"\n    path: /volume1/b\n'))

    def test_name_defaults_to_last_path_segment(self):
        cfg = config_mod.load(write_config(
            'drives:\n  - uuid: "a"\n    path: /volume1/USB_Thing\n'))
        self.assertEqual(cfg.drives[0].name, "USB_Thing")

    def test_missing_file_gives_a_useful_message(self):
        with self.assertRaises(ConfigError) as ctx:
            config_mod.load("/nonexistent/nope.yaml")
        self.assertIn("config.example.yaml", str(ctx.exception))


class Credentials(unittest.TestCase):
    """Secrets come from the environment only -- never the config file."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       (config_mod.ENV_ACCOUNT, config_mod.ENV_PASSWORD)}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_password_in_config_file_is_ignored(self):
        cfg = config_mod.load(write_config(
            VALID + '\ndsm:\n  host: localhost\n  password: "hunter2"\n'))
        self.assertEqual(cfg.dsm.password, "",
                         "a password in the config file must never be used")

    def test_credentials_are_read_from_env(self):
        os.environ[config_mod.ENV_ACCOUNT] = "admin"
        os.environ[config_mod.ENV_PASSWORD] = "s3cret"
        cfg = config_mod.load(write_config(VALID))
        self.assertEqual(cfg.dsm.account, "admin")
        self.assertEqual(cfg.dsm.password, "s3cret")
        config_mod.require_credentials(cfg)  # must not raise

    def test_require_credentials_names_the_missing_variables(self):
        cfg = config_mod.load(write_config(VALID))
        with self.assertRaises(ConfigError) as ctx:
            config_mod.require_credentials(cfg)
        self.assertIn(config_mod.ENV_ACCOUNT, str(ctx.exception))
        self.assertIn(config_mod.ENV_PASSWORD, str(ctx.exception))


class ProblemClassification(unittest.TestCase):
    """Absent hardware and a wrong bind must never be confused.

    This distinction decides whether it is safe to cut power to a drive. A
    device that is genuinely gone can be power-cycled; a path that is
    mounted-but-empty means the drive is LIVE and only the bind is wrong,
    so cutting it would be an unclean power-off of a running filesystem.
    """

    def setUp(self):
        self.drive = Drive(name="d", uuid="u", path="/volume1/d")

    def test_not_mounted_counts_as_absent(self):
        self.assertTrue(Problem(self.drive, NOT_MOUNTED).device_is_absent)

    def test_device_absent_counts_as_absent(self):
        self.assertTrue(Problem(self.drive, DEVICE_ABSENT).device_is_absent)

    def test_empty_stub_does_NOT_count_as_absent(self):
        self.assertFalse(
            Problem(self.drive, EMPTY_STUB).device_is_absent,
            "an empty stub means the drive is live -- never treat it as gone")

    def test_problem_renders_path_and_reason(self):
        self.assertEqual(str(Problem(self.drive, NOT_MOUNTED)),
                         "/volume1/d: not mounted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
