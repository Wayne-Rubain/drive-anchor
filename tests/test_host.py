"""Tests for host.py -- the only module that touches the system.

Everything the tool does to your NAS goes through this file, so it is worth
testing carefully. The actual `subprocess.run` call is deliberately thin;
what is worth testing is everything around it -- how commands are assembled,
how output is parsed, and what happens when things go wrong.

Nothing here touches a real system. Commands are intercepted, and the
filesystem checks are pointed at temporary directories.

Run with:  python3 tests/test_host.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drive_anchor import host                       # noqa: E402
from drive_anchor.host import HostError             # noqa: E402


def completed(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess(args=[], returncode=code,
                                       stdout=stdout, stderr=stderr)


class CommandAssembly(unittest.TestCase):
    """Whether nsenter is prepended decides if anything happens at all.

    Get this wrong in one direction and every mount is invisible to the rest
    of the NAS; get it wrong in the other and every command fails outright.
    """

    def test_nsenter_is_prepended_inside_a_container(self):
        with mock.patch.object(host, "_needs_nsenter", return_value=True), \
             mock.patch("subprocess.run", return_value=completed()) as run:
            host.run_on_host(["mount", "--bind", "/a", "/b"])
        self.assertEqual(
            run.call_args[0][0],
            ["nsenter", "--target", "1", "--mount", "--",
             "mount", "--bind", "/a", "/b"])

    def test_command_runs_directly_when_already_on_the_host(self):
        with mock.patch.object(host, "_needs_nsenter", return_value=False), \
             mock.patch("subprocess.run", return_value=completed()) as run:
            host.run_on_host(["umount", "/x"])
        self.assertEqual(run.call_args[0][0], ["umount", "/x"])

    def test_nonzero_exit_is_returned_not_raised(self):
        """Callers decide what failure means -- `umount` failing because
        nothing was mounted is normal, not exceptional."""
        with mock.patch.object(host, "_needs_nsenter", return_value=False), \
             mock.patch("subprocess.run", return_value=completed(code=1)):
            self.assertEqual(host.run_on_host(["umount", "/x"]).returncode, 1)

    def test_timeout_raises_HostError(self):
        with mock.patch.object(host, "_needs_nsenter", return_value=False), \
             mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired("x", 5)):
            with self.assertRaises(HostError):
                host.run_on_host(["sleep", "99"])

    def test_missing_binary_raises_HostError(self):
        with mock.patch.object(host, "_needs_nsenter", return_value=False), \
             mock.patch("subprocess.run", side_effect=OSError("not found")):
            with self.assertRaises(HostError):
                host.run_on_host(["nope"])


class NamespaceDetection(unittest.TestCase):
    def test_differing_namespaces_means_nsenter_is_needed(self):
        with mock.patch("os.readlink", side_effect=["mnt:[1]", "mnt:[2]"]):
            self.assertTrue(host._needs_nsenter())

    def test_matching_namespaces_means_it_is_not(self):
        with mock.patch("os.readlink", side_effect=["mnt:[1]", "mnt:[1]"]):
            self.assertFalse(host._needs_nsenter())

    def test_unreadable_namespace_assumes_host(self):
        """Failing this check must not wrap commands in an nsenter that
        cannot work. Running directly is the recoverable wrong answer: it
        affects only the container, and the caller's own verification
        catches it."""
        with mock.patch("os.readlink", side_effect=OSError):
            self.assertFalse(host._needs_nsenter())


class MountTableParsing(unittest.TestCase):
    MOUNTS = (
        "/dev/usb1p1 /volumeUSB2/usbshare ext4 rw 0 0\n"
        "/dev/usb6p1 /volumeUSB5/usbshare ext4 rw 0 0\n"
        "/dev/usb6p1 /volume1/USB2_PC_Image_backup ext4 rw 0 0\n"
        "malformed-line-with-no-second-field\n"
    )

    def _patched(self):
        return mock.patch.object(host, "_sh",
                                 return_value=completed(stdout=self.MOUNTS))

    def test_parses_device_and_mountpoint_pairs(self):
        with self._patched():
            pairs = host.proc_mounts()
        self.assertIn(("/dev/usb1p1", "/volumeUSB2/usbshare"), pairs)
        self.assertIn(("/dev/usb6p1", "/volume1/USB2_PC_Image_backup"), pairs)

    def test_malformed_lines_are_skipped_not_fatal(self):
        with self._patched():
            self.assertEqual(len(host.proc_mounts()), 3)

    def test_is_mounted(self):
        with self._patched():
            self.assertTrue(host.is_mounted("/volume1/USB2_PC_Image_backup"))
            self.assertFalse(host.is_mounted("/volume1/nope"))

    def test_device_at_returns_the_backing_device(self):
        with self._patched():
            self.assertEqual(host.device_at("/volumeUSB5/usbshare"),
                             "/dev/usb6p1")
            self.assertIsNone(host.device_at("/volume1/nope"))

    def test_unreadable_proc_mounts_raises(self):
        """If the mount table cannot be read we know nothing, and guessing
        would be worse than stopping."""
        with mock.patch.object(host, "_sh",
                               return_value=completed(code=1, stderr="denied")):
            with self.assertRaises(HostError):
                host.proc_mounts()


class ParentDeviceName(unittest.TestCase):
    """Deriving a whole disk from a partition name."""

    def test_p_separated_partitions(self):
        self.assertEqual(host.parent_device_name("usb5p1"), "usb5")
        self.assertEqual(host.parent_device_name("usb12p3"), "usb12")
        self.assertEqual(host.parent_device_name("nvme0n1p2"), "nvme0n1")

    def test_directly_numbered_partitions(self):
        self.assertEqual(host.parent_device_name("sda1"), "sda")
        self.assertEqual(host.parent_device_name("sdaa12"), "sdaa")

    def test_whole_disks_ending_in_a_digit_are_left_alone_by_the_p_rule(self):
        self.assertEqual(host.parent_device_name("usb5p1"), "usb5")

    def test_only_one_suffix_is_stripped(self):
        """Regression for the progressive-stripping bug: repeatedly trimming
        characters until something matched let 'usb12p1' reach 'usb1'."""
        self.assertEqual(host.parent_device_name("usb12p1"), "usb12")
        self.assertNotEqual(host.parent_device_name("usb12p1"), "usb1")


class BlockDevicePresence(unittest.TestCase):
    """The check the whole tool trusts when mount, df and ls are lying."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.blocks = os.path.join(self.tmp, "block")
        os.makedirs(self.blocks)
        # Mirrors the real NAS: usb1-usb4 and usb6 present, usb5 gone.
        for d in ("usb1", "usb2", "usb3", "usb4", "usb6", "sata1", "sda"):
            os.makedirs(os.path.join(self.blocks, d))
        self._patch = mock.patch("os.path.exists", side_effect=self._exists)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _exists(self, path):
        if path.startswith("/sys/block/"):
            return os.path.isdir(os.path.join(self.blocks,
                                              path[len("/sys/block/"):]))
        return False

    def test_present_partition(self):
        self.assertTrue(host.block_device_present("/dev/usb6p1"))

    def test_absent_partition(self):
        """usb5 really was gone while its bind still pointed at it."""
        self.assertFalse(host.block_device_present("/dev/usb5p1"))

    def test_whole_disk_ending_in_a_digit_is_found(self):
        """usb1 must not be treated as partition 1 of a disk called 'usb'."""
        self.assertTrue(host.block_device_present("/dev/usb1"))
        self.assertTrue(host.block_device_present("/dev/sata1"))

    def test_scsi_style_partition(self):
        self.assertTrue(host.block_device_present("/dev/sda1"))

    def test_longer_name_does_NOT_match_a_shorter_existing_disk(self):
        """Regression for the false positive. usb12 does not exist; usb1
        does. Reporting 'present' here would tell the tool a dead drive is
        alive, which is the exact failure this check exists to catch."""
        self.assertFalse(host.block_device_present("/dev/usb12p1"))
        self.assertFalse(host.block_device_present("/dev/usb12"))


