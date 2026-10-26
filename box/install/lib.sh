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
