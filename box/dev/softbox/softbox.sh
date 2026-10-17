#!/usr/bin/env bash
# Host command of the software box. README.md beside this file explains what it is for.
set -o errexit
set -o nounset
set -o pipefail

usage() {
    cat <<'EOF'
Usage: softbox.sh COMMAND [ARGUMENT...]

  build [OPTION]   build the image (options of "docker build", for example --no-cache)
  start            start the box in the background
  logs [OPTION]    show the output of the box (options of "docker logs", for example --follow)
  exec COMMAND...  run a command inside the running box, as the box user
  restart          restart the box as a box that is switched off and on: what it stored is kept
  stop             stop the box and remove its container
  suite [ARG...]   run the box suite in a container of its own; arguments go to "python3 -m unittest"
  run COMMAND...   run one command in a container of its own, as the box user
  rmi              remove the image

Parameters, read from the environment:

  SOFTBOX_CORE      repository of core that holds the commit to run (needed by build, start, suite, run)
  SOFTBOX_BRANCH    branch of that repository to run             (default: the branch it has checked out)
  SOFTBOX_LAYER     tree of the layer                            (default: the tree this file is in)
  SOFTBOX_MODULES   layer = the layer first, then core; core = core alone         (default: layer)
  SOFTBOX_HANDLERS  where the simulated devices go: layer, core or none   (default: SOFTBOX_MODULES)
  SOFTBOX_NETWORK   docker network of the container                              (default: none)
  SOFTBOX_PUBLISH   ports of the box to publish, each as 127.0.0.1:HOST:BOX      (default: none)
  SOFTBOX_SERIAL    serial number of the simulated board
  SOFTBOX_IMAGE     name of the image                            (default: viindoo-iot-softbox)
  SOFTBOX_NAME      name of the box container, also its host name (default: viindoo-iot-softbox)
EOF
}

die() {
    echo "softbox.sh: $*" >&2
    exit 1
}

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
image="${SOFTBOX_IMAGE:-viindoo-iot-softbox}"
name="${SOFTBOX_NAME:-viindoo-iot-softbox}"
modules="${SOFTBOX_MODULES:-layer}"
network="${SOFTBOX_NETWORK:-none}"
posbox=addons/point_of_sale/tools/posbox

# Names in the first column of a package list, without its comments.
listed() {
    sed 's/#.*//' "${here}/packages/$1" | awk 'NF {print $1}'
}

# One of the two package lists of the image script of core.
core_packages() {
    git -C "${core}" show "${branch}:${posbox}/overwrite_before_init/etc/init_posbox_image.sh" \
        | awk -v name="$1" '
            $0 ~ "^" name "=\"" {listing = 1; next}
            listing && /"$/ {sub(/"$/, ""); print; listing = 0; next}
            listing' \
        | tr -d '\134' | awk '{for (field = 1; field <= NF; field++) print $field}'
}

resolve_core() {
    [ -n "${SOFTBOX_CORE:-}" ] || die "set SOFTBOX_CORE to a repository of core"
    core="$(cd "${SOFTBOX_CORE}" && pwd)" || die "SOFTBOX_CORE is not a directory"
    branch="${SOFTBOX_BRANCH:-$(git -C "${core}" symbolic-ref --quiet --short HEAD)}" \
        || die "the core repository has no branch checked out: set SOFTBOX_BRANCH"
    git -C "${core}" rev-parse --quiet --verify "${branch}^{commit}" > /dev/null \
        || die "the core repository has no branch ${branch}"
}

build() {
    local apt pip
    resolve_core
    apt="$( { core_packages PKGS_TO_INSTALL | grep --invert-match --line-regexp --fixed-strings \
                --file <(listed apt.omit); listed apt.extra; } | sort --unique | xargs)"
    pip="$(core_packages PIP_TO_INSTALL | grep --invert-match --line-regexp --fixed-strings \
                --file <(listed pip.omit) | xargs)"
    if [ -z "${apt}" ] || [ -z "${pip}" ]; then
        die "no package list found in the image script of core"
    fi
    docker build "$@" --tag "${image}" --build-arg "APT_PACKAGES=${apt}" --build-arg "PIP_PACKAGES=${pip}" "${here}"
}

