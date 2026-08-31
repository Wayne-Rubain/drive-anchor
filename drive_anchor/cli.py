"""Command line interface.

    drive-anchor status     is everything where it should be?
    drive-anchor list       what USB devices exist, and are they configured?
    drive-anchor detach     safely release and eject, keeping it configured
    drive-anchor attach     bind, verify, resume services
    drive-anchor repair     fix broken binds if any -- built for a schedule
    drive-anchor add        discover a new drive and produce its config entry
    drive-anchor remove     take a drive off for good, and unconfigure it

`detach` and `remove` perform the same safe eject. They differ in intent:
`detach` expects the drive back, `remove` does not, and so also tells you
what to delete from the config.

`add` and `remove` deliberately do not rewrite your config file. Editing
YAML programmatically destroys comments and reorders keys, and this is a
file people annotate. Both commands do the part that is genuinely hard --
finding the UUID, checking the drive, ejecting it safely -- and then print
the exact lines to add or delete.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import List

from . import binds, config as config_mod, host, quiesce, repair as repair_mod, verify
from .config import Config, ConfigError
from . import dsm as dsm_mod
from .sequence import SequenceError, attach, detach, dev_id_for


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s", stream=sys.stdout)


def _load(args) -> Config:
    # getattr with a default: the shared flags use argparse.SUPPRESS, so they
    # are absent from the namespace entirely when not given.
    cfg = config_mod.load(getattr(args, "config", None))
    if getattr(args, "live", False):
        cfg.dry_run = False
    return cfg


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_status(cfg: Config, args) -> int:
    print(f"Drive Anchor -- {len(cfg.drives)} drive(s) configured"
          f"{'  [DRY RUN MODE]' if cfg.dry_run else ''}\n")

    # Which way ejects will be performed, and therefore whether credentials
    # are needed at all. Worth stating plainly: it is the first thing anyone
    # debugging an eject wants to know.
    if dsm_mod.needs_credentials(cfg.dsm):
        have = "set" if cfg.dsm.password else "NOT SET"
        print(f"  Eject transport: DSM HTTP API, credentials {have}")
    else:
        print("  Eject transport: synowebapi (no credentials needed)")
    print()

    missing = quiesce.missing_tools()
    if missing:
        print("  WARNING: Synology tools not found: " + ", ".join(missing))
        print("  Service pausing will not work. Is this a Synology NAS?\n")

    problems = {p.drive.path: p for p in verify.check_all(cfg)}
    for drive in cfg.drives:
        problem = problems.get(drive.path)
        if not problem:
            device = host.device_at(drive.path)
            print(f"  OK       {drive.name:<24} {drive.path}  ({device})")
        else:
            print(f"  PROBLEM  {drive.name:<24} {problem.reason}")

    if not problems:
        print(f"\nAll {len(cfg.drives)} drive(s) present and populated.")
        return 0
    print(f"\n{len(problems)} drive(s) need attention. "
          f"Try: drive-anchor attach --live")
    return 1


def cmd_list(cfg: Config, args) -> int:
    """Show every USB filesystem the host can see, configured or not."""
    configured = {d.uuid: d for d in cfg.drives}
    print("USB filesystems visible to the host:\n")

    result = host.run_on_host(["blkid"], timeout=30)
    rows = []
    for line in result.stdout.splitlines():
        if "UUID=" not in line:
            continue
        device = line.split(":", 1)[0]
        uuid = line.split('UUID="', 1)[1].split('"', 1)[0]
        mountpoint = next((mp for dev, mp in host.proc_mounts()
                           if dev == device), "-")
        if not mountpoint.startswith("/volumeUSB") and uuid not in configured:
            continue  # internal volume, not our business
        drive = configured.get(uuid)
        rows.append((device, uuid, mountpoint,
                     drive.name if drive else "NOT CONFIGURED"))

    if not rows:
        print("  none found")
        return 0
    width = max(len(r[3]) for r in rows)
    for device, uuid, mountpoint, name in rows:
        print(f"  {name:<{width}}  {device:<14} {mountpoint:<26} {uuid}")

    unconfigured = [r for r in rows if r[3] == "NOT CONFIGURED"]
    if unconfigured:
        print(f"\n{len(unconfigured)} drive(s) not in your config. "
              f"Run: drive-anchor add")
    return 0


def cmd_detach(cfg: Config, args) -> int:
    only = None
    if args.drive:
        only = [d for d in cfg.drives if d.name in args.drive]
        missing = set(args.drive) - {d.name for d in only}
        if missing:
            print(f"ERROR: no configured drive named: {', '.join(missing)}",
                  file=sys.stderr)
            return 2
    try:
        detach(cfg, only=only)
    except (SequenceError, ConfigError) as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_attach(cfg: Config, args) -> int:
    ok, problems = attach(cfg)
    if ok:
        print("\nDone. All drives are bound and verified.")
        return 0
    print("\nFINISHED WITH PROBLEMS -- these paths are not right:",
          file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    if cfg.quiesce.packages:
        print("\nMedia packages were deliberately left stopped so they do not "
              "scan a half-mounted library.", file=sys.stderr)
    return 1


def cmd_repair(cfg: Config, args) -> int:
    """Check, and fix only what is broken. Built to run unattended.

    Exit code is the interface here, because the caller is usually a
    scheduler rather than a person:
        0  nothing was wrong, or it was fixed
        1  something is still wrong and a human is needed
    """
    if args.max_per_hour is not None:
        cfg.repair.max_per_hour = args.max_per_hour

    outcome = repair_mod.run(cfg)

    if outcome.situation == repair_mod.HEALTHY:
        return 0
    if outcome.refused_reason:
        print(f"\nNot repaired: {outcome.refused_reason}", file=sys.stderr)
        return outcome.exit_code
    if outcome.repaired:
        print(f"\nRepaired: {', '.join(outcome.repaired)}"
              f"  ({outcome.repairs_this_hour} repair(s) this hour)")
    if outcome.remaining:
        print("Still broken:", file=sys.stderr)
        for p in outcome.remaining:
            print(f"  - {p}", file=sys.stderr)
    return outcome.exit_code


def cmd_add(cfg: Config, args) -> int:
    """Find drives that are attached but not configured, and emit their YAML."""
    configured = {d.uuid for d in cfg.drives}
    found = []
    result = host.run_on_host(["blkid"], timeout=30)
    for line in result.stdout.splitlines():
        if "UUID=" not in line:
            continue
        device = line.split(":", 1)[0]
        uuid = line.split('UUID="', 1)[1].split('"', 1)[0]
        mountpoint = next((mp for dev, mp in host.proc_mounts()
                           if dev == device), None)
        if mountpoint and mountpoint.startswith("/volumeUSB") and uuid not in configured:
            found.append((device, uuid, mountpoint))

    if not found:
        print("Every attached USB drive is already configured.")
        return 0

    print(f"Found {len(found)} unconfigured USB drive(s).\n")
    print("Before adding one, make sure DSM has a share for it. Do NOT use")
    print("Control Panel > Shared Folder > Create -- that makes a folder on")
    print("the internal volume with no connection to the USB device. Instead,")
    print("RENAME the share DSM created for you (it appears as 'usbshareN').\n")
    print("Then add this to the 'drives:' list in your config:\n")
    for device, uuid, mountpoint in found:
        suggested = mountpoint.split("/")[-1]
        print(f"  # currently {device} at {mountpoint}")
        print(f"  - name: CHANGE_ME")
        print(f"    uuid: \"{uuid}\"")
        print(f"    path: /volume1/CHANGE_ME")
        print(f"    share: {suggested}")
        print()
    print("Then run:  drive-anchor attach --live")
    return 0


def cmd_remove(cfg: Config, args) -> int:
    drive = cfg.drive_by_name(args.name)
    if not drive:
        print(f"ERROR: no configured drive named {args.name!r}. "
              f"Known: {', '.join(d.name for d in cfg.drives) or '(none)'}",
              file=sys.stderr)
        return 2

    print(f"Removing {drive.name} ({drive.path})\n")
    try:
        detach(cfg, only=[drive])
    except (SequenceError, ConfigError) as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        print("The drive was NOT safely ejected. Do not unplug it.",
              file=sys.stderr)
        return 1

    if not cfg.dry_run:
        print(f"\n{drive.name} is ejected and safe to unplug.")
    print(f"\nTo stop managing it, delete this entry from your config:\n")
    print(f"  - name: {drive.name}")
    print(f"    uuid: \"{drive.uuid}\"")
    print(f"    path: {drive.path}")
    if drive.share:
        print(f"    share: {drive.share}")
    return 0


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    # The global flags are attached to BOTH the top-level parser and every
    # subcommand, so `--live repair` and `repair --live` both work. People
    # reach for the second form naturally, and having it fail with
    # "unrecognized arguments" is a miserable first impression.
    #
    # default=SUPPRESS is what makes that safe: without it, a subparser
    # re-parsing the same flag would write its own default back over a value
    # already set before the subcommand, so `--live repair` would silently
    # run in dry-run mode. Absent flags simply do not appear on the
    # namespace, so read them with getattr and a default.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help="path to config.yaml (default: "
                             "$DRIVE_ANCHOR_CONFIG or /data/config.yaml)")
    common.add_argument("--live", action="store_true", default=argparse.SUPPRESS,
                        help="actually make changes. Without this, the "
                             "config's dry_run setting applies, and it "
                             "defaults to true.")
    common.add_argument("-v", "--verbose", action="store_true",
                        default=argparse.SUPPRESS)

    p = argparse.ArgumentParser(
        prog="drive-anchor", parents=[common],
        description="Keep Synology USB drives at stable /volume1 paths, and "
                    "detach them without corrupting anything.")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status", parents=[common],
                   help="are all configured drives present?")
    sub.add_parser("list", parents=[common],
                   help="show every USB drive, configured or not")
    sub.add_parser("add", parents=[common],
                   help="discover a new drive and print its config")

    d = sub.add_parser("detach", parents=[common],
                       help="safely release and eject; stays configured, "
                            "for a drive you intend to bring back")
    d.add_argument("--drive", action="append",
                   help="only this drive, by name (repeatable). Default: all.")

    sub.add_parser("attach", parents=[common],
                   help="bind, verify, and resume services")

    rp = sub.add_parser("repair", parents=[common],
                        help="fix broken binds if any; for scheduled runs")
    rp.add_argument("--max-per-hour", type=int, default=None,
                    help="override the hourly repair cap (default from config)")

    r = sub.add_parser("remove", parents=[common],
                       help="take a drive off the NAS for good: eject it, "
                            "then show how to unconfigure it")
    r.add_argument("name", help="configured drive name")
    return p


COMMANDS = {
    "status": cmd_status, "list": cmd_list, "detach": cmd_detach,
    "attach": cmd_attach, "add": cmd_add, "remove": cmd_remove,
    "repair": cmd_repair,
}


def main(argv: List[str] = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    try:
        cfg = _load(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        return COMMANDS[args.command](cfg, args)
    except host.HostError as exc:
        print(f"Could not reach the host system: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
