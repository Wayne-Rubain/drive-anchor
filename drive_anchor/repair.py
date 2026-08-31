"""Unattended repair of broken binds.

`attach` sets everything up and is meant to be run by a person. `repair` is
the version you put on a schedule: it checks first, fixes only what is
actually broken, refuses in the cases where fixing would be wrong, and exits
with a code a scheduler can act on.

Why this is worth automating at all
-----------------------------------
A stale bind is not inert. The path is still mounted, `ls` still lists files
from cache, and writes still appear to succeed -- they land in page cache and
are lost. So the window between breaking and noticing is not merely downtime,
it is the window in which a backup job can report success and write nothing.
Shortening it matters more than it would for most faults.

The tool already knows what is wrong and already knows the fix. Waiting for a
human to read an alert is the slowest part of the whole loop.

When it deliberately refuses
----------------------------
**Every drive missing.** That is not a stale bind, it is the drives being
absent -- the enclosure is off, or a restore is still in progress. Re-binding
would wait the full timeout for each one and fix nothing, and on a slow
enclosure that can take longer than the interval the job runs on. Report it
and let a person look.

**Too many repairs in an hour.** Self-healing that silently papers over a
drive dropping off the bus every ten minutes is worse than no self-healing,
because nobody ever learns the drive is dying. Past the cap it stops and says
so.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import List, Optional

from . import binds, verify
from .config import Config
from .verify import Problem

log = logging.getLogger(__name__)

HEALTHY = "healthy"
PARTIAL = "partial"
WHOLESALE = "wholesale"


@dataclass
class Outcome:
    """What a repair run did, and whether anyone needs to know."""
    situation: str
    repaired: List[str]
    remaining: List[Problem]
    refused_reason: Optional[str] = None
    repairs_this_hour: int = 0

    @property
    def needs_attention(self) -> bool:
        return bool(self.remaining) or bool(self.refused_reason)

    @property
    def exit_code(self) -> int:
        return 1 if self.needs_attention else 0


def classify(problems: List[Problem], total_drives: int) -> str:
    """Decide which of the three situations we are in.

    The distinction that matters is whether the *hardware* is there. If every
    configured drive is missing, nothing is bindable and this is a power or
    restore situation. If even one drive is healthy -- or if a problem is an
    empty stub, which means that device is present and only the bind is wrong
    -- then the hardware is around and re-binding is the right move.
    """
    if not problems:
        return HEALTHY
    if len(problems) == total_drives and all(p.device_is_absent for p in problems):
        return WHOLESALE
    return PARTIAL


# ---------------------------------------------------------------------------
# Repair history
#
# Kept on disk so the cap survives between scheduled runs -- the whole point
# is to notice a drive that keeps breaking across invocations. If the state
# directory is not writable the cap is skipped with a warning rather than
# failing: a single run can only ever repair once, so the unbounded case is
# a scheduler calling it repeatedly, and that is the case a warning is for.
# ---------------------------------------------------------------------------

def _history_file(cfg: Config) -> Optional[str]:
    if not cfg.repair.state_dir:
        return None
    try:
        os.makedirs(cfg.repair.state_dir, exist_ok=True)
        return os.path.join(cfg.repair.state_dir, "repair_history")
    except OSError:
        return None


def repairs_last_hour(cfg: Config) -> int:
    path = _history_file(cfg)
    if not path or not os.path.exists(path):
        return 0
    cutoff = time.time() - 3600
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read().split()
    except OSError:
        return 0

    # Parse tolerantly, one token at a time. A single unreadable entry must
    # not zero the whole count: this cap is a safety limit, and a limit that
    # fails OPEN on corrupt input is worse than no limit, because it looks
    # like it is working. Skipping the bad token keeps the cap enforced on
    # everything still legible.
    stamps = []
    for token in raw:
        try:
            stamps.append(float(token))
        except ValueError:
            log.warning("  ignoring an unreadable entry in the repair history")

    recent = [s for s in stamps if s >= cutoff]
    try:  # prune so the file cannot grow without bound
        with open(path, "w", encoding="utf-8") as fh:
            # The trailing newline is load-bearing. record_repair() appends,
            # so a file that does not end in one gets its last timestamp
            # glued to the next -- producing a single unparseable token and,
            # before the tolerant parsing above, silently disabling the cap.
            for stamp in recent:
                fh.write(f"{stamp}\n")
    except OSError:
        pass
    return len(recent)


def record_repair(cfg: Config) -> None:
    path = _history_file(cfg)
    if not path:
        log.warning("  repair history is not writable; the hourly cap is not "
                    "being enforced across runs")
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.time()}\n")
    except OSError as exc:
        log.warning("  could not record this repair (%s); the hourly cap may "
                    "not be enforced", exc)


# ---------------------------------------------------------------------------

def run(cfg: Config) -> Outcome:
    """Check, and repair only if repairing is the right thing to do."""
    problems = verify.check_all(cfg)
    situation = classify(problems, len(cfg.drives))

    if situation == HEALTHY:
        log.info("All %d drive(s) present and populated. Nothing to do.",
                 len(cfg.drives))
        return Outcome(HEALTHY, [], [])

    for p in problems:
        log.warning("  %s", p)

    if situation == WHOLESALE:
        reason = (f"all {len(cfg.drives)} drives are missing, so this is not a "
                  f"stale bind -- the enclosure is probably powered off, or a "
                  f"restore is still in progress. Not attempting a repair.")
        log.error("  %s", reason)
        return Outcome(WHOLESALE, [], problems, refused_reason=reason)

    used = repairs_last_hour(cfg)
    if used >= cfg.repair.max_per_hour:
        reason = (f"already repaired {used} time(s) in the last hour "
                  f"(limit {cfg.repair.max_per_hour}). A bind that keeps "
                  f"breaking usually means the drive or its enclosure is "
                  f"dropping off the USB bus. Not repairing again.")
        log.error("  %s", reason)
        return Outcome(PARTIAL, [], problems, refused_reason=reason,
                       repairs_this_hour=used)

    if cfg.dry_run:
        names = ", ".join(p.drive.label for p in problems)
        log.info("  [dry run] would re-bind: %s", names)
        return Outcome(PARTIAL, [], problems,
                       refused_reason="dry run -- nothing was changed")

    log.info("  repairing %d drive(s) (%d/%d repairs used this hour)",
             len(problems), used, cfg.repair.max_per_hour)
    record_repair(cfg)

    repaired = []
    # Only the broken ones. Re-binding a drive that is already correct would
    # churn a working mount for no reason.
    for problem in problems:
        ok, reason = binds.bind_one(problem.drive, cfg)
        log.info("  %s", reason)
        if ok:
            repaired.append(problem.drive.label)

    remaining = verify.check_all(cfg)
    if not remaining:
        log.info("  repaired: all %d drive(s) now verify", len(cfg.drives))
    else:
        log.error("  still broken after repair:")
        for p in remaining:
            log.error("    - %s", p)
    return Outcome(PARTIAL, repaired, remaining, repairs_this_hour=used + 1)
