# Software box

The software box runs the real code of a Viindoo IoT Box - the box modules of Odoo Community
and, loaded before them, the Viindoo layer of this repository - in a container on a Linux x86
workstation. No Raspberry Pi is needed and no box code knows it is simulated: the container
reproduces the file layout of a box, so the code runs without any switch.

It is a development tool. Nothing in this directory is installed on a customer's box.

Never start the box code on a bare workstation instead. Core refuses to load outside the box
layout, and where it does load it restarts the web server and remounts the root file system of
the machine it runs on.

## What you need

- Docker, usable by your user.
- A git repository of core whose checked-out branch, or the branch `SOFTBOX_BRANCH` names,
  ends at the commit to run. A full clone, a shallow clone and a bare repository all work; a
  linked work tree does not, because its repository is not inside it. The software box only
  reads this repository, and every start prints the core commit it runs.

## Commands

All commands are `softbox.sh` in this directory. `SOFTBOX_CORE` is the path of the core
repository; `softbox.sh help` lists every parameter.

| Goal | Command |
|---|---|
| Build the image | `SOFTBOX_CORE=/path/to/core ./softbox.sh build` |
| Start the box | `SOFTBOX_CORE=/path/to/core ./softbox.sh start` |
| Read its output | `./softbox.sh logs --follow` |
| Run a command in the running box | `./softbox.sh exec curl -s http://127.0.0.1:8069/hw_posbox_homepage/ping` |
| Switch the box off and on, keeping what it stored | `./softbox.sh restart` |
| Stop the box and remove its container | `./softbox.sh stop` |
| Run the whole box suite | `SOFTBOX_CORE=/path/to/core ./softbox.sh suite` |
| Run one test method | `SOFTBOX_CORE=/path/to/core ./softbox.sh suite test_pairing.TestRefusals.test_code_the_server_does_not_know_is_refused_as_invalid_code` |
| Run one command in the box layout | `SOFTBOX_CORE=/path/to/core ./softbox.sh run python3 -c "import odoo"` |
| Check the seams of the layer against the core the configuration names, starting nothing | `SOFTBOX_CORE=/path/to/core ./softbox.sh run python3 /home/pi/iot-hw/viin_hw_drivers/boot.py --config /home/pi/odoo.conf` |
| Remove the image | `./softbox.sh rmi` |

`suite` and `run` each use a container of their own, removed when the command ends, and return
the exit code of the command: zero when the suite passed. `rmi` leaves the Debian base image
and the build cache of docker, which other images may share.

The image is built once. It has to be rebuilt only when a file of this directory changes or
when core changes the package lists of its image script. An edit in the layer or a new core
commit needs no rebuild: both trees are read when a container starts, so the next `start`,
`suite` or `run` uses them.

## Core alone, or the layer first

`SOFTBOX_MODULES=layer` (the default) writes the configuration of a Viindoo box:

    addons_path = /home/pi/odoo/addons,/home/pi/iot-hw
    server_wide_modules = viin_hw_drivers,hw_drivers,hw_escpos,hw_posbox_homepage,viin_hw_homepage,web

In this mode the container also runs `box/system/apply.sh` of the layer, as the installer of the
image does, so the system files of the layer are in place: the web server blocks under
`/etc/nginx/viindoo/` (including `name.conf.template`, which installing a public name needs), the
service files, the roll-back guard and the box-down page. It runs after the files of core and
refuses what it refuses on a box: if it fails, the container does not start. The container runs
no init system, so the installed units are files only.

`SOFTBOX_MODULES=core` keeps the configuration core ships, without the layer. Core alone calls
hosts that a Viindoo box must never reach, so this mode runs without a network only.

## The box suite

`suite` runs `python3 -m unittest` as the box user, in `viin_hw_drivers/tests` of the layer,
with core importable. Without arguments it discovers every `test*.py` there; arguments are
passed on, so a module, a class, a method or any option of unittest can be given. A run in
which no test ran fails, so a mistyped selection cannot pass for a verdict.

The test files are loaded as top-level modules: `test_pairing`, not
`viin_hw_drivers.tests.test_pairing`. Nothing is imported or started before them, so a test
decides itself when the configuration is parsed and when the box modules are loaded. A helper
shared by several test files is imported by its plain name (`import common`).

