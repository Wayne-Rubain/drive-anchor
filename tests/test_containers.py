"""Tests for finding and fixing containers left on a drive's old device.

The fault: a USB drive drops off the bus and comes back as a new device
(usb6 -> usb9). The host paths are rebound and look healthy, but a container
started earlier still holds usb6 behind the same path. These tests pin down
when that is detected, when it is not, and when a restart is allowed.

Nothing here touches Docker or a real system; host calls are intercepted.

Run with:  python3 tests/test_containers.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drive_anchor import config as config_mod, containers, host, repair   # noqa: E402
from drive_anchor.config import (                                          # noqa: E402
    Config, ConfigError, ContainersConfig, Drive, RepairConfig)
from drive_anchor.host import Container, HostError                         # noqa: E402

BACKUP = Drive(name="backup", uuid="u-1", path="/volume1/USB_Backup",
               share="USB_Backup")
MEDIA = Drive(name="media", uuid="u-2", path="/volume1/USB_Media")


def completed(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess(args=[], returncode=code,
                                       stdout=stdout, stderr=stderr)


def cfg(**kw):
    c = Config(drives=[BACKUP, MEDIA], dry_run=False)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


class Fake:
    """A NAS with some containers, as Drive Anchor would see it.

    host_devices: what the host has at each drive path now
    container_view: {pid: {destination: device}} as each container sees it
    present: devices that exist in /sys/block
    """

    def __init__(self, containers_, host_devices, container_view, present):
        self.containers = containers_
        self.host_devices = host_devices
        self.view = container_view
        self.present = present
        self.restarted = []

    def restart(self, name, timeout=120):
        self.restarted.append(name)
        # A restart re-resolves the container's mounts to the current device.
        for c in self.containers:
            if c.name == name:
                for src, dst in c.binds:
                    for path, dev in self.host_devices.items():
                        if src == path or src.startswith(path + "/"):
                            self.view[c.pid][dst] = dev
        return True, name

    def patches(self):
        return [
            mock.patch.object(host, "running_containers",
                              side_effect=lambda: self.containers),
            mock.patch.object(host, "container_mount_devices",
                              side_effect=lambda pid: dict(self.view[pid])),
            mock.patch.object(host, "device_at",
                              side_effect=lambda p: self.host_devices.get(p)),
            mock.patch.object(host, "block_device_present",
                              side_effect=lambda d: d in self.present),
            mock.patch.object(host, "restart_container", side_effect=self.restart),
        ]

    def __enter__(self):
        self._p = self.patches()
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._p:
            p.stop()


def urbackup_after_dropout():
    """The 2026-10-07 incident: drive renumbered usb6 -> usb9."""
    return Fake(
        [Container("urbackup", 101,
                   [("/volume1/USB_Backup/urbackup_backups", "/backups"),
                    ("/volume1/docker/urbackup/db", "/var/urbackup")])],
        {"/volume1/USB_Backup": "/dev/usb9p1"},
        {101: {"/backups": "/dev/usb6p1", "/var/urbackup": "/dev/mapper/cachedev_1"}},
        {"/dev/usb9p1"})


class Matching(unittest.TestCase):
    """Which container mounts belong to a configured drive."""

    def test_exact_and_nested_paths_match(self):
        c = cfg()
        self.assertIs(containers.drive_for_source("/volume1/USB_Backup", c), BACKUP)
        self.assertIs(containers.drive_for_source("/volume1/USB_Backup/x/y", c), BACKUP)

    def test_a_longer_name_is_not_a_prefix_match(self):
        """/volume1/USB_Backup2 is a different drive, not a subfolder."""
        self.assertIsNone(containers.drive_for_source("/volume1/USB_Backup2", cfg()))

    def test_container_manager_share_path_matches_by_share(self):
        src = f"{containers.ALL_SHARES}/USB_Backup/data"
        self.assertIs(containers.drive_for_source(src, cfg()), BACKUP)

    def test_unrelated_paths_do_not_match(self):
        self.assertIsNone(containers.drive_for_source("/volume1/docker/app", cfg()))


class Detection(unittest.TestCase):

    def test_the_renumbered_drive_is_found(self):
        with urbackup_after_dropout():
            stale = containers.find_stale(cfg())
        self.assertEqual(len(stale), 1)
        s = stale[0]
        self.assertEqual((s.container, s.destination, s.seen, s.gone),
                         ("urbackup", "/backups", "/dev/usb6p1", True))
        self.assertIn("no longer exists", str(s))

    def test_a_healthy_container_is_not_flagged(self):
        fake = urbackup_after_dropout()
        fake.view[101]["/backups"] = "/dev/usb9p1"
        with fake:
            self.assertEqual(containers.find_stale(cfg()), [])

    def test_old_name_reused_by_another_drive_is_still_caught(self):
        """usb6 exists again -- but it is a different drive now. Checking
        only 'does the device exist' would miss this."""
        fake = urbackup_after_dropout()
        fake.present.add("/dev/usb6p1")
        with fake:
            stale = containers.find_stale(cfg())
        self.assertEqual(len(stale), 1)
        self.assertFalse(stale[0].gone)
        self.assertIn("is now /dev/usb9p1", str(stale[0]))

    def test_containers_not_using_managed_drives_are_never_read(self):
        fake = Fake([Container("web", 7, [("/volume1/docker/web", "/srv")])],
                    {}, {}, set())
        with fake, mock.patch.object(host, "container_mount_devices") as read:
            self.assertEqual(containers.find_stale(cfg()), [])
            read.assert_not_called()

    def test_no_docker_means_nothing_to_check(self):
        with mock.patch.object(host, "running_containers", return_value=None):
            self.assertIsNone(containers.find_stale(cfg()))


class Restarting(unittest.TestCase):

    def test_stale_container_is_restarted_and_rechecked(self):
        with urbackup_after_dropout() as fake:
            restarted, remaining = containers.check_and_fix(cfg())
        self.assertEqual(fake.restarted, ["urbackup"])
        self.assertEqual(restarted, ["urbackup"])
        self.assertEqual(remaining, [])

    def test_dry_run_restarts_nothing_and_reports_it(self):
        with urbackup_after_dropout() as fake:
            restarted, remaining = containers.check_and_fix(cfg(dry_run=True))
        self.assertEqual(fake.restarted, [])
        self.assertEqual(len(remaining), 1)

    def test_report_only_mode(self):
        c = cfg(containers=ContainersConfig(restart_stale=False))
        with urbackup_after_dropout() as fake:
            _, remaining = containers.check_and_fix(c)
        self.assertEqual(fake.restarted, [])
        self.assertEqual(len(remaining), 1)

    def test_excluded_container_is_left_alone(self):
        c = cfg(containers=ContainersConfig(exclude=["urbackup"]))
        with urbackup_after_dropout() as fake:
            _, remaining = containers.check_and_fix(c)
        self.assertEqual(fake.restarted, [])
        self.assertEqual(len(remaining), 1)

    def test_one_restart_per_container_however_many_mounts(self):
        fake = urbackup_after_dropout()
        fake.containers[0].binds.append(("/volume1/USB_Backup/other", "/other"))
        fake.view[101]["/other"] = "/dev/usb6p1"
        with fake:
            containers.check_and_fix(cfg())
        self.assertEqual(fake.restarted, ["urbackup"])

    def test_an_unanswerable_docker_is_reported_not_passed(self):
        """'Could not ask' must never come back as 'nothing is stale'."""
        with mock.patch.object(host, "running_containers",
                               side_effect=HostError("daemon down")):
            restarted, remaining = containers.check_and_fix(cfg())
        self.assertEqual(restarted, [])
        self.assertEqual(len(remaining), 1)
        self.assertIn("could not check containers", remaining[0])


class RepairIntegration(unittest.TestCase):
    """The incident case: drives verify, a container does not."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = cfg(repair=RepairConfig(max_per_hour=3, state_dir=self.tmp))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_healthy_drives_with_a_stale_container_get_repaired(self):
        with urbackup_after_dropout() as fake, \
                mock.patch.object(repair.verify, "check_all", return_value=[]):
            out = repair.run(self.cfg)
        self.assertEqual(fake.restarted, ["urbackup"])
        self.assertEqual(out.situation, repair.CONTAINERS)
        self.assertEqual(out.repaired, ["container urbackup"])
        self.assertEqual(out.exit_code, 0)
        self.assertEqual(repair.repairs_last_hour(self.cfg), 1)

    def test_container_restarts_respect_the_hourly_cap(self):
        for _ in range(3):
            repair.record_repair(self.cfg)
        with urbackup_after_dropout() as fake, \
                mock.patch.object(repair.verify, "check_all", return_value=[]):
            out = repair.run(self.cfg)
        self.assertEqual(fake.restarted, [])
        self.assertIsNotNone(out.refused_reason)
        self.assertEqual(out.exit_code, 1)

    def test_all_healthy_is_still_healthy(self):
        fake = urbackup_after_dropout()
        fake.view[101]["/backups"] = "/dev/usb9p1"
        with fake, mock.patch.object(repair.verify, "check_all", return_value=[]):
            out = repair.run(self.cfg)
        self.assertEqual(out.situation, repair.HEALTHY)
        self.assertEqual(out.exit_code, 0)
        self.assertEqual(repair.repairs_last_hour(self.cfg), 0)


