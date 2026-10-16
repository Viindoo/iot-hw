"""What the test files of the Wi-Fi of the box share: a NetworkManager that stands in for the one of a box.

The software box has no radio and no NetworkManager. `network_manager` puts, first on the path
of the box process, programs that answer as those of a box do for the Wi-Fi: `nmcli`, `ip`,
`hostname`, `rfkill`, and `sudo` for each of these and for the access point script of core.
`sudo` runs every other command for real, so that what the box copies to its card lands there,
owned by root. The stand-in lists each call it answers, keeps the profiles it made as files,
and joins a network only with the very name and password a network around it has.

It answers as nmcli 1.42 and NetworkManager 1.42 do. nmcli lists the access points of a scan,
and the value it gives with `-g`, in its terse form: a backslash before each colon and each backslash. It
takes the value of a setting given to `connection add` as it is, whatever it begins with, and
an argument of `connection up` that begins like one of its own options (`--ask`, `-s`) for that
option. It reads a password from the file given to `connection up` as `passwd-file`, one
`<setting>:<value>` line each, the value with its backslash escapes undone and the spaces at
either end that no backslash escapes dropped; NetworkManager then saves it in the profile. A
join asked by `connection up` reaches a network no access point announces too: NetworkManager
asks for it by its name. NetworkManager joins a profile by itself (`join_by_itself`) only when
an access point announces its network, or when the profile says the network is hidden; it
stops doing so once a handshake failed, until a `connection up` asks for the profile again.

For `viin-box-wifi`, which runs as root, it answers `iw` as iw 5.19 does: who is connected to
the access point (`station dump`), and the access points a scan hears, each name as iw spells
it; a scan that asks for a name hears a hidden network of that name too. It answers core's
access point script as that script does: the access point starts when the box has no address.
`WatchOfTheBox` drives `viin-box-wifi watch` look by look, at the times a test gives, with the
log in which the web server of a box notes each request it answers.

nmcli writes what it prints in the character set of its locale, each character that set lacks
as `?`: it sets its locale from its environment as the C library does, and a locale the system
does not have leaves it in ASCII. The stand-in asks the C library of the box which locales it
has, and `with_settings_naming_a_locale_the_box_lacks` gives the box such settings.

`settled` waits, at the end of its block, for every thread and every program the box started
during the block: a join runs after the route has answered.
"""
import contextlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from unittest import mock

import common
import services_common as box

# The address of the access point of the box on its radio, as core's access point script gives it.
ACCESS_POINT_ADDRESS = '10.11.12.1/24'
RADIO = 'wlan0'
# The programs the stand-in answers for, and those it runs for real when it does not.
PROGRAMS = ('sudo', 'nmcli', 'ip', 'hostname', 'rfkill', 'iw', 'service', 'wireless_ap.sh')
REAL_PROGRAMS = ('sudo', 'ip', 'hostname', 'service')
# The program of the box that joins the network it keeps as it starts and, given `watch`, brings the box back to it.
WIFI_PROGRAM = box.SYSTEM_FILES / 'usr' / 'local' / 'sbin' / 'viin-box-wifi'
# Seconds. How long a join from the access point asks again, and how often, a radio that hears nothing.
HEARING_WAIT, HEARING_PERIOD = 1, 0.2
# The name NetworkManager gives a profile netplan renders for it, from the netplan file of that profile.
NETPLAN_RENDERED = '/run/NetworkManager/system-connections/netplan-NM-{uuid}.nmconnection'

