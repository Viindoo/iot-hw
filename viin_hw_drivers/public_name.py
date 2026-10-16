"""The Viindoo name of the box: a public name of its own, with a certificate whose key never leaves it.

The box asks its own server for a name, makes a key and a signing request for
exactly that name, and serves the certificate it receives beside its own,
through a second web server block. It tells its server the name only once the
site's network resolved the name to the box and the box answered itself under
it. Whatever fails, the box keeps its own certificate and its address: the
first pair of the web server is never touched here.
"""
import datetime
import http.client
import json
import logging
import os
import random
import re
import socket
import ssl
import subprocess
import threading
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

import urllib3
from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from OpenSSL import crypto

from . import pairing, selftest, trust, update
from .seams import core

_logger = logging.getLogger(__name__)

ROUTE = '/iot/certificate'
PROTOCOL = 1
# How long the box waits for its server, which itself waits for the provider of names.
TIMEOUT = 45
MAX_ANSWER = 64 * 1024
RECORD_KEY = selftest.PUBLIC_NAME_KEY
MANAGED_NAME_SERVER_KEY = selftest.MANAGED_NAME_SERVER_KEY

KEY_SIZE = 2048
PUBLIC_EXPONENT = 65537
# Longest common name a certificate subject may carry; a longer name is asked for by its alternative name alone.
COMMON_NAME_MAX = 64

FIRST_QUESTION = 120
WAKE = 60
# The server relays one question of a box at most once a minute and answers any other within it `refused` /
# `too_soon`, with the seconds left. The box asks again a few seconds after the minute: its clock and the server's
# do not count to the same second.
FLOOR = 60
FLOOR_MARGIN = 5
FOLLOW_UP = FLOOR + FLOOR_MARGIN
RETRY_SHORTEST = 60
RETRY_LONGEST = 86400
UNREACHABLE_WAIT = 3600
DAY = 86400
JITTER = 3600
NO_PROVIDER_WAIT = 7 * DAY
# The server's wait when the week's allowance of new certificates is used: the request is in a queue.
QUEUED_WAIT = 3600
# After an install or a move to another address the site's resolver may give the old answer for some minutes.
CHECK_RETRY = 300
CHECK_RETRIES = 6
FAILED_DAYS_TO_DROP = 3
QUESTIONS_AT_ONCE = 3

NAME_FILE = 'nginx-viin-name'


def _beside(paths, name):
    return tuple(str(Path(path).with_name(name)) for path in paths)


# The pair of the name lives in `trust.NAME_PAIR_DIRECTORIES`, in the generation `current` names.
NAME_PAIR_DIRECTORIES = trust.NAME_PAIR_DIRECTORIES
NAME_CERTIFICATE_FILE = f'{NAME_PAIR_DIRECTORIES[0]}/{trust.CURRENT}/fullchain.pem'
NAME_KEY_FILE = f'{NAME_PAIR_DIRECTORIES[0]}/{trust.CURRENT}/privkey.pem'
PENDING_KEY_FILES = _beside(trust.KEY_FILES, f'{NAME_FILE}.pending.key')
PENDING_REQUEST_FILES = _beside(trust.KEY_FILES, f'{NAME_FILE}.pending.csr')
# /etc lives in memory on a box: the second path of each pair is the same file on the card.
SITE_FILES = (
    '/etc/nginx/viindoo/names/viindoo-name.conf',
    '/root_bypass_ramdisks/etc/nginx/viindoo/names/viindoo-name.conf',
)
SITE_TEMPLATE = '/etc/nginx/viindoo/name.conf.template'
NAME_MARK = '@NAME@'
NGINX_TEST = ('sudo', '-n', 'nginx', '-t', '-q')
SYSTEM_TIMEOUT = 60
# The web server takes a reload a moment after the command returned: the box waits this long, at most, until it
# presents the certificate of the name, looking every PRESENTATION_LOOK seconds.
PRESENTATION_WAIT = 10
PRESENTATION_LOOK = 0.25
# The web server of the box answers on every address of the box, this one included.
LOOPBACK = '127.0.0.1'

# What the server may answer, as {status: (keys it always carries, keys it may carry)}, each with its type.
ANSWERS = {
    'unavailable': ({'reason': str}, {}),
    'name': ({'fqdn': str}, {}),
    'pending': ({'fqdn': str, 'retry_after': int}, {}),
    'issued': ({'fqdn': str, 'certificate': str, 'not_after': str}, {}),
    'refused': ({'reason': str}, {'retry_after': int}),
}
UNREACHABLE = 'unreachable'
# Answers that bring the box nearer a name, or say for good that it gets none: they are no failure of a renewal.
NOT_FAILURES = (('name', ''), ('pending', ''), ('unavailable', 'disabled'), ('unavailable', 'box_unknown'),
                ('unavailable', 'no_provider'), ('unavailable', 'not_offered'))
