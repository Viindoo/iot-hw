#!/usr/bin/env bash
# Rules of a Viindoo IoT Box release, in one place on the build side: the build, the installer and the checks
# all ask this file. A box applies the same rules to the pointer it fetches (viin_hw_drivers/update.py), so a
# change to a rule here is made there too.
#
#   check_release.sh pointer [FILE]
#       FILE (default: box-release.json beside the repository root of this file) is a valid release pointer.
#       A copy of this script kept outside the repository has no such file beside it: give FILE then.
#   check_release.sh tag TAG [--series-ref REF] [--repository DIR]
#       TAG is box-<series>-<release> and names a commit whose pointer says the same release. A release
#       that is exactly rc.<n>, or ends in -rc.<n>, is a candidate; a release that contains "rc." in any other
#       way is refused; every other one is supported. A supported release starts with a digit and its series is
#       NN.N (two digits, a dot, one digit), and it is refused unless its commit is contained in REF (the series
#       branch), so an unmerged branch never makes one.
#
# On success it prints one "key=value" line per fact (series, release, candidate, odoo_commit, min_image,
# and for a tag also mode, layer_commit, channel) and exits 0. On failure it says why on stderr and exits 1.
set -o errexit
set -o nounset
set -o pipefail

fail() {
    echo "check_release.sh: $*" >&2
    exit 1
}

# Reads the pointer from stdin, checks every rule, prints the facts. The pointer is data, never code.
# The shapes of release, min_image and the candidate mark are the ones a box applies to the same file
# (viin_hw_drivers/update.py), so a pointer this file accepts is one every box accepts. A supported release
# is held to two more rules, which only this file applies.
readonly CHECK_POINTER='
import json
import re
import sys

try:
    pointer = json.load(sys.stdin)
except ValueError as error:
    sys.exit(f"the release pointer is not valid JSON: {error}")
if not isinstance(pointer, dict):
    sys.exit("the release pointer is not a JSON object")
expected = {"release": str, "series": str, "odoo_commit": str, "min_image": str, "candidate": bool}
for key, kind in expected.items():
    if not isinstance(pointer.get(key), kind):
        sys.exit(f"the release pointer has no {kind.__name__} named {key}")
if not re.fullmatch(r"[0-9]+\.[0-9]+", pointer["series"]):
    sys.exit("series is not a series number")
if not re.fullmatch(r"[0-9a-f]{40}", pointer["odoo_commit"]):
    sys.exit("odoo_commit is not a full commit id")
if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]{0,39}", pointer["release"]):
    sys.exit("release is not 1 to 40 letters, digits, dots, hyphens or underscores, starting with a letter or digit")
if not re.fullmatch(r"[0-9]{1,4}\.[0-9]{1,4}", pointer["min_image"]):
    sys.exit("min_image is not an image version of the form N.N")
is_candidate = bool(re.fullmatch(r"(?:.*-)?rc\.[0-9]+", pointer["release"]))
if "rc." in pointer["release"] and not is_candidate:
    sys.exit("a release that contains rc. must be rc.<n> or end in -rc.<n>")
if pointer["candidate"] != is_candidate:
    sys.exit("candidate is true exactly when release is rc.<n> or ends in -rc.<n>")
if not pointer["candidate"]:
    # The deployment refs of the release environment add two rules a box does not apply.
    if not re.fullmatch(r"[0-9][0-9]\.[0-9]", pointer["series"]):
        sys.exit("the series of a supported release is two digits, a dot and one digit")
    if not re.match(r"[0-9]", pointer["release"]):
        sys.exit("a supported release starts with a digit")
for key in ("series", "release", "odoo_commit", "min_image"):
    print(f"{key}={pointer[key]}")
print("candidate=" + ("true" if pointer["candidate"] else "false"))
'

check_pointer() {
    python3 -I -c "${CHECK_POINTER}" || exit 1
}

check_tag() {
    local tag="$1" series_ref="" repository="" series release commit pointer facts mode candidate
    shift
    while [ $# -gt 0 ]; do
        case "$1" in
            --series-ref) series_ref="${2:?--series-ref needs a value}"; shift 2 ;;
            --repository) repository="${2:?--repository needs a value}"; shift 2 ;;
            *) fail "unknown option $1" ;;
        esac
    done
    [[ "${tag}" =~ ^box-([0-9]+\.[0-9]+)-([0-9A-Za-z][0-9A-Za-z._-]*)$ ]] \
        || fail "${tag} is not a tag of the form box-<series>-<release>"
    series="${BASH_REMATCH[1]}"
    release="${BASH_REMATCH[2]}"
    repository="${repository:-$(git rev-parse --show-toplevel)}"
    commit="$(git -C "${repository}" rev-parse --verify --quiet "refs/tags/${tag}^{commit}")" \
        || fail "the repository has no tag ${tag}"
    pointer="$(git -C "${repository}" show "${commit}:box-release.json")" \
        || fail "the commit of ${tag} holds no box-release.json"
    facts="$(printf '%s\n' "${pointer}" | check_pointer)" || exit 1
    [ "$(sed -n 's/^series=//p' <<< "${facts}")" = "${series}" ] || fail "the series of ${tag} is not the one of its pointer"
    [ "$(sed -n 's/^release=//p' <<< "${facts}")" = "${release}" ] || fail "the release of ${tag} is not the one of its pointer"
    candidate="$(sed -n 's/^candidate=//p' <<< "${facts}")"
    if [ "${candidate}" = true ]; then
        mode=candidate
    else
        mode=supported
        [ -n "${series_ref}" ] || fail "a supported release needs --series-ref, the branch that must hold its commit"
        git -C "${repository}" rev-parse --verify --quiet "${series_ref}^{commit}" > /dev/null \
            || fail "the repository has no ${series_ref}"
        git -C "${repository}" merge-base --is-ancestor "${commit}" "${series_ref}" \
            || fail "the commit of ${tag} is not on ${series_ref}: only a merged commit makes a supported image"
    fi
    printf '%s\n' "${facts}"
    echo "mode=${mode}"
    echo "layer_commit=${commit}"
    if [ "${mode}" = candidate ]; then
        echo "channel=refs/heads/box-candidate/${series}"
    fi
}

main() {
    case "${1:-}" in
        pointer)
            local file="${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/box-release.json}"
            [ -f "${file}" ] || fail "no such file: ${file} (a copy of this script outside the repository needs the pointer file as its argument)"
            check_pointer < "${file}"
            ;;
        tag)
            [ $# -ge 2 ] || fail "tag needs a tag name"
            shift
            check_tag "$@"
            ;;
        *) fail "usage: check_release.sh pointer [FILE] | tag TAG [--series-ref REF] [--repository DIR]" ;;
    esac
}

main "$@"
