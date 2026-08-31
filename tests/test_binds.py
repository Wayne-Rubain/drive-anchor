"""Tests for binds.py -- creating and releasing the stable /volume1 paths.

Everything that touches the system is intercepted, so these run anywhere.
The focus is on the decisions: which mountpoint gets chosen, when an existing
bind is left alone, when a stale one is cleared first, and what a caller is
told when something fails.

Run with:  python3 tests/test_binds.py
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drive_anchor import binds                       # noqa: E402
from drive_anchor.config import Config, Drive        # noqa: E402
from drive_anchor.host import HostError              # noqa: E402

DRIVE = Drive(name="dock", uuid="a29ca641", path="/volume1/USB_Dock")


def cfg(dry_run=False, **kw):
    return Config(dry_run=dry_run, drives=[DRIVE], **kw)


class FindSourceMount(unittest.TestCase):
    """Resolving UUID -> device -> the mountpoint DSM chose."""

    def test_finds_the_volumeUSB_mountpoint(self):
        with mock.patch.object(binds.host, "device_for_uuid",
                               return_value="/dev/usb6p1"), \
             mock.patch.object(binds.host, "proc_mounts", return_value=[
                 ("/dev/usb1p1", "/volumeUSB2/usbshare"),
                 ("/dev/usb6p1", "/volumeUSB5/usbshare"),
             ]):
            self.assertEqual(binds.find_source_mount(DRIVE),
                             "/volumeUSB5/usbshare")

    def test_absent_uuid_gives_None(self):
        with mock.patch.object(binds.host, "device_for_uuid", return_value=None):
            self.assertIsNone(binds.find_source_mount(DRIVE))

    def test_ignores_the_existing_bind_and_returns_the_real_source(self):
        """The same device is mounted twice: once by DSM under /volumeUSB,
        and once at our stable path. Binding our path onto itself would be
        nonsense, so only the /volumeUSB entry counts as a source."""
        with mock.patch.object(binds.host, "device_for_uuid",
                               return_value="/dev/usb6p1"), \
             mock.patch.object(binds.host, "proc_mounts", return_value=[
                 ("/dev/usb6p1", "/volume1/USB_Dock"),
                 ("/dev/usb6p1", "/volumeUSB5/usbshare"),
             ]):
            self.assertEqual(binds.find_source_mount(DRIVE),
                             "/volumeUSB5/usbshare")

    def test_device_present_but_not_mounted_gives_None(self):
        with mock.patch.object(binds.host, "device_for_uuid",
                               return_value="/dev/usb6p1"), \
             mock.patch.object(binds.host, "proc_mounts", return_value=[]):
            self.assertIsNone(binds.find_source_mount(DRIVE))


class WaitForDrive(unittest.TestCase):
    def test_returns_as_soon_as_it_appears(self):
        with mock.patch.object(binds, "find_source_mount",
                               return_value="/volumeUSB5/usbshare"), \
             mock.patch("time.sleep") as slept:
            got = binds.wait_for_drive(DRIVE, timeout_sec=90, poll_sec=3)
        self.assertEqual(got, "/volumeUSB5/usbshare")
        slept.assert_not_called()

    def test_polls_until_it_shows_up(self):
        """Slow hardware is normal, not a fault -- powered docks can take
        well over a minute."""
        with mock.patch.object(binds, "find_source_mount",
                               side_effect=[None, None, "/volumeUSB5/usbshare"]), \
             mock.patch("time.sleep"):
            self.assertEqual(binds.wait_for_drive(DRIVE, 90, 3),
                             "/volumeUSB5/usbshare")

    def test_gives_up_and_returns_None(self):
        with mock.patch.object(binds, "find_source_mount", return_value=None), \
             mock.patch("time.sleep"):
            self.assertIsNone(binds.wait_for_drive(DRIVE, timeout_sec=0,
                                                   poll_sec=0))


class BindOne(unittest.TestCase):
    def test_dry_run_changes_nothing(self):
        with mock.patch.object(binds.host, "bind_mount") as bind, \
             mock.patch.object(binds.host, "unmount") as um, \
             mock.patch.object(binds.host, "make_dir") as mk:
            ok, why = binds.bind_one(DRIVE, cfg(dry_run=True))
        self.assertTrue(ok)
        self.assertIn("dry run", why)
        bind.assert_not_called()
        um.assert_not_called()
        mk.assert_not_called()

    def test_missing_drive_reports_the_uuid_and_does_not_bind(self):
        with mock.patch.object(binds, "wait_for_drive", return_value=None), \
             mock.patch.object(binds.host, "bind_mount") as bind:
            ok, why = binds.bind_one(DRIVE, cfg())
        self.assertFalse(ok)
        self.assertIn(DRIVE.uuid, why)
        bind.assert_not_called()

    def test_already_correct_is_left_alone(self):
        """Re-binding a working mount would churn it for no reason."""
        with mock.patch.object(binds, "wait_for_drive",
                               return_value="/volumeUSB5/usbshare"), \
             mock.patch.object(binds.host, "make_dir"), \
             mock.patch.object(binds.host, "device_at", return_value="/dev/usb6p1"), \
             mock.patch.object(binds.host, "device_for_uuid", return_value="/dev/usb6p1"), \
             mock.patch.object(binds.host, "unmount") as um, \
             mock.patch.object(binds.host, "bind_mount") as bind:
            ok, why = binds.bind_one(DRIVE, cfg())
        self.assertTrue(ok)
        self.assertIn("already bound", why)
        um.assert_not_called()
        bind.assert_not_called()

    def test_stale_bind_is_cleared_before_rebinding(self):
        """The real failure this fixes: the path is still bound to a device
        that has gone away. Binding on top would stack a new mount over the
        dead one instead of replacing it."""
        with mock.patch.object(binds, "wait_for_drive",
                               return_value="/volumeUSB5/usbshare"), \
             mock.patch.object(binds.host, "make_dir"), \
             mock.patch.object(binds.host, "device_at", return_value="/dev/usb5p1"), \
             mock.patch.object(binds.host, "device_for_uuid", return_value="/dev/usb6p1"), \
             mock.patch.object(binds.host, "unmount") as um, \
             mock.patch.object(binds.host, "bind_mount") as bind:
            ok, _ = binds.bind_one(DRIVE, cfg())
        self.assertTrue(ok)
        um.assert_called_once_with(DRIVE.path)
        bind.assert_called_once_with("/volumeUSB5/usbshare", DRIVE.path)

    def test_nothing_mounted_binds_without_unmounting(self):
        with mock.patch.object(binds, "wait_for_drive",
                               return_value="/volumeUSB5/usbshare"), \
             mock.patch.object(binds.host, "make_dir"), \
             mock.patch.object(binds.host, "device_at", return_value=None), \
             mock.patch.object(binds.host, "device_for_uuid", return_value="/dev/usb6p1"), \
             mock.patch.object(binds.host, "unmount") as um, \
             mock.patch.object(binds.host, "bind_mount") as bind:
            ok, _ = binds.bind_one(DRIVE, cfg())
        self.assertTrue(ok)
        um.assert_not_called()
        bind.assert_called_once()

    def test_a_failing_mount_is_reported_not_raised(self):
        with mock.patch.object(binds, "wait_for_drive",
                               return_value="/volumeUSB5/usbshare"), \
             mock.patch.object(binds.host, "make_dir"), \
             mock.patch.object(binds.host, "device_at", return_value=None), \
             mock.patch.object(binds.host, "device_for_uuid", return_value="/dev/usb6p1"), \
             mock.patch.object(binds.host, "bind_mount",
                               side_effect=HostError("permission denied")):
            ok, why = binds.bind_one(DRIVE, cfg())
        self.assertFalse(ok)
        self.assertIn("permission denied", why)


class ReleaseOne(unittest.TestCase):
    def test_dry_run_changes_nothing(self):
        with mock.patch.object(binds.host, "unmount") as um:
            ok, why = binds.release_one(DRIVE, dry_run=True)
        self.assertTrue(ok)
        self.assertIn("dry run", why)
        um.assert_not_called()

    def test_not_mounted_counts_as_released(self):
        """Nothing to do is success, not failure -- a detach must not stall
        because a drive was already unmounted."""
        with mock.patch.object(binds.host, "is_mounted", return_value=False), \
             mock.patch.object(binds.host, "unmount") as um:
            ok, _ = binds.release_one(DRIVE)
        self.assertTrue(ok)
        um.assert_not_called()

    def test_successful_unmount(self):
        with mock.patch.object(binds.host, "is_mounted", return_value=True), \
             mock.patch.object(binds.host, "unmount", return_value=True):
            ok, why = binds.release_one(DRIVE)
        self.assertTrue(ok)
        self.assertIn("released", why)

    def test_busy_path_fails_and_says_why_the_eject_will_fail(self):
        with mock.patch.object(binds.host, "is_mounted", return_value=True), \
             mock.patch.object(binds.host, "unmount", return_value=False):
            ok, why = binds.release_one(DRIVE)
        self.assertFalse(ok)
        self.assertIn("still has it open", why)


class AllDrives(unittest.TestCase):
    def test_bind_all_collects_every_failure_and_keeps_going(self):
        """One bad drive must not stop the others from being handled."""
        good = Drive(name="a", uuid="u1", path="/volume1/a")
        bad = Drive(name="b", uuid="u2", path="/volume1/b")
        c = Config(dry_run=False, drives=[good, bad])
        with mock.patch.object(binds, "bind_one",
                               side_effect=[(True, "ok"), (False, "b: broke")]):
            failures = binds.bind_all(c)
        self.assertEqual(failures, ["b: broke"])

    def test_release_all_reports_every_failure(self):
        d1 = Drive(name="a", uuid="u1", path="/volume1/a")
        d2 = Drive(name="b", uuid="u2", path="/volume1/b")
        c = Config(dry_run=False, drives=[d1, d2])
        with mock.patch.object(binds, "release_one",
                               side_effect=[(False, "a: busy"), (False, "b: busy")]):
            self.assertEqual(binds.release_all(c), ["a: busy", "b: busy"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
