#!/usr/bin/env bash
# Installs the Viindoo layer on a box whose operating system and Odoo Community box files are in place.
#
# It is the one script that makes a Viindoo IoT Box out of a box of Odoo Community: the image build runs it
# inside the image it made, and install_on_raspios.sh runs it on a stock Raspberry Pi OS. Running it again
# on the same release changes nothing that matters, except that the password of the box user is locked again.
#
#   sudo install_layer.sh --layer-ref REF [OPTION...]
#
#   --layer-ref REF       tag, branch or commit of the layer to install (required)
#   --layer-source SRC    where to fetch the layer from            (default: the public repository)
#   --core-source SRC     where to fetch core from                 (default: the public repository)
#   --channel REF         the full ref a CANDIDATE box follows; the release must be a candidate then,
#                         and without this option it must not be one
#   --home DIR            home of the box user                     (default: /home/pi)
#   --skip-apt            install no Debian package (avahi-daemon is expected to be there)
#   --skip-system         do not run the system file installer of the layer
set -o errexit
set -o nounset
set -o pipefail

SCRIPT_NAME=install_layer.sh
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly here
# shellcheck source=box/install/lib.sh
source "${here}/lib.sh"

layer_ref=""
layer_source="${PUBLIC_LAYER_REPOSITORY}"
core_source="${PUBLIC_CORE_REPOSITORY}"
channel=""
BOX_HOME=/home/pi
skip_apt=""
skip_system=""

# What the binaries of core's image script leave on a box, that a Viindoo box does not carry:
# the third-party tunnel and the two payment-terminal programs.
readonly REMOVED_BINARIES="/usr/bin/ngrok /usr/local/bin/eftdvs /usr/lib/eftapi.so"
# An earlier version of this script wrote this sudo rules file; a box that has it loses it here.
readonly OBSOLETE_SUDOERS_FILE=/etc/sudoers.d/011_viindoo-iot-box
readonly SERVER_WIDE_MODULES="viin_hw_drivers,hw_drivers,hw_escpos,hw_posbox_homepage,viin_hw_homepage,web"

while [ $# -gt 0 ]; do
    case "$1" in
        --layer-ref) layer_ref="${2:?--layer-ref needs a value}"; shift 2 ;;
        --layer-source) layer_source="${2:?--layer-source needs a value}"; shift 2 ;;
        --core-source) core_source="${2:?--core-source needs a value}"; shift 2 ;;
        --channel) channel="${2:?--channel needs a value}"; shift 2 ;;
        --home) BOX_HOME="${2:?--home needs a value}"; shift 2 ;;
        --skip-apt) skip_apt=yes; shift ;;
        --skip-system) skip_system=yes; shift ;;
        *) fail "unknown option $1" ;;
    esac
done
[ -n "${layer_ref}" ] || fail "--layer-ref is required, for example --layer-ref box-<series>-<release>"
readonly layer_tree="${BOX_HOME}/iot-hw"
readonly core_tree="${BOX_HOME}/odoo"
readonly conf="${BOX_HOME}/odoo.conf"
readonly channel_file="${BOX_HOME}/viin-box-channel"

fetch_layer() {
    if [ ! -d "${layer_tree}/.git" ]; then
        as_box_user git init --quiet "${layer_tree}"
        as_box_user git -C "${layer_tree}" remote add origin "${PUBLIC_LAYER_REPOSITORY}"
    fi
    as_box_user git -C "${layer_tree}" fetch --quiet --depth 1 "${layer_source}" "${layer_ref}"
    as_box_user git -C "${layer_tree}" checkout --quiet --force --detach FETCH_HEAD
}

# Sets the keys of the [options] section, given as pairs "key value"; a key already there is replaced,
# the other keys and the other sections (core's box settings come last in the file) stay where they are.
# The file is written whole beside the old one and takes its place by one rename, with the owner and
# the mode of the old one.
set_options() {
    python3 -I - "${conf}" "$@" << 'PYTHON'
import configparser
import os
import sys
import tempfile

path, values = sys.argv[1], sys.argv[2:]
parser = configparser.RawConfigParser()
parser.optionxform = str
with open(path, encoding='utf-8') as source:
    parser.read_file(source)
if not parser.has_section('options'):
    parser.add_section('options')
for key, value in zip(values[::2], values[1::2]):
    parser.set('options', key, value)
original = os.stat(path)
handle, staged = tempfile.mkstemp(dir=os.path.dirname(path), prefix=os.path.basename(path) + '.')
try:
    with os.fdopen(handle, 'w', encoding='utf-8') as file:
        parser.write(file)
        file.flush()
        os.fsync(file.fileno())
    os.chown(staged, original.st_uid, original.st_gid)
    os.chmod(staged, original.st_mode & 0o7777)
    os.replace(staged, path)
except BaseException:
    os.unlink(staged)
    raise
PYTHON
}

