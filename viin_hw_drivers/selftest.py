"""The self-test of the box, its diagnostic file, and its clock.

The self-test has one line per layer the box depends on, from its network to
the layer itself. Each line is ok, warning, failed, neutral (nothing to do, and
nothing wrong) or pending (still being looked at), with one reason and at most
one action, both keys that the page turns into words. It renders without the
server: what needs the network is looked at by one probe at a time, which the
caller waits for a few seconds at most.

A line that fails is written once to the box log, which core sends to the
server, and once more when it is well again. The last result is kept in the
state file of the box.

The clock of a board without a battery starts wrong on a network without a
time server, and then no certificate can be verified. The box can take the
time from its own server: on a paired box, at start, over a connection whose
certificate is checked in everything but its dates; before pairing, at the
installer's request, from the server he names, shown to him before it is set.
"""
import ctypes
import datetime
import errno
import http.client
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import subprocess
import threading
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import netifaces

from odoo import tools

from . import device_tests, lock, pairing, reporting, trust, update, wifi
from .seams import core, identity, integrity
from .seams.handlers import handler_files

_logger = logging.getLogger(__name__)

# The state of a line, and of the whole self-test.
OK = 'ok'
WARNING = 'warning'
FAILED = 'failed'
NEUTRAL = 'neutral'
PENDING = 'pending'

LINES = (
    'network', 'name_resolution', 'clock', 'server', 'certificate', 'registration', 'live_channel',
    'browser_address', 'viindoo_name', 'software', 'devices', 'drivers', 'layer',
)

STATE_KEY = 'selftest'
# What the client of the Viindoo name keeps besides the name identity knows: a key of the configuration, and a record
# in the state file.
MANAGED_NAME_SERVER_KEY = 'viin_managed_name_server'
PUBLIC_NAME_KEY = 'public_name'

# The network part of the self-test: a probe is looked at again once it is older than this.
PROBE_FRESH = 20
# How long a request waits for a probe that is under way.
PROBE_WAIT = 4
# How long a connection of the probe or of the clock waits for the server.
CONNECT_TIMEOUT = 4
# The self-test runs by itself first this long after the box started, then at this period.
FIRST_RUN = 300
PERIOD = 300
# How often the box looks at its clock while it is not synchronised, and for how long after it started.
CLOCK_PERIOD = 30
CLOCK_WATCH = 3600
# A box that has not reported yet this long after it started is not just starting any more.
STARTING = 120
CERTIFICATE_ENDING = datetime.timedelta(days=30)
# For this long after the box made a certificate in place of one it had made, a browser that accepted that one is
# asked again.
ACCEPT_AGAIN = datetime.timedelta(days=7)
MAX_TEXT = 300
LOG_TAIL_BYTES = 256 * 1024
LOG_TAIL_LINES = 500
HIDDEN = '[hidden]'

# OpenSSL: verify a certificate without looking at its dates. The box's clock is what is in doubt.
_NO_CHECK_TIME = 0x200000
# Linux: the kernel clock is not synchronised to a time server.
_STA_UNSYNC = 0x0040

# The keys of the configuration the diagnostic file shows; of any other key it shows the name only.
SHOWN_KEYS = {
    'iot.box': (
        'remote_server', 'db_name', 'subject', 'screen_orientation', 'browser_url', 'wifi_ssid', 'iot_handlers_etag',
        trust.BOX_ADDRESS_KEY, trust.AUTO_UPDATE_KEY, trust.MANAGED_NAME_KEY,
        MANAGED_NAME_SERVER_KEY,
    ),
    'options': ('addons_path', 'server_wide_modules', 'log_level', 'log_handler', 'logfile', 'http_port'),
}
_PRIVATE_KEY = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.DOTALL)

_STARTED = time.monotonic()


def _line(identifier, state, reason, action='', **details):
    return {'id': identifier, 'state': state, 'reason': reason, 'action': action, 'details': details}


def _now():
    return trust.iso_time(trust.utc_now())


def _text(value, limit=MAX_TEXT):
    return ' '.join(str(value or '').split())[:limit]


def _helpers():
    return core('tools.helpers')


def _conf(key):
    return (_helpers().get_conf(key) or '').strip()


def _origin(address):
    """(scheme, host, port) of the address of a server; None when it is none."""
    try:
        parts = urlsplit(str(address or ''))
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return None
    return parts.scheme, parts.hostname, port or (443 if parts.scheme == 'https' else 80)


def _is_address(host):
    try:
        ipaddress.ip_address(str(host).strip('[]'))
    except ValueError:
        return False
    return True


# ------------------------------------------------------------------------------------------------ clock

class _Timex(ctypes.Structure):
    """The `struct timex` of Linux, as far as its status; room is left for the fields that follow."""
    _fields_ = [
        ('modes', ctypes.c_uint), ('offset', ctypes.c_long), ('freq', ctypes.c_long),
        ('maxerror', ctypes.c_long), ('esterror', ctypes.c_long), ('status', ctypes.c_int),
        ('rest', ctypes.c_byte * 256),
    ]


