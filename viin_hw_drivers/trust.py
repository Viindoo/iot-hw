"""Trust between the box and the outside.

The certificates the installer chose to trust for the server of the box, the
verified connections built on them, and the certificate the box itself
presents to browsers.
"""
import contextlib
import datetime
import functools
import hashlib
import hmac
import http.client
import ipaddress
import logging
import os
import re
import secrets
import signal
import socket
import ssl
import stat
import subprocess
import tempfile
import threading
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import certifi
import netifaces
import urllib3
from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .seams import core

_logger = logging.getLogger(__name__)

# The authorities every browser trusts: all that vouches for a host that is not the server of the box.
PUBLIC_AUTHORITIES = certifi.where()

PINS_KEY = 'viin_server_pins'
PINS_FILE = 'viin-server-pins.pem'
SERVER_KEY = 'remote_server'
BOX_ADDRESS_KEY = 'viin_box_address'
# The box's own choice to update by itself; a change of server leaves it as it is.
AUTO_UPDATE_KEY = 'viin_auto_update'
# Stored by the client of the Viindoo name, and only once the box has checked that the name leads to itself.
MANAGED_NAME_KEY = 'viin_managed_name'
TIMEOUT = 10
# How long a call to a server may wait for its answer when its caller says nothing of it.
READ_TIMEOUT = 15

# /etc lives in memory on a box: the second path of each pair is the same file on the card.
CERTIFICATE_FILES = ('/etc/ssl/certs/nginx-cert.crt', '/root_bypass_ramdisks/etc/ssl/certs/nginx-cert.crt')
KEY_FILES = ('/etc/ssl/private/nginx-cert.key', '/root_bypass_ramdisks/etc/ssl/private/nginx-cert.key')
# What a system file is written under until it is whole, then renamed from.
STAGED = '.viin-new'
# The pair of the Viindoo name: one directory per place, a generation directory inside it for each pair, and
# `current`, the link to the generation in use. /etc lives in memory on a box: the second directory is the card's.
NAME_PAIR_DIRECTORIES = ('/etc/ssl/private/viindoo-name', '/root_bypass_ramdisks/etc/ssl/private/viindoo-name')
CURRENT = 'current'
GENERATION_PREFIX = 'g-'
SHIPPED_NAME = 'OdooTempIoTBoxCertificate'
BOX_NAME = 'Viindoo IoT Box'
# Holds the fingerprint of the last certificate the box made in place of an earlier one it had made itself.
RENEWAL_FILE = 'viin-box-certificate-renewal'
LIFETIME = datetime.timedelta(days=820)
RENEW_BEFORE = datetime.timedelta(days=60)
RENEWAL_CHECK_PERIOD = 24 * 3600
# How long before it is made that a certificate of the box starts being valid: browsers whose clock is a little
# behind accept it from the moment it exists.
BACKDATE = datetime.timedelta(days=1)
# How often the keeper looks at the addresses of the box, to make a certificate for a new one soon after it appears.
ADDRESS_LOOK_PERIOD = 15
# How long the keeper waits to try again after a look that failed.
FAILED_LOOK_WAIT = 300
# How many characters of the text of a failure each one-line repeat of it carries: the diagnostic file keeps the end
# of the log only, which may no longer hold the full record of a failure that comes back look after look.
REPEAT_TEXT_LIMIT = 300
WEB_SERVER_RESTARTS = 3
# Makes the running web server read its certificate again; fails when no web server runs.
WEB_SERVER_RELOAD = ('sudo', '-n', 'nginx', '-s', 'reload')
# Where the master process of the web server writes its process number: `pid` of the nginx.conf of the image.
WEB_SERVER_PID_FILE = '/run/nginx.pid'
# How long a reload of the web server waits, at the most, for the web server to read its files.
WEB_SERVER_RELOAD_WAIT = 10
# How many looks in a row, LOOK_INTERVAL seconds apart, must find the master process of the web server at rest.
CALM_LOOKS = 3
LOOK_INTERVAL = 0.005
# How long the keeper waits, at the most, for core to start the web server before its first look.
WEB_SERVER_START_WAIT = 60
_RELOAD_SIGNAL = 1 << (signal.SIGHUP - 1)

_GENERATION = re.compile(r'g-[0-9a-f]{16}')
_PRIVATE_KEY_BLOCK = re.compile(rb'-----BEGIN [A-Z ]*PRIVATE KEY-----')
_ONE_PRIVATE_KEY = re.compile(rb'\s*-----BEGIN PRIVATE KEY-----[A-Za-z0-9+/=\s]+-----END PRIVATE KEY-----\s*')
_PEM_CERTIFICATE = re.compile(rb'-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----', re.DOTALL)
# The line of the trust file that names the server its certificates were accepted for.
_SERVER_LINE = re.compile(rb'^server: (\S+)$', re.MULTILINE)
_HOST_NAME = re.compile(r'(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$')
# Why OpenSSL refused a certificate, by its verification code.
_PROBLEMS = {
    9: 'not_yet_valid',
    10: 'expired',
    18: 'unknown_authority',
    19: 'unknown_authority',
    20: 'unknown_authority',
    21: 'unknown_authority',
    62: 'name_mismatch',
    64: 'name_mismatch',
}

_bundle = {}
_bundle_lock = threading.Lock()
_certificate_listeners = []
# Whether the last look at the certificate of the box failed, and whether a certificate the box made is still waiting
# for the web server to present it.
_look = {'failed': False, 'unserved': False}
# Held by every change of a pair the web server reads, and by every reload of the web server until it has read its
# files: the web server never reads the key of one pair beside the certificate of another.
_changing = threading.RLock()
_pause = threading.Event()
_warned = {}
# The failure that last ended a look of the keeper or of the watcher of the addresses, logged in full; None once a
# look succeeds.
_failure_logged = {'last': None}


def _exclusive(function):
    """Run `function` with no other change of a pair the web server reads, and no reload of it, under way: the
    keeper, the watcher of the addresses, a request of the customer and the client of the Viindoo name take turns."""
    @functools.wraps(function)
    def exclusive(*arguments, **options):
        with _changing:
            return function(*arguments, **options)

    return exclusive


