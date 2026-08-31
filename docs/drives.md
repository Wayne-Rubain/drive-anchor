# Adding, removing and replacing drives

Most of this page is about **one mistake**, because it is the one nearly
everybody makes, it looks like it worked, and it can quietly fill up your
internal disk for weeks before you notice.

If you read nothing else, read the next section.

---

## The mistake: Create versus Rename

When you add a USB drive, Drive Anchor needs DSM to already have a shared
folder for it. There are two ways to end up with a shared folder of the right
name, and **only one of them works**.

### Wrong

> Control Panel > Shared Folder > **Create**

This is the obvious thing to do, and it is wrong. It creates a brand new,
empty folder **on your internal hard drives**. It has no connection to the
USB drive whatsoever. It just happens to have the name you wanted.

### Right

> Plug the drive in, let DSM create its own folder, then **rename** that one.
>
> Control Panel > Shared Folder > select `usbshare2` > **Edit** > General >
> change the Name field

DSM makes a folder for every USB drive automatically, called something like
`usbshare1` or `usbshare2`. Renaming *that* folder keeps it attached to the
actual drive.

### Why the wrong way is so convincing

Here is the part that makes this genuinely nasty.

If you create the share the wrong way, **everything still appears to work.**
Drive Anchor will bind the real USB drive over the top of that internal
folder. Your files show up. `drive-anchor status` reports OK. Your media
server finds its library.

But underneath, hidden by the bind mount, sits a real folder on your internal
disk. And any time the bind is not active — after a reboot, before the tool
has run, while the drive is detached — **writes go into that hidden internal
folder instead of onto the USB drive.**

You cannot see them, because the bind mount covers them up. They still
consume space on your internal volume. People discover this months later,
wondering why Volume 1 is mysteriously full.

---

## How to tell which one you have

Go to **Control Panel > Shared Folder** and look at the **Location** column.

| Location shows | Meaning |
|---|---|
| A USB volume, e.g. `USB Disk 1` | Correct. Renamed share, attached to the drive. |
| `Volume 1` (or any internal volume) | **Wrong.** This is a folder on your internal disks. |

That single column is the whole test. It takes five seconds and it is worth
checking even if you are confident.

Note that `drive-anchor status` **cannot** detect this for you. From the
tool's point of view the bind is present and the files are there, which is
exactly what a healthy drive looks like. Only DSM knows the difference.

---

## Fixing it if you already did it the wrong way

Nothing is lost. Work through this in order.

**1. Release the bind so you can see underneath.**

```bash
./drive-anchor detach --drive <name> --live
```

**2. Look at what is hiding under there.**

```bash
ls -la /volume1/<the-share-name>
```

Anything here is on your **internal** disk. It is data that was written while
the bind was not active. It might be empty, or it might be a lot.

**3. Rescue anything you want to keep.** Copy it somewhere safe. Do not skip
this on the assumption that it is empty — check.

**4. Delete the wrongly-created share.**

> Control Panel > Shared Folder > select it > Delete

DSM will warn you it is deleting data. That is the hidden internal copy, which
is what you want gone. Your USB drive is a separate thing and is not affected.

**5. Now do it the right way.** Unplug the USB drive and plug it back in. DSM
will create a fresh `usbshareN` folder for it. Rename *that* one.

**6. Put back anything you rescued in step 3**, then:

```bash
./drive-anchor attach --live
```

**7. Confirm.** Control Panel > Shared Folder, check the Location column now
shows a USB volume.

---

## Adding a drive, start to finish

**1. Plug it in.** Give DSM a moment to notice it. It appears under Control
Panel > External Devices.

**2. Rename its shared folder.** Control Panel > Shared Folder, find
`usbshareN`, Edit, change the Name to something meaningful like `USB_Media`.
**Rename, do not create.**

**3. Ask Drive Anchor to find it.**

```bash
./drive-anchor add
```

It looks for USB drives that are plugged in but not yet in your
configuration, and prints a ready-made entry for each:

```yaml
  # currently /dev/usb3p1 at /volumeUSB2/usbshare
  - name: CHANGE_ME
    uuid: "a1b2c3d4-5678-90ab-cdef-1234567890ab"
    path: /volume1/CHANGE_ME
    share: usbshare
```

**4. Paste it into `config.yaml`** under `drives:`, and replace both
`CHANGE_ME` values. `name` is just a label for the commands. `path` is where
the drive will appear, and it should match the share name you chose:

```yaml
  - name: media
    uuid: "a1b2c3d4-5678-90ab-cdef-1234567890ab"
    path: /volume1/USB_Media
    share: USB_Media
```

**Leave the `uuid` exactly as printed.** It is the drive's permanent
identifier and it is what makes this survive reboots and reshuffles.

**5. Attach it.**

```bash
./drive-anchor attach --live
```

**6. Check.**

```bash
./drive-anchor status
```

---

## Why UUID and not the drive letter

Drive Anchor identifies drives by **filesystem UUID** — a long unique string
baked into the drive when it was formatted.

It could have used the device name, like `/dev/usb3p1`. It deliberately does
not, because **those names move around.** Unplug two drives, plug them back
in a different order, and yesterday's `usb3` is today's `usb5`. On the machine
this tool was written for, a drive went from `usb5` to `usb6` on its own,
overnight, with nothing touched.

If the configuration referred to `usb5`, it would then be pointing at the
wrong disk, or at nothing at all. The UUID never changes, so it always finds
the right drive no matter where the system decided to put it this time.

You will only ever need a new UUID if you **reformat** the drive, which
creates a new one.

---

## Removing a drive

```bash
./drive-anchor remove <name> --live
```

This releases the bind, asks DSM to eject the drive, waits until DSM confirms
it has really gone, and only then tells you it is safe to unplug. If the eject
cannot be confirmed, it says so and does **not** tell you it is safe.

Afterwards it prints the lines to delete from `config.yaml`. It does not edit
the file for you, because editing YAML automatically destroys comments and
reshuffles everything, and that file is one people annotate.

Once it is unplugged, you can also delete the shared folder in DSM if you do
not intend to use the drive again.

---

## Replacing a drive

A replacement drive is a **different** drive, even if it goes in the same slot
and you give it the same name.

1. Remove the old one as above
2. Physically swap them
3. Rename the new drive's `usbshareN` share
4. `./drive-anchor add` to get the **new** UUID
5. Update that drive's entry in `config.yaml` with the new UUID
6. `./drive-anchor attach --live`

The most common slip is copying the old entry and forgetting the UUID line.
The tool will then wait for a drive that no longer exists and report the path
as not mounted.

---

## When a drive does not come back

**Give it longer than you think.** Some hardware, powered docks especially,
takes well over a minute to reappear. The tool waits 90 seconds by default;
raise `bind_wait_sec` if yours is slower. Assuming a drive is dead too early
is the most common false alarm here.

**`status` says "mounted, but the backing device is gone".** The drive
dropped off and came back under a different device name, so the bind is
pointing at something that no longer exists. Run `./drive-anchor attach --live`
to rebind it. This is worth fixing promptly: a bind in that state still lists
files from memory and still accepts writes, which then go nowhere.

**`status` says "mounted but empty".** The drive is present but the bind is
wrong. Also fixed by `attach --live`. Do not power-cycle the drive; it is
alive and working.

**Everything is missing at once.** That is usually power, not a bind problem.
Check the enclosure is on before anything else.
