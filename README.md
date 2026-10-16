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

## Get a box (customers and their IT staff)

Open the releases of this repository and take the latest release of your series (the title names it, for
example "Viindoo IoT Box 18.0 ..."). Download the `.img.xz` file and `SHA256SUMS` into one folder and check
the file there:

- Linux: `sha256sum --check SHA256SUMS`
- macOS: `shasum -a 256 --check SHA256SUMS`
- Windows: `certutil -hashfile <file> SHA256`, then compare the hash it prints with the one beside the file
  name in `SHA256SUMS` (Windows has no option that does the comparison).

On Linux and macOS the answer is `<file>: OK`. Then write the image with Raspberry Pi Imager: "Use custom",
without OS customisation. `manifest.txt`, on the same page, is not needed for this. Releases marked
CANDIDATE and flagged as pre-releases are test builds for Viindoo's own box; never write one to a box that
serves a customer.

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