# Answers that take the name from the box: the server no longer gives it one, or no longer knows the box's token
# (the record was deleted or replacement hardware took it), so the name it held is not the box's any more.
NAME_LOST = (('unavailable', 'disabled'), ('unavailable', 'box_unknown'))

# The reasons `_install` gives for a certificate the box cannot take, that are the certificate's own, or the
# request's: no certificate answering that request can be taken, so only a new request leads on.
#   invalid_certificate, key_mismatch, name_mismatch: the certificate.
#   not_yet_valid, expired: the certificate, because the server's time confirmed it is outside its dates.
#   key_missing: the key of the request is gone from the card, so its certificate can never be used.
# Every other reason - box_clock_wrong, validity_unconfirmed, untrusted, not_written, not_readable,
# web_server_refused, no_site_template, and any the box may give later - is the box's: a new request would only
# spend one of the name's weekly orders. Two of them need a word:
#   validity_unconfirmed: the dates look wrong but the server gave no time, so the box cannot tell its clock from
#     the certificate.
#   untrusted: the certificate does not lead to the authorities the box carries. Usually that list is out of date
#     in the box, and the next certificate would carry the same chain.
CERTIFICATE_FAULTS = frozenset({
    'invalid_certificate', 'key_mismatch', 'name_mismatch', 'not_yet_valid', 'expired', 'key_missing',
})

_REASON = re.compile(r'[a-z][a-z0-9_]{0,63}')
_TIME = '%Y-%m-%dT%H:%M:%SZ'

EMPTY_RECORD = {
    'status': '', 'reason': '', 'fqdn': '', 'not_after': '', 'next_question_at': '', 'asked_at': '',
    'in_queue': False, 'check': '', 'renewal_failing': False, 'install_problem': '',
    'server': '', 'failing_since': '', 'last_answer': '',
}

_lock = threading.RLock()
_record = dict(EMPTY_RECORD)
# The certificate of the name the box holds: {'fqdn', 'not_before', 'not_after', 'fingerprint', 'served'}.
_held = {}
# Whether a pair of the name may be on the box. Only root reads the pair, so a box that is not allowed a
# name asks root once, at the start, and after that only when this process may have put a pair there.
_pair_may_exist = True
# Whether the files of the name were read since this process started: until they were, `_held` says nothing.
_held_read = False
_plan = {'question_at': None, 'check_at': None, 'check_retries': 0, 'sent_ip': None, 'asked_at': None,
         'new_request': False}
_said = {}
_errors = []


class _Problem(Exception):
    """A certificate the box does not take; `reason` is the key the self-test shows for it.

    `reload_tried`: the web server was asked to read its configuration, so it may hold what was being installed.
    """

    def __init__(self, reason, reload_tried=False):
        super().__init__(reason)
        self.reason = reason
        self.reload_tried = reload_tried


# ------------------------------------------------------------------------------------------- boundaries

def _now():
    """Now, as the naive UTC time certificates carry their dates in."""
    return trust.utc_now()


def _jitter():
    return random.randint(0, JITTER)


def _helpers():
    return core('tools.helpers')


def _address():
    """The address of the box on the site's network, as its server is told it; None without a network."""
    return _helpers().get_ip()


def _post(url, body):
    """POST `body` to the server of the box: (HTTP status, at most MAX_ANSWER + 1 bytes of answer, its Date header).

    Raises OSError or urllib3's HTTPError when no answer came.
    """
    pool = trust.pool_manager(timeout=urllib3.Timeout(total=TIMEOUT), retries=False)
    try:
        response = pool.request(
            'POST', url, body=body, headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
            redirect=False, preload_content=False)
        try:
            data = response.read(MAX_ANSWER + 1)
        finally:
            response.release_conn()
        return response.status, data, response.headers.get('Date')
    finally:
        pool.clear()


def _resolve(fqdn):
    """The IPv4 addresses the site's resolver gives for `fqdn`. Raises OSError when it gives none."""
    found = socket.getaddrinfo(fqdn, 443, socket.AF_INET, socket.SOCK_STREAM)
    return sorted({entry[4][0] for entry in found})


def _fingerprint_of(der):
    return trust.fingerprint(x509.load_der_x509_certificate(der))


def _say_hello(fqdn, address):
    """(fingerprint of the certificate presented, what the box answers itself) at https://`fqdn`/hw_proxy/hello,
    reached at `address`.

    The certificate is verified against the public authorities only, as a
    browser of the site does. Raises ssl.SSLCertVerificationError when it is
    refused, OSError or http.client.HTTPException when no answer came.
    """
    context = ssl.create_default_context(cafile=trust.PUBLIC_AUTHORITIES)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    connection = http.client.HTTPSConnection(fqdn, 443, timeout=trust.TIMEOUT, context=context)
    try:
        plain = socket.create_connection((address, 443), timeout=trust.TIMEOUT)
        try:
            connection.sock = context.wrap_socket(plain, server_hostname=fqdn)
        except BaseException:
            plain.close()
            raise
        presented = _fingerprint_of(connection.sock.getpeercert(binary_form=True))
        connection.request('GET', '/hw_proxy/hello')
        response = connection.getresponse()
        return presented, response.read(64).decode(errors='replace') if response.status == 200 else ''
    finally:
        connection.close()


