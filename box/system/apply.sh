#!/usr/bin/env bash
# Installs the system files of a Viindoo IoT Box: README.md beside this file says what each is for.
#
# Run as root when the box software is installed and after each of its updates. It copies what
# differs and nothing else, so running it again changes nothing. On a running system it restarts
# the Wi-Fi watch when its program or unit changed, and starts it otherwise; it starts and stops
# no other service: the box service restarts the web server each time it starts, and an update
# restarts the box service.
set -o errexit
set -o nounset
set -o pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly here
# The card of a box is read-only, and /etc lives in memory: the second path leads to the card.
readonly CARD="/ /root_bypass_ramdisks"
readonly CARD_ETC=/root_bypass_ramdisks/etc
readonly GUARD=usr/local/sbin/viin-box-guard
readonly FIRST_START=viin-box-first-start.service
readonly WIFI=viin-box-wifi.service
readonly WIFI_WATCH=viin-box-wifi-watch.service
readonly WIFI_PROGRAM=usr/local/sbin/viin-box-wifi
readonly ACCESS_POINT_NAME=usr/local/sbin/viin-box-access-point-name

# Each file under this directory, as its path on the box, with its permissions.
readonly FILES="
etc/avahi/services/viindoo-iot.service 644
etc/nginx/sites-enabled/default 644
etc/nginx/viindoo/box.conf 644
etc/nginx/viindoo/name.conf.template 644
etc/systemd/system/odoo.service.d/viindoo.conf 644
etc/systemd/system/hostapd.service.d/viindoo.conf 644
etc/systemd/system/${FIRST_START} 644
etc/systemd/system/${WIFI} 644
etc/systemd/system/${WIFI_WATCH} 644
usr/local/sbin/viin-box-first-start 755
${WIFI_PROGRAM} 755
${ACCESS_POINT_NAME} 755
${GUARD} 755
usr/share/viindoo-iot/box-down.html 644
"

closed=""
changed=0
installed=0
watch_changed=0

say() {
    echo "apply.sh: $*"
}

fail() {
    echo "apply.sh: $*" >&2
    exit 1
}

read_only() {
    awk -v at="$1" '$2 == at { state = ("," $4 ",") ~ /,ro,/ } END { exit !state }' /proc/mounts 2> /dev/null
}

# The card is left as it was found: whoever runs this file inside its own write window keeps it open.
open_card() {
    local point
    for point in ${CARD}; do
        if read_only "${point}"; then
            mount -o remount,rw "${point}"
            closed="${closed} ${point}"
        fi
    done
}

close_card() {
    local point
    for point in ${closed}; do
        mount -o remount,ro "${point}" || true
    done
    # Closing the card closes the directory of the print server with it, which must stay writable.
    if [ -n "${closed}" ] && mountpoint -q "${CARD_ETC}/cups"; then
        mount -o remount,rw "${CARD_ETC}/cups" || true
    fi
}

# /etc as the running system reads it and, on a box, /etc of the card.
etc_places() {
    echo /etc
    if [ -d "${CARD_ETC}" ] && [ ! "${CARD_ETC}" -ef /etc ]; then
        echo "${CARD_ETC}"
    fi
}

# The places a path of the box is written to.
places() {
    local etc
    case "$1" in
        etc/*)
            while read -r etc; do
                echo "${etc}/${1#etc/}"
            done < <(etc_places)
            ;;
        *)
            echo "/$1"
            ;;
    esac
}

# A file takes its place whole or not at all. The name it is written under first starts with a
# dot: the web server reads every other file of a directory it includes.
put() {
    local source="$1" target="$2" mode="$3" staged
    if cmp --silent "${source}" "${target}" 2> /dev/null && [ "$(stat --format %a "${target}")" = "${mode}" ]; then
        return 0
    fi
    staged="$(dirname "${target}")/.$(basename "${target}").viin-apply"
    install -D --owner root --group root --mode "${mode}" "${source}" "${staged}"
    mv --force "${staged}" "${target}"
    say "installed ${target}"
    installed=1
}

link() {
    local target="$1" name="$2"
    if [ "$(readlink "${name}" 2> /dev/null || true)" != "${target}" ]; then
        mkdir -p "$(dirname "${name}")"
        ln --symbolic --force --no-dereference "${target}" "${name}"
        say "linked ${name}"
        changed=1
    fi
}

[ "$(id -u)" -eq 0 ] || fail "run this file as root"

# The guard is what undoes a release that does not start: one that cannot run replaces nothing.
python3 -I "${here}/${GUARD}" --check > /dev/null || fail "the roll-back guard of this release cannot run; nothing was installed"

trap close_card EXIT
open_card

while read -r path mode; do
    [ -n "${path}" ] || continue
    [ -f "${here}/${path}" ] || fail "${path} is missing from this release"
    installed=0
    while read -r place; do
        put "${here}/${path}" "${place}" "${mode}"
    done < <(places "${path}")
    if [ "${installed}" -eq 1 ]; then
        changed=1
        case "${path}" in
            "${WIFI_PROGRAM}" | "etc/systemd/system/${WIFI_WATCH}") watch_changed=1 ;;
        esac
    fi
done <<< "${FILES}"

while read -r etc; do
    # Where the box writes the web server block of a name it was issued.
    mkdir -p "${etc}/nginx/viindoo/names"
    link "../${FIRST_START}" "${etc}/systemd/system/basic.target.wants/${FIRST_START}"
    link "../${WIFI}" "${etc}/systemd/system/multi-user.target.wants/${WIFI}"
    link "../${WIFI_WATCH}" "${etc}/systemd/system/multi-user.target.wants/${WIFI_WATCH}"
done < <(etc_places)

if command -v nginx > /dev/null; then
    nginx -t -q || fail "the web server refuses its configuration"
fi
if [ -d /run/systemd/system ]; then
    if [ "${changed}" -eq 1 ]; then
        systemctl daemon-reload || true
    fi
    # The watch runs the program it started with: after an update, or a roll-back, it must run the one installed.
    # It ends the look it is in before it stops; --no-block, since the box service may be what runs this file.
    if [ "${watch_changed}" -eq 1 ]; then
        systemctl --no-block restart "${WIFI_WATCH}" || say "the Wi-Fi watch could not be restarted"
    else
        systemctl --no-block start "${WIFI_WATCH}" || say "the Wi-Fi watch could not be started"
    fi
fi
[ "${changed}" -eq 1 ] || say "nothing to change"
