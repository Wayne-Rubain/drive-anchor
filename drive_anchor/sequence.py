"""The two orchestrated sequences: detach and attach.

Ordering is the whole point of this module. Each step exists because doing
it later, or skipping it, causes a specific failure:

DETACH
  1. Pause services      -- otherwise they hold files open and the eject fails
  2. Release binds       -- a bind holds the filesystem busy; DSM will refuse
  3. Eject via DSM       -- retried, then confirmed against DSM's device list
  4. Confirm nothing left mounted

ATTACH
  1. Bind by UUID        -- waiting for hardware that may be slow to appear
  2. Verify              -- mounted, device real, and not an empty stub
  3. Resume services     -- only if step 2 passed

The rule that matters most: **a sequence reports success only when every
step it claims to have done was confirmed.** A detach that says "safe" while
a drive is still mounted is worse than one that says "failed", because a
person acts on the first and investigates the second.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional, Tuple

import requests

from . import binds, quiesce, verify
from .config import Config, Drive, require_credentials
from .dsm import DsmApiError, DsmClient

log = logging.getLogger(__name__)


class SequenceError(RuntimeError):
    """A sequence could not complete safely. The message says what stopped it."""


def dev_id_for(drive: Drive) -> Optional[str]:
    """DSM's device id for a configured drive, e.g. 'usb5'.

    DSM identifies devices by a short name, while Drive Anchor keys on
    filesystem UUID. Bridging them means resolving the UUID to a device node
    (/dev/usb5p1) and stripping the partition suffix.
    """
    from . import host
    device = host.device_for_uuid(drive.uuid)
    if not device:
        return None
    name = os.path.basename(device)
    for cut in range(len(name)):
        candidate = name[: len(name) - cut]
        if candidate and os.path.exists(f"/sys/block/{candidate}"):
            return candidate
    return None


def detach(cfg: Config, only: List[Drive] = None) -> None:
    """Safely release and eject drives. Raises SequenceError if not fully safe.

    `only` restricts the eject to specific drives; by default every USB
    device DSM reports is ejected, including ones not in the config. That
    default is deliberate -- an unmanaged drive left spinning is exactly the
    one nobody remembers to handle.
    """
    require_credentials(cfg)
    targets = only if only is not None else cfg.drives

    log.info("Step 1: pausing services that hold the drives open")
    quiesce.pause(cfg)

    log.info("Step 2: releasing stable-path binds")
    failed_releases = []
    for drive in targets:
        ok, reason = binds.release_one(drive, cfg.dry_run)
        log.info("  %s", reason)
        if not ok:
            failed_releases.append(reason)

    if cfg.dry_run:
        log.info("Step 3: [dry run] would ask DSM to eject every attached "
                 "USB device and confirm each one")
        log.info("Dry run complete -- nothing was changed.")
        return

    log.info("Step 3: ejecting via DSM")
    try:
        with DsmClient(cfg.dsm) as dsm:
            devices = dsm.list_devices()
            if only is not None:
                wanted = {dev_id_for(d) for d in targets}
                devices = [(i, t) for i, t in devices if i in wanted]
            if not devices:
                log.info("  DSM reports no matching USB devices attached")
            else:
                log.info("  DSM reports %d device(s): %s", len(devices),
                         ", ".join(f"{t} ({i})" for i, t in devices))
            unconfirmed = [t for i, t in devices if not dsm.eject_with_retry(i, t)]
    except (DsmApiError, requests.RequestException) as exc:
        # Explicitly not swallowed. If DSM could not be asked, we do not know
        # whether the drives are safe, and saying nothing would let a caller
        # assume they are.
        raise SequenceError(
            f"DSM could not be reached, so the drives were NOT safely "
            f"ejected: {exc}") from exc

    log.info("Step 4: confirming nothing is still mounted")
    still_mounted = verify.confirm_detached(cfg)

    problems = failed_releases + [str(p) for p in still_mounted]
    if unconfirmed:
        problems += [f"DSM never confirmed {t} was ejected" for t in unconfirmed]
    if problems:
        raise SequenceError(
            "detach did NOT complete safely:\n  - " + "\n  - ".join(problems))
    log.info("All drives released and confirmed ejected. Safe to remove power.")


def attach(cfg: Config) -> Tuple[bool, List[verify.Problem]]:
    """Bind drives to their fixed paths and verify. Returns (ok, problems).

    Does not raise on failure. A partial attach is a state a person needs
    reported in detail rather than as an exception, and the caller still has
    cleanup to do -- specifically, deciding not to restart media servers.
    """
    log.info("Step 1: binding drives to their stable paths")
    binds.bind_all(cfg)

    if cfg.dry_run:
        log.info("Step 2: [dry run] would verify every path is mounted, "
                 "backed by a real device, and non-empty")
        return True, []

    log.info("Step 2: verifying")
    problems: List[verify.Problem] = []
    for attempt in range(1, cfg.verify_attempts + 1):
        problems = verify.check_all(cfg)
        if not problems:
            log.info("  all %d drive(s) verified present and populated",
                     len(cfg.drives))
            break
        log.warning("  attempt %d/%d found %d problem(s):",
                    attempt, cfg.verify_attempts, len(problems))
        for p in problems:
            log.warning("    - %s", p)
        if attempt < cfg.verify_attempts:
            import time
            time.sleep(cfg.verify_retry_sec)
            binds.bind_all(cfg)

    ok = not problems
    log.info("Step 3: resuming services")
    quiesce.resume(cfg, start_packages=ok)
    return ok, problems