def _presented(fqdn, timeout):
    """Fingerprint of the certificate the web server of the box presents to a browser asking for `fqdn`.

    Read without verifying it and without sending a request. Raises OSError when the web server does not answer.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((LOOPBACK, 443), timeout=timeout) as plain:
        with context.wrap_socket(plain, server_hostname=fqdn) as secured:
            return _fingerprint_of(secured.getpeercert(binary_form=True))


def _exists(path):
    return os.path.lexists(path)


def _read_system_file(path):
    """The bytes of a file, read as root when the box user may not; None when it does not exist.

    Raises trust.Unreadable when it could not be read: a file that may be there is never taken for none.
    """
    try:
        return Path(path).read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except PermissionError:
        pass
    except OSError:
        raise trust.Unreadable('the file could not be read') from None
    return trust.read_as_root(path)


def _write_system_files(paths, content, mode):
    trust.write_system_files(paths, content, mode)


def _remove_system_files(paths):
    trust.remove_system_files(paths)


def _nginx_accepts():
    """Whether the web server accepts its whole configuration as it stands on the disk."""
    try:
        return subprocess.run(NGINX_TEST, capture_output=True, check=False, timeout=SYSTEM_TIMEOUT).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _reload_web_server():
    trust.serve_new_certificate()


# ------------------------------------------------------------------------------------------- keys and certificates

def _spki(public_key):
    return public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def acceptable_name(fqdn):
    """Whether the box takes `fqdn` for a name of its own: a host name of two labels at least, not of the local link."""
    return (isinstance(fqdn, str) and bool(trust._HOST_NAME.fullmatch(fqdn))
            and '.' in fqdn and not fqdn.endswith('.local'))


def _private_key(data):
    """The private key a file of the name holds, or None; the whole file is the key."""
    try:
        return serialization.load_pem_private_key(data or b'', password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        return None


def _key_pem(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def signing_request(fqdn, key):
    """The request for `fqdn` signed with `key`: that one name as the common name and as the only alternative name."""
    subject = [x509.NameAttribute(NameOID.COMMON_NAME, fqdn)] if len(fqdn) <= COMMON_NAME_MAX else []
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name(subject))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(fqdn)]), critical=False)
        .sign(key, hashes.SHA256())
    )


def _requested_name(request):
    try:
        names = request.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return ''
    found = names.get_values_for_type(x509.DNSName)
    return found[0] if len(found) == 1 else ''


def _pem(request):
    return request.public_bytes(serialization.Encoding.PEM).decode('ascii')


def _pending_request():
    """The request waiting for its certificate, or None; raises trust.Unreadable when the file could not be read."""
    data = _read_system_file(PENDING_REQUEST_FILES[0])
    try:
        return x509.load_pem_x509_csr(data)
    except (TypeError, ValueError):
        return None


def _new_request(fqdn):
    """Make a key and a request for `fqdn`, and keep both on the card before the request is sent anywhere."""
    key = rsa.generate_private_key(public_exponent=PUBLIC_EXPONENT, key_size=KEY_SIZE)
    request = signing_request(fqdn, key)
    _write_system_files(PENDING_KEY_FILES, _key_pem(key), '600')
    _write_system_files(PENDING_REQUEST_FILES, _pem(request).encode(), '644')
    return request


def _discard_pending():
    _remove_system_files(PENDING_KEY_FILES + PENDING_REQUEST_FILES)


def _chain_verifies(leaf, chain, moment):
    """Whether `leaf` leads, through `chain`, to one of the public authorities the box carries, at `moment`."""
    store = crypto.X509Store()
    store.load_locations(trust.PUBLIC_AUTHORITIES)
    store.set_time(moment)
    context = crypto.X509StoreContext(
        store, crypto.X509.from_cryptography(leaf), [crypto.X509.from_cryptography(other) for other in chain])
    try:
        context.verify_certificate()
    except crypto.X509StoreContextError:
        return False
    return True


def _judge(certificates, request, fqdn, server_time):
    """Raise _Problem unless the first of `certificates` carries the key of `request`, names `fqdn`, is valid now,
    and leads to the public authorities."""
    if not certificates:
        raise _Problem('invalid_certificate')
    leaf = certificates[0]
    if _spki(leaf.public_key()) != _spki(request.public_key()):
        raise _Problem('key_mismatch')
    if fqdn not in [name.lower() for name in trust.names(leaf)]:
        raise _Problem('name_mismatch')
    now = _now()
    if not leaf.not_valid_before <= now < leaf.not_valid_after:
        if server_time is None:
            raise _Problem('validity_unconfirmed')
        if leaf.not_valid_before <= server_time < leaf.not_valid_after:
            raise _Problem('box_clock_wrong')
        raise _Problem('not_yet_valid' if now < leaf.not_valid_before else 'expired')
    if not _chain_verifies(leaf, certificates[1:], now):
        raise _Problem('untrusted')


def _describe(certificate, fqdn, served):
    return {
        'fqdn': fqdn,
        'not_before': certificate.not_valid_before,
        'not_after': certificate.not_valid_after,
        'fingerprint': trust.fingerprint(certificate),
        'served': served,
    }


def _load_held():
    """Read what the files of the name hold into `_held`; raises trust.Unreadable, changing nothing, when they
    could not be read."""
    global _pair_may_exist, _held_read
    chain = _read_system_file(NAME_CERTIFICATE_FILE)
    _held.clear()
    _held_read = True
    _pair_may_exist = chain is not None
    certificates = trust.load_certificates(chain or b'')
    if not certificates:
        return
    names = [name.lower() for name in trust.names(certificates[0])]
    fqdn = _record['fqdn'] if _record['fqdn'] in names else next(iter(names), '')
    if acceptable_name(fqdn):
        _held.update(_describe(certificates[0], fqdn, _exists(SITE_FILES[0])))


def _renewal_due(now):
    validity = _held['not_after'] - _held['not_before']
    return _held['not_after'] - now < validity / 3


def _ended(text, now):
    try:
        return datetime.datetime.strptime(text, _TIME) <= now
    except (TypeError, ValueError):
        return False


# ------------------------------------------------------------------------------------------- serving the name

def _site(fqdn):
    """The web server block of `fqdn`: the template, its directives naming `fqdn`; its comments are left as they are."""
    lines = (_read_system_file(SITE_TEMPLATE) or b'').decode(errors='replace').splitlines(keepends=True)
    filled = [line if line.lstrip().startswith('#') else line.replace(NAME_MARK, fqdn) for line in lines]
    if filled == lines:
        raise _Problem('no_site_template')
    return ''.join(filled).encode()


def _unserve():
    """Remove the web server block of the name; the web server forgets it when what remains is accepted.

    The address is reported again first: browsers are never sent to a name the box no longer serves.
    """
    _clear_managed_name()
    if not _exists(SITE_FILES[0]):
        return
    _remove_system_files(SITE_FILES)
    if _held:
        _held['served'] = False
    if _nginx_accepts():
        _reload_web_server()


def _await_presentation(fqdn):
    """Whether the web server presents the certificate held for `fqdn` within PRESENTATION_WAIT seconds."""
    deadline = time.monotonic() + PRESENTATION_WAIT
    while True:
        try:
            if _presented(fqdn, max(deadline - time.monotonic(), PRESENTATION_LOOK)) == _held['fingerprint']:
                return True
        except (OSError, ValueError, TypeError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(PRESENTATION_LOOK)


def _serve(fqdn):
    """Give `fqdn` its web server block, have the web server read it, and let it present the certificate held.

    Raises _Problem when the block cannot be written or the web server refuses
    the configuration or cannot be made to read it; the caller puts back what
    the block was. A web server still presenting another certificate once
    PRESENTATION_WAIT is over is left for the self-check to find.
    """
    try:
        _write_system_files(SITE_FILES, _site(fqdn), '644')
    except (OSError, subprocess.SubprocessError):
        raise _Problem('not_written') from None
    if not _nginx_accepts():
        raise _Problem('web_server_refused')
    try:
        _reload_web_server()
    except (OSError, subprocess.SubprocessError):
        raise _Problem('web_server_refused', reload_tried=True) from None
    _held['served'] = True
    _await_presentation(fqdn)


def _block_before():
    """The web server block of the name as bytes, None when the box has none.

    Raises _Problem when it exists but cannot be read: it could not be put back.
    """
    if not _exists(SITE_FILES[0]):
        return None
    try:
        return _read_system_file(SITE_FILES[0])
    except trust.Unreadable:
        raise _Problem('not_readable') from None


def _put_back(previous, block, reload_tried):
    """Leave the name as `_install` found it - `current` on `previous` generation, the block as it was - and the
    web server on a configuration it accepts.

    The web server reads its configuration again only when an install asked it
    to, and only when it accepts what is then on the disk; when it does not, the
    block of the name goes.
    """
    try:
        trust.restore_pair(NAME_PAIR_DIRECTORIES, previous)
        trust.prune_pairs(NAME_PAIR_DIRECTORIES)
        if block is None:
            _remove_system_files(SITE_FILES)
        else:
            _write_system_files(SITE_FILES, block, '644')
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        _logger.warning('The Viindoo name of the box could not put back the files it held (%s)', type(error).__name__)
    try:
        if not _nginx_accepts():
            _unserve()
        elif reload_tried:
            _reload_web_server()
    except (OSError, subprocess.SubprocessError) as error:
        _logger.warning('The web server could not be made to read the files of the Viindoo name again (%s)',
                        type(error).__name__)


def _install(answer, request, key_files, server_time):
    """Install the certificate of an `issued` answer to `request`, whose key is in `key_files`; raise _Problem.

    Whatever step fails, the box is left as it was: the certificate it held
    before still on the disk and, if it was served, still served.
    """
    fqdn = _requested_name(request)
    certificates = trust.load_certificates(answer['certificate'].encode())
    _judge(certificates, request, fqdn, server_time)
    try:
        key = _private_key(_read_system_file(key_files[0]))
    except trust.Unreadable:
        raise _Problem('not_readable') from None
    if key is None or _spki(key.public_key()) != _spki(request.public_key()):
        raise _Problem('key_missing')
    chain = b''.join(certificate.public_bytes(serialization.Encoding.PEM) for certificate in certificates)
    block = _block_before()
    global _pair_may_exist
    _pair_may_exist = True
    try:
        previous = trust.replace_pair(NAME_PAIR_DIRECTORIES, chain, _key_pem(key))
    except (OSError, subprocess.SubprocessError, ValueError):
        raise _Problem('not_written') from None
    try:
        _held.clear()
        _held.update(_describe(certificates[0], fqdn, False))
        _serve(fqdn)
    except _Problem as problem:
        _put_back(previous, block, problem.reload_tried)
        raise
    _prune_pairs()
    if key_files == PENDING_KEY_FILES:
        _discard_pending()


def _prune_pairs():
    """Remove the generations of the pair `current` does not name; what is left is removed at the next start."""
    try:
        trust.prune_pairs(NAME_PAIR_DIRECTORIES)
    except (OSError, subprocess.SubprocessError) as error:
        _logger.warning('The Viindoo name of the box could not remove its old files (%s)', type(error).__name__)


def _set_managed_name(fqdn, server):
    helpers = _helpers()
    if ((helpers.get_conf(trust.MANAGED_NAME_KEY) or '') != fqdn
            or (helpers.get_conf(MANAGED_NAME_SERVER_KEY) or '') != server):
        helpers.update_conf({trust.MANAGED_NAME_KEY: fqdn, MANAGED_NAME_SERVER_KEY: server})


def _clear_managed_name():
    helpers = _helpers()
    if helpers.get_conf(trust.MANAGED_NAME_KEY) or helpers.get_conf(MANAGED_NAME_SERVER_KEY):
        helpers.update_conf({trust.MANAGED_NAME_KEY: '', MANAGED_NAME_SERVER_KEY: ''})


def _drop(files=True):
    """Lose the name: report the address again, remove the block of the name and, with `files`, its files.

    The box's own certificate is never touched, and neither the box nor its service restarts.
    """
    global _pair_may_exist
    _clear_managed_name()
    _unserve()
    if files:
        trust.remove_pair(NAME_PAIR_DIRECTORIES)
        _pair_may_exist = False
        _remove_system_files(PENDING_KEY_FILES + PENDING_REQUEST_FILES)
        _held.clear()
        _plan.update(check_at=None, check_retries=0)


# ------------------------------------------------------------------------------------------- self-check

def _resolution(fqdn, address):
    try:
        found = _resolve(fqdn)
    except OSError:
        return 'not_resolved'
    if not found:
        return 'not_resolved'
    return 'ok' if address and found == [address] else 'other_address'


def _answers_itself(fqdn, address):
    try:
        presented, text = _say_hello(fqdn, address)
    except ssl.SSLCertVerificationError:
        return 'wrong_certificate'
    except (OSError, http.client.HTTPException, ValueError, TypeError):
        return 'no_answer'
    if presented != _held['fingerprint']:
        return 'wrong_certificate'
    return 'ok' if text.strip() == 'ping' else 'wrong_answer'


def _check(now):
    """Find out whether the site reaches the box by its name; tell the server the name only when it does.

    A check failing FAILED_DAYS_TO_DROP days running removes the block of the
    name; the certificate stays, and the block comes back once the site's
    resolver gives the box's address for the name again.
    """
    fqdn, address = _held['fqdn'], _address()
    result = _resolution(fqdn, address)
    if result == 'ok' and not _held['served']:
        try:
            _serve(fqdn)
        except _Problem as problem:
            _unserve()
            _record['install_problem'] = result = problem.reason
    if result == 'ok':
        result = _answers_itself(fqdn, address)
    _record['check'] = result
    _plan['check_at'] = None
    if result == 'ok':
        _record['failing_since'] = ''
        _plan['check_retries'] = 0
        _set_managed_name(fqdn, _record['server'])
        return
    _clear_managed_name()
    if not _record['failing_since']:
        _record['failing_since'] = trust.iso_time(now)
    elif _ended(_record['failing_since'], now - datetime.timedelta(days=FAILED_DAYS_TO_DROP - 1)):
        _unserve()
    if _plan['check_retries'] > 0:
        _plan['check_retries'] -= 1
        _plan['check_at'] = now + datetime.timedelta(seconds=CHECK_RETRY)


# ------------------------------------------------------------------------------------------- the question

def _request_to_send(now):
    """(request, files of its key) the next question carries, or None to ask for a name only."""
    fqdn = _record['fqdn']
    if not acceptable_name(fqdn):
        return None
    pending = _pending_request()
    fresh = _plan['new_request']
    if pending and not fresh and _requested_name(pending) == fqdn:
        return pending, PENDING_KEY_FILES
    if pending:
        _discard_pending()
    # The wish for a new request is met from here on: no pending request is left to be reused.
    _plan['new_request'] = False
    if _held and _held['fqdn'] == fqdn and not fresh and not _renewal_due(now):
        # The request of the certificate in use: the server answers it with that certificate, ordering nothing.
        key = _private_key(_read_system_file(NAME_KEY_FILE))
        if key is not None:
            return signing_request(fqdn, key), (NAME_KEY_FILE,)
    return _new_request(fqdn), PENDING_KEY_FILES


def _server_time(value):
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if moment is not None and moment.tzinfo:
        moment = moment.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return moment


def _read_answer(status, data):
    """The answer of the server as the box takes it: {'status', 'reason', ...}."""
    if status == 404:
        # A server without the route offers no name, as one without a provider says it.
        return {'status': 'unavailable', 'reason': 'no_provider'}
    if status != 200 or len(data) > MAX_ANSWER:
        return {'status': UNREACHABLE, 'reason': 'bad_answer'}
    try:
        result = json.loads(data)['result']
    except (ValueError, LookupError, TypeError):
        return {'status': UNREACHABLE, 'reason': 'bad_answer'}
    status = result.get('status') if isinstance(result, dict) else None
    if status not in ANSWERS:
        return {'status': 'unavailable', 'reason': 'unspecified'}
    required, optional = ANSWERS[status]
    answer = {'status': status, 'reason': ''}
    for key, kind in {**required, **optional}.items():
        value = result.get(key)
        if isinstance(value, kind) and not isinstance(value, bool):
            answer[key] = value
        elif key in required:
            return {'status': 'unavailable', 'reason': 'unspecified'}
    if 'reason' in required and not _REASON.fullmatch(answer['reason']):
        answer['reason'] = 'unspecified'
    if 'fqdn' in answer and not acceptable_name(answer['fqdn']):
        return {'status': 'unavailable', 'reason': 'unspecified'}
    return answer


def _ask(server, address, now):
    """Ask the server: (its answer, the request the question carried or None, the server's clock or None)."""
    request = _request_to_send(now)
    params = {'token': _helpers().get_token(), 'protocol': PROTOCOL}
    if address:
        params['ip'] = address
    try:
        params['iot_mac'] = core('main').manager.mac_address
    except (KeyError, AttributeError):
        pass
    if request:
        params['csr'] = _pem(request[0])
    body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params})
    try:
        status, data, date = _post(server + ROUTE, body)
    except (OSError, urllib3.exceptions.HTTPError):
        return {'status': UNREACHABLE, 'reason': 'no_answer'}, request, None
    return _read_answer(status, data), request, _server_time(date)


