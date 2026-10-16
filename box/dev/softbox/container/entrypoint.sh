#!/usr/bin/env bash
# Entry point of the software box. It lays out the files a box has, starts the services
# a box runs beside its own, then runs one of:
#   box              the box service, as the service unit of the image starts it
#   suite [ARG...]   the box suite of the layer, through "python3 -m unittest"
#   run COMMAND...   one command
# The last two run as the box user and their exit code is the exit code of the container.
set -o errexit
set -o nounset
set -o pipefail

readonly CORE_REMOTE=/mnt/core
readonly LAYER_SOURCE=/mnt/layer
readonly CORE_DIR=/home/pi/odoo
readonly LAYER_DIR=/home/pi/iot-hw
readonly CONF=/home/pi/odoo.conf
readonly POSBOX=addons/point_of_sale/tools/posbox
readonly OVERLAY="${POSBOX}/overwrite_after_init"
readonly STATE_DIR=/var/lib/softbox
readonly LAID_OUT="${STATE_DIR}/laid-out"
readonly SERIAL_FILE=/sys/firmware/devicetree/base/serial-number
readonly LAYER_FIRST_MODULES=viin_hw_drivers,hw_drivers,hw_escpos,hw_posbox_homepage,viin_hw_homepage,web
readonly PRINT_QUEUES="softbox_office softbox_zpl softbox_tm-m30"
readonly AS_PI=(setpriv --reuid=pi --regid=pi --init-groups env HOME=/home/pi USER=pi LOGNAME=pi)

SOFTBOX_MODULES="${SOFTBOX_MODULES:-layer}"
SOFTBOX_HANDLERS="${SOFTBOX_HANDLERS:-${SOFTBOX_MODULES}}"
SOFTBOX_SERIAL="${SOFTBOX_SERIAL:-10000000c0ffee01}"

# unittest reports success when it ran no test at all, which a mistyped selection would turn
# into a verdict; the runner answers that case with a failure.
readonly SUITE_RUNNER='
import sys
import unittest

program = unittest.main(module=None, argv=["python3 -m unittest", *sys.argv[1:]], exit=False)
if not program.result.testsRun:
    sys.exit("softbox: no test ran")
sys.exit(not program.result.wasSuccessful())
'

fail() {
    echo "softbox: $*" >&2
    exit 1
}

core_git() {
    "${AS_PI[@]}" git -C "${CORE_DIR}" "$@"
}

layer_git() {
    "${AS_PI[@]}" git -C "${LAYER_DIR}" "$@"
}

# Core reads the board serial at import and the link state for its page. Both live under
# /sys, which holds no board here: the container gets an empty memory file system there.
lay_out_board() {
    if ! { mkdir -p "$(dirname "${SERIAL_FILE}")" 2> /dev/null \
            && printf '%s\0' "${SOFTBOX_SERIAL}" > "${SERIAL_FILE}" 2> /dev/null; }; then
        fail "cannot write ${SERIAL_FILE}: the container needs a memory file system on /sys" \
             "and must not run under the default AppArmor profile (softbox.sh sets both)"
    fi
    ip -o link show | awk '{
        name = $2; sub(/[@:].*/, "", name)
        for (field = 3; field < NF; field++) if ($field == "state") print name, tolower($(field + 1))
    }' | while read -r name state; do
        mkdir -p "/sys/class/net/${name}"
        echo "${state}" > "/sys/class/net/${name}/operstate"
    done
}

# The clone, the sparse paths and the two steps after it are the ones of the image scripts of core.
clone_core() {
    [ -n "${SOFTBOX_CORE_BRANCH:-}" ] || fail "SOFTBOX_CORE_BRANCH is not set"
    "${AS_PI[@]}" git clone --quiet --branch "${SOFTBOX_CORE_BRANCH}" --no-local --no-checkout --depth 1 \
        "${CORE_REMOTE}" "${CORE_DIR}" \
        || fail "cannot clone branch ${SOFTBOX_CORE_BRANCH} of the core repository mounted on ${CORE_REMOTE}"
    core_git config core.sparsecheckout true
    printf '%s\n' addons/web 'addons/hw_*' "${POSBOX}/configuration" odoo/ odoo-bin \
        | "${AS_PI[@]}" tee "${CORE_DIR}/.git/info/sparse-checkout" > /dev/null
    core_git read-tree -mu HEAD
    echo "addons/hw_drivers/iot_devices/" | "${AS_PI[@]}" tee "${CORE_DIR}/.git/info/exclude" > /dev/null
    mv "${CORE_DIR}/${POSBOX}/configuration/odoo.conf" "${CONF}"
}

