# Viindoo IoT Box software

This repository holds the software layer that turns the box files of core (the server software at the
commit `box-release.json` names) into a Viindoo IoT Box, and the tools that make it an image you can write
to a card.

| Path | What it is |
|---|---|
| `viin_hw_drivers`, `viin_hw_homepage` | The layer: loaded before the box modules of core, it keeps the box paired to its Viindoo server, updates it and shows its page |
| `box-release.json` | The release pointer: the release, the series, the core commit it runs, the oldest image it accepts, and whether it is a candidate |
| `box/system` | Files the layer puts on the system of the box (web server, roll-back guard, announcement on the network) |
| `box/install` | The installer of the layer, and the script that installs everything on a stock Raspberry Pi OS |
| `box/build` | The image build and the checks of a release |
| `box/dev/softbox` | The software box: the real box code in a container, for development and for the checks |
| `.github/workflows` | The checks of every change, the build and publication of a release, and the roll-back of the release channel |

## Contents

- [Set up a box from zero](#set-up-a-box-from-zero-customers-and-their-it-staff) - the guide for a first-time user on Ubuntu 26.04
- [Build an image](#build-an-image)
- [Install the layer on a stock Raspberry Pi OS](#install-the-layer-on-a-stock-raspberry-pi-os)
- [Publish a release](#publish-a-release)
- [Roll back a release](#roll-back-a-release)
- [Development](#development)
- [Public repository](#public-repository)

## Set up a box from zero (customers and their IT staff)

This guide assumes you have never done this. You sit at a computer running a fresh Ubuntu 26.04 desktop, with a
Raspberry Pi board in hand. You end with a Viindoo IoT Box on your network, paired with your Viindoo server, with
its devices listed there. Do the parts in order; each step says what you should see afterwards. If it does not
happen, go to [When something goes wrong](#9-when-something-goes-wrong).

Every screen text in quotes is the text the box page or the Viindoo screen shows (English; the box page also
speaks Vietnamese, with buttons at its top). The same path, with screenshots, is in the user documentation:
[Set up and manage IoT boxes](https://viindoo.com/documentation/18.0/applications/internet-of-things/manage-iot-box.html).
This README gives the short version and the command lines; the documentation holds the long lists (every
self-test line, every message).

> **State on 2026-10-26.** Release pages exist for test candidates only: `box-18.0-rc.3`, a pre-release marked
> CANDIDATE, which is for Viindoo's own test box. No supported release has been published yet. Until one
> appears, a customer cannot finish part 2; ask Viindoo support (apps.support@viindoo.com) when it will. Look at
> [the releases](https://github.com/Viindoo/iot-hw/releases) for the current state: this paragraph is the only
> dated one in this guide, and the rest of the guide always says "the latest release of your series".

### 1. What you need

| You need | Notes |
|---|---|
| A Raspberry Pi board | The image is built on Raspberry Pi OS Lite, 32-bit (bookworm), and installs libraries that only a Raspberry Pi board has. This repository does not list board models and the build does not check one; before you buy, ask Viindoo support (apps.support@viindoo.com) which board to take. |
| The power supply of the board | The one made for your board model. |
| A microSD card of 8 GB or more | The image is about 5 GB once written. The sources name no speed class: take a card from a known maker. Writing erases everything on it. |
| A way to plug the card into your computer | A microSD slot, or a USB card reader (an SD slot works with a microSD-to-SD adapter). |
| A network | A cable to a router that hands out addresses (DHCP) is the simplest. Wi-Fi also works (see part 4). The network must let the box reach your Viindoo server, and the box needs internet access to update itself. |
| Your Viindoo server | Viindoo Cloud, or your own Viindoo server, with the Internet Of Things app (`viin_iot`). A person with the access right **IoT Box Creator** or **Administrator** of that app does part 5. |
| Optional | An HDMI screen or a USB receipt printer: the box shows or prints its network address on them. Printers, scanners, scales, footswitches and screens come later; [the documentation lists](https://viindoo.com/documentation/18.0/applications/internet-of-things/manage-iot-box/prepare-hardware-and-network.html) which kinds the box drives and how to connect each. |
| About 2 GB of free disk space on your computer | For the download. The compressed image is about 1.2 GB for the candidate listed today; the build refuses a file of 2 GiB or more. |

### 2. Install the tools on Ubuntu 26.04

Open a terminal (press `Ctrl+Alt+T`).

1. Install Raspberry Pi Imager, the program that writes the card:

       sudo apt update
       sudo apt install rpi-imager

   On Ubuntu 26.04 the package `rpi-imager` is in the `universe` component (version 1.8.5 when this was written),
   which a desktop installation has enabled. You should end with no error, and "Raspberry Pi Imager" appears in the
   application list.
2. Checking the download needs `sha256sum`, which Ubuntu already has. Nothing to install.
3. Optional, to download from the terminal instead of the browser: `sudo apt install gh`. Downloading a public
   release needs no GitHub account.

If you cannot use Imager, part 3 has a command-line way to write the card.

### 3. Download, check and write the image

**Find the release.** Open [the releases of this repository](https://github.com/Viindoo/iot-hw/releases).

- Your series is the first part of the version of your Viindoo database (in Viindoo: **Settings > General
  Settings**, the *About* block), for example `18.0`.
- Take the **latest release of your series** that is **not** marked *Pre-release*. Its title is
  "Viindoo IoT Box `<series>` `<release>`" and its image file is `viindoo-iot-box-<series>-<release>.img.xz`. The
  most recent release on the page may belong to another series; read the title.
- A release marked **CANDIDATE** and flagged *Pre-release* (file name ending `-CANDIDATE.img.xz`) is a test build
  for Viindoo's own test box. Never write it to a box that serves a customer. A box written from a candidate
  follows a test channel of its own and takes test builds only.

**Download two files** from that release page into one folder, say `~/Downloads`: the `.img.xz` file and
`SHA256SUMS`. `manifest.txt` is not needed. Do not unpack the `.img.xz` file. From the terminal instead (replace the
tag with the one on the page; `gh release list -R Viindoo/iot-hw --exclude-pre-releases` lists the supported
releases):

    cd ~/Downloads
    gh release download box-<series>-<release> -R Viindoo/iot-hw --pattern '*.img.xz' --pattern SHA256SUMS

**Check the file.**

    cd ~/Downloads
    sha256sum --check SHA256SUMS

You should see `viindoo-iot-box-<series>-<release>.img.xz: OK`. If it says `FAILED`, the download is damaged or
incomplete: download it again. Do not write a file that does not say `OK`.

(On another computer: macOS `shasum -a 256 --check SHA256SUMS`; Windows `certutil -hashfile <file> SHA256`, then
compare the hash it prints with the one beside the file name in `SHA256SUMS`, because Windows has no option that
compares them.)

**Write the card with Raspberry Pi Imager.** Put the card in the slot or the reader, then:

1. Open "Raspberry Pi Imager" from the application list (or run `rpi-imager` in the terminal).
2. If it shows "CHOOSE DEVICE" ("Raspberry Pi Device"), choose your board model if it is listed. The image of this
   box is not in the list Imager offers: you pick it from your computer in the next step.
3. Click "CHOOSE OS", scroll to the end of the list and choose "Use custom" ("Select a custom .img from your
   computer") and pick the `.img.xz` file.
4. Click "CHOOSE STORAGE" and choose your card. Check its size against the card you hold: Imager will erase whatever
   you choose. Choosing your computer's own disk by mistake destroys your data.
5. Click "Next". Imager asks "Would you like to apply OS customization settings?". Answer "NO" ("NO, CLEAR
   SETTINGS" if it shows that instead). The box makes its own host name, password and certificate at first start;
   the settings of Imager must not touch the card.
6. Imager says "All existing data on '<card>' will be erased. Are you sure you want to continue?". Choose "YES".
7. Wait for "Write Successful", then take the card out. If Ubuntu offers to format a partition of the card, cancel:
   the card is ready as it is.

**Without Imager (command line).** Be certain of the device: `dd` overwrites whatever it is pointed at without asking.

    lsblk -o NAME,SIZE,MODEL,TRAN,RM,MOUNTPOINTS

Find the card by its size and by `RM` = 1 (removable). It is `/dev/sdX` for a USB reader or `/dev/mmcblkN` for the
built-in slot; your system disk (usually `/dev/nvme0n1` or `/dev/sda` with `MOUNTPOINTS` such as `/`) is never the
card. Unplug the card and run `lsblk` again if in doubt: the device that disappears is the card. Unmount its
partitions (`sudo umount <mountpoint>` for each mount point `lsblk` shows on it), then write, replacing `/dev/sdX`
with your card (without a partition number):

    CARD=/dev/sdX
    xz --decompress --stdout viindoo-iot-box-<series>-<release>.img.xz | sudo dd of="$CARD" bs=4M conv=fsync status=progress
    sync

When the prompt returns, the card is written.

### 4. Start the box and connect it to your network

When the box starts for the first time it gives itself a master password of its own (random, shown nowhere) and a
host name `viindoo-iot-<last 4 characters of the serial number of the board>` (for example `viindoo-iot-1a2b`; the
host name never changes afterwards), makes its own certificate, and announces its page on the local network. While
the box software starts, every address of the box shows the page "The box is not ready yet", which checks again by
itself and opens the box page when the box answers. The sources give no start-up time; if nothing changes after 15
minutes, unplug the power supply, wait ten seconds and plug it in again.

**With a network cable (preferred).**

1. Put the card in the board, plug in the network cable, then the power supply.
2. Find the address of the box. Any one of these:
   - an HDMI screen shows the identification page, with the line "To set up this box, open http://`<address>` in a
     browser on the same network.";
   - a USB receipt printer prints a ticket titled "Viindoo IoT Box" with the IP address and a `Homepage:` line;
   - your router's list of devices shows the host name `viindoo-iot-xxxx`;
   - `http://viindoo-iot-xxxx.local` (the host name from the box), from a computer on the same network segment. It
     does not work across routers, on a guest network that isolates clients, or through a VPN.
3. On your computer, open `http://<address>` in a browser. You should see the box page: from top to bottom
   "Self-test", "Server", "Network", "Devices", "Software" and "Advanced". The "Network" section says "Wired
   network". The "Server" section says "The box is not connected to a server."

**With Wi-Fi.** When the box starts with no cable and no Wi-Fi network it can join, it opens its own Wi-Fi network,
which has no password, named `Viindoo-IoT-` followed by the hardware address of its Wi-Fi without colons
(for example `Viindoo-IoT-b827eb1234ab`). It does so at start when it cannot join a network, and
after ten minutes without any address while it runs; wait up to ten minutes before you decide it is missing.

1. On Ubuntu, open the network menu at the top right, choose the Wi-Fi network `Viindoo-IoT-...` and connect. Ubuntu
   may say there is no internet: that is right.
2. Open `http://10.11.12.1`. The "Network" section says "Own Wi-Fi network of the box".
3. Click "Wi-Fi". Under "Name of the Wi-Fi network" choose yours from the list, or type its name (a hidden network is
   not listed: type it exactly). Under "Password (leave empty for a network without one)" type its password.
   Networks that ask for a user name as well (802.1X) cannot be joined.
4. Keep "Keep this network after a restart" ticked, then click "Join the network".
5. The page says "The box is joining `<name>`." and loses contact with the box. Connect your computer back to the same
   network the box joined, wait a minute, and open the box by the name the page shows
   (`http://<host name>.local`) or by the address it has on that network (find it as in the cable steps).
   A wrong password is not reported on the page: the box goes back to its previous network, or opens
   `Viindoo-IoT-...` again; join that and retry.

The box page can be reached on port 80 (`http://`), 443 (`https://`, with a certificate your browser warns about
until you accept it once) and 8069.

### 5. Pair the box with your Viindoo server

Pairing links this box to one Viindoo database with a code that works once and for 15 minutes.

**In Viindoo** (a user with **IoT Box Creator** or **Administrator** of the Internet Of Things app; give the right
under **Settings > Users & Companies > Users**, tab **Access Rights**, *Internet of Things (IoT)*; install the app
from **Apps** by searching `Internet Of Things` and clicking **Activate**):

1. Open Viindoo with the address the box can reach (the pairing link carries the address you use).
2. Open **IoT > Overview** and click **Connect an IoT Box**. In a database with several companies, first choose the
   **Company** the box belongs to.
3. Click **Generate Pairing Code**. The "Pairing Code" window shows the **Pairing Code** (12 characters), the
   **Pairing Link** and **Valid Until**. It is shown only now. Keep the window open.

**On the box page:**

4. In the "Server" section click "Connect". The "Connect the box to a server" window opens.
5. With "Pairing link" selected, paste the whole link, including the part after `#`. Or choose "Address and code" and
   type "Address of the server" (the address you open Viindoo with, for example `https://erp.example.com`) and "Pairing
   code".
6. Click "Connect". The box restarts ("The box is connected and restarts to finish."); do not switch it off. The page
   comes back by itself and says "Connected to `<server>` as `<name>`." The "Server" section then shows "Registered".
7. Back in Viindoo, click **Done** in the Pairing Code window.

If the address starts with `http:` the window warns that the exchange is not encrypted: use `https` when the server has
it. If the server's certificate is not issued by a public authority, the window shows its fingerprint: compare it with
the one your server administrator gives you, and only if they match choose "Trust this certificate and connect".

After pairing, every change on the box page asks for the **settings code** ("Locked: a change asks for the settings
code that the administrators read on the box record in Viindoo."). An **Administrator** of the app reads it in the
box record, field **Box Settings Code** in the *Advanced* group. Reading the page needs no code.

### 6. Check that it works

- In Viindoo, **IoT > Overview** shows the box as a card marked **Connected**, in green. Open the card to see **Last
  Seen**, **Reported Name** (the host name of the box), **Hardware Address**, **IP Address**, **IoT Box Home Page**
  and **Image Version**. Give it the name your team will use in **Box Name** and save; the box keeps its own host
  name.
- **IoT > Devices** lists the devices the box found, grouped by box. The box page's "Devices" section lists the same
  devices: a plugged-in device appears within seconds, a printer within two minutes.
- The box page's "Self-test" says "Everything is in order." when nothing needs you; otherwise each line that needs
  you says "What to do:".
- Under "Advanced" (click "Show"), "This box" shows "Name of the box" (the host name `viindoo-iot-xxxx`), the
  hardware address and the serial number.

Next, connect devices and assign them in the apps that use them: see the documentation pages
[Connect and test devices](https://viindoo.com/documentation/18.0/applications/internet-of-things/manage-iot-box/connect-and-test-devices.html)
and the integration pages of the IoT documentation.

### 7. Updates

A box follows the release channel of its series, and nothing else. It looks for a new release when it starts, every
night, and when you click "Look for a new release" in the "Software" section. With "Update automatically" on (the
default) it applies a new release by itself and restarts; a release that does not start well is undone by the box
and never tried again. With it off, click "Update to `<release>`". The sentence in "Software" says where the box
stands, for example "The box software is the latest release.", "A new release is on trial after a restart; the box
keeps it once it runs well." or, without internet, "The box could not reach the place where releases are published.
Updates need internet access; devices work without it." A box written from a supported image does not install a
candidate ("The release that is published is a candidate, which this box does not install."). If the box says "The
new release needs a newer image of the box than the one on its card.", write a newer image to the card (part 3), then
pair the box again: in Viindoo open the box and click **Re-pair**, and use the new code on the box page.

### 8. Logs and help

- The box page, "Self-test": "Download diagnostic file" saves one file with the self-test, the configuration and the
  last log lines, without any password, code or key. Send it to apps.support@viindoo.com.
- "Advanced": "Box log" opens the live log of the box; "Log levels" chooses how detailed it is and has a switch "Send
  the log of the box to the server".
- In Viindoo, **IoT > Reporting > Device Logs** (or **Log IoT Box** on the box) shows what the box sends: its warnings
  and errors by default.
- "Password for shell access" (under "Advanced") is for a technician and only when support asks; note it at once, the
  box does not show it again.

### 9. When something goes wrong

Only the problems the box page and the server report are listed. The documentation lists every message:
[Troubleshoot IoT boxes](https://viindoo.com/documentation/18.0/applications/internet-of-things/manage-iot-box/troubleshoot-iot-boxes.html).

| What you see | What to do |
|---|---|
| `sha256sum` says `FAILED` | The download is damaged or incomplete: download it again. |
| Imager does not list the card | Take the card out and put it in again, or try another reader. |
| No `Viindoo-IoT-...` Wi-Fi network | It opens only with no cable and no Wi-Fi it can join, and after ten minutes without an address later on. Wait ten minutes, or plug in a network cable. |
| The page "The box is not ready yet" stays | Wait 15 minutes, then unplug the power supply, wait ten seconds and plug it in again. |
| "This page cannot reach the box." | Check that your computer is on the same network as the box and that the box is powered on; the page tries again by itself. |
| `http://viindoo-iot-xxxx.local` does not open | The name works on the same network segment only. Use the address from the screen, the ticket or the router. |
| After joining Wi-Fi the page is gone | Join your computer to the same Wi-Fi and open the box by name or address. If `Viindoo-IoT-...` is back, join it and check the name and the password. |
| "This is not a pairing link." | Paste the whole link, or use "Address and code". |
| "The server does not know this code.", "This code is too old." or "This code was already used." | Generate a new code in Viindoo and copy it exactly. |
| "This server does not offer pairing for Viindoo IoT boxes." | The address may be wrong, the Internet Of Things app may not be installed there, or the server serves several databases and none is selected. |
| "This box is already registered as `<name>`." | In Viindoo open that box, click **Re-pair**, and use that code on the box page. |
| "The clock of the box is wrong, so secure connections fail." | Choose "Set the clock from the server", or let the box reach a time server (UDP port 123), then connect again. |
| "The server `<server>` did not answer in time." or "...cannot be reached." | Check the server address, the firewall and the proxy between the box and the server. |
| The box asks for the settings code and nobody has it | Switch the box off, put its card in a computer, create an empty file named `viindoo-reset` (or `viindoo-reset.txt`) at the top of the partition the computer shows, put the card back and start the box. It forgets its server and its lock; pair it again with a new code (**Re-pair** on its record, or **Connect an IoT Box**). |
| The box shows **Disconnected** in Viindoo | Open the box page and read the self-test; each line that needs you says what to do. When the box reports again it returns to **Connected** by itself. |

If none of this helps, download the diagnostic file (part 8) and send it to apps.support@viindoo.com.

## Build an image

An image is built from a tag `box-<series>-<release>`, on a Linux machine, as root, with `kpartx`,
`qemu-user-static`, `zerofree`, `xz-utils`, `rsync`, `fdisk`, `wget` and about 10 GB of free disk:

    sudo box/build/build_image.sh --tag box-<series>-<release> --series-ref origin/<series>

It puts core at the commit `box-release.json` names, runs the image script of core unmodified on
the pinned operating system (`box/build/base-image.conf`), installs the layer in the image with
`box/install/install_layer.sh`, checks what the image holds, and writes in `box-image-out/` the compressed
image, `SHA256SUMS` (the checksum of the image) and `manifest.txt` (the core and layer commits, the base system checksum, every Debian
package and Python library). The build stops on purpose if the compressed file would reach 2 GiB, the limit of
one file on a release page. Debian packages and Python libraries are whatever their mirrors serve that day:
two builds are equal in function, not byte for byte. The image holds neither the third-party tunnel program
nor the payment-terminal programs that the image script of core downloads; the installer removes them.

The build needs hosts outside Viindoo's control (the operating system download, the Debian and Python
mirrors, and the hosts the image script of core fetches from). When one is down the build fails; no box is
touched.

The workflow `box-image.yml` does the same on a hosted runner when a tag `box-*` is pushed, and passes
`--expect-mode` with the kind of release its gate decided; the script stops if its own reading of the tag
differs. The build also stops unless the image holds `/etc/sudoers.d/010_pi-nopasswd` with exactly
`pi ALL=(ALL) NOPASSWD: ALL` and no `/etc/sudoers.d/011_viindoo-iot-box`, and unless the password field of `pi`
in `/etc/shadow` is exactly `!` and no backup of the password files (`/etc/shadow-`, `/etc/gshadow-`) is left.
The image script of core prints the password it first gives `pi`; the build replaces that line in its log, and
a build that finds the field not locked says only that, never the field.

## Install the layer on a stock Raspberry Pi OS

This path is documented, not supported: the supported way to get a box is the image of a release. It exists
for development, for hardware the image does not boot on, and for an integrator who has to start from an
operating system of their own. On a freshly written Raspberry Pi OS Lite (32-bit, bookworm), as root:

    sudo box/install/install_on_raspios.sh --layer-ref box-<series>-<release> --i-know-this-makes-the-machine-a-box

It takes the same steps as the image build (the files and packages of the box of core, then
`install_layer.sh`), so it replaces the network, file system and start-up files of the machine, including a
read-only root file system. Use it only on a machine meant to become a box. A candidate release also needs
`--channel refs/heads/box-candidate/<series>`; a supported release needs no channel, because a box without one
follows `box-release/<series>`.

## Publish a release

A box follows one git ref of this repository, its release channel: the branch `box-release/<series>`. Nothing
merged into the series branch (`<series>`, where work lands) reaches a box. A box applies the head of its
release channel only when a tag `box-<series>-<release>` names that same commit and `<release>` is the
`release` of the `box-release.json` the commit holds, so two protected refs, moved by two different actors,
must agree before any box moves.

1. Candidate, before the box work is merged: push the tag `box-<series>-rc.<n>` on the head of the work
   branch. The workflow builds a candidate image and publishes it as a pre-release whose file name and title
   say CANDIDATE. Its box follows the branch `box-candidate/<series>`, which the workflow creates at the
   tagged commit if it does not exist; no customer box can select that channel. The release pointer of a
   candidate has `candidate: true`, and `release` is `rc.<n>` or ends in `-rc.<n>`; a release that contains
   `rc.` in any other way (`rc.1-final`, `1.0-rc.1x`, `xrc.1`) is refused by the checker and by the box. A
   candidate never moves a release channel and never runs in the environment that holds the deploy key.
   Anyone who can write to the repository may create the tags `box-<series>-rc.<n>`; a candidate tag of any
   other shape, like every supported tag, needs a release manager.
2. Supported, after the merge:
   1. A reviewed pull request sets `box-release.json` on the series branch (`candidate` false, the new
      `release`, the `odoo_commit`). `box-checks.yml` runs on it and is a required status check of the
      series branch.
   2. A release manager pushes the tag `box-<series>-<release>` on that merged commit. Only release managers
      can create such a tag, and nobody can move or delete it. The release begins with a digit and the series is
      `NN.N` (two digits, a dot, one digit); the checker refuses any other supported tag.
   3. `box-image.yml` runs. Its `gate` reads the tag with the checker of the series branch
      (`box/build/check_release.sh`, taken from `origin/<series>`, never from the tagged tree) and refuses
      the tag unless its commit is on the series branch and the pointer is not a candidate's; while the series
      branch holds no checker yet, it accepts a candidate tag only. Then `checks` runs `box-checks.yml` on the
      tagged commit, `build` makes the image (and fails if its own reading of the tag differs from the gate's),
      and `publish` creates the release page.
   4. `publish` and `advance` run in the environment `box-release`, so the owner approves before either
      starts. `advance` refuses a release whose page is withdrawn, a pre-release or a draft, and otherwise
      pushes the tagged commit to `box-release/<series>` with the deploy key of that environment. It refuses
      unless the channel does not exist yet or its head is contained in the tagged commit, pushes without force,
      and then reads the channel back and requires the tagged commit. Boxes see the release only when
      `advance` ends. Only then does `latest` mark the release page as the latest one. If `advance` fails
      after `publish` succeeded, "Re-run failed jobs" finishes it.

A supported release page is never replaced: a run for a supported tag whose release exists fails with "a
supported release is never replaced; publish a new release". Only a candidate's files are replaced when its tag
is built again.

`box-checks.yml` runs on every pull request and every push to a series or box work branch, and `box-image.yml`
runs it again on the tagged commit before anything is built: script syntax and lint, workflow lint, the release
pointer rules, the unit of the box service as systemd reads it, the box suite in the software box and the seam
check against the core commit the pointer names. A change of `box-release.json` therefore cannot reach a box
before the seams of the layer have been checked against the core it names.

`box-image-pr.yml` builds the image on a pull request that changes what goes into one (`box/build`, `box/install`,
`box/system`, `box-release.json` or the two image workflows), with the same `box/build/build_image.sh` and the same
checks of the result, and publishes nothing: the tag it builds from exists only on the runner, there is no
environment and no secret, and the files stay as a workflow artifact for three days. A pointer that is not a
candidate builds as the supported image it names, with the commit under test standing for the series branch;
the rules `check_release.sh` applies to a real tag are not changed by that. The workflow is not a required
status check, so a pull request can be merged while it runs; read its result before cutting a tag.

What this does and does not stop. A run for a tag uses the workflow file of the tagged commit, and anyone who can
write to the repository can make a release page by hand. The checker taken from the series branch stops
mistakes; the environment and the rulesets below stop a single actor; the release channel is what customers'
boxes trust, and nothing but `advance` and `box-rollback.yml` can move it. The release page is as trustworthy as
the list of people who can write to the repository.

The repository owner sets up, once. GitHub matches ref patterns like `fnmatch`: `*` stands for any characters,
`-` and `.` included, so a pattern with a `*` before the release part overlaps with others. The patterns below
start with the series shape `[0-9][0-9].[0-9]` (a series such as `18.0`) and put no `*` before the release:

- a ruleset on `refs/heads/box-release/*`: creation, update and deletion restricted, force pushes blocked, the
  deploy key below as the only bypass (and repository administrators, for an emergency);
- a ruleset on the tags: include `box-*`, exclude `box-[0-9][0-9].[0-9]-rc.*`. Creation of a tag the ruleset
  covers is restricted to the release managers; update and deletion are blocked for everyone. Of the tags
  `box-[0-9][0-9].[0-9]-rc.*`, which anyone who can write may create, update and deletion are blocked too;
- the environment `box-release`: the owner as required reviewer, the secret `BOX_RELEASE_DEPLOY_KEY` (the
  private half of a deploy key with write access that is used for nothing else), and deployment refs limited
  to the tags `box-[0-9][0-9].[0-9]-[0-9]*` and the exact names of the series branches, today `18.0` (never a
  pattern such as `[0-9][0-9].[0-9]`, which a new branch of that shape would satisfy). The owner adds the name
  of a new series branch to this list only after that branch has been created and protected (the last item
  below: no deletion, pull request only), never before, since a name listed while the branch does not exist
  yet could be created by a writer first. A release name begins with a digit, an exempt
  tag's release begins with `rc.`, so no tag a writer may create can satisfy the tag pattern (checked with
  `fnmatch.fnmatchcase` over the pattern, including `box-18.0-rc.1`, `box-18.0-rc.1-final`, `box-18.0-rc.1x`,
  `box-18.0-rc.1-1` and every name of up to four characters of `01rc.-x9` after `box-18.0-`); and no branch a
  writer may create can satisfy a branch name, because the series branch exists already, so its name cannot be
  created again, and it cannot be deleted (last item below). Hence no ref a writer may create can satisfy a
  deployment ref of this environment. This relies on the tag pattern, on the exact branch names, and on the
  protection of the series branch. A supported release whose name does not begin with a digit cannot be
  published, and neither can one of a series that is not exactly two digits, a dot and one digit (`NN.N`):
  `box/build/check_release.sh` refuses both before anything is built;
- the environment `box-candidate`: no secret and no reviewer is needed; deployment refs limited to the tags
  `box-*` and the same exact series branch names as `box-release`, so a candidate started by hand from the
  series branch (Run workflow) publishes as well as one started by a tag push. Candidate pages are published
  here, so a ref a writer creates never reaches the deploy key;
- protection of the series branch: pull request with review required, `box-checks` as a required status
  check, no force push, no deletion.

## Roll back a release

A bad release is taken away from the boxes by pointing the release channel back at an earlier supported
release. Run the workflow `box-rollback.yml` from the series branch (Run workflow, branch `<series>`) with the
tag of that earlier release.

1. `gate` reads the tag with the checker of the series branch. The tag must be a supported release whose commit
   is in the history of the channel head, and its release page must exist and not be withdrawn.
2. `move`, in the environment `box-release` (the owner approves), first marks the page of each release the
   channel names now as a pre-release whose title starts with `WITHDRAWN` (its files stay: customers checked
   their checksums against them), so a run of `advance` for such a release, started again at that moment,
   refuses it. Then it pushes the tagged commit to `box-release/<series>` with `--force-with-lease`, so it
   fails if the channel moved since the gate read it.
3. `pages` marks the page of the returned release as the latest one.

Boxes apply the older release like any other. A box that itself undid that release keeps refusing it. A published
release or tag is never changed; the fix is a new release.

## Development

`box/dev/softbox/README.md` explains how to run the box code and its suite without a Raspberry Pi.

## Public repository

Nothing in this repository, its workflows, its release pages or its logs may hold a credential, an internal
host name, a machine path or source of a private repository. The workflows use only the token GitHub gives
each run, name its permissions, and pin every action by commit.
