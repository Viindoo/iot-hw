"""The Wi-Fi network the box joins, and the one it keeps to join again when it starts.

NetworkManager joins the network and holds it, its password included, as the
profile PROFILE, in a file only root reads. The layer runs `nmcli` through no
shell: the name reaches it as the value of a setting, the password on its
standard input, so that no log of the commands run as root and no list of
processes shows the password. The layer writes neither into a file of its own
nor into the configuration of the box: nothing in them is ever run or read as a
setting. Core's script for the same join writes both into that configuration
through `sed` and `echo` without escaping them, which loses or rewrites a
password with a `/`, a `&`, a backslash, a `=` or a line break, and runs a
command a value is made for; the layer does not use it.

/etc lives in memory on a box: a profile is lost when the box restarts, unless
it is copied to the card. Keeping a network is that copy; forgetting it removes
the copy. As the box starts, `viin-box-wifi` (a program of the box, see
box/system) brings the profile kept up before core's rc.local runs
ACCESS_POINT_SCRIPT, which starts the access point of the box only when the box
has no address; while the box runs, `viin-box-wifi watch` brings the box back
to the network it keeps. Neither changes the Wi-Fi while a join holds LOCK, and
a join is not started while either holds it.
"""
import fcntl
import json
import logging
import os
import re
import subprocess
import threading
import time
import unicodedata
from pathlib import Path

from odoo.tools.misc import file_path

from .seams import core

_logger = logging.getLogger(__name__)

