# Drive Anchor

Keep Synology USB drives at stable `/volume1` paths, and detach them without
corrupting anything.

---

## Read this first

**This is a snapshot of something I built for my own NAS, published in case
it is useful.** It is not a supported product.

- It has only ever run on **one machine**: a DS923+ on DSM 7.2, with a
  4-bay USB enclosure and a single-drive dock.
- It asks for **root on your NAS** and a **DSM admin password**.
- It uses an **undocumented DSM API** that Synology can change without notice.
- It **ejects drives**. Getting that wrong loses data.

Defaults are set to `dry_run: true` so nothing happens until you deliberately
turn it on. Please leave it that way until you have read the output.

Issues are open and I will read them, but I make no promise to fix anything.
If that is not the deal you want, do not depend on this.

---

## The problem it solves

DSM mounts USB drives under `/volumeUSBn/usbshare`. That path is stable --
DSM pins each `volumeUSBn` to the drive's serial number, and it does this
correctly. **Renumbering is not the problem, and any tool claiming to fix it
is solving something DSM already handles.**

The actual problem is that `/volumeUSBn` sits *outside* `/volume1`:

- **Container Manager will not bind host paths outside `/volume1` shares.**
  A container cannot reach a USB drive directly.
- **Most DSM packages expect `/volume1` paths too**, media servers included.

The usual answer is a hand-made symlink into `/volume1`. It breaks the first
time you eject the drive: the link is left dangling, and whatever was reading
through it starts seeing an empty directory rather than an error.

Then there is the second problem, which is worse and quieter. Detaching a USB
drive safely on a Synology means doing several things in a specific order,
and skipping any of them fails in a way that looks like success:

- A bind mount holds the filesystem busy, so DSM's eject silently refuses
- A running media server holds files open, with the same result
- An indexer that scans while a drive is away decides the content was
  deleted, and rewrites its database to say so
- `mount`, `df`, `ls` and `touch` all keep reporting success against a
  device that has physically gone away, because the kernel serves stale
  cache -- so your health check says fine while the drive is not there

Drive Anchor does those steps in the right order and **confirms each one**.

## What it does not do

- It does not manage internal drives, RAID or storage pools
- It does not back anything up
- It does not control power to your enclosure
- It does not run as a daemon or watch for anything -- you run commands
- It does not work on non-Synology hardware

---

## Why this needs so much privilege

This is the honest version, because you should not run it otherwise.

| What it asks for | Why |
|---|---|
| `pid: host` | To enter PID 1's mount namespace. Without it, a bind mount would exist only inside the container and be invisible to DSM, Docker, and every package on the NAS. |
| `privileged: true` | To mount, unmount, and read the host's block devices under `/sys/block`. |
| DSM admin account | DSM's eject API requires an authenticated admin session. There is no unprivileged way to ask DSM to eject a drive. |

Together that is **effectively root on your NAS**, and the NAS holds
everything you own. You should not take that on trust.

So: **every operation that reaches the host is confined to one file,
[`drive_anchor/host.py`](drive_anchor/host.py).** Nothing else in the package
shells out or touches the filesystem. It is about 180 lines. If you are going
to audit one thing before running this, audit that.

Credentials are read from the environment only, never from the config file,
so they cannot end up in a screenshot or a git commit.

---

## Installing

Requires Container Manager, which needs a 64-bit CPU. All x86_64 Synology
models have it, as do current entry-level ARM models (DS120j, DS220j,
DS223j, DS124). Some older units are excluded.

```bash
git clone https://github.com/Wayne-Rubain/drive-anchor.git
cd drive-anchor
cp config.example.yaml config.yaml
```

Edit `config.yaml`, then create a `.env` beside it:

```
DRIVE_ANCHOR_DSM_ACCOUNT=your_admin_user
DRIVE_ANCHOR_DSM_PASSWORD=your_password
```

Both files are gitignored. Keep it that way.

```bash
docker compose build
./drive-anchor status
```

This container is **not a daemon**. It runs one command and exits, so there
is nothing to `up -d` and nothing left running afterwards.

---

## Using it

```bash
# What USB drives exist, and which are configured?
./drive-anchor list

# Are all my configured drives actually present and populated?
./drive-anchor status

# Bind everything to its stable path and verify
./drive-anchor attach --live

# Safely release and eject everything
./drive-anchor detach --live

# Just one drive
./drive-anchor detach --drive media --live
```

Without `--live`, the config's `dry_run` setting applies, and it defaults to
true. Run everything once without `--live` first.

`./drive-anchor` is a small wrapper that finds the repo, loads `.env`, and
runs the container for you. It passes your arguments through untouched and
preserves the exit code, so `status` still exits non-zero when a drive is
missing. Set `DRIVE_ANCHOR_MODE=native` to skip Docker and run directly with
python3 instead.

