#!/usr/bin/env bash
# Builds the image of a Viindoo IoT Box release from a tag of the layer repository.
#
#   sudo build_image.sh --tag box-<series>-<release> [OPTION...]
#
#   --tag TAG          the release to build (required); rc.<n> builds a candidate, anything else a supported image
#   --expect-mode MODE candidate or supported: fail unless the script's own reading of the tag gives that kind
#   --expect-commit SHA fail unless the tag names this commit and the repository is checked out at it
#   --series-ref REF   the branch a supported release must be on     (required for a supported release)
#   --repository DIR   the layer repository that holds the tag       (default: the one this file is in)
#   --work DIR         scratch space, about 10 GB                     (default: ./box-image-work)
#   --output DIR       where the files are written                   (default: ./box-image-out)
#
# Steps: (1) check the tag; (2) put core at the commit the tag's pointer names; (3) run the image script of core,
# unmodified, on the pinned operating system; (4) install the layer in the image it made; (5) check what went in,
# clear free space, compress, and write the checksum file and the manifest.
#
# Package downloads of steps (3) and (4) do not depend on one mirror: the files of box/build/apt/ are put in the image
# before its script installs packages, and taken out again before the checks of step (5), which fail an image that
# still holds one. Its apt-get repeats an update that fails while the extra sources are listed without them.
#
# Step (1) comes first, right after the options: the tag is read, and --expect-mode and --expect-commit are checked,
# before root, the tools named below or any work directory are needed. It needs only bash, git and python3.
#
# The host needs: kpartx, qemu-user-static (qemu-arm-static, registered for ARM programs), zerofree, xz, rsync,
# fdisk, wget, git, python3 and a unit NetworkManager (the image script of core reloads it). On a Linux PC this
# runs as it is; the workflow of the repository does the same on a hosted runner. It needs root.
#
# Sourcing this file defines its functions and runs nothing.
set -o errexit
set -o nounset
set -o pipefail

SCRIPT_NAME=build_image.sh
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly here
# shellcheck source=box/install/lib.sh
source "${here}/../install/lib.sh"
# shellcheck source=box/build/base-image.conf
source "${here}/base-image.conf"

readonly POSBOX=addons/point_of_sale/tools/posbox
readonly BOX_REQUIREMENTS="git python3 kpartx qemu-arm-static zerofree xz rsync fdisk wget unxz sha256sum chroot mount"
readonly REMOVED_FROM_IMAGE="usr/bin/ngrok usr/local/bin/eftdvs usr/lib/eftapi.so"
# The package configuration of the build only, as a tree of the paths it has in the image.
readonly BUILD_ONLY_APT="${here}/apt"
# Where the programs of the image are looked for while the image script of core and the layer installer run: the
# apt-get of box/build/apt comes first. The image script of core gives its chroot the path it has itself, so the
# directories of the image are put in front of the path of the host, which keeps the tools of the host in reach.
readonly IMAGE_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# The unit of the base system that makes the SSH host keys of a box at its first start.
readonly HOST_KEY_UNIT=regenerate_ssh_host_keys.service

tag=""
expect_mode=""
expect_commit=""
series_ref=""
repository="$(cd "${here}/../.." && pwd)"
work="$(pwd)/box-image-work"
output="$(pwd)/box-image-out"
series="" release="" mode="" odoo_commit="" layer_commit="" channel="" image_name=""
posbox_dir="" root="" root_device="" boot_device=""