STAND_IN = r'''
import fcntl
import io
import json
import locale
import os
import re
import sys
import time
import uuid
from pathlib import Path

STATE = Path(os.environ['BOX_SUITE_NETWORK'])
SETUP = json.loads((STATE / 'setup.json').read_text())
PROFILES = Path(SETUP['profiles'])
NETPLAN = Path(SETUP['netplan'])
NOT_FOUND = 10
NOT_JOINED = 4
INVALID_ARGUMENTS = 2
NOT_RUNNING = 8
# The options nmcli takes wherever it reads an argument through its own parser.
OPTIONS = ('ask', 'show-secrets')
# What a profile holds of each setting `connection add` and `connection modify` take, by every name nmcli knows.
SETTINGS = {
    'type': 'type', 'con-name': 'id', 'connection.id': 'id', 'ifname': 'interface',
    'connection.interface-name': 'interface', 'connection.autoconnect': 'autoconnect', 'ssid': 'ssid',
    '802-11-wireless.ssid': 'ssid', 'wifi.ssid': 'ssid', '802-11-wireless.hidden': 'hidden', 'wifi.hidden': 'hidden',
    '802-11-wireless-security.key-mgmt': 'key_mgmt', 'wifi-sec.key-mgmt': 'key_mgmt',
    '802-11-wireless-security.wep-key-type': 'wep_key_type', '802-11-wireless-security.wps-method': 'wps_method',
    '802-11-wireless-security.psk': 'psk', 'wifi-sec.psk': 'psk', '802-11-wireless-security.wep-key0': 'psk',
}
YES_OR_NO = ('autoconnect', 'hidden')
# The setting that holds the password of a network, by how the profile protects it; none for an open one.
SECRET = {'wpa-psk': '802-11-wireless-security.psk', 'sae': '802-11-wireless-security.psk',
          'none': '802-11-wireless-security.wep-key0'}


def world():
    return json.loads((STATE / 'world.json').read_text())


def change(**values):
    found = world()
    found.update(values)
    (STATE / 'world.json').write_text(json.dumps(found))


def note(command):
    with (STATE / 'calls').open('a', encoding='utf-8') as calls:
        calls.write(json.dumps([time.time(), command]) + '\n')


def record_join(**fields):
    with (STATE / 'joins').open('a', encoding='utf-8') as joins:
        joins.write(json.dumps(fields) + '\n')


def real(program, arguments):
    found = SETUP['real'][program]
    os.execv(found, [found, *arguments])


def profiles():
    found = []
    for folder in (PROFILES, NETPLAN):
        for path in sorted(folder.glob('*')):
            try:
                profile = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            found.append((path, profile))
    return found


def named(name):
    return [(path, profile) for path, profile in profiles() if profile['id'] == name]


def save(path, profile):
    path.write_text(json.dumps(profile), encoding='utf-8')


def new_path(name, identifier):
    if world()['netplan']:
        return NETPLAN / f'90-NM-{identifier}.yaml'
    path = PROFILES / f'{name}.nmconnection'
    return PROFILES / f'{name}-{identifier}.nmconnection' if path.exists() else path


def filename(path, profile):
    if path.parent == NETPLAN:
        return SETUP['netplan_rendered'].format(uuid=profile['uuid'])
    return str(path)


def access_points():
    found = world()
    if found['access_points'] is not None:
        return found['access_points']
    return [{'bssid': f'02:00:5E:00:53:{number:02X}', 'signal': 70, 'ssid': ssid}
            for number, ssid in enumerate(found['networks'], start=1)]


def announced(ssid):
    return any(found['ssid'] == ssid for found in access_points())


def scan_fields(found):
    protected = world()['networks'].get(found['ssid']) is not None
    return {
        'BSSID': found['bssid'], 'SIGNAL': str(found['signal']), 'SSID': found['ssid'],
        'SECURITY': found.get('security', 'WPA2' if protected else ''),
        'WPA-FLAGS': found.get('wpa_flags', '(none)'),
        'RSN-FLAGS': found.get('rsn_flags', 'pair_ccmp group_ccmp psk' if protected else '(none)'),
    }


def read_as_option(word):
    if not word.startswith('-'):
        return False
    rest = word[2:] if word[1:2] == '-' else word[1:]
    return bool(rest) and any(option.startswith(rest) for option in OPTIONS)


def escaped(value):
    return value.replace('\\', '\\\\').replace(':', '\\:')


def unescaped(value):
    """A value of a passwd-file as nmcli reads it (nm_utils_buf_utf8safe_unescape, spaces stripped)."""
    value = value.lstrip(' \t\n\v\f\r')
    found, rest = [], value
    while '\\' in rest:
        before, rest = rest.split('\\', 1)
        found.append(before)
        if not rest:
            break
        if rest[0].isdigit():
            digits = rest[0]
            for character in rest[1:3]:
                if character not in '01234567':
                    break
                digits += character
            found.append(chr(int(digits[0]) * 8 ** (len(digits) - 1) + (int(digits[1:], 8) if digits[1:] else 0)
                             & 0xFF))
            rest = rest[len(digits):]
        else:
            found.append({'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t', 'v': '\v'}.get(rest[0], rest[0]))
            rest = rest[1:]
    found.append(rest.rstrip(' \t\n\v\f\r'))
    return ''.join(found)


def passwords(text):
    """{setting: password} of a passwd-file, as nmcli reads it; None when nmcli refuses it."""
    found = {}
    for line in re.split(r'\r\n|\n|\r', text):
        line = line.lstrip(' \t\n\v\f\r')
        if not line or line.startswith('#'):
            continue
        separator = re.search('[:=]', line)
        if not separator:
            return None
        setting, value = line[:separator.start()].rstrip(' \t\n\v\f\r'), line[separator.end():]
        if '.' not in setting:
            return None
        group, name = setting.split('.', 1)
        group = '802-11-wireless-security' if group in ('wifi-sec', 'wifi') else group
        found[f'{group}.{name}'] = unescaped(value)
    return found


def protections(ssid):
    """The protections the access points announcing `ssid` take; both of a password when none announces it."""
    flags = set()
    for found in access_points():
        if found['ssid'] == ssid:
            listed = scan_fields(found)
            flags |= set(listed['WPA-FLAGS'].split()) | set(listed['RSN-FLAGS'].split())
    if not any(found['ssid'] == ssid for found in access_points()):
        return {'wpa-psk', 'sae'}
    return {protection for flag, protection in (('psk', 'wpa-psk'), ('sae', 'sae')) if flag in flags}


def joinable(profile):
    """Whether the network of `profile` lets the box in: in reach, the radio free, the protection it announces, the
    password it has."""
    networks = world()['networks']
    if world()['access_point_running'] or profile['ssid'] not in networks:
        return False
    if profile.get('key_mgmt') in ('wpa-psk', 'sae') and profile['key_mgmt'] not in protections(profile['ssid']):
        return False
    if networks[profile['ssid']] is None:
        return profile.get('key_mgmt') in (None, 'owe')
    return profile.get('key_mgmt') in SECRET and profile.get('psk') == networks[profile['ssid']]


def settings_of(pairs):
    found = {}
    for key, value in zip(pairs[::2], pairs[1::2]):
        if key not in SETTINGS:
            return None
        found[SETTINGS[key]] = value.lower() in ('yes', 'true', '1', 'on') if SETTINGS[key] in YES_OR_NO else value
    return found


def connection_add(words):
    settings = settings_of(words)
    if settings is None or len(words) % 2 or settings.get('type') not in ('wifi', '802-11-wireless') \
            or not settings.get('id') or not settings.get('ssid'):
        return INVALID_ARGUMENTS
    identifier = str(uuid.uuid4())
    profile = {'id': settings['id'], 'uuid': identifier, 'autoconnect': True}
    profile.update({key: value for key, value in settings.items() if key not in ('type', 'interface', 'id')})
    save(new_path(profile['id'], identifier), profile)
    record_join(uuid=identifier, ssid=profile['ssid'])
    return 0


def connection_up(words, asked_by_the_page):
    if any(read_as_option(word) for word in words) or len(words) % 2:
        return INVALID_ARGUMENTS
    asked = dict(zip(words[::2], words[1::2]))
    if set(asked) - {'id', 'ifname', 'ap', 'passwd-file'} or 'id' not in asked:
        return INVALID_ARGUMENTS
    found = named(asked['id'])
    if not found:
        print(f"Error: unknown connection '{asked['id']}'.", file=sys.stderr)
        return NOT_FOUND
    path, profile = found[0]
    given = {}
    if 'passwd-file' in asked:
        source = Path(asked['passwd-file'])
        given = passwords(sys.stdin.read() if str(source) == '/dev/stdin' else source.read_text())
        if given is None:
            return INVALID_ARGUMENTS
    bssid = asked.get('ap')
    if bssid is not None:
        through = [found for found in access_points() if found['bssid'].upper() == bssid.upper()]
        if not through:
            return NOT_FOUND
        if through[0]['ssid'] != profile['ssid']:
            return NOT_JOINED
        bssid = through[0]['bssid']
    # Asked by a person: whatever NetworkManager blocked for the profile no longer holds.
    profile.pop('blocked', None)
    setting = SECRET.get(profile.get('key_mgmt'))
    if setting and 'psk' not in profile and setting in given:
        profile['psk'] = given[setting]
    save(path, profile)
    record_join(uuid=profile['uuid'], secret=profile.get('psk'))
    if world()['hold'] == 'every join' or world()['hold'] and asked_by_the_page:
        deadline = time.monotonic() + 60
        while not (STATE / 'released').exists() and time.monotonic() < deadline:
            time.sleep(0.02)
    if not joinable(profile):
        return NOT_JOINED
    change(active=profile['id'], active_ssid=profile['ssid'], active_bssid=bssid)
    return 0


def join_by_itself():
    """What NetworkManager does by itself for a box on no network: join the first profile it may join."""
    if world()['active'] is not None or world()['access_point_running']:
        return 0
    for path, profile in profiles():
        if not profile.get('autoconnect') or profile.get('blocked'):
            continue
        if not (profile.get('hidden') or announced(profile['ssid'])) or profile['ssid'] not in world()['networks']:
            continue
        if joinable(profile):
            change(active=profile['id'], active_ssid=profile['ssid'], active_bssid=None)
            return 0
        # The handshake failed: NetworkManager asks for a password nobody gives, and blocks the profile.
        profile['blocked'] = True
        save(path, profile)
    return 0


def in_the_character_set_of_the_locale():
    """Write as nmcli does: it calls setlocale(LC_ALL, "") and prints through GLib, which writes `?` for each
    character the character set of the locale lacks. The C library falls back to ASCII when the environment names a
    locale the system does not have."""
    try:
        locale.setlocale(locale.LC_ALL, '')
        utf_8 = locale.nl_langinfo(locale.CODESET) == 'UTF-8'
    except locale.Error:
        utf_8 = False
    if not utf_8:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='ascii', errors='replace', line_buffering=True)


def nmcli(arguments, asked_by_the_page=False):
    in_the_character_set_of_the_locale()
    options, words, rest = {}, [], list(arguments)
    while rest:
        word = rest.pop(0)
        if word in ('-t', '--terse'):
            options['terse'] = True
        elif word in ('-f', '-g', '--wait') and not words:
            options[word] = rest.pop(0)
        else:
            words.append(word)
    if words == ['general', 'reload'] or words == ['radio', 'wifi', 'on']:
        return 0
    if words == ['connection', 'show']:
        if options.get('-g') == 'NAME':
            print('\n'.join(escaped(profile['id']) for _path, profile in profiles()))
        else:
            for path, profile in profiles():
                place = filename(path, profile) if world()['files_listed'] else ''
                print(':'.join(escaped(field) for field in (profile['id'], profile['uuid'], place)))
        return 0
    if words[:3] == ['connection', 'show', 'id'] and options.get('-g') == '802-11-wireless.ssid':
        found = named(words[3])
        if not found:
            print(f'Error: {words[3]} - no such connection profile.', file=sys.stderr)
            return NOT_FOUND
        print(escaped(found[0][1]['ssid']))
        return 0
    if words[:2] == ['connection', 'add']:
        return connection_add(words[2:])
    if words[:2] == ['connection', 'up']:
        return connection_up(words[2:], asked_by_the_page)
    if words[:3] == ['connection', 'delete', 'id']:
        if read_as_option(words[3]):
            return INVALID_ARGUMENTS
        found = named(words[3])
        for path, _profile in found:
            path.unlink()
        return 0 if found else NOT_FOUND
    if words[:3] == ['connection', 'modify', 'id']:
        found = named(words[3])
        settings = settings_of(words[4:])
        if settings is None:
            return INVALID_ARGUMENTS
        for path, profile in found:
            profile.update(settings)
            save(path, profile)
        return 0 if found else NOT_FOUND
    if words[:3] == ['device', 'wifi', 'list']:
        if world()['empty_scans_left']:
            change(empty_scans_left=world()['empty_scans_left'] - 1)
            return 0
        fields = options.get('-f', 'BSSID,SIGNAL,SECURITY,WPA-FLAGS,RSN-FLAGS,SSID').split(',')
        for found in access_points():
            listed = scan_fields(found)
            print(':'.join(escaped(listed[field]) for field in fields))
        return 0
    if words[:2] == ['device', 'show'] and options.get('-g') == 'GENERAL.STATE':
        print('100 (connected)' if world()['active'] is not None else '30 (disconnected)')
        return 0
    if words[:2] == ['device', 'show'] and options.get('-g') == 'GENERAL.CONNECTION':
        print(escaped(world()['active'] or ''))
        return 0
    return INVALID_ARGUMENTS


def iw_spelling(name):
    data = name.encode()
    return ''.join(chr(byte) if 0x20 < byte < 0x7f and byte != 0x5c or byte == 0x20 and 0 < place < len(data) - 1
                   else f'\\x{byte:02x}' for place, byte in enumerate(data))


def iw(arguments):
    if arguments == ['dev', SETUP['radio'], 'station', 'dump']:
        stations = world()['stations']
        if stations is None:
            print('command failed: Operation not supported (-95)', file=sys.stderr)
            return 161
        for station in stations:
            print(f"Station {station} (on {SETUP['radio']})\n\tinactive time:\t120 ms")
        return 0
    if arguments[:3] == ['dev', SETUP['radio'], 'scan']:
        rest = arguments[3:]
        if world()['access_point_running'] and 'ap-force' not in rest:
            print('command failed: Operation not supported (-95)', file=sys.stderr)
            return 161
        if world()['connects_during_scan']:
            change(stations=[*(world()['stations'] or []), world()['connects_during_scan']], connects_during_scan=None)
        if world()['access_point_running'] and world()['deaf_on_access_point']:
            return 0
        asked = rest[rest.index('ssid') + 1:] if 'ssid' in rest else []
        heard = [found['ssid'] for found in access_points()]
        heard += [name for name in asked if name in world()['networks'] and name not in heard]
        for number, name in enumerate(heard, start=1):
            print(f"BSS 02:00:5e:00:54:{number:02x}(on {SETUP['radio']})\n\tfreq: 2437\n\tSSID: {iw_spelling(name)}")
        return 0
    return 1


def access_point_stopped():
    """hostapd stops: the radio may scan again, and it hears nothing in the first scans the world says."""
    change(access_point_running=False, empty_scans_left=world()['empty_scans_after_access_point'])


def record_restart(what):
    """Note `what` restarted, and whether the watch held its lock meanwhile: the restart takes a moment."""
    lock = world().get('lock')
    if not lock or not os.path.exists(lock):
        return
    time.sleep(0.3)
    held = False
    descriptor = os.open(lock, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        held = True
    finally:
        os.close(descriptor)
    with (STATE / 'restarts').open('a', encoding='utf-8') as restarts:
        restarts.write(json.dumps([what, held]) + '\n')


def access_point_script():
    """Core's script: the access point starts when the box has no address; it restarts the box service either way."""
    record_restart('access point script')
    if not world()['addresses'].split():
        # The profiles NetworkManager may join by itself, and so take the radio from the access point.
        taking = [profile['id'] for _path, profile in profiles() if profile.get('autoconnect')
                  and not profile.get('blocked')]
        change(on_access_point=True, access_point_running=True, dnsmasq_running=True,
               radio_taken_from_the_access_point=[*world()['radio_taken_from_the_access_point'], taking])
    return 0


def as_root(arguments):
    note(['sudo', *arguments])
    arguments = arguments[1:] if arguments[:1] == ['-n'] else arguments
    program = Path(arguments[0]).name if arguments else ''
    if program == 'nmcli':
        return nmcli(arguments[1:], asked_by_the_page=True)
    if program == 'service':
        if arguments[1:] == ['hostapd', 'stop']:
            access_point_stopped()
        return 0
    if program == 'ip' and 'del' in arguments:
        if SETUP['access_point'] in arguments:
            change(on_access_point=False)
        return 0
    if program == SETUP['access_point_script']:
        return access_point_script()
    if program in world()['failing']:
        return 1
    real('sudo', sys.argv[1:])


def main():
    program, arguments = Path(sys.argv[0]).name, sys.argv[1:]
    if program == 'network-stand-in' and arguments == ['join-by-itself']:
        return join_by_itself()
    if program == 'sudo':
        return as_root(arguments)
    note([program, *arguments])
    if program in world()['broken']:
        print('Error: NetworkManager is not running.', file=sys.stderr)
        return NOT_RUNNING
    if program == 'nmcli':
        return nmcli(arguments)
    if program == 'ip' and arguments == ['-o', 'address', 'show', 'dev', SETUP['radio']]:
        if world()['on_access_point']:
            print(f"3: {SETUP['radio']}    inet {SETUP['access_point']} scope global {SETUP['radio']}")
        return 0
    if program == 'hostname' and arguments == ['-I']:
        print(world()['addresses'])
        return 0
    if program == 'rfkill':
        return 0
    if program == 'ip' and arguments == ['address', 'del', SETUP['access_point'], 'dev', SETUP['radio']]:
        change(on_access_point=False)
        return 0
    if program == 'service' and arguments == ['hostapd', 'stop']:
        access_point_stopped()
        return 0
    if program == 'service' and arguments == ['odoo', 'restart']:
        record_restart('box service')
        return 0
    if program == 'service' and arguments == ['dnsmasq', 'stop']:
        change(dnsmasq_running=False)
        return 0
    if program == 'iw':
        return iw(arguments)
    if program == SETUP['access_point_script']:
        return access_point_script()
    real(program, arguments)


sys.exit(main())
'''