### Fixing a broken bind automatically

```bash
./drive-anchor repair --live
```

`repair` is the unattended version of `attach`. It checks first, fixes only
what is actually broken, and **refuses in the two cases where fixing would be
wrong**:

- **Every drive is missing.** That is not a stale bind, it is the enclosure
  being off or a restore still running. Re-binding would wait the full
  timeout for each drive and fix nothing.
- **It has already repaired too often this hour** (3 by default). A bind that
  keeps breaking means the drive is dropping off the USB bus, and silently
  papering over that until the drive dies is worse than not fixing it at all.

Exit codes are the interface, because the caller is usually a scheduler:

| Code | Meaning |
|---|---|
| `0` | Nothing was wrong, or it was fixed |
| `1` | Still broken, or repair was refused -- a person is needed |

This matters more than it sounds. A stale bind is not inert: the path is
still mounted, `ls` still lists files from cache, and writes still appear to
succeed while landing nowhere. The gap between breaking and noticing is the
gap in which a backup job reports success and writes nothing.

### Running it on a schedule

The wrapper takes an absolute path, which is what DSM's Task Scheduler wants.
Create a Triggered Task > User-defined script, running as root:

```
/volume1/docker/drive-anchor/drive-anchor attach
```

On a **boot-up** trigger that re-establishes your stable paths after a
restart. On a **scheduled** trigger it pairs with Hyper Backup's "eject
after backup": the backup ejects the drive, and a later `attach` brings it
back and re-binds it, which otherwise needs doing by hand.

For an ongoing watch, schedule `repair` instead -- hourly is reasonable:

```
/volume1/docker/drive-anchor/drive-anchor repair --live
```

It is quiet and exits 0 when nothing is wrong, so it only shows up in Task
Scheduler's notifications when something actually needs you.

### Adding a drive

**Do not create the share with Control Panel > Shared Folder > Create.** That
makes a folder on your internal volume with no connection to the USB device,
and it looks like it worked. This mistake is easy to make and annoying to
undo.

1. Plug the drive in and let DSM auto-create its share -- it appears as
   `usbshareN` in Control Panel > Shared Folder
2. **Rename** that share to what you want (Edit > General > Name)
3. Run `./drive-anchor add`, which finds the drive and prints its config entry
4. Paste that into the `drives:` list in `config.yaml` and edit the name
   and path
5. `./drive-anchor attach --live`

### Removing a drive

```bash
./drive-anchor remove media --live
```

It releases the bind, ejects the drive, confirms DSM agrees it is gone, and
then prints the config lines to delete. It will not tell you the drive is
safe to unplug unless the eject was actually confirmed.

`add` and `remove` do not rewrite `config.yaml`. Editing YAML
programmatically destroys comments and reorders keys, and this is a file
people annotate.

---

## When it breaks

**"DSM login failed"** -- the account needs admin rights. Check
`DRIVE_ANCHOR_DSM_PASSWORD` is actually set in the container's environment.

**A drive shows "not mounted" right after attaching** -- some hardware is
slow. Powered docks in particular can take over a minute to re-enumerate.
Raise `bind_wait_sec` before concluding the drive is faulty.

**A drive shows "mounted but empty"** -- the device is present but the bind
is wrong. Re-run `attach`. Do not power-cycle the drive; it is live.

**A drive shows "mounted, but the backing device is gone"** -- this is the
stale bind, and it is the reason the device check exists. The drive dropped
off USB and came back under a different device node; DSM remounted it
correctly, but the `/volume1` bind still points at the node that no longer
exists. `ls` through the bind will happily list files from cache, so nothing
looks wrong until a write is silently lost. Run `attach --live` to rebind.

Some hardware does this spontaneously, with no power cut involved -- powered
docks especially. If you see it repeatedly on one drive, a scheduled
`status` is a cheap way to catch it early.

**Eject fails with the drive apparently idle** -- something still has it
open. Add the responsible package to `quiesce.packages`.

**Everything broke after a DSM update** -- likely the undocumented eject
API changed. See the warning at the top of
[`drive_anchor/dsm.py`](drive_anchor/dsm.py).

---

## Prior art

Point solutions exist and may be all you need:

- [`aivus/synology-dsm-scripts`](https://github.com/aivus/synology-dsm-scripts)
  -- eject and remount a USB disk by device name
- [`schmidhorst/synology-UsbEject`](https://github.com/schmidhorst/synology-UsbEject)
  -- a DSM package letting non-admins eject devices

What they do not do is the ordering: bind, release, quiesce, eject, confirm,
verify. That is the gap this fills.

---

## License

MIT. See [LICENSE](LICENSE). Note in particular the part where it comes with
no warranty of any kind.