# The files the image copies over the system, limited to the services the software box runs.
lay_out_system() {
    local image_version certificate_name
    core_git archive HEAD \
        "${OVERLAY}/etc/cups" "${OVERLAY}/etc/nginx" "${OVERLAY}/var/www" "${OVERLAY}/home/pi/odoo" \
        | tar --extract --directory / --strip-components 5 --no-same-owner --no-overwrite-dir
    chown -R pi:pi "${CORE_DIR}/addons/point_of_sale"

    image_version="$(core_git show "HEAD:${POSBOX}/posbox_create_image.sh" | sed -n 's/^VERSION_IOTBOX=//p')"
    [ -n "${image_version}" ] || fail "the image script of core names no image version"
    echo "${image_version}" > /var/odoo/iotbox_version

    # The image ships a certificate whose private key is public. The software box keeps its
    # subject, by which core tells it from a real one, and generates a key of its own.
    certificate_name="$(core_git show "HEAD:${OVERLAY}/etc/ssl/certs/nginx-cert.crt" \
        | openssl x509 -noout -subject -nameopt multiline | sed -n 's/^ *commonName *= *//p')"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=${certificate_name}" \
        -keyout /etc/ssl/private/nginx-cert.key -out /etc/ssl/certs/nginx-cert.crt 2> /dev/null

    mkdir -p /var/run/odoo
    chown pi:pi /var/run/odoo
}

# The layer becomes a repository of its own inside the container: the tree on the host is
# mounted read-only and may be a work tree whose repository is not reachable from here.
copy_layer() {
    [ -d "${LAYER_SOURCE}" ] || fail "no layer tree is mounted on ${LAYER_SOURCE}"
    mkdir -p "${LAYER_DIR}"
    rsync --recursive --links --perms --times --exclude=/.git --exclude=__pycache__ \
        "${LAYER_SOURCE}/" "${LAYER_DIR}/"
    chown -R pi:pi "${LAYER_DIR}"
    layer_git init --quiet --initial-branch "${SOFTBOX_CORE_BRANCH}"
    layer_git add --all
    layer_git -c user.name=softbox -c user.email=softbox@localhost \
        commit --quiet --allow-empty --message "Layer tree copied into the software box"
}

configure_modules() {
    case "${SOFTBOX_MODULES}" in
        core) ;;
        layer)
            # The configuration of core has a single section at this point, so appended lines belong to it.
            sed -i '/^server_wide_modules/d' "${CONF}"
            printf 'addons_path = %s\nserver_wide_modules = %s\n' \
                "${CORE_DIR}/addons,${LAYER_DIR}" "${LAYER_FIRST_MODULES}" >> "${CONF}"
            ;;
        *) fail "SOFTBOX_MODULES must be layer or core, not ${SOFTBOX_MODULES}" ;;
    esac
    chown pi:pi "${CONF}"
}

# The system files of the layer, laid out by the installer script of the image. It runs after the
# files of core, as in the image, and stops at the first thing it refuses. Nothing is skipped for the
# container: the card is not read-only here, no unit is started, and the installer reloads the
# init system only when one runs.
lay_out_layer_system() {
    local installer="${LAYER_DIR}/box/system/apply.sh"
    [ -f "${installer}" ] || fail "the layer has no system file installer: ${installer} is missing"
    bash "${installer}" || fail "the system file installer of the layer failed"
}