# Everything a container of the software box is allowed. Nothing else is given: no
# privileged mode, no device, no host network, no docker socket, no volume.
container_options() {
    local layer port
    [ "${network}" != host ] || die "the software box never uses the host network: unset SOFTBOX_NETWORK or name another docker network"
    resolve_core
    layer="$(cd "${SOFTBOX_LAYER:-${here}/../../..}" && pwd)" || die "SOFTBOX_LAYER is not a directory"
    case "${modules}" in
        layer) ;;
        core)
            # Core alone calls hosts a Viindoo box must never reach.
            [ "${network}" = none ] || die "core alone runs without a network: unset SOFTBOX_NETWORK"
            ;;
        *) die "SOFTBOX_MODULES must be layer or core" ;;
    esac
    options=(
        --init
        --hostname "${name}"
        --add-host "${name}:127.0.1.1"
        --network "${network}"

        # Root inside the container keeps six capabilities, all about users and files inside it.
        # The services of the box (web server, print server, message bus) and the entry point
        # change user (SETUID, SETGID) and signal the processes they started as another user
        # (KILL); sudo, through which core runs its system commands, needs the first two as
        # well. The entry point gives the box user its files (CHOWN, FOWNER, DAC_OVERRIDE).
        --cap-drop ALL
        --cap-add SETUID --cap-add SETGID --cap-add KILL
        --cap-add CHOWN --cap-add FOWNER --cap-add DAC_OVERRIDE

        # Core reads the serial number of the board under /sys/firmware when it is imported.
        # An empty memory file system replaces /sys, so the box sees nothing of the hardware
        # of the host; the default AppArmor profile of docker forbids every access under
        # /sys/firmware, even there, so the container runs without that profile.
        --tmpfs "/sys:rw,nosuid,nodev,noexec,size=64k"
        --security-opt apparmor=unconfined

        # Both trees are copied inside the container; nothing is written back to the host.
        --mount "type=bind,source=${core},target=/mnt/core,readonly"
        --mount "type=bind,source=${layer},target=/mnt/layer,readonly"

        --env "SOFTBOX_CORE_BRANCH=${branch}"
        --env "SOFTBOX_MODULES=${modules}"
    )
    [ -z "${SOFTBOX_HANDLERS:-}" ] || options+=(--env "SOFTBOX_HANDLERS=${SOFTBOX_HANDLERS}")
    [ -z "${SOFTBOX_SERIAL:-}" ] || options+=(--env "SOFTBOX_SERIAL=${SOFTBOX_SERIAL}")
    for port in ${SOFTBOX_PUBLISH:-}; do
        case "${port}" in
            127.0.0.1:*) options+=(--publish "${port}") ;;
            *) die "publish ports on 127.0.0.1 only, not ${port}" ;;
        esac
    done
    if [ -t 0 ] && [ -t 1 ]; then
        terminal=(--interactive --tty)
    else
        terminal=()
    fi
}

command="${1:-}"
[ $# -eq 0 ] || shift
case "${command}" in
    build)
        build "$@"
        ;;
    start)
        container_options
        docker rm --force --volumes "${name}" > /dev/null 2>&1 || true
        docker run --detach --name "${name}" "${options[@]}" "${image}" box > /dev/null
        echo "The box runs in the container ${name}."
        ;;
    logs)
        docker logs "$@" "${name}"
        ;;
    exec)
        [ $# -gt 0 ] || die "exec needs a command"
        if [ -t 0 ] && [ -t 1 ]; then terminal=(--interactive --tty); else terminal=(); fi
        docker exec "${terminal[@]}" --user pi --workdir /home/pi --env HOME=/home/pi "${name}" "$@"
        ;;
    restart)
        docker restart "${name}" > /dev/null
        ;;
    stop)
        docker rm --force --volumes "${name}" > /dev/null
        ;;
    suite | run)
        container_options
        docker run --rm --name "${name}-${command}-$$" "${terminal[@]}" "${options[@]}" "${image}" "${command}" "$@"
        ;;
    rmi)
        docker rmi "${image}"
        ;;
    "" | help | -h | --help)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
