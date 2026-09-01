"""Checking that drives are genuinely present -- not merely reported present.

Why this module exists at all
-----------------------------
On a Synology NAS, `mount`, `df`, `ls` and even `touch` will all keep
reporting success against a USB device that has physically disappeared.
The kernel serves stale VFS and page-cache data, and writes land in cache
rather than failing. A drive can be gone for minutes while every ordinary
command insists it is fine, and anything relying on those commands will
report healthy right up until the data is lost.

There is also a quieter failure: the bind target exists and is mounted, but
is *empty*. A media server pointed at it does not error -- it rescans, finds
nothing, and records that your library is gone.

So verification here asks three separate questions, and a drive has to pass
all three:

  1. Is the path mounted, per /proc/mounts?
  2. Does the backing device really exist, per /sys/block?
  3. Does the path actually contain anything?
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from . import host
from .config import Config, Drive

NOT_MOUNTED = "not mounted"
DEVICE_ABSENT = "mounted, but the backing device is gone"
EMPTY_STUB = "mounted but empty"
READ_ONLY = "mounted read-only"
NOT_WRITABLE = "mounted, but will not accept a write"


def stacked(layers: int) -> str:
    return f"{layers} mounts stacked on the same path"


@dataclass
class Problem:
    """One drive that failed verification, and why."""
    drive: Drive
    reason: str

    @property
    def device_is_absent(self) -> bool:
        """True when the hardware genuinely is not there.

        Callers use this to tell apart the two failure kinds, because the
        right response differs sharply. An absent device may be recoverable
        by waiting or by power-cycling it. Every other reason here --
        stacked, read-only, not-writable, empty -- means the hardware IS
        present and only the mount is wrong, so power-cycling would cut a
        live drive for nothing. Those are fixed by re-binding instead.
        """
        return self.reason in (NOT_MOUNTED, DEVICE_ABSENT)

    def __str__(self) -> str:
        return f"{self.drive.path}: {self.reason}"


def check_drive(drive: Drive) -> Optional[Problem]:
    """Verify one drive. Returns None when healthy.

    Order matters. Stacking is checked first because it is what produces the
    other faults: a stale layer underneath is invisible to anything that looks
    only at the effective mount. Then the device, then whether the filesystem
    will actually take a write, and only then whether there is content.
    """
    layers = host.mount_layers(drive.path)
    if layers == 0:
        return Problem(drive, NOT_MOUNTED)
    if layers > 1:
        return Problem(drive, stacked(layers))

    device = host.device_at(drive.path)
    if not device or not host.block_device_present(device):
        return Problem(drive, DEVICE_ABSENT)

    # EXT4 remounts read-only on I/O error, which leaves a mount that is
    # structurally perfect and completely useless.
    if host.is_read_only(drive.path):
        return Problem(drive, READ_ONLY)

    # And the mount options can still lie, so actually try it.
    if not host.can_write(drive.path):
        return Problem(drive, NOT_WRITABLE)

    if host.dir_is_empty(drive.path):
        return Problem(drive, EMPTY_STUB)
    return None


def check_all(cfg: Config) -> List[Problem]:
    """Verify every configured drive. Empty list means all healthy."""
    problems = []
    for drive in cfg.drives:
        problem = check_drive(drive)
        if problem:
            problems.append(problem)
    return problems


def confirm_detached(cfg: Config) -> List[Problem]:
    """The inverse check, for after a detach: nothing should still be mounted.

    Reported as Problems so the caller can refuse to continue. A detach
    sequence that believes it finished while a drive is still mounted is
    precisely the state that corrupts filesystems, so this is checked
    explicitly rather than assumed from the absence of errors.
    """
    still_here = []
    for drive in cfg.drives:
        if host.is_mounted(drive.path):
            still_here.append(Problem(drive, "still mounted after detach"))
    return still_here
