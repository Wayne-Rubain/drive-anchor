"""Every operation that reaches the host lives in this file.

This is deliberately the *only* module that shells out, creates mounts, or
reads the host's /proc and /sys. If you are auditing what Drive Anchor does
with the privileges it asks for, you can read this one file and know the
whole story. Nothing elsewhere in the package escapes the process.

Why the host namespace matters
------------------------------
Running inside a container, the process has its own mount namespace. A bind
mount created there is invisible to DSM, to Docker, and to every package on
the NAS -- it would look like it worked and change nothing. So host-affecting
commands are run through `nsenter --target 1 --mount`, which executes them in
PID 1's namespace, i.e. the real host.

When Drive Anchor runs directly on the NAS (no container), that indirection
is unnecessary and `nsenter` may not even be present. `_needs_nsenter()`
detects which situation we are in by comparing namespace identities, so the
same code works both ways with no configuration.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import List, Optional, Tuple

DEFAULT_TIMEOUT = 60


class HostError(RuntimeError):
    """A host command could not be run at all (missing binary, timeout).

    Distinct from a command that ran and returned non-zero -- callers often
    treat that as meaningful (e.g. `umount` failing because nothing was
    mounted) rather than as an error.
    """


def _needs_nsenter() -> bool:
    """True when this process is in a different mount namespace than PID 1.

    Returns False on any error reading the namespace links: if we cannot
    tell, assume we are on the host and run commands directly. Wrapping a
    direct command in an unnecessary nsenter would fail outright, whereas a
    direct command in a container simply affects only the container -- the
    safer wrong answer, and one the caller's own verification will catch.
    """
    try:
        return os.readlink("/proc/self/ns/mnt") != os.readlink("/proc/1/ns/mnt")
    except OSError:
        return False


def run_on_host(argv: List[str], timeout: int = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    """Run a command so its effects land on the host, wherever we are running.

    Returns the CompletedProcess without raising on a non-zero exit; callers
    decide what a failure means. Raises HostError only when the command could
    not be executed or did not finish in time.
    """
    cmd = (["nsenter", "--target", "1", "--mount", "--"] if _needs_nsenter() else []) + argv
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HostError(f"timed out after {timeout}s: {' '.join(argv)}")
    except OSError as exc:
        raise HostError(f"could not run {' '.join(argv)}: {exc}")


def _sh(script: str, timeout: int = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    """Run a small shell snippet on the host.

    Used only where a pipeline or a file read is genuinely simpler than
    marshalling the same thing through argv.
    """
    return run_on_host(["sh", "-c", script], timeout=timeout)


# ---------------------------------------------------------------------------
# Reading host state
#
# NOTE: `mount`, `df`, `ls` and `touch` can all report success against a
# device that has physically gone away -- the kernel serves stale VFS and
# page-cache data for a surprisingly long time, and writes land in cache
# rather than erroring. Every "is this really there?" question in this
# package is therefore answered from /proc/mounts and /sys/block, never from
# those commands.
# ---------------------------------------------------------------------------

def mount_entries() -> List[Tuple[str, str, str]]:
    """Every (device, mountpoint, options) the host currently has mounted.

    Options matter: EXT4 remounts a filesystem read-only when it hits an I/O
    error, and a read-only mount is structurally indistinguishable from a
    healthy one unless you look at this field.
    """
    result = _sh("cat /proc/mounts")
    if result.returncode != 0:
        raise HostError(f"could not read /proc/mounts: {result.stderr.strip()}")
    entries = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            entries.append((parts[0], parts[1], parts[3] if len(parts) > 3 else ""))
    return entries


def proc_mounts() -> List[Tuple[str, str]]:
    """Every (device, mountpoint) pair the host currently has mounted."""
    return [(dev, mp) for dev, mp, _ in mount_entries()]


def mount_layers(path: str) -> int:
    """How many mounts are stacked at `path`.

    More than one is a fault, not a curiosity. Bind mounts stack: mount the
    same path twice and both entries exist, with the kernel resolving to the
    last. A stale layer underneath a healthy one is invisible to every check
    that looks only at the effective mount -- and on 2026-09-01 that hid a
    dead READ-ONLY mount beneath a good one, so a 30GB copy wrote into
    nothing while every health check reported OK.
    """
    return sum(1 for _, mp, _ in mount_entries() if mp == path)


def is_read_only(path: str) -> bool:
    """True if the effective mount at `path` is mounted read-only."""
    opts = None
    for _, mp, options in mount_entries():
        if mp == path:
            opts = options        # last wins, same as the kernel
    if opts is None:
        return False
    return "ro" in opts.split(",")


# A fixed name, deliberately. If a run is killed between the write and the
# delete, at most ONE stale probe can exist per path, and the next run
# overwrites it rather than littering.
WRITE_PROBE = ".drive-anchor-writetest"


def can_write(path: str) -> bool:
    """Whether `path` will actually accept a write.

    The only check that proves the thing that matters. Mount options can say
    `rw` while the filesystem is wedged, the device is half-gone, or share
    permissions are wrong -- and a backup target that silently refuses writes
    looks perfectly healthy to every structural test.
    """
    probe = f"{path.rstrip('/')}/{WRITE_PROBE}"
    result = run_on_host(["touch", probe], timeout=30)
    if result.returncode != 0:
        return False
    run_on_host(["rm", "-f", probe], timeout=30)
    return True


def is_mounted(path: str) -> bool:
    """True if `path` is a mountpoint on the host right now."""
    return any(mp == path for _, mp in proc_mounts())


def device_at(path: str) -> Optional[str]:
    """The device backing `path`, or None if nothing is mounted there.

    Returns the LAST matching entry, not the first. Bind mounts stack: mount
    the same path twice and both entries appear in /proc/mounts, with the most
    recent one shadowing the earlier. The kernel resolves the path to the last
    one, so anything reading the directory sees that device, and reporting the
    first would describe a mount nobody can actually reach.

    This is not theoretical. Running Drive Anchor alongside another script that
    binds the same paths produced exactly this on a live NAS.
    """
    found = None
    for dev, mp in proc_mounts():
        if mp == path:
            found = dev
    return found


def device_for_uuid(uuid: str) -> Optional[str]:
    """Resolve a filesystem UUID to its device node, or None if absent.

    UUID is the identifier Drive Anchor keys on. It belongs to the
    filesystem, survives reformat-free moves between ports and enclosures,
    and -- unlike the kernel's /dev/usbN or /dev/sdX names -- does not change
    when devices are re-enumerated in a different order.
    """
    result = run_on_host(["blkid", "-U", uuid], timeout=30)
    # blkid exits 2 for "no such UUID", which is a real answer: the drive is
    # not attached. Any OTHER non-zero code means the lookup itself failed,
    # and reporting that as "absent" would be a guess dressed as a fact --
    # the caller would wait out its timeout blaming the hardware.
    if result.returncode not in (0, 2):
        raise HostError(
            f"blkid lookup for {uuid} failed: {result.stderr.strip()}")
    dev = result.stdout.strip()
    return dev or None


def parent_device_name(name: str) -> str:
    """The whole-disk name for a partition name, or the name unchanged.

        usb5p1    -> usb5        Synology USB, and NVMe, use a 'p' separator
        nvme0n1p2 -> nvme0n1
        sda1      -> sda         SCSI and SATA append the number directly
        usb5      -> usb5        already a whole disk, nothing to strip

    Exactly one suffix is removed. An earlier version stripped characters off
    the end repeatedly until something in /sys/block matched, which quietly
    produced false positives: on a NAS with usb1 present but usb12 absent,
    'usb12p1' walked down to 'usb1' and reported the device as present. That
    is the worst possible bug here, because this check is what the rest of the
    tool trusts when `mount` and `ls` are lying to it.
    """
    match = re.match(r"^(.+)p\d+$", name)
    if match:
        return match.group(1)
    # Trailing digits count as a partition number only when something
    # non-numeric precedes them, so 'sda1' -> 'sda' but 'usb1' is left alone
    # (it is a whole disk, and callers check the unmodified name first).
    match = re.match(r"^(.*\D)\d+$", name)
    if match:
        return match.group(1)
    return name


def block_device_present(device: str) -> bool:
    """True if the device really exists in /sys/block -- the honest check.

    Accepts either a whole disk (/dev/usb5) or a partition (/dev/usb5p1).
    The unmodified name is checked first, so whole disks whose names end in a
    digit -- usb1, sata2, loop3, md0 -- are never mistaken for partitions.
    """
    name = os.path.basename(device)
    if os.path.exists(f"/sys/block/{name}"):
        return True
    parent = parent_device_name(name)
    return parent != name and os.path.exists(f"/sys/block/{parent}")


def dir_is_empty(path: str) -> bool:
    """True if `path` has no entries. Raises HostError if it cannot be read.

    An empty bind target is the classic silent failure: the mount looks
    present, the directory is a stub, and anything reading it sees an empty
    library rather than an error.

    The first version of this could not tell "empty" from "broken", and said
    empty for both:

        ls -A <path> 2>/dev/null | head -1

    `2>/dev/null` threw away the reason, and piping into `head` meant the exit
    status belonged to `head` -- which succeeds no matter how badly `ls` failed.
    A missing path, a permission error and a genuinely empty directory all
    produced empty stdout and returncode 0.

    That is the same mistake this whole tool exists to catch: treating the
    absence of evidence as evidence of a specific state. `find -print -quit`
    fixes it properly -- it stops at the first entry, so it stays cheap on a
    directory holding a million files, and its exit status is its own.
    """
    result = run_on_host(
        ["find", path, "-mindepth", "1", "-maxdepth", "1", "-print", "-quit"])
    if result.returncode != 0:
        raise HostError(
            f"could not read {path}: {result.stderr.strip() or 'find failed'}")
    return result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Changing host state
# ---------------------------------------------------------------------------

def make_dir(path: str) -> None:
    result = run_on_host(["mkdir", "-p", path])
    if result.returncode != 0:
        raise HostError(f"could not create {path}: {result.stderr.strip()}")


def bind_mount(source: str, target: str) -> None:
    result = run_on_host(["mount", "--bind", source, target])
    if result.returncode != 0:
        raise HostError(
            f"bind {source} -> {target} failed: {result.stderr.strip()}")


MAX_MOUNT_LAYERS = 10


def unmount(path: str) -> bool:
    """Unmount `path` completely. True if nothing is mounted there afterwards.

    Unmounts repeatedly, because bind mounts stack. One `umount` removes one
    layer, so a path bound twice is still mounted after a single call. The
    original version did exactly one, which made a detach report "could not
    unmount, something still has it open" when the real answer was "there was
    another bind underneath" -- and worse, would have left a live mount in
    place while reporting the drive as released.

    Stops early if a `umount` fails to reduce the count, so a genuinely busy
    filesystem reports quickly instead of retrying ten times. A failure here
    is routine rather than exceptional -- most often the path simply was not
    mounted -- so this reports rather than raises.
    """
    for _ in range(MAX_MOUNT_LAYERS):
        before = sum(1 for _, mp in proc_mounts() if mp == path)
        if before == 0:
            return True
        run_on_host(["umount", path])
        after = sum(1 for _, mp in proc_mounts() if mp == path)
        if after >= before:
            return False        # busy, or something is re-mounting it
    return not is_mounted(path)


def _quote(value: str) -> str:
    """Single-quote a value for safe inclusion in a shell snippet."""
    return "'" + value.replace("'", "'\\''") + "'"
