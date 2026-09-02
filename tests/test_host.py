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

    def test_device_at_returns_the_LAST_entry_when_binds_are_stacked(self):
        """Regression. Bind the same path twice and both entries appear in
        /proc/mounts. The kernel resolves to the last, so returning the first
        describes a mount nobody can actually reach."""
        stacked = ("/dev/usb5p1 /volume1/x ext4 rw 0 0\n"
                   "/dev/usb6p1 /volume1/x ext4 rw 0 0\n")
        with mock.patch.object(host, "_sh",
                               return_value=completed(stdout=stacked)):
            self.assertEqual(host.device_at("/volume1/x"), "/dev/usb6p1")

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
    """Bind mounts stack, so unmounting is not a single operation."""

    def _shrinking(self, counts):
        """proc_mounts returning counts[i] entries on the i-th call."""
        seq = iter(counts)

        def fake():
            return [("/dev/usb1p1", "/volume1/x")] * next(seq)
        return fake

    def test_single_layer_unmounts(self):
        with mock.patch.object(host, "run_on_host", return_value=completed()), \
             mock.patch.object(host, "proc_mounts",
                               side_effect=self._shrinking([1, 0, 0])):
            self.assertTrue(host.unmount("/volume1/x"))

    def test_not_mounted_at_all_succeeds_without_running_umount(self):
        with mock.patch.object(host, "run_on_host") as run, \
             mock.patch.object(host, "proc_mounts", return_value=[]):
            self.assertTrue(host.unmount("/volume1/x"))
        run.assert_not_called()

    def test_STACKED_binds_are_all_removed(self):
        """Regression. Two binds on one path used to leave it still mounted
        after a 'successful' release, so a detach reported the drive as
        released while a live mount was still sitting there. Found by running
        Drive Anchor alongside another script that binds the same paths."""
        with mock.patch.object(host, "run_on_host", return_value=completed()) as run, \
             mock.patch.object(host, "proc_mounts",
                               side_effect=self._shrinking([2, 1, 1, 0, 0])):
            self.assertTrue(host.unmount("/volume1/x"))
        self.assertEqual(run.call_count, 2, "should unmount once per layer")

    def test_a_busy_mount_gives_up_quickly(self):
        """No point retrying ten times against something genuinely held open."""
        with mock.patch.object(host, "run_on_host", return_value=completed(code=1)) as run, \
             mock.patch.object(host, "proc_mounts",
                               side_effect=self._shrinking([1, 1])):
            self.assertFalse(host.unmount("/volume1/x"))
        self.assertEqual(run.call_count, 1)


class UuidLookup(unittest.TestCase):
    def test_returns_the_device(self):
        with mock.patch.object(host, "run_on_host",
                               return_value=completed(stdout="/dev/usb6p1\n")):
            self.assertEqual(host.device_for_uuid("abc"), "/dev/usb6p1")

    def test_absent_uuid_returns_None_not_empty_string(self):
        with mock.patch.object(host, "run_on_host",
                               return_value=completed(stdout="\n", code=2)):
            self.assertIsNone(host.device_for_uuid("abc"))


class MountLayers(unittest.TestCase):
    """Stacking is a fault, and it is the one that hides the others."""

    STACKED = ("/dev/usb5p1 /volume1/x ext4 ro,relatime 0 0\n"
               "/dev/usb6p1 /volume1/x ext4 rw,relatime 0 0\n"
               "/dev/usb1p1 /volume1/y ext4 rw,relatime 0 0\n")

    def _patched(self):
        return mock.patch.object(host, "_sh",
                                 return_value=completed(stdout=self.STACKED))

    def test_counts_layers(self):
        with self._patched():
            self.assertEqual(host.mount_layers("/volume1/x"), 2)
            self.assertEqual(host.mount_layers("/volume1/y"), 1)
            self.assertEqual(host.mount_layers("/volume1/nope"), 0)

    def test_read_only_uses_the_LAST_layer_like_the_kernel(self):
        """The first layer here is ro and the second is rw. The kernel
        resolves to the last, so the path is writable and must not be
        reported read-only."""
        with self._patched():
            self.assertFalse(host.is_read_only("/volume1/x"))

    def test_detects_a_read_only_effective_mount(self):
        ro = "/dev/usb1p1 /volume1/x ext4 ro,relatime 0 0\n"
        with mock.patch.object(host, "_sh", return_value=completed(stdout=ro)):
            self.assertTrue(host.is_read_only("/volume1/x"))

    def test_relatime_is_not_mistaken_for_ro(self):
        """Naive substring matching would see 'ro' inside other options."""
        rw = "/dev/usb1p1 /volume1/x ext4 rw,relatime,errors=remount-ro 0 0\n"
        with mock.patch.object(host, "_sh", return_value=completed(stdout=rw)):
            self.assertFalse(host.is_read_only("/volume1/x"))

    def test_unmounted_path_is_not_read_only(self):
        with mock.patch.object(host, "_sh", return_value=completed(stdout="")):
            self.assertFalse(host.is_read_only("/volume1/x"))