# The profile of the network the box is on, or was last asked to join, and of the one it keeps; `viin-box-wifi`, which
# joins the network kept as the box starts and brings the box back to it, knows it by this name too.
PROFILE = 'viindoo-wifi'
# The profile of a network being joined, until it is: a join that fails leaves PROFILE as it was.
TRIAL_PROFILE = 'viindoo-wifi-trial'
INTERFACE = 'wlan0'
# The network interfaces of the box, one directory each.
INTERFACES = Path('/sys/class/net')
# Where NetworkManager writes a profile it holds itself, and where it reads those netplan renders for it.
PROFILES = Path('/etc/NetworkManager/system-connections')
NETPLAN = Path('/etc/netplan')
RENDERED_BY_NETPLAN = re.compile(r'/run/NetworkManager/system-connections/netplan-[^/]*\.nmconnection')
# /etc as it is on the card: the copies made there are the profile the box finds again when it starts, under one
# name each, so that keeping a network replaces the copy of the one kept before.
CARD = Path('/root_bypass_ramdisks')
KEPT_FILES = (
    CARD / PROFILES.relative_to('/') / f'{PROFILE}.nmconnection',
    CARD / NETPLAN.relative_to('/') / f'90-NM-{PROFILE}.yaml',
)
# Core's script that runs as the box starts, and again, given `force`, after a join that failed: it starts the access
# point of the box when the box has no address, and restarts the box service.
ACCESS_POINT_SCRIPT = 'point_of_sale/tools/posbox/configuration/wireless_ap.sh'
ACCESS_POINT = ('10.11.12.1/24', INTERFACE)
# What the layer relies on in that script, each as a line it holds, and what the box does wrong without it.
CORE_LINES = {
    ACCESS_POINT_SCRIPT: {
        'FORCE_HOST_AP="${1}"': 'a join that failed may no longer start the access point',
        'WIRED_IP=$(hostname -I)': 'the box may start its access point although it joined the network it keeps',
        'if [ -z "${WIRED_IP}" ] ; then': (
            'the box may start its access point although it joined the network it keeps'),
        # hostapd names the access point as it starts as a service (box/system, hostapd.service.d).
        'service hostapd restart': 'the access point of the box may announce the name core gives it',
    },
}
# Held by `viin-box-wifi`, which runs as root, while it changes the Wi-Fi of the box, and by the joins of the box page
# from the moment the first is asked until the last one ends: a join is not started while that program holds it, and
# that program leaves the Wi-Fi alone while a join holds it. Absent on a box where that program never ran.
LOCK = Path('/run/viin-box-wifi.lock')
# Core's keys of a network: core's script joins it as the box starts when both hold a value. The layer keeps none
# there, and empties them when it keeps a network, so that core never joins another one.
SECTION = 'iot.box'
CORE_KEYS = ('wifi_ssid', 'wifi_password')
# What nmcli tells of each access point the radio hears, the name last: `nmcli -t` escapes a `:` in a value.
SCAN_FIELDS = ('BSSID', 'SIGNAL', 'SECURITY', 'WPA-FLAGS', 'RSN-FLAGS', 'SSID')
# The settings of a profile that protect a network, and those that hold its password.
KEY_MGMT = '802-11-wireless-security.key-mgmt'
WEP_KEY_TYPE = '802-11-wireless-security.wep-key-type'
PSK = '802-11-wireless-security.psk'
# A box has no button to press for WPS: NetworkManager would otherwise start WPS while it asks for the password.
NO_WPS = ('802-11-wireless-security.wps-method', 'disabled')
WEP_KEY = '802-11-wireless-security.wep-key0'
HIDDEN = '802-11-wireless.hidden'
# Where nmcli reads the password of a join from: its standard input.
SECRETS_FILE = '/dev/stdin'
# The locale nmcli runs in. nmcli writes in the character set of its locale, and a character that set lacks as `?`;
# a box whose settings name a locale it has not installed falls back to ASCII. Every system has C.UTF-8, so a name
# reads the same whatever the box's locale settings are. sudo keeps LC_ALL when its value is safe.
NMCLI_LOCALE = 'C.UTF-8'
# The longest network name Wi-Fi has, in bytes.
MAX_NAME = 32
# The join waits this long, in seconds, so that the answer of the route reaches the browser before the box leaves
# the network the browser reached it on.
ANSWER_DELAY = 3
# Seconds. How long a join asked from the box page waits for `viin-box-wifi` to let go of LOCK before the page is told
# the box is busy, and how often it tries: the program holds LOCK for a moment at each of its looks even when it changes
# nothing, far shorter than a change of the Wi-Fi, which takes ten seconds at least.
HOLD_WAIT = 3
HOLD_RETRY = 0.1
# How long NetworkManager may take to join a network, in seconds.
JOIN_WAIT = 45
RESTART_DELAY = 1
# Seconds. A radio that has just served the access point may scan before it hears anything: the scan is asked again,
# this often, until it lists access points or this long has passed.
HEARING_WAIT = 15
HEARING_PERIOD = 1
# What a join comes to: the value `last_join` gives as 'result', and the reason a page tells. The box service restarts
# once a join is over, so the latest one is noted in a file of /run/odoo, which a restart keeps and a start of the box
# empties: {'ssid', 'result', 'at': seconds since the epoch}.
JOINED, PASSWORD_REFUSED, NOT_REACHED, BUSY = 'joined', 'password_refused', 'not_reached', 'busy'
RESULTS = (JOINED, PASSWORD_REFUSED, NOT_REACHED, BUSY)
LAST_JOIN = Path('/run/odoo/viin-wifi-join.json')
# What nmcli prints, in NMCLI_LOCALE, when the password was refused: NetworkManager's handshake with the access point
# failed and it found no other password to try.
PASSWORD_REFUSED_BY = re.compile(r'Secrets were required|4-way handshake|activation failed: \(7\)', re.IGNORECASE)
# One join runs at a time; of those asked meanwhile, the last one asked is the one that runs.
_joining = threading.Lock()
_asked = threading.Lock()
_last_asked = 0
# The descriptor of LOCK held for the joins asked and not over, while it is held; how many they are.
_holding = []
_pending = 0
# One writer of the card at a time: a keep, or a forget. A forget asked after a join keeps nothing of that join.
_card = threading.Lock()
_forgets = 0


def refusal(name, password):
    """Why the box joins no network named `name` with `password`: the reason a page shows; None when it may.

    A name has 1 to MAX_NAME bytes in UTF-8. Neither holds a control character
    - a line break, a tab, a NUL - which a person does not type, and which
    neither an argument of a command nor a line of its input carries whole. An
    open network has the empty password.
    """
    if not _plain(name) or not 0 < len(name.encode()) <= MAX_NAME:
        return 'invalid_name'
    if not _plain(password):
        return 'invalid_password'
    return None


def _plain(value):
    """Whether `value` is a text that UTF-8 can write, and that holds no control character."""
    if not isinstance(value, str):
        return False
    try:
        value.encode()
    except UnicodeEncodeError:
        return False
    return not any(unicodedata.category(character) == 'Cc' for character in value)