def _wait_after(answer):
    """Seconds until the next question after `answer`."""
    status, reason = answer['status'], answer['reason']
    if status == UNREACHABLE or (status, reason) == ('unavailable', 'provider_unreachable'):
        return UNREACHABLE_WAIT
    if (status, reason) == ('unavailable', 'no_provider'):
        return NO_PROVIDER_WAIT
    if status == 'pending' or (status, reason) == ('refused', 'try_later'):
        return min(max(answer.get('retry_after', RETRY_LONGEST), RETRY_SHORTEST), RETRY_LONGEST)
    return DAY + _jitter()


def _take_certificate(answer, request, server_time, now):
    """Install, or confirm, the certificate of an `issued` answer; return whether that failed.

    A failure that is the certificate's makes the next question carry a new request; one that is the box's does
    not: the same request is answered with the same certificate again, and no new one is ordered.
    """
    certificates = trust.load_certificates(answer['certificate'].encode())
    if _held and certificates and trust.fingerprint(certificates[0]) == _held['fingerprint']:
        _check(now)
        return False
    held_before = dict(_held)
    try:
        _install(answer, *request, server_time)
    except _Problem as problem:
        _record['install_problem'] = problem.reason
        if problem.reason in CERTIFICATE_FAULTS:
            _plan['new_request'] = True
        try:
            _load_held()
        except trust.Unreadable:
            _held.clear()
            _held.update(held_before)
        return True
    _record.update(fqdn=_held['fqdn'], failing_since='')
    _plan['check_retries'] = CHECK_RETRIES
    _check(now)
    return False