# Succeeds when the [options] section of the configuration holds the key, whatever its value.
has_option() {
    python3 -I - "${conf}" "$1" << 'PYTHON'
import configparser
import sys

parser = configparser.RawConfigParser()
parser.optionxform = str
with open(sys.argv[1], encoding='utf-8') as source:
    parser.read_file(source)
sys.exit(0 if parser.has_option('options', sys.argv[2]) else 1)
PYTHON
}

configure_box_service() {
    [ -f "${conf}" ] || fail "${conf} is missing: the box files of core are not installed"
    set_options addons_path "${core_tree}/addons,${layer_tree}" server_wide_modules "${SERVER_WIDE_MODULES}" list_db False
    # An empty master password forbids every use of the database pages of the web module; core would
    # otherwise fall back to "admin". The box gives itself its own at its first start (viin-box-first-start),
    # so no image carries one that every box shares. A value already there is kept.
    if ! has_option admin_passwd; then
        set_options admin_passwd ""
    fi
    chown "${BOX_USER}:${BOX_USER}" "${conf}"
    chmod 600 "${conf}"
}

install_packages() {
    [ -z "${skip_apt}" ] || return 0
    if ! dpkg --status avahi-daemon > /dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get update --quiet=2
        DEBIAN_FRONTEND=noninteractive apt-get install --yes --quiet=2 avahi-daemon
    fi
}

remove_vendor_binaries() {
    local binary
    for binary in ${REMOVED_BINARIES}; do
        rm -f -- "${binary}"
    done
}

# The box user is root on a box, as on core's: the base system gives it sudo without a password, and core
# and the updater rely on that. What protects a box is the lock on its routes, the absence of any published
# credential and the release channel, so the layer adds no sudo rules; it only removes those of an older version.
remove_obsolete_sudoers() {
    rm -f -- "${OBSOLETE_SUDOERS_FILE}"
}

# Core's image script gives the box user a random password and prints it, which a public build log would
# keep. The password field is set to exactly "!" so that no password logs in and no hash is kept: SSH with
# a password works only after the box page has shown one ("SSH password"), which writes a new field.
# The shadow tools keep the files as they were before the change in /etc/shadow- and /etc/gshadow-, which
# would still hold the old hash; they are removed, and the next change to an account writes them again.
lock_box_user_password() {
    local field
    usermod --password '!' "${BOX_USER}"
    field="$(getent shadow "${BOX_USER}" | cut --delimiter=: --fields=2)"
    [ "${field}" = '!' ] || fail "the password of ${BOX_USER} could not be locked"
    rm -f -- /etc/shadow- /etc/gshadow-
}

# A candidate image names the ref its box follows; a supported image carries no such file.
write_channel() {
    if [ -n "${channel}" ]; then
        printf '%s\n' "${channel}" > "${channel_file}"
        chown "${BOX_USER}:${BOX_USER}" "${channel_file}"
    else
        rm -f -- "${channel_file}"
    fi
}

main() {
    local facts series candidate odoo_commit
    require_root
    id "${BOX_USER}" > /dev/null 2>&1 || fail "the box user ${BOX_USER} does not exist on this system"
    command -v git > /dev/null || fail "git is not installed"
    own_box_home
    own_box_trees "${layer_tree}" "${core_tree}"
    fetch_layer
    facts="$(bash "${layer_tree}/box/build/check_release.sh" pointer "${layer_tree}/box-release.json")" \
        || fail "the box-release.json of ${layer_ref} is not a valid release pointer"
    series="$(sed -n 's/^series=//p' <<< "${facts}")"
    candidate="$(sed -n 's/^candidate=//p' <<< "${facts}")"
    odoo_commit="$(sed -n 's/^odoo_commit=//p' <<< "${facts}")"
    if [ -n "${channel}" ]; then
        [ "${candidate}" = true ] || fail "--channel is for a candidate release; this one is supported"
        [[ "${channel}" =~ ^refs/[0-9A-Za-z._/-]+$ ]] || fail "--channel is not a full ref"
    else
        [ "${candidate}" = false ] || fail "this release is a candidate: pass --channel with the ref its box follows"
    fi
    as_box_user git -C "${layer_tree}" checkout --quiet -B "${series}"

    pin_core "${core_tree}" "${core_source}" "${odoo_commit}" "${series}"
    chown -R "${BOX_USER}:${BOX_USER}" "${layer_tree}" "${core_tree}"
    configure_box_service
    install_packages
    remove_vendor_binaries
    remove_obsolete_sudoers
    write_channel
    if [ -z "${skip_system}" ]; then
        bash "${layer_tree}/box/system/apply.sh"
    fi
    lock_box_user_password
    say "layer $(as_box_user git -C "${layer_tree}" rev-parse HEAD) on core ${odoo_commit}, release $(sed -n 's/^release=//p' <<< "${facts}")"
    say "SSH: use 'SSH password' on the box page"
}

main