def utc_now():
    """Now, as the naive UTC time certificates carry their dates in."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def iso_time(moment):
    return moment.replace(microsecond=0).isoformat() + 'Z'


def _pem(certificate):
    return certificate.public_bytes(serialization.Encoding.PEM)


def load_certificates(data):
    """The certificates of a PEM text, in its order; what is not a certificate is left out."""
    certificates = []
    for block in _PEM_CERTIFICATE.findall(data):
        with contextlib.suppress(ValueError):
            certificates.append(x509.load_pem_x509_certificate(block))
    return certificates


def fingerprint(certificate):
    return ':'.join(f'{byte:02X}' for byte in certificate.fingerprint(hashes.SHA256()))


def same_fingerprint(typed, certificate):
    """Whether a fingerprint a person sent back, in any usual spelling, is the one of `certificate`."""
    typed = re.sub(r'[^0-9a-f]', '', str(typed or '').lower())
    return hmac.compare_digest(typed, certificate.fingerprint(hashes.SHA256()).hex())


def names(certificate):
    """The host names and addresses a certificate is issued for."""
    try:
        alternative = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return []
    return [
        *alternative.get_values_for_type(x509.DNSName),
        *(str(address) for address in alternative.get_values_for_type(x509.IPAddress)),
    ]


def describe(certificate):
    """What a person needs to recognise a certificate: fingerprint (SHA-256), subject, expiry (UTC), names."""
    return {
        'fingerprint': fingerprint(certificate),
        'subject': certificate.subject.rfc4514_string(),
        'expiry': iso_time(certificate.not_valid_after),
        'names': names(certificate),
    }


def _warn_once(subject, message, *arguments):
    """Log a warning about `subject` unless the last one about it said the same."""
    said = message % arguments
    if _warned.get(subject) != said:
        _warned[subject] = said
        _logger.warning(said)


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


def _is_server(scheme, host, port):
    """Whether a connection goes to the server this box has."""
    server = _origin(core('tools.helpers').get_conf(SERVER_KEY))
    return bool(server) and server == (scheme, str(host).lower().strip('[]'), port)


def _pins_path(helpers):
    return Path(helpers.get_conf(PINS_KEY) or helpers.path_file(PINS_FILE))


def _stored(helpers):
    """(address of the server they were accepted for, certificates) of the trust file; (None, []) without one."""
    try:
        data = _pins_path(helpers).read_bytes()
    except OSError as error:
        if helpers.get_conf(PINS_KEY) or not isinstance(error, FileNotFoundError):
            _warn_once(
                'trust file',
                'The certificates trusted for the server of this box cannot be read (%s): the box accepts '
                'public certificates only', type(error).__name__)
        return None, []
    _warned.pop('trust file', None)
    server = _SERVER_LINE.search(data)
    return server and server.group(1).decode(errors='replace'), load_certificates(data)


def _pins():
    """The certificates trusted for the server the box has now: none once it has another."""
    helpers = core('tools.helpers')
    server, certificates = _stored(helpers)
    current = _origin(helpers.get_conf(SERVER_KEY))
    return certificates if current and _origin(server) == current else []


def trusted_certificates():
    """The certificates the installer trusted for the server this box has, each as `describe` gives it."""
    return [describe(certificate) for certificate in _pins()]


def _write_whole(path, content):
    """Give `path` all of `content`, or leave it as it was: never a part, whatever interrupts the writing."""
    temporary = path.with_name(path.name + STAGED)
    with open(temporary, 'wb') as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _store_pins(server, certificates):
    helpers = core('tools.helpers')
    if not certificates or not _origin(server):
        forget_certificates(helpers)
        return
    known_server, known = _stored(helpers)
    unchanged = list(map(fingerprint, known)) == list(map(fingerprint, certificates))
    if unchanged and _origin(known_server) == _origin(server):
        return
    path = _pins_path(helpers)
    with helpers.writable():
        _write_whole(path, b'server: %s\n' % server.encode() + b''.join(map(_pem, certificates)))
    if helpers.get_conf(PINS_KEY) != str(path):
        helpers.update_conf({PINS_KEY: str(path)})


def trust_certificate(certificate):
    """Trust one more certificate (an object, or PEM or DER bytes) for the server this box has.

    It then vouches for the connections of the box to that server, and for no
    other host. Returns its fingerprint; ValueError when the box has no server.
    """
    helpers = core('tools.helpers')
    server = helpers.get_conf(SERVER_KEY)
    if not _origin(server):
        raise ValueError('this box has no server to trust a certificate for')
    if not isinstance(certificate, x509.Certificate):
        found = load_certificates(certificate)
        certificate = found[0] if found else x509.load_der_x509_certificate(certificate)
    known = _pins()
    if all(fingerprint(other) != fingerprint(certificate) for other in known):
        _store_pins(server, [*known, certificate])
    return fingerprint(certificate)


@contextlib.contextmanager
def replacing_certificates(server, certificates):
    """Trust `certificates`, and nothing else, for `server`, around the block that makes it the server of this box.

    `server` is the address the block stores. When the store or the block
    fails, the box trusts again what it trusted before, for the server it had.
    """
    known = _stored(core('tools.helpers'))
    try:
        _store_pins(server, certificates)
        yield
    except Exception:
        try:
            _store_pins(*known)
        except Exception as error:
            # Its traceback would show the failure it follows, whose message may quote what the block was storing.
            _logger.error('The certificates this box trusted could not be put back (%s)', type(error).__name__)
        raise


def forget_certificates(helpers=None):
    """Trust public authorities only again. `helpers` is core's helper module when core is not loaded yet."""
    helpers = helpers or core('tools.helpers')
    helpers.unlink_file(str(_pins_path(helpers)))
    if helpers.get_conf(PINS_KEY):
        helpers.update_conf({PINS_KEY: ''})


def _private_directory():
    directory = Path(tempfile.gettempdir(), f'viin-trust-{os.getuid()}')
    with contextlib.suppress(FileExistsError):
        os.mkdir(directory, 0o700)
    status = os.lstat(directory)
    # A directory someone else prepared under that name could feed the box authorities of their own.
    if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid() or status.st_mode & 0o077:
        return Path(tempfile.mkdtemp(prefix='viin-trust-'))
    return directory