def _take(answer, request, server_time, now):
    """Act on `answer` to a question that carried `request`; return seconds until the next question."""
    status, reason = answer['status'], answer['reason']
    if (status, reason) == ('refused', 'too_soon'):
        # Nothing the box shows changes: the same pending request is sent once the minute's floor is over.
        return min(max(answer.get('retry_after', FLOOR), 1), FLOOR) + FLOOR_MARGIN
    fqdn = answer.get('fqdn', '')
    asked_for = _requested_name(request[0]) if request else ''
    _record['last_answer'] = f'{status}:{reason}' if reason else status
    _record['install_problem'] = ''
    renewing = bool(_held) and _renewal_due(now)
    failed = (status, reason) not in NOT_FAILURES
    wait = _wait_after(answer)

    if (status, reason) in NAME_LOST:
        _drop()
        _record.update(fqdn='', not_after='', check='', failing_since='')
    elif status in ('name', 'pending') and asked_for and fqdn != asked_for:
        # The name the request was made for is no longer this box's: the next request names the new one.
        _drop()
        _record.update(fqdn=fqdn, not_after='', check='', failing_since='')
        failed, wait = False, FOLLOW_UP
    elif status == 'name' and asked_for:
        # A request for the very name given is not taken: the next one is new, tomorrow.
        _plan['new_request'] = True
    elif status in ('name', 'pending'):
        _record['fqdn'] = fqdn
        wait = FOLLOW_UP if status == 'name' else wait
    elif status == 'refused' and reason == 'invalid_request' and request and _plan['sent_ip']:
        # A question without `ip` is refused for that, not for its request: the request and its key stay, and the
        # same one is sent again once an address appears or tomorrow.
        _plan['new_request'] = True
    elif status == 'issued':
        failed = _take_certificate(answer, request, server_time, now) if request else True

    _record['in_queue'] = status == 'pending' and answer['retry_after'] >= QUEUED_WAIT
    held_valid = bool(_held) and _held['not_after'] > now
    lapsed = not held_valid and failed and _record['status'] == 'issued' and _ended(_record['not_after'], now)
    if held_valid or lapsed:
        # The name in use, or the one that ended, stays what the self-test shows while renewal does not succeed.
        _record.update(status='issued', reason='', renewal_failing=(renewing or lapsed) and failed)
    else:
        _record.update(status=status, reason=reason, renewal_failing=False)
    if held_valid:
        _record['not_after'] = trust.iso_time(_held['not_after'])
    elif status == 'issued':
        _record['not_after'] = answer['not_after'][:40]
    return wait


