"""Binding USB drives to fixed /volume1 paths.

The problem this solves
-----------------------
DSM mounts USB drives under /volumeUSBn/usbshare. That location is stable --
DSM pins each volumeUSBn to a drive serial -- but it sits *outside*
/volume1, and that is where the trouble starts:

  * Synology's Container Manager will not bind host paths outside /volume1
    shares, so a container cannot reach a USB drive directly.
  * Most DSM packages, media servers included, expect /volume1 paths too.

The usual workaround is a hand-made symlink into /volume1. It breaks the
first time the drive is ejected: the link is left dangling, and whatever was
reading through it starts seeing an empty directory instead of an error.

Drive Anchor uses a bind mount instead of a symlink, keyed on the
filesystem UUID, and re-establishes it deliberately after every reattach.
A bind mount either exists or does not -- there is no dangling state.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional, Tuple

from . import host
from .config import Config, Drive

log = logging.getLogger(__name__)


def find_source_mount(drive: Drive) -> Optional[str]:
    """Where DSM currently has this drive mounted, or None if it is absent.

    Resolves UUID -> device -> mountpoint. The device node is looked up
    fresh every time because its name is not stable across reconnects; the
    UUID is.
    """
    device = host.device_for_uuid(drive.uuid)
    if not device:
        return None
    for dev, mountpoint in host.proc_mounts():
        if dev == device and mountpoint.startswith("/volumeUSB"):
            return mountpoint
    return None


def wait_for_drive(drive: Drive, timeout_sec: int, poll_sec: int) -> Optional[str]:
    """Wait for DSM to mount the drive, returning its mountpoint or None.

    Reattaching hardware is not instant, and it is not uniform: some
    enclosures re-enumerate in a couple of seconds, while others -- powered
    docks especially -- can take a minute or more. Polling to a generous
    timeout is what makes this reliable across unknown hardware. Do not
    shorten the default without measuring your own slowest device; declaring
    a drive dead too early is the most common false alarm here.
    """
    deadline = time.time() + timeout_sec
    while True:
        mountpoint = find_source_mount(drive)
        if mountpoint:
            return mountpoint
        if time.time() >= deadline:
            return None
        time.sleep(poll_sec)


def bind_one(drive: Drive, cfg: Config) -> Tuple[bool, str]:
    """Bind one drive to its fixed path. Returns (ok, human-readable reason).

    Idempotent: if the correct device is already bound there, it does
    nothing rather than churning the mount.
    """
    if cfg.dry_run:
        return True, f"[dry run] would bind {drive.uuid} -> {drive.path}"

    source = wait_for_drive(drive, cfg.bind_wait_sec, cfg.bind_poll_sec)
    if not source:
        return False, (f"{drive.label}: no filesystem with UUID {drive.uuid} "
                       f"appeared within {cfg.bind_wait_sec}s")

    host.make_dir(drive.path)

    current = host.device_at(drive.path)
    expected = host.device_for_uuid(drive.uuid)
    if current and current == expected:
        return True, f"{drive.label}: already bound correctly"

    # Something else -- or a stale bind of the same drive under an older
    # device name -- is on the target. Clear it before rebinding, otherwise
    # the new bind stacks on top and the old one stays underneath.
    if current:
        host.unmount(drive.path)

    try:
        host.bind_mount(source, drive.path)
    except host.HostError as exc:
        return False, f"{drive.label}: {exc}"
    return True, f"{drive.label}: bound {source} -> {drive.path}"


def bind_all(cfg: Config) -> List[str]:
    """Bind every configured drive. Returns a list of failure messages."""
    failures = []
    for drive in cfg.drives:
        ok, reason = bind_one(drive, cfg)
        log.info("  %s", reason)
        if not ok:
            failures.append(reason)
    return failures


def release_one(drive: Drive, dry_run: bool = False) -> Tuple[bool, str]:
    """Unmount a drive's fixed path.

    This must happen before DSM can eject the drive: the bind holds the
    underlying filesystem busy, and DSM's eject will simply refuse while
    anything has it open.
    """
    if dry_run:
        return True, f"[dry run] would release {drive.path}"
    if not host.is_mounted(drive.path):
        return True, f"{drive.label}: {drive.path} was not mounted"
    if host.unmount(drive.path):
        return True, f"{drive.label}: released {drive.path}"
    return False, (f"{drive.label}: could not unmount {drive.path} -- "
                   "something still has it open; DSM eject will likely fail")


def release_all(cfg: Config) -> List[str]:
    """Release every configured bind. Returns a list of failure messages."""
    failures = []
    for drive in cfg.drives:
        ok, reason = release_one(drive, cfg.dry_run)
        log.info("  %s", reason)
        if not ok:
            failures.append(reason)
    return failures
