#!/usr/bin/env bash
# Turns a freshly written stock Raspberry Pi OS Lite (32-bit) into a Viindoo IoT Box, with the same
# steps the image build takes and the same layer installer (install_layer.sh) as its last one.
#
# Documented, not supported: the supported way to get a box is the image of a release. This path
# is for development, for hardware the image does not boot on and for an integrator who must start
# from an operating system of their own.
#
# It replaces the network, file system and start-up files of the machine with those of a box, among
# them a read-only root file system. Run it only on a machine that is meant to become a box.
#
#   sudo install_on_raspios.sh --layer-ref REF --i-know-this-makes-the-machine-a-box [OPTION...]
#
# The options of install_layer.sh (--layer-source, --core-source, --channel, --home, --skip-apt) are passed on.
set -o errexit
set -o nounset
set -o pipefail

SCRIPT_NAME=install_on_raspios.sh
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly here
# shellcheck source=box/install/lib.sh
source "${here}/lib.sh"

readonly POSBOX=addons/point_of_sale/tools/posbox

layer_ref=""
layer_source="${PUBLIC_LAYER_REPOSITORY}"
core_source="${PUBLIC_CORE_REPOSITORY}"
BOX_HOME=/home/pi
confirmed=""
passed=()

while [ $# -gt 0 ]; do
    case "$1" in
        --i-know-this-makes-the-machine-a-box) confirmed=yes; shift ;;
        --layer-ref) layer_ref="${2:?--layer-ref needs a value}"; passed+=("$1" "$2"); shift 2 ;;
        --layer-source) layer_source="${2:?--layer-source needs a value}"; passed+=("$1" "$2"); shift 2 ;;
        --core-source) core_source="${2:?--core-source needs a value}"; passed+=("$1" "$2"); shift 2 ;;
        --home) BOX_HOME="${2:?--home needs a value}"; passed+=("$1" "$2"); shift 2 ;;
        --channel) passed+=("$1" "${2:?--channel needs a value}"); shift 2 ;;
        --skip-apt) passed+=("$1"); shift ;;
        *) fail "unknown option $1" ;;
    esac
done
[ -n "${layer_ref}" ] || fail "--layer-ref is required, for example --layer-ref box-<series>-<release>"
[ -n "${confirmed}" ] || fail "this makes the machine a box and cannot be undone: pass --i-know-this-makes-the-machine-a-box"
require_root

# Which core commit this layer release runs is the layer's own word, in its pointer file.
read_pointer() {
    local scratch facts
    scratch="$(mktemp --directory)"
    chown "${BOX_USER}:${BOX_USER}" "${scratch}"
    as_box_user git init --quiet "${scratch}"
    as_box_user git -C "${scratch}" fetch --quiet --depth 1 "${layer_source}" "${layer_ref}"
    as_box_user git -C "${scratch}" show FETCH_HEAD:box-release.json > "${scratch}/box-release.json"
    facts="$(bash "${here}/../build/check_release.sh" pointer "${scratch}/box-release.json")" || { rm -rf "${scratch:?}"; exit 1; }
    rm -rf "${scratch:?}"
    pointer_series="$(sed -n 's/^series=//p' <<< "${facts}")"
    pointer_commit="$(sed -n 's/^odoo_commit=//p' <<< "${facts}")"
}

# The box user of the image of Raspberry Pi OS runs every command as root without a password, and core's
# box depends on it (it runs chpasswd, iwlist and service, and its service unit runs mkdir, chown and
# timedatectl through sudo). A box user this script creates gets the same rule, in the file of the same name.
allow_box_user_sudo() {
    local rule
    rule="$(mktemp)"
    printf '%s ALL=(ALL) NOPASSWD: ALL\n' "${BOX_USER}" > "${rule}"
    visudo --check --quiet --file "${rule}" || { rm -f "${rule}"; fail "the sudo rule written for ${BOX_USER} is not valid"; }
    install --owner root --group root --mode 0440 "${rule}" "/etc/sudoers.d/010_${BOX_USER}-nopasswd"
    rm -f "${rule}"
}

prepare_system() {
    DEBIAN_FRONTEND=noninteractive apt-get update --quiet=2
    DEBIAN_FRONTEND=noninteractive apt-get install --yes --quiet=2 git ca-certificates openssl sudo rsync
    if ! id "${BOX_USER}" > /dev/null 2>&1; then
        useradd --create-home --home-dir "${BOX_HOME}" --shell /bin/bash "${BOX_USER}"
        allow_box_user_sudo
    fi
    own_box_home
}

# The files of core's image, in the order its build copies them. The tunnel program core's build
# downloads is left out: the layer would delete it again.
install_core_files() {
    local tree="${BOX_HOME}/odoo"
    pin_core "${tree}" "${core_source}" "${pointer_commit}" "${pointer_series}"
    as_box_user git -C "${tree}" archive "${pointer_commit}" "${POSBOX}/overwrite_before_init" \
        | tar --extract --directory / --strip-components 5 --no-same-owner
    # The image script of core installs the packages and services of a box and takes the configuration from the core tree.
    bash /etc/init_posbox_image.sh
    as_box_user git -C "${tree}" archive "${pointer_commit}" "${POSBOX}/overwrite_after_init" \
        | tar --extract --directory / --strip-components 5 --no-same-owner
    install --directory /var/odoo
    as_box_user git -C "${tree}" show "${pointer_commit}:${POSBOX}/posbox_create_image.sh" \
        | sed -n 's/^VERSION_IOTBOX=//p' > /var/odoo/iotbox_version
    [ -s /var/odoo/iotbox_version ] || fail "the image script of core names no image version"
}

main() {
    local pointer_series pointer_commit
    prepare_system
    read_pointer
    install_core_files
    bash "${here}/install_layer.sh" "${passed[@]}"
    say "done: restart the machine to start it as a box"
}

main