class WriteProbe(unittest.TestCase):
    """Mount options can say rw while the filesystem refuses writes."""

    def test_successful_probe_is_cleaned_up(self):
        with mock.patch.object(host, "run_on_host",
                               return_value=completed()) as run:
            self.assertTrue(host.can_write("/volume1/x"))
        cmds = [c[0][0] for c in run.call_args_list]
        self.assertEqual(cmds[0][0], "touch")
        self.assertEqual(cmds[1][0], "rm", "the probe file must be removed")
        self.assertTrue(cmds[0][1].endswith(host.WRITE_PROBE))

    def test_failed_probe_reports_false_and_does_not_try_to_delete(self):
        with mock.patch.object(host, "run_on_host",
                               return_value=completed(code=1)) as run:
            self.assertFalse(host.can_write("/volume1/x"))
        self.assertEqual(run.call_count, 1)

    def test_trailing_slash_does_not_produce_a_double_slash(self):
        with mock.patch.object(host, "run_on_host",
                               return_value=completed()) as run:
            host.can_write("/volume1/x/")
        self.assertNotIn("//", run.call_args_list[0][0][0][1])



class FailureIsNotAnAnswer(unittest.TestCase):
    """A check that cannot fail loudly is worse than no check.

    Both of these shipped reporting a definite state when the underlying
    command had actually failed. They are the same mistake the tool exists to
    catch -- absence of evidence read as evidence of a specific state -- so
    they get their own tests.
    """

    def test_dir_is_empty_raises_when_it_cannot_read_the_path(self):
        """Must NOT report "empty" for a directory it failed to read.

        The original ran `ls -A <p> 2>/dev/null | head -1`: stderr discarded,
        and the exit status was head's, so a missing path was indistinguishable
        from an empty one. Both gave empty stdout and returncode 0.
        """
        with mock.patch.object(host, "run_on_host") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 2, stdout="", stderr="find: '/gone': No such file or directory")
            with self.assertRaises(host.HostError) as ctx:
                host.dir_is_empty("/gone")
            self.assertIn("/gone", str(ctx.exception))

    def test_dir_is_empty_true_only_on_a_successful_empty_listing(self):
        with mock.patch.object(host, "run_on_host") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            self.assertTrue(host.dir_is_empty("/volume1/x"))

    def test_dir_is_empty_false_when_an_entry_is_found(self):
        with mock.patch.object(host, "run_on_host") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, stdout="/volume1/x/Movies", stderr="")
            self.assertFalse(host.dir_is_empty("/volume1/x"))

    def test_dir_is_empty_stops_at_the_first_entry(self):
        """-print -quit keeps this cheap on a directory with a million files."""
        with mock.patch.object(host, "run_on_host") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            host.dir_is_empty("/volume1/x")
            argv = run.call_args[0][0]
        self.assertIn("-quit", argv)
        self.assertIn("-maxdepth", argv)

    def test_uuid_lookup_absent_is_not_confused_with_lookup_failure(self):
        """blkid exit 2 means "no such UUID" -- a real answer. Anything else
        means the lookup broke, and must not be reported as absent hardware."""
        with mock.patch.object(host, "run_on_host") as run:
            run.return_value = subprocess.CompletedProcess([], 2, stdout="", stderr="")
            self.assertIsNone(host.device_for_uuid("dead-beef"))

            run.return_value = subprocess.CompletedProcess(
                [], 4, stdout="", stderr="blkid: cannot open /dev/null")
            with self.assertRaises(host.HostError):
                host.device_for_uuid("dead-beef")

    def test_uuid_lookup_returns_the_device_on_success(self):
        with mock.patch.object(host, "run_on_host") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, stdout="/dev/usb1p1", stderr="")
            self.assertEqual(host.device_for_uuid("abc"), "/dev/usb1p1")

if __name__ == "__main__":
    unittest.main(verbosity=2)