def _question(server, now):
    """Ask, act on the answer, and plan the next question."""
    address = _address() or ''
    # Recorded before the box asks: a question that fails on the box before it is sent still counts, so the
    # address it carried does not bring the next question forward at every wake.
    _plan.update(sent_ip=address, asked_at=now)
    answer, request, server_time = _ask(server, address, now)
    _record.update(asked_at=trust.iso_time(now), server=server)
    wait = _take(answer, request, server_time, now)
    _plan['question_at'] = now + datetime.timedelta(seconds=wait)


# ------------------------------------------------------------------------------------------- record and log

def _save():
    def change(state):
        state[RECORD_KEY] = dict(_record)

    if update.read_state().get(RECORD_KEY, EMPTY_RECORD) != _record:
        update.change_state(change)


def _tell():
    """One information line per change of what the box knows of its name; nothing when a question changed none."""
    said = {key: _record[key] for key in ('status', 'reason', 'fqdn', 'check', 'install_problem', 'renewal_failing')}
    said['served'] = bool(_held.get('served'))
    if said != _said:
        _said.clear()
        _said.update(said)
        _logger.info('Viindoo name of the box: %s', ', '.join(
            f'{key} {value}' for key, value in said.items() if value not in ('', False)) or 'none')


def _warn_once(error):
    """Log a failure of the client unless the last one was of the same kind; never its message, which may quote
    what was being written."""
    kind = type(error).__name__ if error else ''
    if _errors != [kind]:
        _errors[:] = [kind]
        if kind:
            _logger.warning('The Viindoo name of the box met an error (%s); it tries again later', kind)