class HostParsing(unittest.TestCase):

    def test_running_containers_reads_bind_mounts_only(self):
        inspect = json.dumps([{
            "Name": "/urbackup", "State": {"Pid": 101},
            "Mounts": [
                {"Type": "bind", "Source": "/volume1/USB_Backup/b",
                 "Destination": "/backups"},
                {"Type": "volume", "Source": "/var/lib/docker/volumes/x",
                 "Destination": "/var/log"}]}])
        with mock.patch.object(host, "run_on_host",
                               side_effect=[completed("abc\n"), completed(inspect)]):
            found = host.running_containers()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].name, "urbackup")
        self.assertEqual(found[0].pid, 101)
        self.assertEqual(found[0].binds, [("/volume1/USB_Backup/b", "/backups")])

    def test_nothing_running_is_an_empty_list(self):
        with mock.patch.object(host, "run_on_host", return_value=completed("")):
            self.assertEqual(host.running_containers(), [])

    def test_docker_not_installed_is_None(self):
        with mock.patch.object(host, "run_on_host",
                               return_value=completed(stderr="nsenter: failed",
                                                      code=127)):
            self.assertIsNone(host.running_containers())
        with mock.patch.object(host, "run_on_host",
                               side_effect=host.CommandNotFound("no docker")):
            self.assertIsNone(host.running_containers())

    def test_a_daemon_that_will_not_answer_raises(self):
        with mock.patch.object(host, "run_on_host",
                               return_value=completed(stderr="Cannot connect",
                                                      code=1)):
            with self.assertRaises(HostError):
                host.running_containers()

    def test_container_mounts_unescape_spaces(self):
        text = ("/dev/usb9p1 /my\\040backups ext4 rw 0 0\n"
                "overlay / overlay rw 0 0\n")
        with mock.patch.object(host, "run_on_host", return_value=completed(text)):
            found = host.container_mount_devices(5)
        self.assertEqual(found["/my backups"], "/dev/usb9p1")

    def test_missing_binary_natively_is_CommandNotFound(self):
        with mock.patch.object(host, "_needs_nsenter", return_value=False), \
             mock.patch("subprocess.run", side_effect=FileNotFoundError("docker")):
            with self.assertRaises(host.CommandNotFound):
                host.run_on_host(["docker", "ps"])


class ConfigParsing(unittest.TestCase):

    def test_defaults(self):
        c = config_mod._build({})
        self.assertTrue(c.containers.restart_stale)
        self.assertEqual(c.containers.exclude, [])

    def test_section_is_read(self):
        c = config_mod._build({"containers": {"restart_stale": False,
                                              "exclude": ["db"]}})
        self.assertFalse(c.containers.restart_stale)
        self.assertEqual(c.containers.exclude, ["db"])

    def test_bad_types_are_rejected(self):
        with self.assertRaises(ConfigError):
            config_mod._build({"containers": ["urbackup"]})
        with self.assertRaises(ConfigError):
            config_mod._build({"containers": {"exclude": "urbackup"}})


if __name__ == "__main__":
    unittest.main()
