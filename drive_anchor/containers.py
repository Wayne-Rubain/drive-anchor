"""Containers still holding a drive that has moved.

The failure this catches
------------------------
Docker resolves a container's bind mounts once, at start. Suppose a backup
container mounts /volume1/USB_Backup/backups, and the USB enclosure then drops
off the bus for a minute and comes back. DSM remounts the drive under a new
device name (usb6 becomes usb9), Drive Anchor or DSM rebinds the host path,
and every host-side check is green.

The container is not. It still holds the old usb6 mount behind the same
path -- a device that no longer exists. Its writes fail, ext4 logs an aborted
journal against a disk that is not there, and the backup job reports
"Cannot create directory" every night until somebody restarts it.

Nothing about the container's configuration changed, so `docker inspect`
looks fine. The fault is only visible from inside the container's own mount
namespace, which is where this module looks.

Seen on a live NAS on 2026-10-07: an unexplained enclosure dropout at 1 AM,
host paths all repaired, and UrBackup failing silently until it was
restarted by hand that evening.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import host
from .config import Config, Drive

log = logging.getLogger(__name__)

# Container Manager projects can reference a share through this path rather
# than through /volume1/<share>. Both lead to the same drive.
ALL_SHARES = "/volume1/@appdata/ContainerManager/all_shares"


@dataclass
class StaleMount:
    """A container mount whose device is not the drive now at that path."""
    container: str
    destination: str
    drive: Drive
    seen: str                 # device the container still has
    expected: Optional[str]   # device the host now has at the drive's path
    gone: bool                # the seen device no longer exists at all

    def __str__(self) -> str:
        if self.gone:
            why = f"{self.seen}, which no longer exists"
        else:
            why = (f"{self.seen}, but {self.drive.path} is now "
                   f"{self.expected}")
        return (f"container {self.container}: {self.destination} still "
                f"points at {why}")


def _under(path: str, root: str) -> bool:
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


def drive_for_source(source: str, cfg: Config) -> Optional[Drive]:
    """Which configured drive a container's bind source lives on, if any.

    Matched on whole path components, so /volume1/USB_Backup2 is never taken
    for /volume1/USB_Backup.
    """
    for drive in cfg.drives:
        roots = [drive.path]
        if drive.share:
            roots.append(f"{ALL_SHARES}/{drive.share}")
        if any(_under(source, r) for r in roots):
            return drive
    return None


def find_stale(cfg: Config) -> Optional[List[StaleMount]]:
    """Every container mount still on a device that has moved or vanished.

    Returns None when Docker is not installed (nothing to check), and a list
    otherwise. Raises HostError if Docker is installed but cannot be asked:
    an unanswerable question is not reported as "all fine".
    """
    containers = host.running_containers()
    if containers is None:
        return None

    stale = []
    for c in containers:
        relevant = []
        for source, destination in c.binds:
            drive = drive_for_source(source, cfg)
            if drive:
                relevant.append((destination, drive))
        if not relevant or not c.pid:
            continue

        seen_by_container = host.container_mount_devices(c.pid)
        for destination, drive in relevant:
            seen = seen_by_container.get(destination)
            if not seen or not seen.startswith("/dev/"):
                log.debug("  %s: no device recorded for %s, skipping",
                          c.name, destination)
                continue
            expected = host.device_at(drive.path)
            gone = not host.block_device_present(seen)
            if gone or (expected and seen != expected):
                stale.append(StaleMount(c.name, destination, drive,
                                        seen, expected, gone))
    return stale


def restart_stale(cfg: Config, stale: List[StaleMount]) -> Tuple[List[str], List[str]]:
    """Restart the containers behind `stale`. Returns (restarted, skipped).

    Each container is restarted once, however many of its mounts went stale.
    Honours dry_run, restart_stale and the exclude list; anything not
    restarted is returned as skipped with the reason, so the caller can
    report it rather than let it pass as fixed.
    """
    names = []
    for s in stale:
        if s.container not in names:
            names.append(s.container)

    restarted, skipped = [], []
    for name in names:
        if not cfg.containers.restart_stale:
            skipped.append(f"{name} (containers.restart_stale is off)")
            continue
        if name in cfg.containers.exclude:
            skipped.append(f"{name} (in containers.exclude)")
            continue
        if cfg.dry_run:
            log.info("  [dry run] would restart container %s", name)
            skipped.append(f"{name} (dry run)")
            continue
        ok, output = host.restart_container(
            name, cfg.containers.restart_timeout_sec)
        if ok:
            log.info("  restarted container %s", name)
            restarted.append(name)
        else:
            log.error("  could not restart container %s: %s", name, output)
            skipped.append(f"{name} (restart failed: {output})")
    return restarted, skipped


def check_and_fix(cfg: Config) -> Tuple[List[str], List[object]]:
    """Find stale container mounts, restart what may be restarted, re-check.

    Returns (restarted, remaining). `remaining` holds StaleMount objects still
    wrong after the restarts, plus a plain message if Docker could not be
    queried. Empty means every container using a managed drive is on the
    right device -- or that there is no Docker here at all.
    """
    try:
        stale = find_stale(cfg)
    except host.HostError as exc:
        return [], [f"could not check containers: {exc}"]
    if stale is None:
        log.debug("  docker is not installed; no containers to check")
        return [], []
    if not stale:
        log.info("  no container is holding a stale drive mount")
        return [], []

    for s in stale:
        log.warning("  %s", s)
    restarted, _ = restart_stale(cfg, stale)
    if not restarted:
        return [], list(stale)

    try:
        remaining = find_stale(cfg) or []
    except host.HostError as exc:
        return restarted, [f"could not re-check containers: {exc}"]
    return restarted, list(remaining)