def _read_record():
    record = update.read_state().get(RECORD_KEY)
    _record.clear()
    _record.update(EMPTY_RECORD)
    if isinstance(record, dict):
        _record.update({key: record[key] for key, empty in EMPTY_RECORD.items()
                        if type(record.get(key)) is type(empty)})


def _forget():
    """Drop all that a name left on the box once the box may hold none from its server, and empty the record."""
    helpers = _helpers()
    if (_held or _record != EMPTY_RECORD or _exists(SITE_FILES[0]) or _pair_may_exist
            or helpers.get_conf(trust.MANAGED_NAME_KEY) or helpers.get_conf(MANAGED_NAME_SERVER_KEY)):
        _drop()
        _record.clear()
        _record.update(EMPTY_RECORD)
    _plan.update(question_at=None, check_at=None, check_retries=0, sent_ip=None, new_request=False)


def _situation():
    """(whether the box may hold a name from its server, the address of that server)."""
    helpers = _helpers()
    server = (helpers.get_conf(trust.SERVER_KEY) or '').strip().rstrip('/')
    own = (helpers.get_conf(trust.BOX_ADDRESS_KEY) or '').strip()
    stored = (helpers.get_conf(MANAGED_NAME_SERVER_KEY) or '').strip().rstrip('/') or _record['server']
    allowed = pairing.is_paired() and bool(server) and not own and (not stored or stored == server)
    return allowed, server