def clock_synchronised():
    """Whether the kernel says a time server set the clock; None when it cannot be asked."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        timex = _Timex()
        if libc.adjtimex(ctypes.byref(timex)) < 0:
            return None
    except (OSError, AttributeError):
        return None
    return not timex.status & _STA_UNSYNC


def _unverified_context():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _context_without_dates():
    """Verifies the server of the box - authorities, name, signatures - and not the dates of its certificate."""
    context = trust.ssl_context()
    context.verify_flags |= _NO_CHECK_TIME
    return context


def _server_answer(scheme, host, port, context):
    """(HTTP status, the time of the server in UTC or None) of a HEAD request on the root of a server.

    Nothing of the box is sent: no credential, no cookie.
    """
    if scheme == 'https':
        connection = http.client.HTTPSConnection(host, port, timeout=CONNECT_TIMEOUT, context=context)
    else:
        connection = http.client.HTTPConnection(host, port, timeout=CONNECT_TIMEOUT)
    try:
        connection.request('HEAD', '/')
        response = connection.getresponse()
        status, date = response.status, response.headers.get('Date')
    finally:
        connection.close()
    try:
        moment = parsedate_to_datetime(date)
    except (TypeError, ValueError):
        return status, None
    if moment.tzinfo:
        moment = moment.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return status, moment


def _failure(error):
    """The word for what kept the box from its server."""
    if isinstance(error, socket.gaierror):
        return 'not_resolved'
    if isinstance(error, (socket.timeout, TimeoutError)):
        return 'timeout'
    if isinstance(error, ConnectionRefusedError):
        return 'refused'
    if isinstance(error, ssl.SSLError):
        return 'tls'
    if isinstance(error, http.client.HTTPException):
        return 'not_http'
    if isinstance(error, OSError) and error.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH):
        return 'no_route'
    return 'unreachable'


def _server_time(server, verified):
    """(time of the server or None, whether its certificate was checked, the reason when there is no time).

    `verified`: over https the certificate is checked in everything but its
    dates, and a server that fails that check gives no time.
    """
    origin = _origin(server)
    if not origin:
        return None, False, 'invalid_address'
    scheme, host, port = origin
    context = None
    if scheme == 'https':
        context = _context_without_dates() if verified else _unverified_context()
    try:
        _status, moment = _server_answer(scheme, host, port, context)
    except ssl.SSLCertVerificationError:
        return None, False, 'server_certificate_untrusted'
    except (OSError, http.client.HTTPException) as error:
        word = _failure(error)
        return None, False, 'server_not_found' if word == 'not_resolved' else 'server_unreachable'
    if moment is None:
        return None, False, 'no_time'
    return moment, scheme == 'https' and verified, ''


def _clock_source(server_url):
    """(address of the server to read the time from, whether its certificate must be checked) or (None, reason)."""
    own = _conf(trust.SERVER_KEY)
    if pairing.is_paired() and own:
        given = str(server_url or '').strip()
        if given:
            try:
                if pairing.server_address(given) != pairing.server_address(own):
                    return None, 'other_server'
            except ValueError:
                return None, 'invalid_address'
        return own, True
    if not str(server_url or '').strip():
        return None, 'server_missing'
    try:
        return pairing.server_address(server_url), False
    except ValueError:
        return None, 'invalid_address'


def read_server_clock(server_url=None):
    """The time of the server of the box, beside the time of the box, for a person to compare before setting it.

    On a paired box the server is its own and `server_url` may only name it; on
    another box `server_url` names the server, whose word is then unverified.
    {'status': 'read', 'box_time', 'server_time', 'difference' (seconds the
    server is ahead), 'synchronised', 'verified', 'server'} or {'status':
    'failed', 'reason'}: server_missing, invalid_address, other_server,
    server_not_found, server_unreachable, server_certificate_untrusted, no_time.
    """
    server, checked = _clock_source(server_url)
    if server is None:
        return {'status': 'failed', 'reason': checked}
    moment, verified, reason = _server_time(server, checked)
    if moment is None:
        return {'status': 'failed', 'reason': reason}
    box_time = trust.utc_now()
    return {
        'status': 'read',
        'server': server,
        'box_time': trust.iso_time(box_time),
        'server_time': trust.iso_time(moment),
        'difference': round((moment - box_time).total_seconds()),
        'synchronised': clock_synchronised(),
        'verified': verified,
    }


def _set_clock(moment):
    """Set the clock of the box to `moment` (UTC); True when the clock then says so."""
    target = moment.replace(tzinfo=datetime.timezone.utc).timestamp()
    try:
        done = subprocess.run(
            ['sudo', '-n', 'date', '-u', '-s', f'@{int(target)}'], capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and abs(time.time() - target) < 5


def set_clock_from_server(server_url=None):
    """Read the time of the server again, as `read_server_clock` does, and set the clock of the box to it.

    Never on a clock a time server keeps: {'status': 'refused', 'reason':
    'clock_synchronised'}. {'status': 'set', 'box_time', 'server_time',
    'verified'}, or the failure of `read_server_clock`, or {'status': 'failed',
    'reason': 'box_error'} when the box could not set its clock.
    """
    if clock_synchronised():
        return {'status': 'refused', 'reason': 'clock_synchronised'}
    server, checked = _clock_source(server_url)
    if server is None:
        return {'status': 'failed', 'reason': checked}
    started = time.monotonic()
    moment, verified, reason = _server_time(server, checked)
    if moment is None:
        return {'status': 'failed', 'reason': reason}
    before = trust.utc_now()
    moment += datetime.timedelta(seconds=time.monotonic() - started)
    if not _set_clock(moment):
        _logger.warning('The clock of the box could not be set from its server')
        return {'status': 'failed', 'reason': 'box_error'}
    _logger.warning(
        'The clock of the box was set from %s (%s): it said %s, the server %s',
        server, 'checked but for its dates' if verified else 'unverified', trust.iso_time(before),
        trust.iso_time(moment))
    return {
        'status': 'set', 'box_time': trust.iso_time(before), 'server_time': trust.iso_time(moment),
        'verified': verified,
    }


def _clock_at_start():
    """On a paired box whose clock no time server keeps, take the time of its server when it differs.

    True once there is nothing left to do: a time server keeps the clock, the
    kernel cannot say, or the clock was set or found right.
    """
    synchronised = clock_synchronised()
    if synchronised is not False:
        return True
    server = _conf(trust.SERVER_KEY)
    if not pairing.is_paired() or not _origin(server):
        return False
    moment, _verified, reason = _server_time(server, True)
    if moment is None:
        _logger.info('The box could not read the time of its server (%s)', reason)
        return False
    if abs(moment - trust.utc_now()) <= pairing.CLOCK_TOLERANCE:
        return True
    return set_clock_from_server()['status'] == 'set'


# ------------------------------------------------------------------------------------------------ probe

def _probe(server):
    """What the network says of the server of the box: name, reach, time, certificate."""
    found = {'server': server, 'at': _now()}
    scheme, host, port = _origin(server)
    if _is_address(host):
        found['resolution'] = {'state': 'address', 'addresses': [host.strip('[]')]}
    else:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            found['resolution'] = {'state': 'resolved', 'addresses': sorted({info[4][0] for info in infos})}
        except socket.gaierror as error:
            unknown = error.errno in (socket.EAI_NONAME, getattr(socket, 'EAI_NODATA', socket.EAI_NONAME))
            found['resolution'] = {'state': 'no_such_name' if unknown else 'no_answer'}
            found['reach'] = {'reached': False, 'error': 'not_resolved'}
            return found
    reach = {'reached': False, 'error': '', 'server_time': None, 'server_time_verified': False, 'http_status': None}
    # Over https the time is read checked but for the dates first; the reach and the time are worth knowing anyway.
    contexts = [(None, False)]
    if scheme == 'https':
        contexts = [(_context_without_dates(), True), (_unverified_context(), False)]
    for context, checked in contexts:
        try:
            status, moment = _server_answer(scheme, host, port, context)
        except ssl.SSLCertVerificationError:
            continue
        except (OSError, http.client.HTTPException) as error:
            reach['error'] = _failure(error)
            break
        reach.update(reached=True, error='', http_status=status, server_time_verified=checked)
        if moment:
            reach['server_time'] = trust.iso_time(moment)
            reach['difference'] = round((moment - trust.utc_now()).total_seconds())
        break
    found['reach'] = reach
    if scheme == 'https' and reach['reached']:
        found['certificate'] = _certificate_problems(host, port)
    return found


def _certificate_problems(host, port):
    certificate = {'public': None, 'trusted': None, 'presented': None, 'trusted_count': 0}
    try:
        certificate['public'] = trust.verification_problem(host, port, store=False)
        if certificate['public']:
            certificate['trusted'] = trust.verification_problem(host, port)
        certificate['presented'] = trust.describe(trust.presented_certificate(host, port))
        certificate['trusted_count'] = len(trust.trusted_certificates())
    except (OSError, ValueError) as error:
        certificate['error'] = type(error).__name__
    return certificate


class _Probes:
    """At most one probe of the server at a time; its result is kept for the next requests."""

    def __init__(self):
        self.lock = threading.Lock()
        self.result = None
        self.finished = 0.0
        self.running = None

    def get(self, server, wait, refresh=False):
        """(the latest probe of `server` or None, whether one is under way)."""
        with self.lock:
            stale = (self.result is None or self.result['server'] != server
                     or time.monotonic() - self.finished > PROBE_FRESH)
            done = self.running
            if done is None and (stale or refresh):
                done = self.running = threading.Event()
                threading.Thread(
                    target=self._run, args=(server, done), name='ViinSelfTestProbe', daemon=True).start()
        if done is not None and wait > 0:
            done.wait(wait)
        with self.lock:
            result = self.result if self.result and self.result['server'] == server else None
            return result, self.running is not None

    def _run(self, server, done):
        result = None
        try:
            result = _probe(server)
        except Exception:
            _logger.exception('The self-test could not look at the server of the box')
            result = {'server': server, 'at': _now(), 'reach': {'reached': False, 'error': 'box_error'}}
        finally:
            with self.lock:
                self.result, self.finished, self.running = result, time.monotonic(), None
            done.set()


_probes = _Probes()


# ------------------------------------------------------------------------------------------------ lines

def _interfaces():
    """[{'name', 'kind': wired, wifi or other, 'addresses'}] of the interfaces that have an IPv4 address."""
    found = []
    for name in netifaces.interfaces():
        addresses = [entry.get('addr') for entry in netifaces.ifaddresses(name).get(netifaces.AF_INET, ())]
        addresses = [address for address in addresses if address and not address.startswith('127.')]
        if not addresses:
            continue
        kind = 'wired' if name.startswith(('eth', 'en')) else 'wifi' if name.startswith('wl') else 'other'
        found.append({'name': name, 'kind': kind, 'addresses': addresses})
    return found


def _network():
    helpers = _helpers()
    interfaces = _interfaces()
    gateway = (netifaces.gateways().get('default') or {}).get(netifaces.AF_INET)
    kinds = {interface['kind'] for interface in interfaces}
    access_point = any('10.11.12.1' in interface['addresses'] for interface in interfaces)
    kind = ('access_point' if access_point else 'wired' if 'wired' in kinds else 'wifi' if 'wifi' in kinds
            else 'other' if interfaces else 'none')
    ssid = ''
    if access_point or 'wifi' in kinds:
        try:
            ssid = _text(helpers.get_ssid(), 64)
        except (OSError, subprocess.SubprocessError):
            ssid = ''
    return {
        'kind': kind,
        'address': (helpers.get_ip() or '') if interfaces else '',
        'gateway': gateway[0] if gateway else '',
        'ssid': ssid,
        'hostname': helpers.get_hostname(),
        'interfaces': interfaces,
    }


def _network_line(network):
    addresses = [address for interface in network['interfaces'] for address in interface['addresses']]
    details = {key: network[key] for key in ('kind', 'address', 'gateway', 'ssid')}
    if not addresses:
        return _line('network', FAILED, 'no_address', 'connect_network', **details)
    if all(ipaddress.ip_address(address).is_link_local for address in addresses):
        return _line('network', FAILED, 'link_local_only', 'check_dhcp', **details)
    if network['kind'] == 'access_point':
        return _line('network', WARNING, 'access_point', 'join_wifi', **details)
    if not network['gateway']:
        return _line('network', WARNING, 'no_gateway', 'check_router', **details)
    return _line('network', OK, network['kind'], **details)


def _resolution_line(server, probe):
    if not server:
        return _line('name_resolution', NEUTRAL, 'no_server')
    if probe is None:
        return _line('name_resolution', PENDING, 'checking')
    resolution = probe.get('resolution') or {}
    state = resolution.get('state')
    if state == 'address':
        return _line('name_resolution', NEUTRAL, 'address_given', addresses=resolution['addresses'])
    if state == 'resolved':
        return _line('name_resolution', OK, 'resolved', addresses=resolution['addresses'])
    if state == 'no_such_name':
        return _line('name_resolution', FAILED, 'no_such_name', 'check_server_address')
    return _line('name_resolution', FAILED, 'no_answer', 'check_dns')


def _clock_line(probe):
    synchronised = clock_synchronised()
    reach = (probe or {}).get('reach') or {}
    difference = reach.get('difference')
    details = {
        'synchronised': synchronised, 'box_time': _now(), 'server_time': reach.get('server_time'),
        'difference': difference, 'server_time_verified': reach.get('server_time_verified', False),
    }
    if difference is not None and abs(difference) > pairing.CLOCK_TOLERANCE.total_seconds():
        if synchronised:
            # A time server keeps the clock of the box: the clock that is wrong is the server's.
            return _line('clock', WARNING, 'server_clock_differs', 'check_server_clock', **details)
        return _line('clock', FAILED, 'wrong', 'set_clock_from_server', **details)
    if synchronised is False:
        return _line('clock', WARNING, 'not_synchronised', 'allow_time_server', **details)
    if synchronised is None and difference is None:
        return _line('clock', NEUTRAL, 'unknown', **details)
    return _line('clock', OK, 'synchronised' if synchronised else 'agrees_with_server', **details)


SERVER_ACTIONS = {
    'not_resolved': 'check_dns', 'refused': 'check_server_address', 'timeout': 'check_network_firewall',
    'no_route': 'check_network_firewall', 'tls': 'check_server_address', 'not_http': 'check_server_address',
}


def _server_line(server, probe):
    if not server:
        return _line('server', NEUTRAL, 'no_server')
    if probe is None:
        return _line('server', PENDING, 'checking', server=server)
    reach = probe.get('reach') or {}
    if reach.get('reached'):
        return _line('server', OK, 'reachable', server=server, http_status=reach.get('http_status'), at=probe['at'])
    error = reach.get('error') or 'unreachable'
    return _line('server', FAILED, error, SERVER_ACTIONS.get(error, 'check_network_firewall'), server=server,
                 at=probe['at'])


def _certificate_line(server, probe, clock):
    if not server:
        return _line('certificate', NEUTRAL, 'no_server')
    if _origin(server)[0] != 'https':
        return _line('certificate', WARNING, 'not_https', 'use_https')
    if probe is None:
        return _line('certificate', PENDING, 'checking')
    found = probe.get('certificate')
    if not found:
        return _line('certificate', NEUTRAL, 'not_checked')
    details = {'presented': found.get('presented'), 'trusted_count': found.get('trusted_count', 0)}
    if found.get('error'):
        return _line('certificate', NEUTRAL, 'not_checked', **details)
    if found['public'] == '':
        return _line('certificate', OK, 'public', **details)
    if found['trusted'] == '':
        return _line('certificate', OK, 'trusted_by_you', **details)
    problem = found['public']
    if problem in ('expired', 'not_yet_valid'):
        if clock['state'] == FAILED:
            return _line('certificate', FAILED, 'box_clock', 'set_clock_from_server', **details)
        return _line('certificate', FAILED, problem, 'renew_server_certificate', **details)
    if problem == 'name_mismatch':
        return _line('certificate', FAILED, 'name_mismatch', 'check_server_address', **details)
    if problem == 'unknown_authority':
        reason = 'changed' if found.get('trusted_count') else 'untrusted'
        return _line('certificate', FAILED, reason, 'pair_again', **details)
    return _line('certificate', FAILED, 'invalid', 'check_server_address', **details)


# A refused registration and a box the server does not know both mean that no active record of the server holds the
# credential of the box. The settings code that "Pair again" asks for first may then be shown on no record at all;
# the reset file of the card always frees the box for a new pairing code.
UNKNOWN_BOX_ACTION = 'reset_then_pair'


def _registration_line(paired):
    if not paired:
        return _line('registration', WARNING, 'not_paired', 'pair_box')
    outcome = reporting.outcome()
    details = {key: outcome[key] for key in ('at', 'at_age', 'accepted_at', 'accepted_age', 'owed')}
    state = outcome['state']
    if state == reporting.ACCEPTED:
        return _line('registration', OK, 'accepted', **details)
    if state == reporting.REFUSED:
        return _line('registration', FAILED, 'refused', UNKNOWN_BOX_ACTION, **details)
    if state == reporting.UNREACHABLE:
        return _line('registration', FAILED, 'unreachable', 'check_server', **details)
    if time.monotonic() - _STARTED < STARTING:
        return _line('registration', PENDING, 'not_yet', **details)
    return _line('registration', WARNING, 'never', 'check_server', **details)


def _channel_line(paired):
    if not paired:
        return _line('live_channel', NEUTRAL, 'no_server')
    channel = reporting.channel()
    details = {'since': channel['since'], 'since_age': channel['since_age'], 'error': channel['error']}
    state = channel['state']
    if state == reporting.CONNECTED:
        return _line('live_channel', OK, 'connected', **details)
    if state == reporting.CONNECTING:
        return _line('live_channel', PENDING, 'connecting', **details)
    if state == reporting.RECONNECTING:
        return _line('live_channel', FAILED, 'reconnecting', 'check_server', **details)
    if state == reporting.WAITING:
        return _line('live_channel', WARNING, 'waiting', 'check_registration', **details)
    return _line('live_channel', WARNING, 'off', 'restart_box', **details)


def _covers(names, host):
    host = host.lower()
    names = [name.lower() for name in names]
    return host in names or any(
        name.startswith('*.') and '.' in host and host.split('.', 1)[1] == name[2:] for name in names)


def browser_address():
    """The address the box gives to browsers - the one it reports to its server - and which kind it is.

    {'address', 'kind'}: the kind is where identity chose the address from - own_name (the name the installer typed
    for the box, which the certificate of the customer or one the box made may cover), viindoo_name, address,
    local_name (`<hostname>.local`, the box has no browsable address of the site). While the seam that chooses it
    is not in place, core's value stands and is told apart by comparison: own_name, viindoo_name, address or name
    (any other name).
    """
    chosen = identity.reported_address()
    address = str(chosen['address'] or '')
    if chosen['source'] != identity.CORE_VALUE:
        return {'address': address, 'kind': chosen['source']}
    own, managed = _conf(trust.BOX_ADDRESS_KEY).lower(), _conf(trust.MANAGED_NAME_KEY).lower()
    if own and address.lower() == own:
        kind = identity.OWN_NAME
    elif managed and address.lower() == managed:
        kind = identity.VIINDOO_NAME
    elif _is_address(address):
        kind = identity.ADDRESS
    else:
        kind = 'name'
    return {'address': address, 'kind': kind}


def _served_certificate(own):
    """Which certificate a browser gets at an address other than the Viindoo name, and until when.

    {'certificate_kind': box or customer; 'certificate_ends': the end date of that certificate as
    YYYY-MM-DDTHH:MM:SSZ}: the one the box serves (`own`). A certificate the box made adds 'certificate_made_at'
    (when) and 'accept_again': whether it replaced an earlier certificate the box had made, recently enough that a
    browser which accepted that one asks for it once more. The first certificate of a box, and one that replaced the
    image's, a missing or the customer's certificate, never asks it.
    """
    served = {'certificate_kind': own['kind'], 'certificate_ends': own['expiry']}
    if own.get('made_at'):
        made = datetime.datetime.strptime(own['made_at'], '%Y-%m-%dT%H:%M:%SZ')
        accept_again = trust.utc_now() - made < ACCEPT_AGAIN and trust.replaced_own_certificate(own['fingerprint'])
        served.update(certificate_made_at=own['made_at'], accept_again=accept_again)
    return served


def _address_not_covered_line(certificate, details):
    """The line for a certificate that does not cover the address browsers are given.

    The customer's own certificate is theirs to replace. The box's own is replaced by the box: shortly after an
    address appears, it is being made and nothing is asked of anyone; when making it failed, a restart makes the
    box try again; when the box has no reason to make one, only support can say why.
    """
    if certificate['kind'] == 'customer':
        return _line('browser_address', WARNING, 'certificate_not_for_address', 'renew_own_certificate', **details)
    if trust.look_failed():
        return _line('browser_address', WARNING, 'certificate_not_for_address', 'restart_box', **details)
    if trust.will_make_certificate():
        return _line('browser_address', PENDING, 'certificate_being_made', **details)
    return _line('browser_address', WARNING, 'certificate_not_for_address', 'contact_support', **details)


def _viindoo_name_certificate_line(certificate, details):
    """The line for browsers given the Viindoo name: they get the certificate of that name, not `certificate`.

    A certificate of the name that cannot be read leaves unknown what they get. One that has ended is what they get
    until the box gives browsers its address instead, at the next look of its Viindoo name, which a restart brings
    about. That it ends soon, and its renewal, are for the viindoo_name line to say, by the schedule on which the box
    renews it. `certificate`, the box's own, is what a browser gets that opens the box by its address: it ending or
    having ended is a warning, since browsers given the name do not meet it.
    """
    served = trust.name_certificate()
    if served is None:
        return _line('browser_address', WARNING, 'name_certificate_unreadable', 'restart_box', **details)
    details = dict(details, certificate_kind='viindoo_name', certificate_ends=served['expiry'])
    now = trust.utc_now()
    if datetime.datetime.strptime(served['expiry'], '%Y-%m-%dT%H:%M:%SZ') <= now:
        return _line('browser_address', FAILED, 'certificate_expired', 'restart_box', **details)
    own_ends = datetime.datetime.strptime(certificate['expiry'], '%Y-%m-%dT%H:%M:%SZ')
    renew = 'renew_own_certificate' if certificate['kind'] == 'customer' else 'restart_box'
    if own_ends <= now:
        return _line('browser_address', WARNING, 'address_certificate_expired', renew, **details)
    if own_ends - now < CERTIFICATE_ENDING:
        return _line('browser_address', WARNING, 'address_certificate_ending', renew, **details)
    return _line('browser_address', OK, identity.VIINDOO_NAME, **details)


def _browser_address_line():
    given = browser_address()
    certificate = trust.box_certificate()
    details = dict(given, certificate={
        key: certificate.get(key) for key in ('kind', 'expiry', 'subject', 'names', 'made_at')})
    if given['kind'] == identity.ADDRESS and _is_address(given['address']):
        found = ipaddress.ip_address(given['address'].strip('[]'))
        if found.is_loopback or found.is_unspecified:
            return _line('browser_address', FAILED, 'no_address', 'connect_network', **details)
        if found.is_link_local:
            return _line('browser_address', FAILED, 'link_local', 'check_dhcp', **details)
    if certificate['kind'] == 'missing':
        return _line('browser_address', FAILED, 'no_certificate', 'restart_box', **details)
    if certificate['kind'] == 'shipped':
        return _line('browser_address', WARNING, 'shared_certificate', 'restart_box', **details)
    if given['kind'] == identity.VIINDOO_NAME:
        return _viindoo_name_certificate_line(certificate, details)
    if trust.certificate_unserved():
        # What follows reads the certificate file, which browsers do not get yet; a restart starts the web server on it.
        return _line('browser_address', WARNING, 'certificate_not_served', 'restart_box', **details)
    ends = datetime.datetime.strptime(certificate['expiry'], '%Y-%m-%dT%H:%M:%SZ')
    renew = 'renew_own_certificate' if certificate['kind'] == 'customer' else 'restart_box'
    if ends <= trust.utc_now():
        return _line('browser_address', FAILED, 'certificate_expired', renew, **details)
    if given['kind'] == identity.LOCAL_NAME:
        return _line('browser_address', WARNING, 'local_name', 'connect_network', **details)
    if not _covers(certificate.get('names') or [], given['address']):
        return _address_not_covered_line(certificate, details)
    if ends - trust.utc_now() < CERTIFICATE_ENDING:
        return _line('browser_address', WARNING, 'certificate_ending', renew, **details)
    return _line('browser_address', OK, given['kind'], **_served_certificate(certificate), **details)


def viindoo_name():
    """The Viindoo name of the box as its client left it: {'name', 'not_after', 'state', 'reason', 'line'}.

    `state` and `reason` are the last answer of the server to the question for
    a name; `line` is the reason the self-test gives.
    """
    record = update.read_state().get(PUBLIC_NAME_KEY)
    record = record if isinstance(record, dict) else {}
    line = _viindoo_name_line(record)
    return {
        'name': _conf(trust.MANAGED_NAME_KEY) or _text(record.get('fqdn'), 253),
        'not_after': _text(record.get('not_after'), 40),
        'state': _text(record.get('status'), 40),
        'reason': _text(record.get('reason'), 40),
        'line': line['reason'],
    }


# What to do when the box fell back to its own certificate because the name ended and the server did not give
# another, by what the server last answered to the box's question; any other answer is for support to read.
_RENEWAL_ACTIONS = {
    'refused:limit_reached': 'ask_administrator',
    'refused:address_not_private': 'use_own_certificate',
}


def _renewal_action(answer):
    if answer.startswith('unreachable:'):
        return 'check_server'
    return _RENEWAL_ACTIONS.get(answer, 'contact_support')


def _viindoo_name_line(record):
    details = {key: _text(record.get(key), 253) for key in ('fqdn', 'not_after', 'next_question_at', 'asked_at')}
    own = _conf(trust.BOX_ADDRESS_KEY)
    if own:
        # Browsers are given the typed name, so the box asks for no Viindoo name, whichever certificate covers it.
        if trust.customer_certificate_covers(own):
            return _line('viindoo_name', NEUTRAL, 'own_certificate')
        return _line('viindoo_name', NEUTRAL, 'own_name')
    status, reason = record.get('status'), record.get('reason')
    if not pairing.is_paired() or not status:
        return _line('viindoo_name', NEUTRAL, 'not_offered')
    if status == 'unavailable' and reason == 'disabled':
        return _line('viindoo_name', NEUTRAL, 'disabled', **details)
    if status == 'unavailable' and reason in ('no_provider', 'not_offered'):
        return _line('viindoo_name', NEUTRAL, 'not_offered', **details)
    if status == 'unavailable' and reason == 'box_unknown':
        return _line('viindoo_name', WARNING, 'box_unknown', UNKNOWN_BOX_ACTION, **details)
    if status in ('unavailable', 'unreachable'):
        return _line('viindoo_name', NEUTRAL, 'no_answer_yet', **details)
    if status in ('name', 'pending'):
        return _line('viindoo_name', NEUTRAL, 'certificate_on_its_way', in_queue=bool(record.get('in_queue')),
                     **details)
    if status == 'refused':
        if reason == 'address_not_private':
            return _line('viindoo_name', WARNING, 'address_not_private', 'use_own_certificate', **details)
        if reason == 'limit_reached':
            return _line('viindoo_name', WARNING, 'limit_reached', 'ask_administrator', **details)
        if reason == 'try_later':
            return _line('viindoo_name', NEUTRAL, 'try_later', **details)
        if reason == 'invalid_request' and not _helpers().get_ip():
            return _line('viindoo_name', NEUTRAL, 'waiting_for_address', **details)
        return _line('viindoo_name', WARNING, 'refused', 'contact_support', server_reason=_text(reason, 40),
                     **details)
    if status != 'issued':
        return _line('viindoo_name', NEUTRAL, 'no_answer_yet', **details)
    problem = record.get('install_problem')
    if problem == 'box_clock_wrong':
        return _line('viindoo_name', WARNING, 'box_clock_wrong', 'set_clock_from_server', **details)
    if problem == 'validity_unconfirmed':
        return _line('viindoo_name', WARNING, 'validity_unconfirmed', 'set_clock_from_server', **details)
    if problem == 'untrusted':
        return _line('viindoo_name', WARNING, 'authorities_out_of_date', 'write_new_image', **details)
    if problem:
        return _line('viindoo_name', WARNING, 'not_installed', 'contact_support', problem=_text(problem, 40),
                     **details)
    check = record.get('check')
    if check in ('not_resolved', 'other_address'):
        fqdn = details['fqdn']
        return _line('viindoo_name', WARNING, 'network_refuses_name', 'allow_zone', zone=fqdn.partition('.')[2],
                     **details)
    if check and check != 'ok':
        return _line('viindoo_name', WARNING, 'name_check_failed', 'restart_for_name', check=_text(check, 40),
                     **details)
    try:
        ends = datetime.datetime.strptime(details['not_after'], '%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        ends = None
    if ends and ends <= trust.utc_now():
        answer = _text(record.get('last_answer'), 80)
        return _line('viindoo_name', WARNING, 'expired', _renewal_action(answer), answer=answer,
                     **details)
    if record.get('renewal_failing'):
        return _line('viindoo_name', NEUTRAL, 'renewal_retrying', **details)
    if not check:
        return _line('viindoo_name', PENDING, 'checking_name', **details)
    return _line('viindoo_name', OK, 'in_use', **details)


UPDATE_STATES = {
    OK: (update.UP_TO_DATE, update.APPLIED, 'trial', 'checking', 'applying'),
    NEUTRAL: (update.UNREACHABLE, update.NO_CHANNEL, update.RELEASE_UNTAGGED, update.NO_POINTER,
              update.CANDIDATE_REFUSED, 'not_checked'),
    WARNING: (update.AVAILABLE, update.POINTER_INVALID, update.NEEDS_NEW_IMAGE, update.CORE_COMMIT_UNAVAILABLE,
              update.BAD_RELEASE, update.APPLY_FAILED),
}
UPDATE_ACTIONS = {update.AVAILABLE: 'apply_update', update.NEEDS_NEW_IMAGE: 'write_new_image'}


def update_state(software):
    """The one word for where the software of the box stands: a state of the release check, or what it is doing."""
    if software['busy']:
        return software['busy']
    if software['trial']:
        return 'trial'
    if software['offered']:
        return update.AVAILABLE
    return (software['last_check'] or {}).get('state') or 'not_checked'


def _software_line(software):
    word = update_state(software)
    last = software['last_check'] or {}
    details = {
        'release': software['release'], 'candidate_box': software['candidate_box'],
        'auto_update': software['auto_update'], 'checked_at': last.get('at'), 'check_reason': last.get('reason', ''),
        'server_series': last.get('server_series', ''), 'offered': (software['offered'] or {}).get('release', ''),
        'rolled_back': (software['rolled_back'] or {}).get('release', '') if isinstance(
            software['rolled_back'], dict) else '',
    }
    if software['running'].get('pointer') != 'ok':
        return _line('software', WARNING, 'no_release', 'contact_support', update_state=word, **details)
    state = next((state for state, words in UPDATE_STATES.items() if word in words), WARNING)
    return _line('software', state, word, UPDATE_ACTIONS.get(word, ''), **details)


def _devices_line(devices):
    by_kind = {}
    for device in devices:
        by_kind[device['kind']] = by_kind.get(device['kind'], 0) + 1
    if not devices:
        return _line('devices', NEUTRAL, 'none', count=0, by_kind={})
    return _line('devices', OK, 'connected', count=len(devices), by_kind=by_kind)


def _drivers_line():
    files = handler_files()
    if not files['loaded']:
        return _line('drivers', PENDING, 'loading')
    failed = [{'file': failure['file'], 'origin': failure['origin'], 'error': _text(failure['error'])}
              for failure in files['failed']]
    if failed:
        return _line('drivers', FAILED, 'failed_to_load', 'contact_support', failed=failed)
    return _line('drivers', OK, 'loaded', count=len(files['core']) + len(files['layer']))


def _layer_line(state):
    seams = [{'number': seam['number'], 'feature': seam['feature'], 'state': seam['state'], 'reason': seam['reason']}
             for seam in state['seams'] if seam['state'] not in ('applied', 'deferred')]
    details = {'seams': seams, 'problems': state['problems']}
    if state['ok']:
        return _line('layer', OK, 'in_place')
    if not state['load_order_ok']:
        return _line('layer', FAILED, 'load_order', 'contact_support', **details)
    if any(seam['state'] in ('failed', 'skipped') for seam in seams) or state['problems']:
        return _line('layer', FAILED, 'not_in_place', 'contact_support', **details)
    return _line('layer', PENDING, 'starting', **details)


# ------------------------------------------------------------------------------------------------ the self-test

def _lines(context):
    """Each line of the self-test, in the order of LINES; a line that cannot be worked out fails on its own."""
    server, probe, paired = context['server'], context['probe'], context['paired']
    clock = _guarded('clock', lambda: _clock_line(probe))
    builders = {
        'network': lambda: _network_line(context['network']),
        'name_resolution': lambda: _resolution_line(server, probe),
        'clock': lambda: clock,
        'server': lambda: _server_line(server, probe),
        'certificate': lambda: _certificate_line(server, probe, clock),
        'registration': lambda: _registration_line(paired),
        'live_channel': lambda: _channel_line(paired),
        'browser_address': _browser_address_line,
        'viindoo_name': lambda: _viindoo_name_line(context['public_name']),
        'software': lambda: _software_line(context['software']),
        'devices': lambda: _devices_line(context['devices']),
        'drivers': _drivers_line,
        'layer': lambda: _layer_line(context['integrity']),
    }
    return [_guarded(identifier, builders[identifier]) for identifier in LINES]


def _guarded(identifier, build):
    try:
        return build()
    except Exception:
        _logger.exception('The self-test line %s could not be worked out', identifier)
        return _line(identifier, FAILED, 'box_error', 'contact_support')


def _context(wait, refresh):
    server = _conf(trust.SERVER_KEY)
    server = server if _origin(server) else ''
    probe, probing = _probes.get(server, wait, refresh) if server else (None, False)
    record = update.read_state().get(PUBLIC_NAME_KEY)
    return {
        'server': server,
        'probe': probe,
        'probing': probing,
        'paired': pairing.is_paired(),
        'network': _network(),
        'software': update.status(),
        'devices': device_tests.devices(),
        'integrity': integrity(),
        'public_name': record if isinstance(record, dict) else {},
    }


def _summary(lines, context):
    states = {line['state'] for line in lines}
    return {
        'state': next((state for state in (FAILED, WARNING, PENDING) if state in states), OK),
        'at': _now(),
        'probed_at': (context['probe'] or {}).get('at'),
        'probing': context['probing'],
        'failing': [line['id'] for line in lines if line['state'] in (FAILED, WARNING)],
        'lines': lines,
    }


def _run(wait, refresh):
    context = _context(wait, refresh)
    result = _summary(_lines(context), context)
    _note(result)
    return result, context


def run(wait=PROBE_WAIT, refresh=False):
    """The self-test: {'state', 'at', 'probed_at', 'probing', 'failing', 'lines'}.

    `state` is the worst state of a line (neutral counts as ok), `failing` the
    ids of the lines that are warning or failed, `lines` one {'id', 'state',
    'reason', 'action', 'details'} per id of LINES, in that order. What needs
    the server is waited for `wait` seconds at most; `refresh` asks for a new
    look at the server even when the last one is recent.
    """
    return _run(wait, refresh)[0]


# What was last written to the box log and to the state file, per line.
_logged = {}
_kept = {'signature': None}
_keep_lock = threading.Lock()
_keep_wanted = threading.Event()


def _note(result):
    """Write to the box log each line that turned failed, and each that is well again; keep the result."""
    for line in result['lines']:
        identifier, state = line['id'], line['state']
        if state == PENDING:
            continue
        before = _logged.get(identifier)
        if state == FAILED and before != line['reason']:
            _logger.warning(
                'Self-test: %s failed (%s)%s', identifier, line['reason'],
                f" - to do: {line['action']}" if line['action'] else '')
            _logged[identifier] = line['reason']
        elif state != FAILED and before:
            _logger.info('Self-test: %s is %s again', identifier, state)
            _logged.pop(identifier, None)
    signature = [(line['id'], line['state'], line['reason']) for line in result['lines'] if line['state'] != PENDING]
    with _keep_lock:
        if signature == _kept['signature']:
            return
        _kept.update(signature=signature, record={
            'at': result['at'], 'state': result['state'],
            'lines': [{'id': identifier, 'state': state, 'reason': reason} for identifier, state, reason in signature],
        })
    _keep_wanted.set()


def _keep_last_result():
    """Write the latest result to the state file, apart from any request: the card may be busy for minutes."""
    while True:
        _keep_wanted.wait()
        _keep_wanted.clear()
        with _keep_lock:
            record = _kept.get('record')
        try:
            update.change_state(lambda state: state.__setitem__(STATE_KEY, record))
        except OSError as error:
            _logger.info('The last self-test could not be kept (%s)', type(error).__name__)


def last_kept():
    """The last self-test the box kept in its state file - {'at', 'state', 'lines'} - or None."""
    record = update.read_state().get(STATE_KEY)
    return record if isinstance(record, dict) else None


def _watch():
    """Set the clock of a paired box from its server while no time server keeps it; run the self-test regularly."""
    started = time.monotonic()
    clock_done = False
    next_run = started + FIRST_RUN
    while True:
        now = time.monotonic()
        if not clock_done and now - started < CLOCK_WATCH:
            try:
                clock_done = _clock_at_start()
            except Exception:
                _logger.exception('The clock of the box could not be looked at')
        if now >= next_run:
            next_run = now + PERIOD
            try:
                run(wait=CONNECT_TIMEOUT * 6)
            except Exception:
                _logger.exception('The self-test could not run')
        time.sleep(CLOCK_PERIOD if not clock_done and now - started < CLOCK_WATCH else min(PERIOD, 60))


# ------------------------------------------------------------------------------------------------ page data

def status(wait=PROBE_WAIT, refresh=False):
    """What the page of the box shows, under Server, Network, Devices, Software and Advanced, with the self-test.

    Keys: 'selftest' (as `run` gives it), 'server', 'network', 'devices',
    'software', 'advanced', 'layer', 'drivers'.
    """
    result, context = _run(wait, refresh)
    software = context['software']
    manager = core('main').manager
    return {
        'selftest': result,
        'server': {
            'address': context['server'],
            'paired': context['paired'],
            'locked': lock.is_locked(),
            'registration': reporting.outcome(),
            'channel': reporting.channel(),
        },
        'network': context['network'],
        'devices': context['devices'],
        'software': dict(
            software, update_state=update_state(software), candidate_banner=software['candidate_box']),
        'advanced': {
            'certificate': trust.box_certificate(),
            'viindoo_name': viindoo_name(),
            'box_address': dict(browser_address(), own_name=_conf(trust.BOX_ADDRESS_KEY)),
            'trusted_certificates': trust.trusted_certificates(),
            'identity': {
                'hostname': manager.hostname,
                'mac_address': manager.mac_address,
                'serial_number': manager.serial_number,
                'version': manager.version,
            },
        },
        'layer': context['integrity'],
        'drivers': handler_files(),
    }


# ------------------------------------------------------------------------------------------------ diagnostic file

def _kept_wifi():
    """The Wi-Fi network the box keeps: {'card_copy': bool or None, 'profile_network': name or None}.

    `card_copy` is whether the copy of the profile that survives a restart
    exists (None when that cannot be told); `profile_network` is the name held
    by the profile now in memory, which outlives a forgotten network until the
    box restarts. Only the name is read, never the password.
    """
    try:
        copies = [subprocess.run(['sudo', '-n', 'test', '-e', str(path)], capture_output=True, check=False,
                                 timeout=10).returncode for path in wifi.KEPT_FILES]
        card_copy = True if 0 in copies else False if set(copies) == {1} else None
    except (OSError, subprocess.SubprocessError):
        card_copy = None
    network = None
    try:
        # nmcli writes a name in the character set of its locale, `?` for a character that set lacks: a locale
        # every system has keeps the name whole whatever locale the box's settings name.
        done = subprocess.run(['nmcli', '-t', '-g', '802-11-wireless.ssid', 'connection', 'show', 'id',
                               wifi.PROFILE], capture_output=True, env=dict(os.environ, LC_ALL='C.UTF-8'),
                              check=False, timeout=10)
        if not done.returncode:
            output = done.stdout.decode(errors='replace').rstrip('\n')
            network = ':'.join(wifi._terse_fields(output)) or None
    except (OSError, subprocess.SubprocessError):
        pass
    return {'card_copy': card_copy, 'profile_network': network}


def _configuration():
    configuration = _helpers().get_conf()
    shown = {}
    for section, keys in SHOWN_KEYS.items():
        if not configuration.has_section(section):
            continue
        options = configuration.options(section)
        shown[section] = {
            'values': {key: configuration.get(section, key) for key in keys if key in options},
            'other_keys': sorted(key for key in options if key not in keys),
        }
    shown['kept_wifi'] = _kept_wifi()
    return shown


def _log_tail():
    path = Path(tools.config['logfile'] or '/var/log/odoo/odoo-server.log')
    try:
        with path.open('rb') as log:
            log.seek(0, 2)
            size = log.tell()
            log.seek(max(0, size - LOG_TAIL_BYTES))
            data = log.read().decode(errors='replace')
    except OSError as error:
        return [f'(the log cannot be read: {type(error).__name__})']
    lines = data.split('\n')
    if size > LOG_TAIL_BYTES:
        lines = lines[1:]
    return _PRIVATE_KEY.sub(HIDDEN, '\n'.join(lines[-LOG_TAIL_LINES:])).split('\n')


def _secrets():
    """What must never leave the box in a diagnostic file, as text."""
    helpers = _helpers()
    token = helpers.get_token() or ''
    found = [token, lock.settings_code(token) if token else '']
    configuration = helpers.get_conf()
    for section in configuration.sections():
        for key in configuration.options(section):
            if any(word in key for word in ('password', 'passwd', 'token', 'secret', 'code', 'key', 'uuid')):
                found.append(configuration.get(section, key) or '')
    return sorted({value for value in found if len(value) >= 4}, key=len, reverse=True)


def diagnostic():
    """The diagnostic file: what support needs to read the box without a shell, as a JSON text, and its file name.

    The self-test, the page data, the integrity of the layer, the release
    check, the configuration but its secrets, the network and the end of the
    box log. Registration and live channel moments come both as seconds since
    1970 on the clock of the box and as ages on its monotonic clock, with the
    box time at writing and the state of the clock line: the ages stay right
    when the clock was corrected after the moment, the seconds may not. No credential, settings code,
    password or private key: every value the configuration keeps under such a key is replaced
    wherever it appears.
    """
    page = status(wait=CONNECT_TIMEOUT * 3)
    helpers = _helpers()
    content = {
        'generated_at': _now(),
        'status': page,
        'probe': _probes.get(page['server']['address'], 0)[0] if page['server']['address'] else None,
        'last_kept_selftest': last_kept(),
        'configuration': _configuration(),
        'release_check': update.read_state().get('last_check'),
        'clock': {
            'synchronised': clock_synchronised(), 'box_time': _now(), 'box_epoch': time.time(),
            'line': next(({'state': line['state'], 'reason': line['reason']}
                          for line in page['selftest']['lines'] if line['id'] == 'clock'), None),
        },
        'moments': {
            'registration': reporting.outcome(),
            'live_channel': reporting.channel(),
        },
        'log': _log_tail(),
    }
    text = json.dumps(content, indent=1, sort_keys=True, default=str)
    for secret in _secrets():
        text = text.replace(json.dumps(secret)[1:-1], HIDDEN).replace(secret, HIDDEN)
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    name = re.sub(r'[^A-Za-z0-9-]', '-', helpers.get_hostname())[:40]
    return text, f'viindoo-iot-diagnostic-{name}-{stamp}.json'


threading.Thread(target=_keep_last_result, name='ViinSelfTestKeeper', daemon=True).start()
threading.Thread(target=_watch, name='ViinSelfTestWatch', daemon=True).start()
