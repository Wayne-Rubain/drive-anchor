# What this tool does with root access

Drive Anchor asks for a lot: administrator rights on your NAS, and your DSM
admin password. Your NAS probably holds your photos, your documents, and your
backups. You should not hand that over to a stranger's project because a
README told you it was fine.

This page exists so you can check for yourself, without reading Python.

If after reading it you are not comfortable, **don't install it.** That is a
completely reasonable conclusion and no explanation is needed.

---

## Why it needs anything unusual at all

Some background, because the reason is not obvious.

When you plug a USB drive into a Synology, DSM mounts it somewhere like
`/volumeUSB2/usbshare`. That location is outside `/volume1`, which is where
all your normal shared folders live.

That matters because **Synology's Container Manager refuses to give a
container access to anything outside `/volume1`**, and most DSM packages
expect `/volume1` paths too. So a USB drive is awkward to use with Docker or
a media server, even though it is sitting right there.

Drive Anchor fixes that by making the USB drive *also* appear at a normal
`/volume1` path. The technique is called a **bind mount** — think of it as
making the same folder visible in two places at once. Not a copy, not a
shortcut. One set of files, two doors into it.

Creating that second door is an operation only the system administrator can
perform. Hence root.

The second reason is ejecting. Safely disconnecting a USB drive on a
Synology means asking DSM itself to do it, and DSM will only take that
instruction from an administrator account. There is no unprivileged way to
ask.

---

## What each permission is for

If you install with Docker, `docker-compose.yml` asks for three things.

### `privileged: true`

Lets the tool create and remove mounts, and read the list of disks the system
can actually see. Without it, every mount operation is refused.

### `pid: host`

A container normally cannot see the rest of the system. This lets the tool
reach the main system so that a bind mount it creates is visible to DSM, to
Docker, and to your media server.

Without this, mounts would exist **only inside the container** and vanish
when it exits. The tool would appear to work and change nothing.

### A DSM administrator account

Used for exactly one thing: asking DSM to eject a drive, through the same
internal interface Storage Manager uses when you click the eject button.

The account name and password are read from the environment, never from the
configuration file. Config files get shared in forum posts and committed to
Git by accident. This one cannot leak that way, because it is not in there.

---

## The complete list of commands it will run

This is all of it. Not a summary, not the interesting ones. Everything.

| Command | What it does | When |
|---|---|---|
| `blkid` | Lists filesystems and their IDs | `list`, `add` |
| `blkid -U <id>` | Finds which disk has a given ID | Any bind operation |
| `cat /proc/mounts` | Reads what is currently mounted | Every command |
| `ls -A <path>` | Checks whether a folder is empty | Verification |
| `mkdir -p <path>` | Creates the folder the drive will appear at | `attach`, `repair` |
| `mount --bind <a> <b>` | Makes the drive appear at your chosen path | `attach`, `repair` |
| `umount <path>` | Removes that second door | `detach` |
| `test -x <file>` | Checks a Synology tool exists | `status` |
| `synopkg stop\|start <pkg>` | Pauses/resumes media servers you listed | `detach`, `attach` |
| `synoindex_mgr --disable-share\|--enable-share` | Pauses/resumes DSM indexing | `detach`, `attach` |
| `synowebapi --exec api=... method=eject` | Asks DSM to eject a drive | `detach`, `remove` |

That is the entire list. Eleven commands, and no network calls at all on the
default settings.

### About that last one

`synowebapi` is a Synology command, present on the NAS already, that invokes
DSM's own internal API locally. It is how Drive Anchor asks DSM to eject a
drive, and it is the reason **you do not need to give this tool a DSM
password**. Because it runs locally as root, DSM accepts the request without
a login.

If your DSM version does not have that command, the tool falls back to making
an authenticated HTTPS request to **your own NAS** at `localhost:5001`, and
*that* is the only situation in which an admin account is needed. Run
`drive-anchor status` and it will tell you which one your install is using.

---

## What it never does

- **No internet access.** The only network request goes to `localhost`, which
  is the machine it is already running on. Nothing is sent anywhere.
- **No telemetry, analytics, or phoning home.** There is nothing to opt out of.
- **It never formats, partitions, or erases a disk.** There is no such
  command in the list above, and no code path that could reach one.
- **It never deletes your files.** It creates and removes *mounts*, which
  changes where files appear, not whether they exist.
- **It does not touch internal drives, RAID, or storage pools.** It only ever
  operates on the paths you list in your configuration.

---

## Checking these claims yourself

You do not have to take any of the above on trust.

**1. Only one file can run anything.**

Every command the tool runs goes through a single file, `drive_anchor/host.py`.
Nothing else in the project can execute anything. Verify it:

```bash
grep -rn "subprocess" drive_anchor/
```

Every result should be in `host.py`. If any other file appears there, that
promise has been broken and you should be suspicious.

**2. See the command list in the source.**

```bash
grep -n "run_on_host(\[" drive_anchor/*.py
```

That prints most of the table above, straight from the code.

Two entries will not show up there: `cat /proc/mounts` and `ls -A`. Those go
through a small helper for reading text, so they appear as
`run_on_host(["sh", "-c", script])` instead. To see them:

```bash
grep -n "_sh(" drive_anchor/host.py
```

Between those two commands you have seen every command this tool can run.
There is no third route -- `run_on_host` is the only function that executes
anything, and `_sh` just calls it.

**3. Check for network calls.**

```bash
grep -rn "requests\.\|http" drive_anchor/
```

The only URLs are built from the `dsm.host` value in your own configuration,
which defaults to `localhost`.

**4. Watch it without letting it act.**

The tool ships with `dry_run: true`. In that mode it prints what it *would*
do and changes nothing:

```bash
./drive-anchor detach
```

Run every command that way first. Nothing happens until you add `--live`.

---

## Reducing what you have to trust

**Use a dedicated DSM account.** Rather than your own admin login, make a
separate administrator account for this tool. If you ever want to revoke its
access, you disable that one account and nothing else is affected.

**Read `host.py`.** It is about 180 lines, and roughly half of that is
comments explaining why each piece exists. It is genuinely readable in ten
minutes, even if you do not write Python — the command names in it are the
same ones in the table above.

**Run it without Docker.** If handing a container these permissions is the
part you dislike, skip the container:

```bash
DRIVE_ANCHOR_MODE=native ./drive-anchor status
```

That runs the same code directly on the NAS. It still needs root, but there
is no privileged container involved.

---

## If something looks wrong

If you find that this tool does something not on this page, that is a bug in
either the tool or this document, and it is worth reporting either way. Open
an issue.