# ------------------------------------------------------------------------------------------- the client

def reconcile():
    """At each start, keep the name only if the box is paired with the server it got it from and has no name the
    installer typed for it; otherwise drop it and report the address. Never raises."""
    with _lock:
        try:
            _read_record()
            _prune_pairs()
            _load_held()
            allowed, _server = _situation()
            if not allowed:
                _forget()
            elif not _held:
                # No pair loads: a block of the name left on the disk would make the web server refuse its whole
                # configuration at its next start.
                _unserve()
            elif _held['served'] and not _nginx_accepts():
                _drop(files=False)
            elif not _held['served']:
                _clear_managed_name()
            _save()
            _tell()
        except Exception as error:
            _logger.warning('The Viindoo name of the box could not be reconciled at its start (%s)',
                            type(error).__name__)


def begin():
    """Plan the first question of this start, FIRST_QUESTION seconds from now."""
    with _lock:
        _plan.update(question_at=_now() + datetime.timedelta(seconds=FIRST_QUESTION), check_at=None,
                     check_retries=0, sent_ip=None, asked_at=None, new_request=False)


def tick():
    """Do what is due: lose an ended name, ask when the time or a new address says so, check the name.

    Returns the seconds after which the next look is due. Never raises.
    """
    with _lock:
        now = _now()
        failure = None
        try:
            _tick(now)
        except Exception as error:
            failure = error
            _plan['question_at'] = now + datetime.timedelta(seconds=UNREACHABLE_WAIT)
        try:
            _record['next_question_at'] = trust.iso_time(_plan['question_at']) if _plan['question_at'] else ''
            _tell()
            _save()
        except Exception as error:
            failure = failure or error
        _warn_once(failure)
        due = [moment for moment in (_plan['question_at'], _held and _plan['check_at']) if moment]
        return max(min([(moment - now).total_seconds() for moment in due] + [WAKE]), 1)


def _tick(now):
    allowed, server = _situation()
    if not allowed:
        _forget()
        return
    if not _held_read:
        # The files of the name could not be read at the start: no question carries a new key before they are.
        _load_held()
    if _plan['question_at'] is None or (_plan['question_at'] - now).total_seconds() > NO_PROVIDER_WAIT + JITTER:
        # The box was not allowed a name until now, or its clock went back far.
        _plan['question_at'] = now + datetime.timedelta(seconds=FIRST_QUESTION)
    if _held and _held['not_after'] <= now:
        _drop()
        _record.update(check='', failing_since='')
        _plan['question_at'] = now
    address = _address() or ''
    if _plan['sent_ip'] is not None and address and address != _plan['sent_ip']:
        _plan['question_at'] = min(_plan['question_at'], max(now, _plan['asked_at'] + datetime.timedelta(
            seconds=FOLLOW_UP)))
        _plan['check_retries'] = CHECK_RETRIES
    for _number in range(QUESTIONS_AT_ONCE):
        if _plan['question_at'] > now:
            break
        _question(server, now)
    if _held and _plan['check_at'] and _plan['check_at'] <= now:
        _check(now)


class _Keeper(threading.Thread):
    """Runs the client from the start of the box service on; `stop` ends it."""

    def __init__(self):
        super().__init__(name='ViinPublicName', daemon=True)
        self._stopped = threading.Event()

    def stop(self):
        self._stopped.set()

    def run(self):
        begin()
        wait = WAKE
        while not self._stopped.wait(wait):
            wait = tick()


reconcile()
KEEPER = _Keeper()
KEEPER.start()
