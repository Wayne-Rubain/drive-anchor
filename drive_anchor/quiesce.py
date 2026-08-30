"""Pausing and resuming the services that hold USB drives open.

Two separate problems, both worth solving before an eject:

**Open file handles.** A running media server keeps files open on the drive.
DSM's eject refuses while anything has the filesystem busy, so the eject
fails for a reason that looks nothing like "Plex is running".

**Indexers rewriting their catalogue.** Worse than the first, and quieter.
If DSM's file indexer or a media server scans a share while the drive is
detached or half-mounted, it does not error -- it concludes the content has
been deleted and updates its database accordingly. You get the drive back
and the library is empty.

Both are handled by stopping the packages and pausing indexing first, then
restoring them only once the drives have been verified genuinely back.
"""

from __future__ import annotations

import logging
from typing import List

from . import host
from .config import Config

log = logging.getLogger(__name__)

SYNOPKG = "/usr/syno/bin/synopkg"
SYNOINDEX = "/usr/syno/bin/synoindex_mgr"


def _package(action: str, pkg: str, timeout: int) -> bool:
    result = host.run_on_host([SYNOPKG, action, pkg], timeout=timeout)
    if result.returncode == 0:
        log.info("  %s %s", action + "ped" if action == "stop" else action + "ed", pkg)
        return True
    log.warning("  could not %s %s: %s", action, pkg,
                (result.stderr or result.stdout).strip())
    return False


def _index(flag: str, share: str, timeout: int) -> bool:
    result = host.run_on_host([SYNOINDEX, flag, share], timeout=timeout)
    if result.returncode == 0:
        return True
    log.warning("  could not run %s %s: %s", flag, share,
                (result.stderr or result.stdout).strip())
    return False


def pause(cfg: Config) -> None:
    """Stop configured packages and pause indexing. Best effort by design.

    Failures are logged but not raised. If a media server cannot be stopped,
    the eject that follows will fail on its own and report that clearly --
    whereas aborting here would leave the packages half-stopped with no
    detach performed and no obvious way back.
    """
    q = cfg.quiesce
    if cfg.dry_run:
        if q.packages:
            log.info("  [dry run] would stop: %s", ", ".join(q.packages))
        if q.index_shares:
            log.info("  [dry run] would pause indexing on: %s",
                     ", ".join(q.index_shares))
        return
    for pkg in q.packages:
        _package("stop", pkg, q.command_timeout_sec)
    for share in q.index_shares:
        if _index("--disable-share", share, q.command_timeout_sec):
            log.info("  paused indexing for %s", share)


def resume(cfg: Config, start_packages: bool = True) -> None:
    """Re-enable indexing and restart packages.

    `start_packages=False` is the important case: when the drives did not
    verify, indexing is restored but the media servers are deliberately left
    stopped. Starting a media server against a half-mounted library is how
    you lose the library -- it rescans, finds nothing, and saves that. A
    stopped service is an obvious, reversible problem; an emptied catalogue
    is not.
    """
    q = cfg.quiesce
    if cfg.dry_run:
        log.info("  [dry run] would resume indexing and restart packages")
        return
    for share in q.index_shares:
        if _index("--enable-share", share, q.command_timeout_sec):
            log.info("  resumed indexing for %s", share)
    if not start_packages:
        if q.packages:
            log.warning("  NOT starting %s -- drives did not verify.",
                        ", ".join(q.packages))
            log.warning("  Start them by hand once the mounts are correct:")
            for pkg in q.packages:
                log.warning("    %s start %s", SYNOPKG, pkg)
        return
    for pkg in q.packages:
        _package("start", pkg, q.command_timeout_sec)


def missing_tools() -> List[str]:
    """Which Synology helper binaries are absent.

    Used by `status` so a user on a non-Synology box, or a DSM version that
    moved these, gets a clear explanation instead of a puzzling failure
    later on.
    """
    absent = []
    for tool in (SYNOPKG, SYNOINDEX):
        if host.run_on_host(["test", "-x", tool], timeout=15).returncode != 0:
            absent.append(tool)
    return absent