mounted=()
mapped=""
cleanup() {
    local index
    for ((index = ${#mounted[@]} - 1; index >= 0; index--)); do
        umount --lazy "${mounted[index]}" 2> /dev/null || true
    done
    if [ -n "${mapped}" ]; then
        kpartx -d "${mapped}" > /dev/null 2>&1 || true
    fi
}

mount_here() {
    mount "$@"
    mounted+=("${!#}")
}

# The core checkout the image script of core is run from: on the commit the pointer names, on a branch
# of the series name that has a remote, which is what that script asks git for.
prepare_core() {
    local tree="${work:?}/odoo"
    rm -rf "${tree:?}"
    git init --quiet --initial-branch "${series}" "${tree}"
    git -C "${tree}" remote add origin "${PUBLIC_CORE_REPOSITORY}"
    git -C "${tree}" fetch --quiet --depth 1 origin "${odoo_commit}"
    git -C "${tree}" checkout --quiet -B "${series}" FETCH_HEAD
    git -C "${tree}" config "branch.${series}.remote" origin
    posbox_dir="${tree}/${POSBOX}"
    # The base image is pinned here, so the script must be asking for the same file.
    grep --quiet --fixed-strings "${BASE_IMAGE_NAME}" "${posbox_dir}/posbox_create_image.sh" \
        || fail "the image script of core builds on another operating system than the one pinned in base-image.conf"
    refuse_core_tracing
}

# The paths, in the image, of the package configuration of the build only, one a line.
build_only_apt_files() {
    (cd "${BUILD_ONLY_APT}" && find . -type f | sed 's|^\./||' | sort)
}

# The start of the names apt gives the index files it keeps of each source of the build only, in /var/lib/apt/lists:
# the address of the source with its slashes made underscores, then dists and the suite.
build_only_apt_list_prefixes() {
    local file
    while read -r file; do
        sed -n -E 's#^deb[[:space:]]+(\[[^]]*\][[:space:]]+)?https?://([^[:space:]]+)[[:space:]]+([^[:space:]]+).*#\2dists/\3_#p' \
            "${BUILD_ONLY_APT}/${file}" | tr / _
    done < <(build_only_apt_files | grep '\.list$')
}

# The image script of core copies this directory into the image before it installs a package.
place_build_only_apt_configuration() {
    local file
    while read -r file; do
        install -D --mode "$(stat --format %a "${BUILD_ONLY_APT}/${file}")" "${BUILD_ONLY_APT}/${file}" \
            "${posbox_dir:?}/overwrite_before_init/${file}"
    done < <(build_only_apt_files)
}

# The log of a build is public and a trace prints every assignment, so the password of the box user would
# reach it. withhold_core_password cannot hold that value back, so a script of core that traces fails the build:
# a line "set -x", "set -o xtrace", or a "set -" option cluster that holds x, outside a comment.
refuse_core_tracing() {
    local script
    for script in posbox_create_image.sh overwrite_before_init/etc/init_posbox_image.sh; do
        if grep --extended-regexp --quiet \
            '^[[:space:]]*set[[:space:]]+(.*[[:space:]])?(-[A-Za-z]*x[A-Za-z]*|-o[[:space:]]+xtrace)([[:space:]]|;|$)' \
            "${posbox_dir}/${script}"; then
            fail "the image script of core ${script} turns on tracing, which would print the password of the box user in the log"
        fi
    done
}

# The pinned operating system is placed where the image script of core looks for it, so it downloads nothing.
place_base_image() {
    local archive="${work:?}/${BASE_IMAGE_NAME}"
    if [ ! -f "${archive}" ]; then
        wget --quiet --output-document "${archive}.part" "${BASE_IMAGE_URL}"
        mv "${archive}.part" "${archive}"
    fi
    echo "${BASE_IMAGE_SHA256}  ${archive}" | sha256sum --check --quiet \
        || fail "the operating system file does not match the checksum pinned in base-image.conf"
    rm -f "${posbox_dir:?}"/*raspios*.img "${posbox_dir:?}/iotbox.img"
    xz --decompress --keep --stdout "${archive}" > "${posbox_dir}/${BASE_IMAGE_NAME%.xz}"
}

# The image script of core sets a random password for the box user and prints it, on a line of its own after
# a line that reads "password". The log of a build is public, so that value is replaced; every other line passes.
# This holds for the output of the script as written, not for a trace of it (refuse_core_tracing).
withhold_core_password() {
    awk '
        {
            line = $0
            sub(/\r$/, "", line)
            if (withhold) {
                print "[withheld by the Viindoo build]"
                withhold = 0
            } else {
                print $0
                withhold = (line == "password")
            }
            fflush()
        }'
}

# Runs a command and passes its output, standard error included, through withhold_core_password; the status is
# the command's own (pipefail), so a failing command still fails the build. It withholds only the line after
# "password"; a command that traces its own shell commands is not covered.
run_withholding_password() {
    "$@" 2>&1 | withhold_core_password
}

core_script() {
    (cd "${posbox_dir}" && printf '%s\n%s\n' "${series}" Viindoo | PATH="${IMAGE_PATH}:${PATH}" bash ./posbox_create_image.sh)
}

run_core_script() {
    run_withholding_password core_script
    [ -f "${posbox_dir}/iotbox.img" ] || fail "the image script of core wrote no image"
}

# Runs a command inside the image, on the mounted root.
in_image() {
    chroot "${root}" env "PATH=${IMAGE_PATH}" "$@"
}

install_layer_in_image() {
    local box_ids mapping install_args=() layer_source=/tmp/layer-source.git
    root="${work:?}/root"
    mkdir -p "${root}"
    mapped="${posbox_dir}/iotbox.img"
    mapping="$(kpartx -avs "${mapped}")"
    root_device="/dev/mapper/$(tail -n 1 <<< "${mapping}" | awk '{print $3}')"
    boot_device="/dev/mapper/$(head -n 1 <<< "${mapping}" | awk '{print $3}')"
    mount_here "${root_device}" "${root}"
    # The firmware partition is /boot on a box, as core's image script mounts it: the installer writes the screen
    # setting into /boot/config.txt there.
    mount_here "${boot_device}" "${root}/boot"
    mount_here --bind /proc "${root}/proc"
    mount_here --bind /sys "${root}/sys"
    mount_here --bind /dev "${root}/dev"
    mount_here --bind "$(readlink -f /etc/resolv.conf)" "${root}/etc/resolv.conf"
    cp "$(command -v qemu-arm-static)" "${root}/usr/bin/"
    # The image must not start the services of the packages the installer adds while it is only being built.
    printf '#!/bin/sh\nexit 101\n' > "${root}/usr/sbin/policy-rc.d"
    chmod 755 "${root}/usr/sbin/policy-rc.d"

    box_ids="$(awk -F: -v user="${BOX_USER}" '$1 == user {print $3 ":" $4}' "${root}/etc/passwd")"
    [ -n "${box_ids}" ] || fail "the image has no user ${BOX_USER}"
    rm -rf "${root:?}${layer_source}" "${root:?}/tmp/box-install"
    git clone --quiet --bare --no-local "${repository}" "${root}${layer_source}"
    chown -R "${box_ids}" "${root}${layer_source}"
    cp -r "${here}/../install" "${root}/tmp/box-install"
    if [ "${mode}" = candidate ]; then
        install_args=(--channel "${channel}")
    fi
    in_image /tmp/box-install/install_layer.sh --layer-ref "${tag}" --layer-source "${layer_source}" "${install_args[@]}"
    rm -rf "${root:?}${layer_source}" "${root:?}/tmp/box-install"
    rm -f "${root:?}/usr/sbin/policy-rc.d"
    remove_build_only_apt_configuration
    remove_ssh_host_keys
}

# What apt keeps of the sources it has read, besides the index files: the caches it builds from them.
readonly APT_CACHE_FILES=(var/cache/apt/pkgcache.bin var/cache/apt/srcpkgcache.bin)

# The index files apt keeps of the sources of the build, finished and being fetched, one path a line.
build_only_apt_list_files() {
    local prefix directory
    while read -r prefix; do
        for directory in var/lib/apt/lists var/lib/apt/lists/partial; do
            compgen -G "${root}/${directory}/${prefix}*" || true
        done
    done < <(build_only_apt_list_prefixes)
}

# The image a customer writes keeps the package sources and settings of the operating system: what only the build
# used goes, with the index files and the caches apt built from the sources of the build.
remove_build_only_apt_configuration() {
    local file
    while read -r file; do
        rm -f -- "${root:?}/${file}"
    done < <(build_only_apt_files; printf '%s\n' "${APT_CACHE_FILES[@]}")
    while read -r file; do
        rm -f -- "${file}"
    done < <(build_only_apt_list_files)
}

refuse_build_only_apt_configuration() {
    local file left=""
    while read -r file; do
        [ ! -e "${root}/${file}" ] || left="${left} /${file}"
    done < <(build_only_apt_files; printf '%s\n' "${APT_CACHE_FILES[@]}")
    while read -r file; do
        left="${left} ${file#"${root}"}"
    done < <(build_only_apt_list_files)
    [ -z "${left}" ] || fail "the image still holds package configuration of the build:${left}"
}

# The SSH host key files of the mounted image, one path a line.
ssh_host_key_files() {
    [ -d "${root}/etc/ssh" ] || return 0
    find "${root}/etc/ssh" -maxdepth 1 \( -type f -o -type l \) -name 'ssh_host_*'
}

# Succeeds when the base system makes the host keys at the first start: its unit is in the image and enabled.
host_key_unit_enabled() {
    [ -n "$(find "${root}/etc/systemd/system" -path "*.wants/${HOST_KEY_UNIT}" 2> /dev/null)" ] \
        && { [ -e "${root}/usr/lib/systemd/system/${HOST_KEY_UNIT}" ] || [ -e "${root}/etc/systemd/system/${HOST_KEY_UNIT}" ]; }
}

# Every card is written from one image, so a host key in the image would be the key of every box. Keys the build
# made are removed, and each box makes its own at its first start, which only the unit of the base system does: the
# build fails when that unit is not enabled, since a box would then have no host key at all. Nothing runs on a box.
remove_ssh_host_keys() {
    local path
    host_key_unit_enabled || fail "the image does not enable ${HOST_KEY_UNIT}, which makes the SSH host keys of a box at its first start"
    while read -r path; do
        rm -f -- "${path}"
        say "removed /${path#"${root}/"} from the image"
    done < <(ssh_host_key_files)
}

refuse_ssh_host_keys() {
    local path listed=""
    while read -r path; do
        listed="${listed} /${path#"${root}/"}"
    done < <(ssh_host_key_files)
    [ -z "${listed}" ] || fail "the image holds an SSH host key, which every box written from it would share:${listed}"
}

# What the image holds is read from the image, never taken from what the build meant to put there.
verify_and_describe_image() {
    local path layer_in_image core_in_image shadow_fields backup
    for path in ${REMOVED_FROM_IMAGE}; do
        [ ! -e "${root}/${path}" ] || fail "the image still holds /${path}"
    done
    refuse_build_only_apt_configuration
    refuse_ssh_host_keys
    [ -f "${root}/boot/config.txt" ] || fail "the image holds no /boot/config.txt"
    firmware_hotplug check "${root}/boot/config.txt" \
        || fail "/boot/config.txt of the image does not hold ${HDMI_HOTPLUG_SETTING} under its last [all] section"
    # The box user needs no password to use sudo: core's scripts run commands as root on its behalf, and the base
    # system grants exactly this. The layer adds no sudoers file of its own.
    [ -f "${root}/etc/sudoers.d/010_pi-nopasswd" ] || fail "the image holds no /etc/sudoers.d/010_pi-nopasswd"
    [ "$(grep --extended-regexp --invert-match '^[[:space:]]*(#|$)' "${root}/etc/sudoers.d/010_pi-nopasswd")" \
        = "pi ALL=(ALL) NOPASSWD: ALL" ] \
        || fail "/etc/sudoers.d/010_pi-nopasswd in the image grants something other than: pi ALL=(ALL) NOPASSWD: ALL"
    [ ! -e "${root}/etc/sudoers.d/011_viindoo-iot-box" ] || fail "the image still holds /etc/sudoers.d/011_viindoo-iot-box"
    # The password core printed is useless once the box user's field is only "!", and nothing can unlock it again.
    # A failure never prints the field: it may be a hash, and the log is public.
    shadow_fields="$(awk -F: -v user="${BOX_USER}" '$1 == user {print "[" $2 "]"}' "${root}/etc/shadow")"
    [ "${shadow_fields}" = "[!]" ] \
        || fail "the password of ${BOX_USER} in /etc/shadow of the image is not locked"
    # The backups the shadow tools keep of the files above hold the field as it was before the lock.
    for backup in "${root}"/etc/shadow?* "${root}"/etc/gshadow?*; do
        [ ! -e "${backup}" ] || fail "the image holds ${backup#"${root}"}, a backup of the password files"
    done
    layer_in_image="$(git -c safe.directory='*' -C "${root}/home/${BOX_USER}/iot-hw" rev-parse HEAD)"
    core_in_image="$(git -c safe.directory='*' -C "${root}/home/${BOX_USER}/odoo" rev-parse HEAD)"
    [ "${layer_in_image}" = "${layer_commit}" ] || fail "the layer in the image is ${layer_in_image}, not ${layer_commit}"
    [ "${core_in_image}" = "${odoo_commit}" ] || fail "the core in the image is ${core_in_image}, not ${odoo_commit}"
    if [ "${mode}" = candidate ]; then
        [ "$(cat "${root}/home/${BOX_USER}/viin-box-channel")" = "${channel}" ] || fail "the image names another channel than ${channel}"
    else
        [ ! -e "${root}/home/${BOX_USER}/viin-box-channel" ] || fail "a supported image must not carry a channel file"
    fi
    {
        echo "image: viindoo-iot-box ${series} ${release} (${mode})"
        echo "layer commit: ${layer_in_image}"
        echo "core commit: ${core_in_image}"
        echo "base operating system: ${BASE_IMAGE_NAME} sha256 ${BASE_IMAGE_SHA256}"
        echo "built: $(date --utc +%Y-%m-%dT%H:%M:%SZ)"
        echo
        echo "[debian packages]"
        # shellcheck disable=SC2016  # dpkg-query expands the format, not the shell
        in_image dpkg-query --show --showformat='${Package}=${Version}\n' | sort
        echo
        echo "[python libraries]"
        in_image python3 -m pip list --format=freeze 2> /dev/null | sort
    } > "${work}/manifest.txt"
}

close_image() {
    local index
    rm -f "${root:?}/usr/bin/qemu-arm-static"
    for ((index = ${#mounted[@]} - 1; index >= 0; index--)); do
        umount "${mounted[index]}"
    done
    mounted=()
    zerofree -v "${root_device}" || true
    sleep 5
    kpartx -d "${mapped}"
    mapped=""
}

publish_files() {
    local file="${output}/${image_name}" size
    echo "Compressing..."
    xz --threads=0 -6 --stdout "${posbox_dir}/iotbox.img" > "${file}"
    size="$(stat --format %s "${file}")"
    if [ "${size}" -ge "${RELEASE_FILE_LIMIT}" ]; then
        rm -f "${file}"
        fail "the image file would be ${size} bytes, and a release page takes less than ${RELEASE_FILE_LIMIT}:" \
            "remove a package from the image (the unused web browser) instead of splitting the file"
    fi
    cp "${work}/manifest.txt" "${output}/manifest.txt"
    # SHA256SUMS lists only the image: the reader downloads the image and this file, and a listed file that
    # is not there makes the check of the others fail.
    (cd "${output}" && sha256sum "${image_name}" > SHA256SUMS)
    say "wrote ${file} (${size} bytes), SHA256SUMS and manifest.txt in ${output}"
}

# Reads the tag the way the release workflow does and refuses when that reading differs from what the workflow
# expected. It needs only bash, git and python3, so it runs before anything of the build is touched.
read_release() {
    local facts
    facts="$(bash "${here}/check_release.sh" tag "${tag}" --repository "${repository}" \
        ${series_ref:+--series-ref "${series_ref}"})" || exit 1
    series="$(sed -n 's/^series=//p' <<< "${facts}")"
    release="$(sed -n 's/^release=//p' <<< "${facts}")"
    mode="$(sed -n 's/^mode=//p' <<< "${facts}")"
    odoo_commit="$(sed -n 's/^odoo_commit=//p' <<< "${facts}")"
    layer_commit="$(sed -n 's/^layer_commit=//p' <<< "${facts}")"
    channel="$(sed -n 's/^channel=//p' <<< "${facts}")"
    if [ -n "${expect_mode}" ] && [ "${expect_mode}" != "${mode}" ]; then
        fail "${tag} reads as a ${mode} release here, and the workflow expected a ${expect_mode} one"
    fi
    if [ -n "${expect_commit}" ]; then
        [ "${layer_commit}" = "${expect_commit}" ] \
            || fail "${tag} names ${layer_commit}, not the commit ${expect_commit} that was checked"
        [ "$(git -C "${repository}" rev-parse HEAD)" = "${expect_commit}" ] \
            || fail "the repository the build runs from is not at the commit ${expect_commit} that was checked"
    fi
}

main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --tag) tag="${2:?--tag needs a value}"; shift 2 ;;
            --expect-mode) expect_mode="${2:?--expect-mode needs a value}"; shift 2 ;;
            --expect-commit) expect_commit="${2:?--expect-commit needs a value}"; shift 2 ;;
            --series-ref) series_ref="${2:?--series-ref needs a value}"; shift 2 ;;
            --repository) repository="${2:?--repository needs a value}"; shift 2 ;;
            --work) work="${2:?--work needs a value}"; shift 2 ;;
            --output) output="${2:?--output needs a value}"; shift 2 ;;
            *) fail "unknown option $1" ;;
        esac
    done
    [ -n "${tag}" ] || fail "--tag is required"
    case "${expect_mode}" in
        "" | candidate | supported) ;;
        *) fail "--expect-mode is candidate or supported" ;;
    esac
    [[ -z "${expect_commit}" || "${expect_commit}" =~ ^[0-9a-f]{40}$ ]] || fail "--expect-commit is a full commit id"
    read_release
    require_root
    for command_name in ${BOX_REQUIREMENTS}; do
        command -v "${command_name}" > /dev/null || fail "${command_name} is missing: install it first"
    done
    mkdir -p "${work}" "${output}"
    work="$(cd "${work}" && pwd)"
    output="$(cd "${output}" && pwd)"
    trap cleanup EXIT

    if [ "${mode}" = candidate ]; then
        image_name="viindoo-iot-box-${series}-${release}-CANDIDATE.img.xz"
    else
        image_name="viindoo-iot-box-${series}-${release}.img.xz"
    fi
    say "building ${image_name} (${mode}) with core ${odoo_commit} and layer ${layer_commit}"
    prepare_core
    place_build_only_apt_configuration
    place_base_image
    run_core_script
    install_layer_in_image
    verify_and_describe_image
    close_image
    publish_files
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