install_simulated_handlers() {
    local repository handlers
    case "${SOFTBOX_HANDLERS}" in
        none) return ;;
        core) repository="${CORE_DIR}"; handlers=addons/hw_drivers/iot_handlers ;;
        layer) repository="${LAYER_DIR}"; handlers=viin_hw_drivers/iot_handlers ;;
        *) fail "SOFTBOX_HANDLERS must be layer, core or none, not ${SOFTBOX_HANDLERS}" ;;
    esac
    mkdir -p "${repository}/${handlers}/interfaces" "${repository}/${handlers}/drivers"
    cp --recursive /usr/share/softbox/iot_handlers/. "${repository}/${handlers}/"
    chown -R pi:pi "${repository}/${handlers}"
    (cd /usr/share/softbox/iot_handlers && find . -type f -printf "/${handlers}/%P\n") \
        | "${AS_PI[@]}" tee --append "${repository}/.git/info/exclude" > /dev/null

    echo "0.000" > "${STATE_DIR}/scale-weight"
    : > "${STATE_DIR}/scanner-scans"
    chown pi:pi "${STATE_DIR}/scale-weight" "${STATE_DIR}/scanner-scans"
}

# The printer handlers of core need the print server and the system message bus when they are loaded.
start_services() {
    local waited=0 queue message
    # What the services of a container that is started again left behind would keep them from starting.
    rm -f /run/dbus/pid /run/nginx.pid /run/cups/cups.sock /var/run/odoo/odoo.pid
    mkdir -p /run/dbus
    dbus-uuidgen --ensure
    dbus-daemon --system --fork
    cupsd
    until [ -S /run/cups/cups.sock ]; do
        waited=$((waited + 1))
        [ "${waited}" -le 30 ] || fail "the print server did not start"
        sleep 1
    done
    for queue in ${PRINT_QUEUES}; do
        message="$(lpadmin -p "${queue}" -E -v "softbox:/${queue}" -D "Simulated printer of the software box" 2>&1)" \
            || fail "cannot create the print queue ${queue}: ${message}"
    done
}

# The service unit of the image: the box user, the error stream appended to the log file,
# a new start five seconds after a failure. The loop beside it is the cron line of the image.
run_box() {
    local status service="" stopping=""
    while true; do
        rm -f /var/run/odoo/sessions/*
        sleep 60
    done &
    trap 'stopping=yes; kill "${service}" 2> /dev/null' TERM INT
    while true; do
        "${AS_PI[@]}" /usr/bin/python3 "${CORE_DIR}/odoo-bin" --config "${CONF}" \
            > /dev/null 2> >(tee --append /var/log/odoo/odoo-server.log >&2) &
        service=$!
        status=0
        wait "${service}" || status=$?
        if [ -n "${stopping}" ]; then
            wait "${service}" 2> /dev/null || true
            break
        fi
        [ "${status}" -ne 0 ] || break
        sleep 5
    done
}

run_suite() {
    local tests="${LAYER_DIR}/viin_hw_drivers/tests"
    [ -d "${tests}" ] || fail "the layer has no box suite: ${tests} is missing"
    cd "${tests}"
    exec "${AS_PI[@]}" PYTHONPATH="${CORE_DIR}" python3 -c "${SUITE_RUNNER}" "$@"
}

run_command() {
    [ $# -gt 0 ] || fail "run needs a command"
    cd /home/pi
    exec "${AS_PI[@]}" PYTHONPATH="${CORE_DIR}" "$@"
}

main() {
    local command="${1:-box}"
    [ $# -eq 0 ] || shift
    case "${command}" in
        box | suite | run) ;;
        *) fail "unknown command ${command}: use box, suite or run" ;;
    esac
    # /sys is empty again at every start; the rest is laid out once, so that starting a
    # stopped container again is the restart of a box that keeps what it stored.
    lay_out_board
    if [ ! -e "${LAID_OUT}" ]; then
        clone_core
        lay_out_system
        copy_layer
        configure_modules
        [ "${SOFTBOX_MODULES}" != layer ] || lay_out_layer_system
        install_simulated_handlers
        touch "${LAID_OUT}"
    fi
    start_services
    echo "softbox: core $(core_git rev-parse HEAD), modules ${SOFTBOX_MODULES}, simulated handlers ${SOFTBOX_HANDLERS}" >&2
    case "${command}" in
        box) run_box ;;
        suite) run_suite "$@" ;;
        run) run_command "$@" ;;
    esac
}

main "$@"