class NetworkManager:
    """The stand-in: the networks around the box, the profiles it holds, what it was asked."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.state = self.directory / 'state'
        self.programs = self.directory / 'bin'
        self.profiles = self.directory / 'etc' / 'NetworkManager' / 'system-connections'
        self.netplan = self.directory / 'etc' / 'netplan'
        for folder in (self.state, self.programs, self.profiles, self.netplan):
            folder.mkdir(parents=True)
        (self.state / 'setup.json').write_text(json.dumps({
            'profiles': str(self.profiles), 'netplan': str(self.netplan), 'radio': RADIO,
            'access_point': ACCESS_POINT_ADDRESS, 'access_point_script': 'wireless_ap.sh',
            'netplan_rendered': NETPLAN_RENDERED,
            'real': {program: shutil.which(program) for program in REAL_PROGRAMS},
        }))
        self.change(networks={}, access_points=None, addresses='', on_access_point=False, access_point_running=False,
                    active=None, active_ssid=None, active_bssid=None, hold=False, netplan=False, failing=[],
                    broken=[], files_listed=True, stations=[], radio_taken_from_the_access_point=[],
                    empty_scans_after_access_point=0, empty_scans_left=0, connects_during_scan=None,
                    deaf_on_access_point=False, dnsmasq_running=False)
        for record in ('calls', 'joins', 'restarts'):
            (self.state / record).write_text('')
        self.stand_in = stand_in = self.programs / 'network-stand-in'
        # Python sets C.UTF-8 for itself where the environment names no locale it has; nmcli is no Python program.
        stand_in.write_text(f'#!/usr/bin/env -S PYTHONCOERCECLOCALE=0 {sys.executable}\n{STAND_IN}')
        stand_in.chmod(0o755)
        for program in PROGRAMS:
            (self.programs / program).symlink_to(stand_in.name)

    def environment(self, **values):
        """The environment of a program that runs with the stand-in in place of the programs of the box."""
        return dict(os.environ, **values, PATH=f'{self.programs}:{os.environ["PATH"]}',
                    BOX_SUITE_NETWORK=str(self.state))

    def world(self):
        return json.loads((self.state / 'world.json').read_text())

    def change(self, **values):
        """Change the world around the box: `networks` ({name: password, None for an open one}), `access_points` (the
        scan: [{'bssid', 'signal', 'ssid'}], one per network when left None; a network none announces is
        hidden), `addresses` (what `hostname -I` gives), `on_access_point`, `access_point_running` and
        `dnsmasq_running` (what serves the access point), `hold` (a join asked from the box page waits for
        `release`; 'every join' holds those of the watch too), `netplan` (new profiles are netplan files),
        `failing` (programs that fail when run as root), `broken` (programs that fail when the box user runs
        them, as nmcli does when NetworkManager does not run), `files_listed` (whether NetworkManager tells
        the file of each profile), `stations` (who is connected to the access point; None where the radio
        cannot tell), `connects_during_scan` (a device that connects to the access point while the radio
        scans), `deaf_on_access_point` (a radio whose scan succeeds and lists nothing while it serves the
        access point), `empty_scans_after_access_point` (how many scans of NetworkManager list no access
        point at all once hostapd stopped: a radio that has just served the access point may hear nothing
        yet). `radio_taken_from_the_access_point` lists, for each start of the access point, the profiles
        NetworkManager could join by itself, so taking the radio from it."""
        found = json.loads((self.state / 'world.json').read_text()) if (self.state / 'world.json').exists() else {}
        found.update(values)
        (self.state / 'world.json').write_text(json.dumps(found))

    def restarts(self):
        """(what, whether the watch held its lock) of each restart of the box service and each run of the access point
        script, as the watch asked them."""
        return [tuple(json.loads(line)) for line in (self.state / 'restarts').read_text().splitlines()]

    def release(self):
        """Let a join held by `hold` go on."""
        (self.state / 'released').write_text('')

    def moments(self):
        """(when, call) of each call the stand-in answered; a call is the list of its words, `sudo` first for one
        run as root."""
        return [tuple(json.loads(line)) for line in (self.state / 'calls').read_text().splitlines()]

    def calls(self):
        """Each call the stand-in answered, as `moments` gives it."""
        return [call for _moment, call in self.moments()]

    def nmcli_calls(self):
        """The calls of `nmcli`, each without `sudo -n` and without the options of its output."""
        found = []
        for call in self.calls():
            words = call[2:] if call[:2] == ['sudo', '-n'] else call
            if words and words[0] == 'nmcli':
                found.append(words[1:])
        return found

    def joins(self):
        """The arguments of each profile a join of a network made, from `connection add` on."""
        return [words[2:] for words in self.nmcli_calls() if words[:2] == ['connection', 'add']]

    def joined(self):
        """(name of the network, password) of each join, None as the password of a network joined with none.

        The password is the one NetworkManager held when it was first asked to join the profile of the join,
        whichever way it reached it.
        """
        records = [json.loads(line) for line in (self.state / 'joins').read_text().splitlines()]
        made = [record for record in records if 'ssid' in record]
        secrets = {}
        for record in records:
            if 'secret' in record:
                secrets.setdefault(record['uuid'], record['secret'])
        return [(record['ssid'], secrets.get(record['uuid'])) for record in made]

    def join_by_itself(self):
        """Let NetworkManager do what it does by itself for a box on no network."""
        subprocess.run([str(self.stand_in), 'join-by-itself'], env=self.environment(), check=True,
                       timeout=common.PROCESS_TIMEOUT)

    def lose_the_network(self):
        """The box loses the network it is on: its router went off and came back."""
        self.change(active=None, active_ssid=None, active_bssid=None)

    def access_points(self):
        """The access points the box finds when it scans, as `change` describes them."""
        world = self.world()
        if world['access_points'] is not None:
            return world['access_points']
        return [{'bssid': f'02:00:5E:00:53:{number:02X}', 'signal': 70, 'ssid': ssid}
                for number, ssid in enumerate(world['networks'], start=1)]

    def access_point_starts(self):
        """The arguments of each start of the access point script of core, as root."""
        return [call[3:] if call[:2] == ['sudo', '-n'] else call[1:] for call in self.calls()
                if Path(call[2 if call[:2] == ['sudo', '-n'] else 0]).name == 'wireless_ap.sh']

    def held(self):
        """{profile name: what it holds} of the profiles NetworkManager holds, in /etc."""
        found = {}
        for folder in (self.profiles, self.netplan):
            for path in folder.glob('*'):
                profile = json.loads(path.read_text(encoding='utf-8'))
                found[profile['id']] = profile
        return found

    def hold_profile(self, name, ssid, password, autoconnect=True, **settings):
        """Arrange a profile NetworkManager holds already, as one the box found at its start: a network protected
        by WPA when it has a password. `settings` are more of what the profile holds."""
        profile = {'id': name, 'uuid': str(uuid.uuid4()), 'ssid': ssid, 'autoconnect': autoconnect, **settings}
        if password is not None:
            profile.update(key_mgmt='wpa-psk', psk=password)
        (self.profiles / f'{name}.nmconnection').write_text(json.dumps(profile), encoding='utf-8')
        return profile


class _Clock:
    """The module `time` as the watch reads it, its monotonic clock `ahead` seconds later than it is."""

    def __init__(self):
        self.ahead = 0

    def __getattr__(self, name):
        return getattr(time, name)

    def monotonic(self):
        return time.monotonic() + self.ahead


class WatchOfTheBox:
    """`viin-box-wifi watch`, loaded in this process, with the programs of `network`, its files beside it, and a
    copy of the network kept on its card. `look` takes the place of a turn of its loop, at the time a test gives,
    as `time.monotonic` would, and waits for what that look started. The record of the use of the box page is
    `page_use`; the watch reads it against the clock itself, which `clock.ahead` moves on."""

    def __init__(self, case, network):
        loader = importlib.machinery.SourceFileLoader('box_suite_wifi_watch', str(WIFI_PROGRAM))
        self.program = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(self.program)
        self.network = network
        files = network.directory / 'watch'
        files.mkdir()
        self.lock, self.leases, self.card_copy = files / 'lock', files / 'leases', files / 'viindoo-wifi.nmconnection'
        self.page_use = files / 'page-use.json'
        self.clock = _Clock()
        self.card_copy.write_text('')
        network.change(lock=str(self.lock))
        case.enterContext(mock.patch.multiple(
            self.program, KEPT_FILES=(self.card_copy,), LOCK=self.lock, LEASES=self.leases, PAGE_USE=self.page_use,
            ACCESS_POINT_SCRIPT=str(network.programs / 'wireless_ap.sh'), time=self.clock))
        case.enterContext(mock.patch.dict(os.environ, network.environment()))
        self.watched = self.program.Watch()
        # (time of the look, call) of each call a look made.
        self.history = []

    def start_again(self):
        """The watch starts again: it remembers nothing of its earlier looks."""
        self.watched = self.program.Watch()
        self.clock.ahead = 0

    def look(self, at):
        calls = len(self.network.calls())
        with settled():
            self.program.look(self.watched, at)
        self.history += [(at, call) for call in self.network.calls()[calls:]]

    def looks(self, start, end):
        """Look as the watch does, every LOOK_PERIOD seconds from `start` until `end`."""
        for at in range(start, end, self.program.LOOK_PERIOD):
            self.look(at)

    def lend(self, station, address, ends):
        """An address the access point lent to the device `station`, as dnsmasq keeps it, that ends at `ends`."""
        with self.leases.open('a', encoding='utf-8') as leases:
            leases.write(f'{ends} {station} {address} phone *\n')

    def when(self, *words):
        """The times of the looks that ran a command holding `words` in a row."""
        return [at for at, call in self.history
                if any(call[place:place + len(words)] == list(words) for place in range(len(call)))]


# A locale the settings of a box may name and that the box does not have: core's image of a box generates
# en_US.UTF-8 alone, on a system whose own settings name en_GB.UTF-8.
LOCALE_THE_BOX_LACKS = 'en_GB.UTF-8'


def with_settings_naming_a_locale_the_box_lacks(case):
    """Until the test `case` ends, the box and what it runs have locale settings that name a locale it does not have."""
    available = subprocess.run(['locale', '-a'], capture_output=True, text=True, check=True).stdout.split()
    case.assertNotIn(LOCALE_THE_BOX_LACKS.lower().replace('-', ''), [name.lower() for name in available])
    case.enterContext(mock.patch.dict(os.environ, LANG=LOCALE_THE_BOX_LACKS))
    for name in [name for name in os.environ if name.startswith('LC_')]:
        del os.environ[name]


def network_manager(case, answer_delay=0):
    """Stand a `NetworkManager` in for the programs of the box until the test `case` ends; give it.

    The box has a radio. A join starts `answer_delay` seconds after the route answered, when given.
    """
    wifi = common.layer('wifi')
    # A join asked before the test began must not reach the stand-in.
    for thread in threading.enumerate():
        if thread.name == 'viin-wifi-join':
            thread.join(common.PROCESS_TIMEOUT)
    directory = tempfile.mkdtemp(prefix='box-suite-wifi-')
    case.addCleanup(shutil.rmtree, directory, ignore_errors=True)
    manager = NetworkManager(directory)
    interfaces = Path(directory, 'net')
    (interfaces / wifi.INTERFACE).mkdir(parents=True)
    case.enterContext(mock.patch.dict(os.environ, manager.environment()))
    case.enterContext(mock.patch.object(wifi, 'INTERFACES', interfaces))
    case.enterContext(mock.patch.object(wifi, 'PROFILES', manager.profiles))
    case.enterContext(mock.patch.object(wifi, 'NETPLAN', manager.netplan))
    if answer_delay is not None:
        case.enterContext(mock.patch.object(wifi, 'ANSWER_DELAY', answer_delay))
    # The radio of the stand-in hears at once, or after the empty scans the world says: no join waits long for it.
    case.enterContext(mock.patch.multiple(wifi, HEARING_WAIT=HEARING_WAIT, HEARING_PERIOD=HEARING_PERIOD))
    keep_card_as_it_was(case)
    return manager


def card_copies():
    """The paths on the card where the box keeps the Wi-Fi network it joins again when it starts."""
    return [Path(path) for path in common.layer('wifi').KEPT_FILES]


def staged(path):
    """Where the box writes a copy for `path` before it takes its place: a hidden file beside it."""
    return path.with_name(f'.{path.name}.new')


def keep_card_as_it_was(case):
    """Remove from the card, when the test `case` ends, the copies of a kept network and the folders made for them."""
    copies = [found for path in card_copies() for found in (path, staged(path))]
    made = [folder for path in copies for folder in (path.parent, *path.parent.parents) if not folder.exists()]
    case.addCleanup(lambda: common.as_root('rm', '-rf', '--', *map(str, [*copies, *made])))


def on_the_card(path):
    """What a copy on the card holds, read as root alone may read it; None when there is none."""
    done = subprocess.run(['sudo', '-n', 'cat', '--', str(path)], capture_output=True, check=False, timeout=60)
    return json.loads(done.stdout) if not done.returncode else None


def put_on_the_card(path, profile):
    """Arrange a copy on the card, as the box made it when it kept a network earlier."""
    with tempfile.NamedTemporaryFile('w', suffix='.nmconnection', delete=False) as source:
        json.dump(profile, source)
    try:
        common.as_root('install', '-D', '-m', '600', '-o', 'root', '-g', 'root', source.name, str(path))
    finally:
        os.unlink(source.name)


@contextlib.contextmanager
def settled():
    """Wait, as the block ends, for each thread and each program the box started during the block to end."""
    threads, processes = [], []
    start = threading.Thread.start
    popen = subprocess.Popen

    def listed_start(thread):
        threads.append(thread)
        start(thread)

    class Listed(popen):
        def __init__(self, *arguments, **options):
            super().__init__(*arguments, **options)
            processes.append(self)

    with mock.patch.object(threading.Thread, 'start', listed_start), mock.patch.object(subprocess, 'Popen', Listed):
        yield
        for thread in threads:
            thread.join(common.PROCESS_TIMEOUT)
    for process in processes:
        process.wait(common.PROCESS_TIMEOUT)