def join(name, password, keep):
    """Start to join the network `name` with `password` in a thread of its own; keep it when `keep` and it is joined.
    Whether the join starts: it does not while `viin-box-wifi` changes the Wi-Fi of the box, which it is
    given HOLD_WAIT seconds to finish; that is noted as BUSY.

    The caller checked both with `refusal`. The join holds LOCK from now until
    it ends: that program, which restarts the box service once it has brought
    the box back to the network it keeps, never does so under a join it would
    drop. A join asked while another one runs waits for it; of those that
    wait, only the last one asked runs. A forget asked after this join keeps
    it from being kept.
    """
    global _last_asked, _pending
    with _asked:
        if not _hold_wifi():
            _note_join(name, BUSY)
            return False
        _last_asked += 1
        _pending += 1
        number = _last_asked
        forgets = _forgets
    threading.Thread(target=_join_when_due, args=(number, name, password, forgets if keep else None),
                     name='viin-wifi-join', daemon=True).start()
    return True


def last_join():
    """{'ssid', 'result', 'at'} of the latest join asked since the box started, `result` one of RESULTS; None when
    there is none, or its record cannot be read."""
    try:
        record = json.loads(LAST_JOIN.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if (isinstance(record, dict) and isinstance(record.get('ssid'), str) and record.get('result') in RESULTS
            and isinstance(record.get('at'), (int, float))):
        return {key: record[key] for key in ('ssid', 'result', 'at')}
    return None


def _note_join(name, result):
    """Note in LAST_JOIN that the join of `name` came to `result`. Never raises: a join does not depend on its note."""
    staged = LAST_JOIN.with_name(f'.{LAST_JOIN.name}.new')
    try:
        with os.fdopen(os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w', encoding='utf-8') as record:
            json.dump({'ssid': name, 'result': result, 'at': time.time()}, record)
        os.replace(staged, LAST_JOIN)
    except OSError:
        _logger.warning('The box could not note the result of its join of a Wi-Fi network', exc_info=True)


def _join_when_due(number, name, password, keep):
    global _pending
    try:
        time.sleep(ANSWER_DELAY)
        with _joining:
            if number == _last_asked:
                _join(name, password, keep)
    finally:
        with _asked:
            _pending -= 1
            if not _pending:
                _let_go_of_wifi()


def _hold_wifi():
    """Hold LOCK for the joins asked, unless they hold it already; whether they do, once HOLD_WAIT is over. A box
    where LOCK is absent has nobody else to keep from its Wi-Fi."""
    if _holding:
        return True
    try:
        descriptor = os.open(LOCK, os.O_RDONLY)
    except OSError:
        return True
    deadline = time.monotonic() + HOLD_WAIT
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic() >= deadline:
                os.close(descriptor)
                return False
            time.sleep(HOLD_RETRY)
    _holding.append(descriptor)
    return True


def _let_go_of_wifi():
    while _holding:
        os.close(_holding.pop())


def _join(name, password, keep):
    """Join `name`, as core's script does: leave the access point, join, restart the box service once joined.

    `keep` is None, or the count of forgets when the join was asked. The
    network is kept only once it is joined, and only when no forget was asked
    since: a password typed wrong never takes the place of the network kept,
    and a network forgotten during its join is not kept. A join that fails
    brings the network of PROFILE up again; when that fails too, core's
    ACCESS_POINT_SCRIPT starts the access point unless the box has another
    address, and restarts the box service. Before the access point starts,
    NetworkManager is kept from joining PROFILE by itself, which would take the
    radio from the access point: `viin-box-wifi watch` looks for it from then
    on.
    """
    try:
        _join_alone(name, password, keep)
    except Exception:
        _logger.exception('The box could not join a Wi-Fi network')


def _join_alone(name, password, keep):
    if not (INTERFACES / INTERFACE).exists():
        _logger.warning('The box has no Wi-Fi interface: it joins no Wi-Fi network')
        _note_join(name, NOT_REACHED)
        return
    from_access_point = _on_access_point()
    if from_access_point:
        # As core's script leaves the access point before it joins.
        _sudo('service', 'hostapd', 'stop')
        _sudo('ip', 'address', 'del', ACCESS_POINT[0], 'dev', ACCESS_POINT[1])
        _nmcli('general', 'reload')
    _nmcli('connection', 'delete', 'id', TRIAL_PROFILE)
    result = _try(name, password, HEARING_WAIT if from_access_point else 0)
    _note_join(name, result)
    if result == JOINED:
        _nmcli('connection', 'delete', 'id', PROFILE)
        # Joined: NetworkManager may now join it again by itself whenever the box loses it.
        _nmcli('connection', 'modify', 'id', TRIAL_PROFILE, 'connection.id', PROFILE, 'connection.autoconnect', 'yes')
        _logger.info('The box joined the Wi-Fi network %s', name)
        if keep is not None:
            _keep_unless_forgotten(keep)
    else:
        _nmcli('connection', 'delete', 'id', TRIAL_PROFILE)
        # The reason is chosen among fixed words: nothing derived from the password reaches the log.
        why = 'the password was refused' if result == PASSWORD_REFUSED else 'the network did not answer'
        _logger.warning('The box could not join the Wi-Fi network %s: %s', name, why)
        # A box that kept no network has no profile to bring up again.
        held = _profile_held()
        if not (held and _nmcli('--wait', str(JOIN_WAIT), 'connection', 'up', 'id', PROFILE, 'ifname', INTERFACE)):
            if held and not _addresses():
                # In /etc alone, which lives in memory: the copy kept on the card is left as it is for the next start.
                _nmcli('connection', 'modify', 'id', PROFILE, 'connection.autoconnect', 'no')
            subprocess.Popen(['sudo', '-n', file_path(ACCESS_POINT_SCRIPT), 'force'], start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    core('tools.helpers').odoo_restart(RESTART_DELAY)


def _try(name, password, hearing_wait=0):
    """JOINED when the box joined the network `name` with `password`, as the profile TRIAL_PROFILE; else
    PASSWORD_REFUSED when NetworkManager had the password refused, else NOT_REACHED.

    The profile holds the name and how the network is protected, as
    NetworkManager would complete them from the access point heard with the
    strongest signal; a radio that hears nothing at all is asked again for up
    to `hearing_wait` seconds. A name no access point announces is a hidden
    network, which NetworkManager asks for by its name; its protection cannot be heard,
    so with a password it is joined as WPA2 Personal, which a network that
    also takes WPA3 accepts. The name is the value of a setting, which nmcli
    never reads as one of its options. The profile joins nothing by itself
    until it is joined once: NetworkManager would ask for a password nobody
    gives. nmcli reads the password from its standard input when
    NetworkManager asks for it, and NetworkManager then saves it in the
    profile.
    """
    heard = _heard(name, hearing_wait)
    if heard is None:
        bssid, settings = None, (HIDDEN, 'yes')
        protection = ((KEY_MGMT, 'wpa-psk', *NO_WPS), PSK) if password else ((), None)
    else:
        bssid, settings, protection = heard[0], (), _protection(*heard[1:])
        if protection is None:
            _logger.warning('The Wi-Fi network %s asks for more than a password: the box does not join it', name)
            return NOT_REACHED
    added = _nmcli('connection', 'add', 'type', 'wifi', 'con-name', TRIAL_PROFILE, 'connection.autoconnect', 'no',
                   '802-11-wireless.ssid', name, *settings, *protection[0])
    up = ['--wait', str(JOIN_WAIT), 'connection', 'up', 'id', TRIAL_PROFILE, 'ifname', INTERFACE]
    up += ['ap', bssid] if bssid else []
    if not added:
        return NOT_REACHED
    if not (password and protection[1]):
        return _attempt(*up)
    return _attempt(*up, 'passwd-file', SECRETS_FILE, given=_secret_line(protection[1], password))


def _attempt(*arguments, given=None):
    """JOINED when `nmcli` with `arguments`, `given` on its standard input, joined a network; else the reason it did
    not, from what nmcli printed. What it printed is read, never kept."""
    stdin = {'stdin': subprocess.DEVNULL} if given is None else {'input': given.encode()}
    done = subprocess.run(['sudo', '-n', 'nmcli', *arguments], **stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, env=_nmcli_environment(), check=False)
    if not done.returncode:
        return JOINED
    return PASSWORD_REFUSED if PASSWORD_REFUSED_BY.search(done.stdout.decode(errors='replace')) else NOT_REACHED


def _protection(security, wpa_flags, rsn_flags):
    """(settings, the setting of its password) of a network announced so; None when it asks for more than a password.

    The choice is NetworkManager's own, for a network joined by `nmcli device
    wifi connect`: a network that takes both WPA2 and WPA3 is joined by WPA2.
    """
    if 'WEP' in security.split():
        return (KEY_MGMT, 'none', WEP_KEY_TYPE, 'key', *NO_WPS), WEP_KEY
    flags = set(wpa_flags.split()) | set(rsn_flags.split())
    if 'psk' in flags:
        return (KEY_MGMT, 'wpa-psk', *NO_WPS), PSK
    if 'sae' in flags:
        return (KEY_MGMT, 'sae', *NO_WPS), PSK
    if 'owe' in flags:
        return (KEY_MGMT, 'owe'), None
    if flags & {'802.1X', 'wpa-eap-suite-b-192'}:
        return None
    return (), None


def _secret_line(setting, secret):
    """The line of nmcli's passwd-file that gives `secret` to `setting`.

    nmcli reads a backslash there as the start of an escape, and drops the
    spaces at either end of the value: a backslash is written twice and a space
    as the escape of its octal code, so that every password reaches
    NetworkManager as it was typed.
    """
    escaped = secret.replace('\\', '\\\\').replace(' ', '\\040')
    return f'{setting}:{escaped}\n'


def _keep_unless_forgotten(forgets):
    """Keep PROFILE unless a forget was asked since the count of forgets was `forgets`."""
    with _card:
        if forgets != _forgets:
            _logger.info('The box keeps none of the Wi-Fi network it joined: the network kept was forgotten since')
            return
        _keep()


def _keep():
    """Copy the file of PROFILE to the card in place of the copy of the network kept before, then empty core's keys.

    The copy is written beside its place under a name neither NetworkManager
    nor netplan reads, then renamed over it in one step. A keep that fails, or
    is cut off, leaves the network kept before as it was: its copy, and core's
    keys, which are emptied only once the new copy is in place.
    """
    helpers = core('tools.helpers')
    source = _profile_source()
    if source is None:
        _logger.warning('The box cannot keep the Wi-Fi network it joined: the file of its profile was not found')
        return
    target, other = KEPT_FILES if source.parent == PROFILES else KEPT_FILES[::-1]
    with helpers.writable():
        kept = (_sudo('install', '-D', '-m', '600', '-o', 'root', '-g', 'root', str(source), str(_staged(target)))
                and _sudo('mv', '-f', '--', str(_staged(target)), str(target)))
        _sudo('rm', '-f', '--', str(other if kept else _staged(target)))
    if not kept:
        _logger.warning('The box could not keep the Wi-Fi network it joined: it keeps the one it kept before')
        return
    configuration = helpers.get_conf()
    if any(configuration.get(SECTION, key, raw=True, fallback='') for key in CORE_KEYS):
        helpers.update_conf(dict.fromkeys(CORE_KEYS, ''))


def _staged(path):
    """Where the copy for `path` is written first: a name that starts with a dot, which neither reader takes."""
    return path.with_name(f'.{path.name}.new')


def _terse_fields(line):
    """The fields of a line of `nmcli -t`, which writes `:` between them and a backslash before `:` and `\\` in them."""
    fields, field, escaped = [], [], False
    for character in line:
        if escaped:
            field.append(character)
            escaped = False
        elif character == '\\':
            escaped = True
        elif character == ':':
            fields.append(''.join(field))
            field = []
        else:
            field.append(character)
    return [*fields, ''.join(field)]


def _heard(name, wait=0):
    """(BSSID, SECURITY, WPA-FLAGS, RSN-FLAGS) of the access point with the strongest signal announcing exactly
    `name`; None when none does.

    The join goes through that access point, as `nmcli connection up ... ap`
    asks: the profile itself stays bound to none, so that the box joins any
    access point of the network again. A scan that lists no access point at
    all is no proof that `name` is hidden: it is asked again for up to `wait`
    seconds.
    """
    deadline = time.monotonic() + wait
    while True:
        heard_any = False
        for rescan in ('yes', 'auto'):
            listed = subprocess.run(['sudo', '-n', 'nmcli', '-t', '-f', ','.join(SCAN_FIELDS), 'device', 'wifi',
                                     'list', 'ifname', INTERFACE, '--rescan', rescan],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    env=_nmcli_environment(), check=False)
            found = [_terse_fields(line) for line in listed.stdout.decode(errors='replace').splitlines()]
            found = [fields for fields in found if len(fields) == len(SCAN_FIELDS) and fields[1].isdigit()]
            heard_any = heard_any or bool(found)
            found = [(int(fields[1]), fields) for fields in found if fields[-1] == name]
            if found:
                bssid, _signal, *protection, _name = max(found)[1]
                return bssid, *protection
        if heard_any or time.monotonic() >= deadline:
            return None
        time.sleep(HEARING_PERIOD)


def _profile_source():
    """The file in /etc that holds PROFILE, its own or the netplan one it is rendered from; None when not found."""
    listed = subprocess.run(['sudo', '-n', 'nmcli', '-t', '-f', 'NAME,UUID,FILENAME', 'connection', 'show'],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_nmcli_environment(), check=False)
    for line in listed.stdout.decode(errors='replace').splitlines():
        fields = _terse_fields(line)
        if len(fields) != 3 or fields[0] != PROFILE:
            continue
        found = Path(fields[2])
        if found.parent == PROFILES:
            return found
        if RENDERED_BY_NETPLAN.fullmatch(fields[2]):
            return NETPLAN / f'90-NM-{fields[1]}.yaml'
    return None


def forget():
    """Forget the network the box keeps: it is not joined again once the box restarts; the box stays on it until then.

    A join asked before keeps nothing, even when it ends later. Core's own
    route empties core's keys.
    """
    global _forgets
    helpers = core('tools.helpers')
    with _card:
        with _asked:
            _forgets += 1
        with helpers.writable():
            if not _remove_kept_files():
                raise OSError('the Wi-Fi network the box keeps could not be removed from the card')


def _remove_kept_files():
    """Remove the copies of a kept profile from the card, the caller having made it writable; whether it could."""
    return _sudo('rm', '-f', '--', *(str(path) for kept in KEPT_FILES for path in (kept, _staged(kept))))


def core_differences():
    """What the layer relies on in core's scripts that they no longer hold, each as a line of text."""
    found = []
    for script, lines in CORE_LINES.items():
        try:
            held = {line.strip() for line in Path(file_path(script)).read_text(encoding='utf-8').splitlines()}
        except (OSError, ValueError):
            found.append(f'core no longer has {script}: the box may start its access point at every start')
            continue
        found += [f'{script} no longer holds `{line}`: {loss}' for line, loss in lines.items() if line not in held]
    return found


def _addresses():
    """The addresses of the box, as core's ACCESS_POINT_SCRIPT reads them to decide on the access point."""
    shown = subprocess.run(['hostname', '-I'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, check=False)
    return shown.stdout.decode(errors='replace').split()


def _profile_held():
    """Whether NetworkManager holds PROFILE."""
    listed = subprocess.run(['sudo', '-n', 'nmcli', '-t', '-g', 'NAME', 'connection', 'show'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env=_nmcli_environment(), check=False)
    return PROFILE in listed.stdout.decode(errors='replace').splitlines()


def _on_access_point():
    """Whether the access point of the box holds its address."""
    shown = subprocess.run(['ip', '-o', 'address', 'show', 'dev', ACCESS_POINT[1]],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    return f' {ACCESS_POINT[0]} ' in shown.stdout.decode(errors='replace')


def _nmcli(*arguments, given=None):
    """Whether `nmcli` with `arguments`, `given` on its standard input, succeeded. Its failure is not raised."""
    return _sudo('nmcli', *arguments, given=given)


def _sudo(*command, given=None):
    """Whether `command`, run as root with `given` on its standard input, succeeded."""
    stdin = {'stdin': subprocess.DEVNULL} if given is None else {'input': given.encode()}
    return not subprocess.run(['sudo', '-n', *command], **stdin, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env=_nmcli_environment(), check=False).returncode


def _nmcli_environment():
    return dict(os.environ, LC_ALL=NMCLI_LOCALE)