Nothing of core is replaced for the suite: a test that loads the box modules gets the real
threads of core, which restart the web server and talk to the print server of the container.

## Simulated devices

The devices that core can only find on real hardware are replaced by handler files in
`iot_handlers/`. They are built on the `Driver` and `Interface` classes of core and answer
through its event manager, so every part of the box above the hardware runs unchanged.

| Device | Identifier | How to use it, inside the container |
|---|---|---|
| Scale | `simulated_scale` | write the weight in kilograms to `/var/lib/softbox/scale-weight` |
| Barcode scanner | `simulated_scanner` | append one line per scan to `/var/lib/softbox/scanner-scans` |

    ./softbox.sh exec sh -c 'echo 1.250 > /var/lib/softbox/scale-weight'
    ./softbox.sh exec sh -c 'echo 8934567890123 >> /var/lib/softbox/scanner-scans'

A device is plugged in for as long as its file exists: delete the file to unplug it, create it
again to plug it back.

`SOFTBOX_HANDLERS` says where the container copies the handler files:

- `layer` - into `viin_hw_drivers/iot_handlers/` of the layer copy, which the layer loads after
  the handlers of core. This is the default when the layer is loaded, and it gives the box its
  simulated devices. Handlers that a server sends to the box do not replace them.
- `core` - into the handler directory of the core copy, which core loads itself. This is the
  default for core alone. With the layer loaded it gives the same devices, but handlers that a
  server sends to the box replace this directory.
- `none` - nowhere.

Printers are not simulated by a handler. The container runs a real print server with three raw
queues, which the printer interface of core finds like any other queue:

| Queue | Kind core gives it |
|---|---|
| `softbox_office` | office printer |
| `softbox_zpl` | label printer |
| `softbox_tm-m30` | receipt printer |

Every job ends, byte for byte, in `/var/lib/softbox/prints/<queue>-job-<number>`.

## What is not simulated

- Screens and the browser on them. The box reports the screen core gives a box without a
  monitor.
- Wi-Fi and the access point of the box. The page of core says "Not Connected" when the
  container has no network.
- The read-only card. The root file system stays writable: the remount commands of core run
  and fail without effect, and `/root_bypass_ramdisks` leads to the same files as `/`.
- The init system. No service unit runs; the entry point does what the unit of the box
  service, the start script and the cron line of the image do. Restarting the device and
  setting the clock do not work.
- USB, serial and Bluetooth buses, cameras, payment terminals.
- The certificate of the web server. The container generates one when it lays out the box, once
  per container, with the subject of the certificate the image ships and a key of its own.
  `restart` keeps it; `stop` and `start` make a new one.

## What the container is allowed

The containers get no privileged mode, no host device, no host network, no docker socket and
no volume. `softbox.sh` states each right next to the reason for it:

- No network unless `SOFTBOX_NETWORK` names a docker network, and never the host network:
  `softbox.sh` refuses `SOFTBOX_NETWORK=host` before it creates a container. Ports are
  published on `127.0.0.1` only, through `SOFTBOX_PUBLISH`.
- Six capabilities instead of the default set, all about users and files inside the
  container: changing user, signalling its own processes, owning and reading files.
- An empty memory file system in place of `/sys`, and no AppArmor profile: core reads the
  serial number of the board under `/sys/firmware`, which the default profile of docker
  forbids even inside a memory file system.
- Two read-only mounts: the core repository and the layer tree. Both are copied inside the
  container, so a run writes nothing to the host, not even a bytecode cache.

Inside the container the box user has `sudo` without a password, as on a box.

Two lines in the output of the box come from these limits and are expected: `mount: /:
permission denied` for each remount core attempts, and `sudo: unable to send audit message`
for each command core runs through sudo. The command itself runs.

## Files

| File | Role |
|---|---|
| `softbox.sh` | the commands above, and every right a container gets |
| `Dockerfile` | the image: Debian with the packages of the box image |
| `packages/` | the packages left out of, and added to, the lists of the box image, each with its reason |
| `container/entrypoint.sh` | lays out the box inside the container, then runs the box, the suite or a command |
| `container/print-backend.sh` | writes each print job to a file |
| `container/sudoers` | `sudo` without a password for the box user |
| `iot_handlers/` | the simulated scale and scanner |