class ShellQuoting(unittest.TestCase):
    """Paths come from a config file and are interpolated into a shell
    snippet, so quoting them is a correctness and safety matter."""

    def test_plain_path(self):
        self.assertEqual(host._quote("/volume1/USB_Media"),
                         "'/volume1/USB_Media'")

    def test_spaces_are_contained(self):
        self.assertEqual(host._quote("/volume1/My Drive"),
                         "'/volume1/My Drive'")

    def test_embedded_single_quote_cannot_break_out(self):
        quoted = host._quote("/volume1/it's")
        self.assertEqual(quoted, "'/volume1/it'\\''s'")
        # Round-trip it through a real shell to prove it survives intact.
        out = subprocess.run(["sh", "-c", f"printf %s {quoted}"],
                             capture_output=True, text=True)
        self.assertEqual(out.stdout, "/volume1/it's")

    def test_command_substitution_is_not_evaluated(self):
        quoted = host._quote("/volume1/$(touch /tmp/pwned)")
        out = subprocess.run(["sh", "-c", f"printf %s {quoted}"],
                             capture_output=True, text=True)
        self.assertEqual(out.stdout, "/volume1/$(touch /tmp/pwned)")


class Unmount(unittest.TestCase):
    def test_reports_success_by_checking_afterwards_not_by_exit_code(self):
        """umount's exit code is unreliable here, so the result is decided by
        looking at the mount table again."""
        with mock.patch.object(host, "run_on_host", return_value=completed(code=1)), \
             mock.patch.object(host, "is_mounted", return_value=False):
            self.assertTrue(host.unmount("/volume1/x"))

    def test_reports_failure_when_still_mounted(self):
        with mock.patch.object(host, "run_on_host", return_value=completed(code=0)), \
             mock.patch.object(host, "is_mounted", return_value=True):
            self.assertFalse(host.unmount("/volume1/x"))


class UuidLookup(unittest.TestCase):
    def test_returns_the_device(self):
        with mock.patch.object(host, "run_on_host",
                               return_value=completed(stdout="/dev/usb6p1\n")):
            self.assertEqual(host.device_for_uuid("abc"), "/dev/usb6p1")

    def test_absent_uuid_returns_None_not_empty_string(self):
        with mock.patch.object(host, "run_on_host",
                               return_value=completed(stdout="\n", code=2)):
            self.assertIsNone(host.device_for_uuid("abc"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
