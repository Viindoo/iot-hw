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