def _write_bundle(content):
    directory = _private_directory()
    handle, temporary = tempfile.mkstemp(dir=directory)
    with os.fdopen(handle, 'wb') as bundle:
        bundle.write(content)
    path = directory / 'ca-bundle.pem'
    os.replace(temporary, path)
    return str(path)


def ca_bundle_path():
    """Path of a PEM file with the public authorities and the certificates trusted for the server of this box.

    For the connections of the box to that server and no other: `authorities_for`
    tells them apart. It is the file of the public authorities itself as long as
    nothing else is trusted, and a file in memory-backed storage otherwise;
    OSError when that file cannot be written.
    """
    pins = b''.join(map(_pem, _pins()))
    if not pins:
        return PUBLIC_AUTHORITIES
    public = os.stat(PUBLIC_AUTHORITIES)
    stamp = (hashlib.sha256(pins).hexdigest(), PUBLIC_AUTHORITIES, public.st_mtime_ns, public.st_size)
    with _bundle_lock:
        if _bundle.get('stamp') != stamp or not os.path.exists(_bundle['path']):
            _bundle.update(stamp=stamp, path=_write_bundle(Path(PUBLIC_AUTHORITIES).read_bytes() + b'\n' + pins))
        return _bundle['path']


def _authorities(scheme, host, port):
    return ca_bundle_path() if _is_server(scheme, host, port) else PUBLIC_AUTHORITIES


def authorities_for(url):
    """Path of the PEM file that verifies the https server at `url`.

    `ca_bundle_path()` when `url` is on the server of this box, the public
    authorities for any other host: a certificate the installer trusted never
    vouches for another host.
    """
    return _authorities(*(_origin(url) or (None, '', None)))


def ssl_context(candidates=(), store=True):
    """A client context for a connection to the server of this box, and to no other host.

    It verifies the server, name included, against the public authorities,
    `candidates` and, unless `store` is false, the certificates trusted for it.
    """
    context = ssl.create_default_context(cafile=PUBLIC_AUTHORITIES)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    extra = [*(_pins() if store else ()), *candidates]
    if extra:
        context.load_verify_locations(cadata=b''.join(map(_pem, extra)).decode())
    return context


class _VerifyingPoolManager(urllib3.PoolManager):
    """Verifies every https server it calls, whatever its caller asked for.

    The authorities are chosen when a connection is opened, for each host a
    request or a redirection leads to: a trust store that cannot be used fails
    that request, and nothing is sent unverified. A server that does not answer
    does not hold its caller for ever: connecting and each wait for an answer
    end after TIMEOUT and READ_TIMEOUT seconds, unless the caller names a
    timeout, for the manager or for a request.
    """

    def __init__(self, *arguments, **options):
        options = {'timeout': urllib3.Timeout(connect=TIMEOUT, read=READ_TIMEOUT), **options}
        super().__init__(*arguments, **dict(options, cert_reqs='CERT_REQUIRED', ca_certs=PUBLIC_AUTHORITIES))

    def connection_from_host(self, host, port=None, scheme='http', pool_kwargs=None):
        if scheme == 'https':
            pool_kwargs = dict(
                pool_kwargs or {}, cert_reqs='CERT_REQUIRED', ca_certs=_authorities(scheme, host, port or 443))
        return super().connection_from_host(host, port=port, scheme=scheme, pool_kwargs=pool_kwargs)


def pool_manager(*arguments, **options):
    """A urllib3 pool manager for calls to the server of this box; its arguments are those of `urllib3.PoolManager`.

    Each https connection is verified: the server of the box against the public
    authorities and the certificates trusted for it, any other host against the
    public authorities alone. A request fails with OSError when the trusted
    certificates cannot be used.
    """
    return _VerifyingPoolManager(*arguments, **options)


def verification_problem(host, port, candidates=(), store=True):
    """Why the certificate of a server is refused, '' when it is accepted. Nothing is sent to the server.

    One of not_yet_valid, expired, unknown_authority, name_mismatch, invalid.
    A server that cannot be reached raises OSError. `candidates` are those of
    `ssl_context`; the trusted certificates count for the server of this box
    only, and not at all when `store` is false.
    """
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT) as plain:
            context = ssl_context(candidates, store and _is_server('https', host, port))
            with context.wrap_socket(plain, server_hostname=host):
                return ''
    except ssl.SSLCertVerificationError as error:
        return _PROBLEMS.get(error.verify_code, 'invalid')


