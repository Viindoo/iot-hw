# shellcheck shell=bash
# shellcheck disable=SC2034  # the constants are read by the scripts that source this file
# Shared by the installer scripts of this directory; sourced, never run.

readonly PUBLIC_LAYER_REPOSITORY=https://github.com/Viindoo/iot-hw.git
readonly PUBLIC_CORE_REPOSITORY=https://github.com/Viindoo/odoo.git
readonly BOX_USER=pi
# The paths of core a box keeps checked out, as the image script of core clones them.
readonly CORE_SPARSE_PATHS="addons/web
addons/hw_*
addons/point_of_sale/tools/posbox/configuration
odoo/
odoo-bin"

say() {
    echo "${SCRIPT_NAME:-install}: $*"
}

fail() {
    echo "${SCRIPT_NAME:-install}: $*" >&2
    exit 1
}

require_root() {
    [ "$(id -u)" -eq 0 ] || fail "run this file as root"
}

# Runs a command as the box user, with its home: the trees of the box belong to that user, and git
# refuses to work in a tree that belongs to another one.
as_box_user() {
    runuser -u "${BOX_USER}" -- env "HOME=${BOX_HOME}" "$@"
}

# Makes sure the home of the box user exists and belongs to it, whatever owned it before. The image script of
# core copies its files into the image as root, which leaves /home/pi owned by root; the box user could then
# create neither the trees of the box below it nor, later, the file in which the box software stages its state
# before it replaces the old one. Only the directory itself changes hands: its mode and what is inside stay.
own_box_home() {
    mkdir -p "${BOX_HOME}"
    chown "${BOX_USER}:${BOX_USER}" "${BOX_HOME}"
}

# Hands the trees the box keeps below its home to the box user, when they exist already. Git refuses to work as
# a user in a repository that belongs to another one ("dubious ownership"), and the image script of core leaves
# its core tree in that state: it clones the tree as root and, after it handed the tree to the box user inside the
# image, copies more files over it as root, which gives the directories on the way back to root. The safety
# check of git stays as it is; the trees go to the user that works in them. A tree that is not there is left to
# be made by the box user.
own_box_trees() {
    local tree
    for tree in "$@"; do
        if [ -e "${tree}" ]; then
            chown -R "${BOX_USER}:${BOX_USER}" "${tree}"
        fi
    done
}

# Puts the core tree at one commit, the way the image script of core builds it (sparse, shallow).
# An existing tree is kept and moved; a missing one is made.
pin_core() {
    local tree="$1" source="$2" commit="$3" series="$4"
    if [ ! -d "${tree}/.git" ]; then
        as_box_user git init --quiet --initial-branch "${series}" "${tree}"
        as_box_user git -C "${tree}" remote add origin "${source}"
        as_box_user git -C "${tree}" config core.sparsecheckout true
        printf '%s\n' "${CORE_SPARSE_PATHS}" | as_box_user tee "${tree}/.git/info/sparse-checkout" > /dev/null
    fi
    as_box_user git -C "${tree}" fetch --quiet --depth 1 "${source}" "${commit}"
    as_box_user git -C "${tree}" reset --hard --quiet "${commit}"
    # Core's own image script keeps the folder of the device drivers a customer adds out of the repository.
    printf '%s\n' "addons/hw_drivers/iot_devices/" | as_box_user tee "${tree}/.git/info/exclude" > /dev/null
}

# The firmware setting that makes the board report a screen on its HDMI output whether or not one is plugged in.
# Core's display manager starts X on that output only when the firmware reports one; with the setting a box
# started with no screen runs X as a box with a screen does, and a screen plugged in later meets a running output.
readonly HDMI_HOTPLUG_SETTING=hdmi_force_hotplug=1

# Reads or sets the setting in the firmware configuration file named by $2: `check` succeeds when the last
# section [all] of the file holds it, `set` puts it there. A file whose last section is another one, or that
# has none, gets a section [all] of its own at its end; a value the section holds already is replaced; a file
# that holds the setting needs no change and is not written.
firmware_hotplug() {
    python3 -I - "$1" "$2" "${HDMI_HOTPLUG_SETTING}" << 'PYTHON'
import os
import sys

mode, path, setting = sys.argv[1:4]
key = setting.partition('=')[0]
with open(path, encoding='utf-8') as source:
    lines = source.read().splitlines()


def section(line):
    text = line.strip()
    return text if text.startswith('[') and text.endswith(']') else None


last = max((number for number, line in enumerate(lines) if section(line) is not None), default=None)
if last is not None and section(lines[last]) == '[all]':
    start = last + 1
else:
    start = None
held = [number for number in range(start, len(lines)) if lines[number].strip().partition('=')[0] == key] \
    if start is not None else []
if mode == 'check':
    sys.exit(0 if held and lines[held[-1]].strip() == setting else 1)
if held and lines[held[-1]].strip() == setting:
    sys.exit(0)
if held:
    lines[held[-1]] = setting
else:
    if start is None:
        lines.append('[all]')
    lines.append(setting)
# The file is on a FAT partition, which holds no owner and no mode and takes a rename badly: it is written in place.
with open(path, 'w', encoding='utf-8') as file:
    file.write('\n'.join(lines) + '\n')
    file.flush()
    os.fsync(file.fileno())
PYTHON
}