def _unverified_context():
    """Accepts any certificate: for reading what a server shows, never for sending what must stay secret."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def presented_certificate(host, port):
    """The certificate a server presents, read without verifying it and without sending a request."""
    with socket.create_connection((host, port), timeout=TIMEOUT) as plain:
        with _unverified_context().wrap_socket(plain, server_hostname=host) as secured:
            return x509.load_der_x509_certificate(secured.getpeercert(binary_form=True))


def unverified_server_time(host, port):
    """The clock of an https server (UTC), or None. Anyone on the way can forge it: show it, never rely on it."""
    connection = http.client.HTTPSConnection(host, port, timeout=TIMEOUT, context=_unverified_context())
    try:
        connection.request('HEAD', '/')
        moment = parsedate_to_datetime(connection.getresponse().headers.get('Date'))
    except (OSError, http.client.HTTPException, TypeError, ValueError):
        return None
    finally:
        connection.close()
    if moment.tzinfo:
        moment = moment.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return moment


def on_certificate_change(callback):
    """Call `callback()` each time the web server of the box gets another certificate."""
    _certificate_listeners.append(callback)


def _web_server_at_rest():
    """Whether the master process of the web server sleeps with no reload signal pending; True when none runs.

    The master reads its files between the moment it takes the signal and its next sleep. Only /proc is read,
    which every user may: no command runs.
    """
    try:
        pid = int(Path(WEB_SERVER_PID_FILE).read_text())
        state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
        status = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines()
                      if ':' in line)
        pending = int(status['SigPnd'], 16) | int(status['ShdPnd'], 16)
    except (OSError, ValueError, IndexError, KeyError):
        return True
    return state == 'S' and not pending & _RELOAD_SIGNAL


def wait_until_web_server_reloaded(timeout=WEB_SERVER_RELOAD_WAIT):
    """Return once the web server has read its files for every reload it was asked for: True then, and when no
    web server runs; False when `timeout` seconds went by first. Never raises.

    A reload command returns as soon as its signal is sent, and the web server reads the files later: a change
    of them in between could have it read the key of one pair beside the certificate of another. The web server
    is taken to have read them once its master process is found at rest CALM_LOOKS times in a row.
    """
    deadline = time.monotonic() + timeout
    calm = 0
    while True:
        calm = calm + 1 if _web_server_at_rest() else 0
        if calm >= CALM_LOOKS:
            return True
        if time.monotonic() >= deadline:
            return False
        _pause.wait(LOOK_INTERVAL)


def _uptime():
    """Seconds since the machine started, None when they cannot be read."""
    try:
        return float(Path('/proc/uptime').read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _web_server_master():
    """The process number of the master process of the web server, None when none runs.

    A number its file names that a stopped web server left behind, and another process took since, is not one.
    """
    try:
        pid = int(Path(WEB_SERVER_PID_FILE).read_text())
        name = Path(f'/proc/{pid}/comm').read_text().strip()
        state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except (OSError, ValueError, IndexError):
        return None
    return pid if name.startswith('nginx') and state not in ('Z', 'X') else None


class WebServerRefused(subprocess.SubprocessError):
    """The web server refused to read its files again; a web server that runs keeps serving those it read before.

    Its text is the exit status of the reload and the first line the web server wrote of it, which names files and
    never what they hold; a line of sudo about itself is passed over when the web server wrote one too.
    """

    def __init__(self, reload):
        lines = [line.strip() for line in (reload.stderr or b'').decode(errors='replace').splitlines() if line.strip()]
        said = [line for line in lines if not line.startswith('sudo:')] or lines
        super().__init__(f'the web server refused to read its files again (reload exit status {reload.returncode}'
                         f'{": " + said[0] if said else ""})')


def _web_server_started_after(uptime):
    """Whether the master process of the web server started after `uptime`, as `_uptime` gives it.

    A master process writes its process number once it has read its files.
    """
    try:
        pid = int(Path(WEB_SERVER_PID_FILE).read_text())
        started = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
        return int(started) / os.sysconf('SC_CLK_TCK') > uptime
    except (OSError, ValueError, IndexError):
        return False


@_exclusive
def _reload_web_server():
    for attempt in range(WEB_SERVER_RESTARTS):
        master = _web_server_master()
        # A reload keeps the web server serving throughout and lets it end the requests in flight, the one that
        # brought this certificate among them. A restart lets them end too, for a few seconds at the most, but
        # between its stop and its start nothing listens and no connection is accepted: it is kept for a web
        # server that does not run. A running web server refuses the reload when it cannot use the files: it keeps
        # serving those it read before, and the refusal is raised, as a restart would stop it and then fail to start
        # on the same files.
        reload = subprocess.run(WEB_SERVER_RELOAD, capture_output=True, check=False, timeout=30)
        if not reload.returncode:
            break
        running = _web_server_master()
        if running is not None and running == master:
            raise WebServerRefused(reload)
        if running is not None:
            # A restart by core started this web server after the reload was asked for: it is asked again.
            continue
        try:
            core('tools.helpers').start_nginx_server()
            break
        except subprocess.CalledProcessError:
            # Core restarts the web server too, and two restarts at the same moment fail one of them.
            if attempt == WEB_SERVER_RESTARTS - 1:
                raise
            time.sleep(2)
    else:
        raise WebServerRefused(reload)
    if not wait_until_web_server_reloaded():
        _logger.warning('The web server of the box took more than %s seconds to read its certificate files again',
                        WEB_SERVER_RELOAD_WAIT)
    # Whatever asked for it, the web server has now read the files as they are, or is reading them past that wait.
    _look['unserved'] = False


def serve_new_certificate():
    """Have the web server of the box read its certificate files again, then call the listeners of
    `on_certificate_change`. Raises OSError or subprocess.SubprocessError when the web server cannot be made to:
    WebServerRefused when it runs and refuses them, and then keeps serving those it read before."""
    _reload_web_server()
    for callback in _certificate_listeners:
        try:
            callback()
        except Exception:
            _logger.exception('A listener of the box certificate failed')


def _as_root(*command, data=None):
    return subprocess.run(['sudo', '-n', *command], input=data, capture_output=True, check=True, timeout=30)


def _read_system_file(path):
    try:
        return Path(path).read_bytes()
    except PermissionError:
        return _as_root('cat', path).stdout


def _distinct(paths):
    """`paths` without those that lead to a file an earlier one leads to already."""
    found = {}
    for path in paths:
        found.setdefault(os.path.realpath(path), str(path))
    return list(found.values())


def _stage(path, content, mode):
    _as_root('install', '-m', mode, '/dev/stdin', path + STAGED, data=content)


def _commit(path):
    _as_root('mv', '-f', path + STAGED, path)


def _discard_staged(paths):
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        _as_root('rm', '-f', '--', *(path + STAGED for path in paths))


def _directories(paths):
    return sorted({os.path.dirname(path) for path in paths})


def write_system_files(paths, content, mode):
    """Write `content` (bytes) as root to each of `paths`, with the permissions `mode` ('600', '644').

    For the files the box service has no right on. /etc lives in memory on a
    box, so a file that must outlast a restart is named twice: under /etc and
    under the path that leads to the card. Whatever interrupts the writing, a
    power cut included, each file holds what it held before or all of `content`.
    Raises OSError or subprocess.SubprocessError when a file could not be written.
    """
    paths = _distinct(paths)
    with core('tools.helpers').writable():
        try:
            for path in paths:
                _stage(path, content, mode)
            _as_root('sync', *(path + STAGED for path in paths))
            for path in paths:
                _commit(path)
            _as_root('sync', *_directories(paths))
        except BaseException:
            _discard_staged(paths)
            raise


def remove_system_files(paths):
    """Delete each of `paths` as root, under /etc and on the card alike; a file that does not exist is no error."""
    with core('tools.helpers').writable():
        _as_root('rm', '-f', '--', *_distinct(paths))


class Unreadable(OSError):
    """A file or link could not be read as root, which is not the same as there being none."""


# Run as root by `sh`: the command named after the file test and the path runs only when the test holds for the
# path; when it does not the status is ABSENT, which no command of the box ends with.
_IF_THERE = '[ "$1" "$2" ] || exit 3; shift 2; exec "$@"'
ABSENT = 3


def _root_output(test, path, *command):
    """The standard output of `command` run as root when `path` passes the file test `test` ('-e', '-L', '-d');
    None when it does not; raises Unreadable when the command fails or cannot be run.

    A command that is refused, ends in an error or takes too long is never taken for an absent file.
    """
    try:
        completed = subprocess.run(['sudo', '-n', 'sh', '-c', _IF_THERE, 'sh', test, path, *command],
                                   capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise Unreadable('the command could not be run as root') from None
    if completed.returncode == ABSENT:
        return None
    if completed.returncode:
        raise Unreadable('the command failed as root')
    return completed.stdout


def read_as_root(path):
    """The bytes of a file read as root; None when the file does not exist; Unreadable when it could not be read."""
    return _root_output('-e', path, 'cat', '--', path)


def _generation_of(directory):
    """The generation `current` of `directory` names, None when it names none or the directory has no `current`.

    Raises Unreadable when the link could not be read.
    """
    link = f'{directory}/{CURRENT}'
    target = _root_output('-L', link, 'readlink', '--', link)
    name = target.decode(errors='replace').strip() if target else ''
    return name if _GENERATION.fullmatch(name) else None


def _point(directory, generation):
    """Make `current` of `directory` name `generation` by one rename; None removes `current`."""
    new_link = f'{directory}/{CURRENT}{STAGED}'
    if generation is None:
        _as_root('rm', '-f', '--', f'{directory}/{CURRENT}', new_link)
        return
    _as_root('ln', '-sfn', generation, new_link)
    _as_root('mv', '-T', '-f', new_link, f'{directory}/{CURRENT}')


def _one_private_key(key_pem):
    if not _ONE_PRIVATE_KEY.fullmatch(key_pem):
        return False
    try:
        serialization.load_pem_private_key(key_pem, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        return False
    return True


@_exclusive
def replace_pair(directories, certificate_pem, key_pem):
    """Make `certificate_pem` and `key_pem` the pair `current` names in each of `directories`; return the
    generation `current` named before in the first of them, None when it named none.

    The pair is written whole in a new generation directory of each place and
    takes its place by one rename of the `current` link, on the card first, so
    whatever interrupts it - a power cut included - `current` names the previous
    pair or the new one, never a key with the certificate of another. When a
    step fails, every place is put back and the new generation removed.
    The certificate is written as `fullchain.pem`, readable by all; the key as
    `privkey.pem`, readable by root alone, in a directory of root alone.
    Raises ValueError, writing nothing, when the certificate text holds a private
    key or the key text is not exactly one private key; OSError or
    subprocess.SubprocessError when a step could not be done.
    """
    certificate_pem = certificate_pem.encode() if isinstance(certificate_pem, str) else bytes(certificate_pem)
    key_pem = key_pem.encode() if isinstance(key_pem, str) else bytes(key_pem)
    if _PRIVATE_KEY_BLOCK.search(certificate_pem):
        raise ValueError('The certificate text holds a private key')
    if not _one_private_key(key_pem):
        raise ValueError('The key text is not exactly one private key')
    places = _distinct(directories)
    generation = GENERATION_PREFIX + secrets.token_hex(8)
    previous = {place: _generation_of(place) for place in places}
    switching = []
    with core('tools.helpers').writable():
        try:
            for place in places:
                _as_root('install', '-d', '-m', '700', place)
                _as_root('install', '-d', '-m', '700', f'{place}/{generation}')
                _as_root('install', '-m', '644', '/dev/stdin', f'{place}/{generation}/fullchain.pem',
                         data=certificate_pem)
                _as_root('install', '-m', '600', '/dev/stdin', f'{place}/{generation}/privkey.pem', data=key_pem)
            _as_root('sync', *(f'{place}/{generation}/{name}' for place in places
                               for name in ('fullchain.pem', 'privkey.pem')))
            for place in reversed(places):
                switching.append(place)
                _point(place, generation)
            _as_root('sync', *places)
        except BaseException:
            for place in switching:
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    _point(place, previous[place])
            for place in places:
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    _as_root('rm', '-rf', '--', f'{place}/{generation}', f'{place}/{CURRENT}{STAGED}')
            raise
    return previous[places[0]] if places else None


@_exclusive
def restore_pair(directories, generation):
    """Make `current` of each of `directories` name `generation` again (None removes `current`), the card first."""
    if generation is not None and not _GENERATION.fullmatch(generation):
        raise ValueError('Not the name of a generation')
    places = _distinct(directories)
    with core('tools.helpers').writable():
        for place in reversed(places):
            _point(place, generation)
        _as_root('sync', *places)


def prune_pairs(directories):
    """Remove from each of `directories` every generation `current` does not name, and any `current.viin-new`:
    what a change that stopped left behind.

    A place whose `current` could not be read, or exists without naming a generation, is left as it is: what it
    holds may be the pair in use.
    """
    leftovers = []
    for place in _distinct(directories):
        try:
            listing = _root_output('-d', place, 'ls', '-A', '-1', '--', place)
            if listing is None:
                continue
            kept = _generation_of(place)
        except Unreadable:
            _warn_once(('prune', place), 'The old files of the Viindoo name could not be listed: they are kept')
            continue
        names_there = listing.decode(errors='replace').splitlines()
        if kept is None and CURRENT in names_there:
            _warn_once(('prune', place), 'The Viindoo name has no pair in use that could be read: its old files '
                       'are kept')
            continue
        _warned.pop(('prune', place), None)
        for name in names_there:
            if (name.startswith(GENERATION_PREFIX) and name != kept) or name == CURRENT + STAGED:
                leftovers.append(f'{place}/{name}')
    if leftovers:
        with core('tools.helpers').writable():
            _as_root('rm', '-rf', '--', *leftovers)


def remove_pair(directories):
    """Delete each of `directories` with all it holds; for the directories of the pair of the Viindoo name only."""
    with core('tools.helpers').writable():
        _as_root('rm', '-rf', '--', *_distinct(directories))


def _places():
    """[(certificate file, key file)] for each place that holds the pair the web server presents, the card last."""
    return list(zip(_distinct(CERTIFICATE_FILES), _distinct(KEY_FILES)))


def _settle_staged_pair(certificate_path, key_path):
    """End a replacement of the pair that stopped half way: True when the place then holds the new pair.

    The certificate is the last file a replacement renames: one that waits beside
    a key it belongs to completes the new pair, and any other is dropped.
    """
    try:
        staged = load_certificates(Path(certificate_path + STAGED).read_bytes())[0]
        complete = _same_key(staged, serialization.load_pem_private_key(_read_system_file(key_path), password=None))
    except (OSError, subprocess.SubprocessError, IndexError, ValueError, TypeError, UnsupportedAlgorithm):
        complete = False
    if complete:
        _commit(certificate_path)
    _as_root('rm', '-f', '--', certificate_path + STAGED, key_path + STAGED)
    return complete


def _finish_installation():
    """Settle what an interrupted replacement of the pair left behind, a power cut included.

    Each place then holds a whole pair again: the new one when its key was in
    place already, the previous one otherwise.
    """
    interrupted = [place for place in _places() if os.path.lexists(place[0] + STAGED)]
    if not interrupted:
        return
    with core('tools.helpers').writable():
        completed = [_settle_staged_pair(*place) for place in interrupted]
        _as_root('sync', *_directories(path for place in interrupted for path in place))
    _logger.warning(
        'A replacement of the web certificate of the box was interrupted: the box keeps the %s certificate',
        'new' if all(completed) else 'previous')


def _install(certificate_pem, key_pem):
    """Replace the pair the web server reads, as root: the box service has no right on the key directory.

    Both files are written whole beside the pair before either takes its place,
    on the card first. Whatever interrupts it, `_finish_installation` leaves the
    previous pair or the new one, never a key with the certificate of another.
    """
    places = _places()
    with core('tools.helpers').writable():
        try:
            for certificate_path, key_path in places:
                _stage(certificate_path, certificate_pem, '644')
                _stage(key_path, key_pem, '600')
            _as_root('sync', *(path + STAGED for place in places for path in place))
            for certificate_path, key_path in reversed(places):
                _commit(key_path)
                _commit(certificate_path)
            _as_root('sync', *_directories(path for place in places for path in place))
        except BaseException:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                for place in places:
                    _settle_staged_pair(*place)
            raise


def _installed_pair():
    """(certificate file, key file) as the web server reads them now, each as bytes; None when one is missing."""
    try:
        return _read_system_file(CERTIFICATE_FILES[0]), _read_system_file(KEY_FILES[0])
    except (OSError, subprocess.SubprocessError):
        return None


@contextlib.contextmanager
def _serving_again_on_failure():
    """Around a replacement of the pair a person asked for: when it fails, the box serves again what it served."""
    previous = _installed_pair()
    try:
        yield
    except Exception:
        try:
            if previous:
                _install(*previous)
                _reload_web_server()
        except Exception as error:
            _logger.error('The certificate the box served before could not be put back (%s)', type(error).__name__)
        raise


def _installed_certificate():
    try:
        return load_certificates(_read_system_file(CERTIFICATE_FILES[0]))[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def _installed_key():
    try:
        return serialization.load_pem_private_key(_read_system_file(KEY_FILES[0]), password=None)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, UnsupportedAlgorithm):
        return None


def _public_bytes(key):
    return key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def _same_key(certificate, key):
    return _public_bytes(certificate.public_key()) == _public_bytes(key.public_key())


def _common_name(certificate):
    found = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return found[0].value if found else ''


def _is_own(certificate):
    return certificate.issuer == certificate.subject and _common_name(certificate).startswith(f'{BOX_NAME} ')


def _own_common_name(helpers):
    return f'{BOX_NAME} {helpers.get_serial_number() or "unknown"}'[:64]


def _interface_addresses():
    """The addresses the interfaces of the box hold, as text, in the order of the interfaces."""
    found = []
    for interface in netifaces.interfaces():
        for family in (netifaces.AF_INET, netifaces.AF_INET6):
            try:
                entries = netifaces.ifaddresses(interface).get(family, ())
            except ValueError:
                # The interface went away between the two questions.
                continue
            found.extend(entry['addr'].split('%')[0] for entry in entries if entry.get('addr'))
    return found


def site_addresses():
    """The IPv4 addresses the box holds that other computers can reach it by; loopback and link-local ones are not."""
    found = set()
    for text in _interface_addresses():
        with contextlib.suppress(ValueError):
            address = ipaddress.ip_address(text)
            if address.version == 4 and not (address.is_loopback or address.is_link_local):
                found.add(str(address))
    return frozenset(found)


def _own_names(helpers):
    host = helpers.get_hostname()
    found = [host, f'{host}.local', (helpers.get_conf(BOX_ADDRESS_KEY) or '').strip().lower(), *_interface_addresses()]
    alternative = []
    for name in dict.fromkeys(found):
        try:
            address = ipaddress.ip_address(name)
        except ValueError:
            if name.isascii() and name:
                alternative.append(x509.DNSName(name))
        else:
            if not (address.is_loopback or address.is_link_local):
                alternative.append(x509.IPAddress(address))
    return alternative


def _why_replace(helpers):
    """Why the box must make itself a new certificate, '' when the one it holds stays."""
    certificate, key = _installed_certificate(), _installed_key()
    if certificate is None or key is None or not _same_key(certificate, key):
        return 'no usable certificate and key'
    name = _common_name(certificate)
    if name == SHIPPED_NAME:
        return 'the certificate is the one every image ships'
    if not name:
        # Core reads the common name when it checks the certificate, and its device manager stops without one.
        return 'the certificate has no common name'
    if not _is_own(certificate):
        return ''
    if name != _own_common_name(helpers):
        return 'the certificate was made for another board'
    if certificate.not_valid_after - utc_now() < RENEW_BEFORE:
        return 'the certificate expires soon'
    present = names(certificate)
    # An IPv6 address is left out: the box gives browsers names and IPv4 addresses, and the addresses an interface
    # chooses for itself change too often for each change to be worth a new certificate.
    if any(str(own.value) not in present for own in _own_names(helpers)
           if isinstance(own, x509.DNSName) or own.value.version == 4):
        return 'the box has another name or address'
    return ''


def _renewal_path(helpers):
    return Path(helpers.path_file(RENEWAL_FILE))


def _note_renewal(helpers, certificate):
    """Keep, on the card, that `certificate` replaced a certificate this box had made; never raises."""
    try:
        with helpers.writable():
            _write_whole(_renewal_path(helpers), fingerprint(certificate).encode())
    except (OSError, subprocess.SubprocessError) as error:
        _logger.warning('The box could not note that it replaced its own certificate (%s)', type(error).__name__)


def replaced_own_certificate(certificate_fingerprint):
    """Whether the certificate of `certificate_fingerprint` is one the box made in place of an earlier certificate
    it had made itself: a browser that accepted that one meets a certificate it does not know."""
    try:
        noted = _renewal_path(core('tools.helpers')).read_text(encoding='ascii').strip()
    except (OSError, ValueError):
        return False
    return bool(certificate_fingerprint) and noted == certificate_fingerprint


def _make_box_certificate(helpers):
    previous = _installed_certificate()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _own_common_name(helpers))])
    now = utc_now()
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - BACKDATE)
        .not_valid_after(now + LIFETIME)
        .add_extension(x509.SubjectAlternativeName(_own_names(helpers)), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    _install(_pem(certificate), key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    if previous is not None and _is_own(previous) and _common_name(previous) == _own_common_name(helpers):
        _note_renewal(helpers, certificate)


@contextlib.contextmanager
def _noting_failure():
    """Around a look at the certificate of the box: remember whether it ended in a failure."""
    try:
        yield
    except Exception:
        _look['failed'] = True
        raise
    _look['failed'] = False


def look_failed():
    """Whether the last look at the certificate of the box could not do what it found to be needed, or a
    certificate the box made is still not presented by the web server."""
    return _look['failed'] or _look['unserved']


def certificate_unserved():
    """Whether the box made itself a certificate that the web server has not presented since: the web server
    refused to read it again. Browsers meet the earlier certificate until it does, or until the box restarts."""
    return _look['unserved']


def will_make_certificate():
    """Whether the box is about to make itself another certificate: its next look finds a reason to."""
    try:
        return bool(_why_replace(core('tools.helpers')))
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_box_certificate():
    """Give the box a key and a certificate of its own unless it holds a pair worth keeping.

    The pair stays while it covers the host name, `<host name>.local` and every
    IPv4 address of the box; otherwise the box makes another that does. A
    certificate the customer installed is theirs to renew and is kept. Returns
    True when the files changed; the web server reads them at its next start.
    """
    helpers = core('tools.helpers')
    with _noting_failure():
        _finish_installation()
        reason = _why_replace(helpers)
        if not reason:
            return False
        _logger.warning('The box replaces its web certificate by one it makes itself: %s', reason)
        _make_box_certificate(helpers)
    return True


@_exclusive
def refresh_box_certificate():
    """`ensure_box_certificate`, then have the web server serve it and tell the listeners when the files changed.

    A certificate the web server refused to take is served again at each later
    call, whether the files change or not, until the web server takes it.
    """
    changed = ensure_box_certificate()
    if changed or _look['unserved']:
        with _noting_failure():
            try:
                serve_new_certificate()
            except Exception:
                _look['unserved'] = True
                raise
    return changed


def _failure_line(failure):
    """(type name, text) of a failure as one line of at most REPEAT_TEXT_LIMIT characters of its text."""
    text = ' '.join(failure[1].split())
    if len(text) > REPEAT_TEXT_LIMIT:
        text = text[:REPEAT_TEXT_LIMIT] + ' [cut]'
    return f'{failure[0]}: {text}' if text else failure[0]


def _log_failed_look(message, error):
    """Log `error`, which ended a look of the keeper or of the watcher: with its traceback when it is not the failure
    logged last, as one line with its type and text when the same failure comes back look after look."""
    failure = (type(error).__name__, str(error))
    if _failure_logged['last'] == failure:
        _logger.warning('%s: the same failure again (%s)', message, _failure_line(failure))
        return
    _failure_logged['last'] = failure
    _logger.error(message, exc_info=error)


def _look_succeeded():
    _failure_logged['last'] = None


class AddressWatcher(threading.Thread):
    """Looks at the IPv4 addresses of the box every ADDRESS_LOOK_PERIOD seconds, and has the box look at its
    certificate once an address appears that the certificate may not cover, or while a certificate it made is not
    served yet.

    A look that fails is logged, in full unless the look before it failed the same way; the next one comes
    FAILED_LOOK_WAIT seconds later at the soonest.
    """

    def __init__(self):
        super().__init__(name='ViinAddressWatcher', daemon=True)

    def run(self):
        seen, retry_at = frozenset(), None
        with contextlib.suppress(Exception):
            seen = site_addresses()
        while True:
            time.sleep(ADDRESS_LOOK_PERIOD)
            try:
                addresses = site_addresses()
                if (addresses <= seen and not _look['unserved']) or (retry_at and time.monotonic() < retry_at):
                    continue
                retry_at = None
                refresh_box_certificate()
                _look_succeeded()
                seen = addresses
            except Exception as error:
                _log_failed_look('The certificate of the box could not be checked after its address changed', error)
                retry_at = time.monotonic() + FAILED_LOOK_WAIT


class CertificateKeeper(threading.Thread):
    """Looks at the certificate of the box when it starts, then every RENEWAL_CHECK_PERIOD seconds; an
    `AddressWatcher` looks at it in between whenever an address of the box appears, and every FAILED_LOOK_WAIT
    seconds while a certificate the box made is not served.

    With `after_core_starts_web_server`, the first look waits until core has started the web server, for
    WEB_SERVER_START_WAIT seconds at the most: a web server that reads the files while a look replaces them may read
    the key of one pair beside the certificate of another and refuse to start, and core's start ends at that error.

    A look that fails is logged as the watcher logs its own, and costs nothing but that look.
    """

    def __init__(self, after_core_starts_web_server=False):
        super().__init__(name='ViinCertificateKeeper', daemon=True)
        self._made_at = _uptime() if after_core_starts_web_server else None

    def _wait_for_core_to_start_web_server(self):
        deadline = time.monotonic() + WEB_SERVER_START_WAIT
        while not _web_server_started_after(self._made_at):
            if time.monotonic() >= deadline:
                _logger.warning('Core did not start the web server within %s seconds; the box looks at its '
                                'certificate all the same', WEB_SERVER_START_WAIT)
                return
            _pause.wait(0.1)

    def run(self):
        AddressWatcher().start()
        if self._made_at is not None:
            self._wait_for_core_to_start_web_server()
        while True:
            try:
                refresh_box_certificate()
                _look_succeeded()
            except Exception as error:
                _log_failed_look('The certificate of the box could not be checked', error)
            time.sleep(RENEWAL_CHECK_PERIOD)


def _kind(certificate):
    if _common_name(certificate) == SHIPPED_NAME:
        return 'shipped'
    return 'box' if _is_own(certificate) else 'customer'


def box_certificate():
    """What the web server of the box presents: {'kind', ...}, with the keys of `describe` unless it is missing.

    `kind` is `box` (made by the box), `customer` (installed by the customer),
    `shipped` (the one of the image) or `missing`. A certificate of kind `box`
    also carries `made_at`, the moment the box made it (UTC, as `expiry` is
    written).
    """
    certificate = _installed_certificate()
    if certificate is None:
        return {'kind': 'missing'}
    kind = _kind(certificate)
    found = dict(describe(certificate), kind=kind)
    if kind == 'box':
        found['made_at'] = iso_time(certificate.not_valid_before + BACKDATE)
    return found


def _host_matches(host, certificate):
    issued_for = [name.lower() for name in names(certificate)]
    with contextlib.suppress(ValueError):
        return str(ipaddress.ip_address(host)) in issued_for
    return any(
        name == host or (name.startswith('*.') and '.' in host and host.split('.', 1)[1] == name[2:])
        for name in issued_for
    )


def customer_certificate_covers(host):
    """Whether the certificate the web server presents is the customer's own and is issued for `host`.

    It reads the certificate file alone and needs nothing of core, so that the
    reset of the box, which runs before core is loaded, can ask it.
    """
    host = str(host or '').strip().lower().rstrip('.')
    certificate = _installed_certificate()
    return bool(host) and certificate is not None and _kind(certificate) == 'customer' and _host_matches(
        host, certificate)


def _why_refuse(certificates, key, host):
    if not certificates:
        return 'invalid_certificate'
    certificate = certificates[0]
    if not _common_name(certificate):
        # Core reads the common name when it checks the certificate, and its device manager stops without one.
        return 'common_name_missing'
    if not _same_key(certificate, key):
        return 'key_mismatch'
    if utc_now() < certificate.not_valid_before:
        return 'not_yet_valid'
    if utc_now() >= certificate.not_valid_after:
        return 'expired'
    with contextlib.suppress(ValueError):
        ipaddress.ip_address(host)
        return '' if _host_matches(host, certificate) else 'host_name_mismatch'
    if not _HOST_NAME.match(host):
        return 'invalid_host_name'
    return '' if _host_matches(host, certificate) else 'host_name_mismatch'


@_exclusive
def install_certificate(certificate_pem, key_pem, host_name):
    """Install the customer's certificate for the web server of the box.

    `certificate_pem` holds the certificate and, after it, the certificates of
    its chain; `key_pem` its private key, not encrypted; `host_name` the name
    the certificate was issued for, which browsers will be given for this box.
    Returns {'status': 'installed', 'host_name', ...the keys of `describe`}, or
    {'status': 'refused', 'reason'}: the box then serves the certificate, and
    gives browsers the address, it did before. Reasons: invalid_certificate,
    common_name_missing, invalid_key, key_encrypted, key_mismatch,
    not_yet_valid, expired, invalid_host_name, host_name_mismatch, box_error.
    """
    helpers = core('tools.helpers')
    host = str(host_name or '').strip().lower().rstrip('.')
    certificates = load_certificates(str(certificate_pem or '').encode())
    try:
        key = serialization.load_pem_private_key(str(key_pem or '').encode(), password=None)
    except TypeError:
        return {'status': 'refused', 'reason': 'key_encrypted'}
    except (ValueError, UnsupportedAlgorithm):
        return {'status': 'refused', 'reason': 'invalid_key'}
    reason = _why_refuse(certificates, key, host)
    if reason:
        return {'status': 'refused', 'reason': reason}
    address = helpers.get_conf(BOX_ADDRESS_KEY)
    try:
        with _serving_again_on_failure():
            _install(b''.join(map(_pem, certificates)), key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            helpers.update_conf({BOX_ADDRESS_KEY: host})
            serve_new_certificate()
    except (OSError, subprocess.SubprocessError) as error:
        _logger.error('The certificate of the customer could not be installed (%s)', type(error).__name__)
        with contextlib.suppress(OSError):
            helpers.update_conf({BOX_ADDRESS_KEY: address or ''})
        return {'status': 'refused', 'reason': 'box_error'}
    _logger.warning(
        'The web certificate of the box was replaced by the certificate of the customer for %s (%s)',
        host, fingerprint(certificates[0]))
    return dict(describe(certificates[0]), status='installed', host_name=host)


@_exclusive
def restore_box_certificate():
    """Put a certificate made by the box back in place of the one the customer installed.

    The address the customer gave for the box stays: the new certificate is
    issued for it too. It goes when the box leaves its server, as no
    certificate of the customer covers it any more. When it fails the box
    serves what it served before, and OSError or subprocess.SubprocessError
    is raised.
    """
    with _serving_again_on_failure():
        _make_box_certificate(core('tools.helpers'))
        serve_new_certificate()
    _logger.warning('The web certificate of the box was replaced by one the box made itself, as asked')
