"""Connecting the box to its server, and making it forget that server.

The box exchanges a one-time pairing code for its credential, over a connection
it verifies whenever the address of the server is https. Neither the code nor
the credential is ever logged, raised or returned.
"""
import contextlib
import http.client
import json
import logging
import os
import re
import socket
import ssl
import subprocess
import threading
from datetime import timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import trust
from .seams import core

_logger = logging.getLogger(__name__)

PAIR_ROUTE = '/iot/pair'
PROTOCOL = 1
TIMEOUT = 15
MAX_ANSWER = 64 * 1024
MAX_NAME = 200
RESTART_DELAY = 1
CLOCK_TOLERANCE = timedelta(minutes=5)
REFUSALS = ('invalid_code', 'expired', 'already_used', 'identifier_exists', 'server_error')
MAX_REASON = 40
# What a reason of a server is: a plain key, the only shape that is safe to show and to log.
REASON_KEY = re.compile(r'[a-z0-9_]+')
SECTION = 'iot.box'
OWN_KEYS = 'viin_'
# The keys of the layer that are settings of the box, not of its server: leaving a server keeps them. The choice
# to update by itself belongs to the box. The name typed for the box is kept only on a condition, see
# `clear_viindoo_configuration`.
KEPT_KEYS = (trust.AUTO_UPDATE_KEY,)
BOOT_PARTITIONS = ('/boot/firmware', '/boot')
# The second name is what a PC that hides file extensions makes of a new, empty text file.
RESET_FILES = ('viindoo-reset', 'viindoo-reset.txt')
MAX_AUTHORITY = 64 * 1024
# Whether a server certified by an authority of the customer's own can be paired, by giving the box the
# certificate of that root authority. False: such a server is refused, whatever `pair` is given as `authority`.
ACCEPTS_AUTHORITY = True
_confirms_readings = False
_attempt = threading.Lock()


class _NotPaired(Exception):
    """Ends an attempt; `answer` is what the caller of `pair` gets."""

    def __init__(self, status, reason, **details):
        super().__init__(reason)
        self.answer = dict(details, status=status, reason=reason)


class _WithoutSecret(logging.Filter):
    """Drops the log records that would show a secret."""

    def __init__(self, secret):
        super().__init__()
        self._secret = secret

    def filter(self, record):
        return self._secret not in record.getMessage()


def declare_confirms_readings():
    """Tell the server, in each pair request, that the box software confirms the readings it issues.

    Called once by the part of the layer that confirms them, before any pairing; it changes nothing else.
    """
    global _confirms_readings
    _confirms_readings = True


def server_address(address):
    """`scheme://host[:port]` of a server address as a person types it, https unless it says http; else ValueError."""
    address = str(address or '').strip()
    parts = urlsplit(address if '://' in address else f'https://{address}')
    # Reading the port raises ValueError when it is not a number.
    if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.port == 0
            or parts.username or parts.password or parts.path not in ('', '/') or parts.query or parts.fragment
            or any(character.isspace() or not character.isprintable() for character in parts.netloc)):
        raise ValueError('not a server address')
    return f'{parts.scheme}://{parts.netloc.lower()}'


def parse_link(link):
    """(server address, pairing code) of a pairing link `<server address>/#pair=<code>`; ValueError otherwise."""
    link = str(link or '').strip()
    address, _separator, fragment = link.partition('#')
    key, _separator, code = fragment.partition('=')
    code = unquote(code).strip()
    if key != 'pair' or not code:
        raise ValueError('not a pairing link')
    return server_address(address), code


def is_paired():
    """Whether this box holds the credential of a server."""
    return bool(core('tools.helpers').get_token())


@contextlib.contextmanager
def changing_server():
    """Run the block as the only change of the server of the box under way; it waits for the one that is.

    A pairing attempt is such a change. What allows the change is asked inside
    the block, so that no other change comes between that answer and this one.
    """
    with _attempt:
        yield


def pair(link=None, server_url=None, code=None, trust_fingerprint=None, authority=None, permitted=None):
    """Connect this box to a server with a one-time pairing code, given as `link` or as `server_url` and `code`.

    Returns one of:

    * {'status': 'paired', 'server', 'box_name'} once server and credential are
      stored the way core stores them and the box service is asked to restart;
    * {'status': 'refused', 'reason'} with the reason of the server -
      invalid_code, expired, already_used, server_error, identifier_exists with
      'box_name' - or unknown_reason with 'server_reason': the word of the
      server when it is a lowercase key of at most 40 characters, else
      'unspecified';
    * {'status': 'failed', 'reason'} when the box got no answer it understands:
      invalid_link, invalid_address and code_missing (address and code given
      apart), invalid_authority, authority_not_root, server_not_found,
      server_unreachable, server_not_compatible, box_error (another change of
      the server is in progress, or the box could not store), box_clock_wrong
      with 'box_time' and 'server_time', or server_certificate_untrusted.

    server_certificate_untrusted always carries 'fingerprint', 'subject',
    'expiry' and 'names' of one certificate, 'problem' - unknown_authority,
    private_authority, name_mismatch, expired, not_yet_valid or invalid - and
    'trustable'. A certificate no public authority vouches for is never
    accepted on its own. When 'trustable' is true the four keys describe what
    a person must confirm - the certificate of the server when it vouches for
    itself, the root authority given as `authority` when it vouches for the
    server - and the caller calls again with `trust_fingerprint` set to that
    fingerprint. When it is false they describe the certificate of the server;
    with private_authority the caller may call again with `authority`, the
    certificate of the root authority that issued it, as PEM text. The text
    may hold several roots: the first one that vouches for the server is the
    one described, confirmed and stored, and when none does the answer is the
    one for no authority at all. A text that holds no certificate fails as
    invalid_authority, one that holds no root as authority_not_root.

    Nothing is stored unless the status is `paired`. The certificates trusted
    for the server the box has do not vouch for this one, and once `paired` the
    box trusts, for this server alone, the certificate confirmed in this call
    and no other. Each attempt that does not pair leaves one warning line.

    `permitted`, when given, is asked once this attempt is the only change of
    the server of the box under way, so that no other change comes between its
    answer and the pairing: unless it says yes, nothing is attempted and None
    is returned.
    """
    if not _attempt.acquire(blocking=False):
        _logger.warning('A pairing attempt was refused: another change of the server of the box is in progress')
        return {'status': 'failed', 'reason': 'box_error'}
    try:
        if permitted is not None and not permitted():
            return None
        answer, server = _pair(link, server_url, code, trust_fingerprint, authority)
    finally:
        _attempt.release()
    if answer['status'] != 'paired':
        _logger.warning(
            'A pairing attempt did not connect this box: %s (%s)', answer['reason'], server or 'no server address')
    return answer


def _pair(link, server_url, code, trust_fingerprint, authority):
    """(the answer of `pair`, the address of the server the caller named or '')."""
    try:
        server, code = _named(link, server_url, code)
    except _NotPaired as refusal:
        return refusal.answer, ''
    try:
        answer, certificate = _exchange(
            server, code, trust_fingerprint, _root_authority(authority) if ACCEPTS_AUTHORITY else None)
    except _NotPaired as refusal:
        return refusal.answer, server
    except Exception:
        _logger.exception('The pairing attempt failed on this box')
        return {'status': 'failed', 'reason': 'box_error'}, server
    try:
        with trust.replacing_certificates(server, [certificate] if certificate else []):
            _store(server, answer['token'])
    except Exception as error:
        # The message of the error may quote what was being written.
        _logger.error('The box could not store its pairing (%s)', type(error).__name__)
        return {'status': 'failed', 'reason': 'box_error'}, server
    core('tools.helpers').odoo_restart(RESTART_DELAY)
    return {'status': 'paired', 'server': server, 'box_name': _name(answer)}, server


def _named(link, server_url, code):
    """(server address, pairing code) of what the caller gave; _NotPaired when it names none."""
    if link or not (server_url or code):
        try:
            return parse_link(link)
        except ValueError:
            raise _NotPaired('failed', 'invalid_link') from None
    try:
        server = server_address(server_url)
    except ValueError:
        raise _NotPaired('failed', 'invalid_address') from None
    code = str(code or '').strip()
    if not code:
        raise _NotPaired('failed', 'code_missing')
    return server, code


def _root_authority(text):
    """The roots, in order, of the PEM text a person gave as the authority of a server; None when none was given."""
    text = str(text or '').strip()
    if not text:
        return None
    certificates = trust.load_certificates(text.encode()) if len(text) <= MAX_AUTHORITY else []
    if not certificates:
        raise _NotPaired('failed', 'invalid_authority')
    roots = [certificate for certificate in certificates if certificate.issuer == certificate.subject]
    if not roots:
        # Only a root is an authority for every connection the box software makes to its server.
        raise _NotPaired('failed', 'authority_not_root')
    return roots


def _name(answer):
    return str(answer.get('box_name') or '')[:MAX_NAME]


def _store(server, token):
    helpers = core('tools.helpers')
    # Core writes what it saves to its debug log.
    core_log = logging.getLogger(helpers.__name__)
    without_token = _WithoutSecret(token)
    core_log.addFilter(without_token)
    try:
        helpers.save_conf_server(server, token, None, None)
    finally:
        core_log.removeFilter(without_token)


def _identity():
    """The box as core describes it to its server when it registers."""
    manager = core('main').manager
    return {
        'name': manager.hostname,
        'identifier': manager.mac_address,
        'ip': manager.domain,
        'version': manager.version,
        'serial_number': manager.serial_number,
    }


@contextlib.contextmanager
def _reaching_server():
    try:
        yield
    except (socket.gaierror, UnicodeError):
        raise _NotPaired('failed', 'server_not_found') from None
    except (OSError, http.client.HTTPException):
        raise _NotPaired('failed', 'server_unreachable') from None
    except ValueError:
        raise _NotPaired('failed', 'server_not_compatible') from None


def _untrusted(certificate, problem, trustable):
    return _NotPaired(
        'failed', 'server_certificate_untrusted', problem=problem, trustable=trustable, **trust.describe(certificate))


def _exchange(server, code, trust_fingerprint, authorities):
    """(answer of the server, certificate to trust or None) for a `paired` answer; _NotPaired for anything else."""
    address = urlsplit(server)
    secure = address.scheme == 'https'
    host, port = address.hostname, address.port or (443 if secure else 80)
    certificate = context = None
    with _reaching_server():
        if secure:
            problem = trust.verification_problem(host, port, store=False)
            if problem:
                certificate = _trusted_by_installer(host, port, problem, trust_fingerprint, authorities)
            context = trust.ssl_context([certificate] if certificate else (), store=False)
        params = {'pairing_code': code, 'iot_box': _identity(), 'protocol': PROTOCOL}
        if _confirms_readings:
            params['confirms_readings'] = True
        try:
            status, data = _post(host, port, context, {'jsonrpc': '2.0', 'method': 'call', 'params': params})
        except ssl.SSLCertVerificationError:
            # The server shows another certificate than the one that was just verified.
            raise _untrusted(trust.presented_certificate(host, port), 'invalid', False) from None
    return _read(status, data), certificate


def _trusted_by_installer(host, port, problem, trust_fingerprint, authorities):
    """The certificate to trust for the server, once a person confirmed it: its own, or the root that signs its chain.

    Whichever it is, trusting it must be all the box needs to accept the server.
    """
    presented = trust.presented_certificate(host, port)
    candidate, remaining = presented, 'unknown_authority'
    for root in authorities or ():
        root_problem = trust.verification_problem(host, port, [root], store=False)
        if root_problem != 'unknown_authority' and (remaining == 'unknown_authority' or not root_problem):
            candidate, remaining = root, root_problem
        if not remaining:
            break
    if remaining == 'unknown_authority':
        # No authority was given, or none of those given vouches for this server.
        candidate, remaining = presented, trust.verification_problem(host, port, [presented], store=False)
    if {problem, remaining} & {'expired', 'not_yet_valid'}:
        _blame_clock(host, port, presented)
    if remaining:
        raise _untrusted(presented, 'private_authority' if remaining == 'unknown_authority' else remaining, False)
    if not trust.same_fingerprint(trust_fingerprint, candidate):
        raise _untrusted(candidate, problem if candidate is presented else 'private_authority', True)
    return candidate


def _blame_clock(host, port, certificate):
    """Fail on the clock of the box when the certificate is in its dates by the clock of the server."""
    server_time = trust.unverified_server_time(host, port)
    box_time = trust.utc_now()
    if (server_time and abs(server_time - box_time) > CLOCK_TOLERANCE
            and certificate.not_valid_before <= server_time <= certificate.not_valid_after):
        raise _NotPaired(
            'failed', 'box_clock_wrong', box_time=trust.iso_time(box_time), server_time=trust.iso_time(server_time))


def _post(host, port, context, body):
    if context:
        connection = http.client.HTTPSConnection(host, port, timeout=TIMEOUT, context=context)
    else:
        connection = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
    try:
        connection.request('POST', PAIR_ROUTE, body=json.dumps(body).encode(), headers={
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        })
        response = connection.getresponse()
        return response.status, response.read(MAX_ANSWER + 1)
    finally:
        connection.close()


def _read(status, data):
    try:
        if status != 200 or len(data) > MAX_ANSWER:
            raise ValueError
        answer = json.loads(data)['result']
        state, reason = answer.get('status'), answer.get('reason')
    except (ValueError, LookupError, TypeError, AttributeError):
        raise _NotPaired('failed', 'server_not_compatible') from None
    if state == 'paired' and answer.get('token') and isinstance(answer['token'], str):
        return answer
    if state == 'refused' and reason in REFUSALS:
        details = {'box_name': _name(answer)} if reason == 'identifier_exists' else {}
        raise _NotPaired('refused', reason, **details)
    if state == 'refused':
        word = reason[:MAX_REASON] if isinstance(reason, str) else ''
        word = word if REASON_KEY.fullmatch(word) else 'unspecified'
        raise _NotPaired('refused', 'unknown_reason', server_reason=word)
    raise _NotPaired('failed', 'server_not_compatible')


def clear_viindoo_configuration(helpers=None):
    """Forget what the layer keeps for a server, and keep the settings of the box itself.

    Forgotten: the certificates trusted for the server and every other key of
    the layer, which is the Viindoo name of the box and the server it came
    from. Kept: KEPT_KEYS, and the name typed for the box while the
    certificate the web server presents is the customer's own and is issued
    for that name: both belong to the site. With any other certificate - one
    the box made, the one of the image, none - the name goes too, so that a
    box moved to another site does not send its browsers to the old one.

    `helpers` is core's helper module when core is not loaded yet.
    """
    helpers = helpers or core('tools.helpers')
    trust.forget_certificates(helpers)
    kept = set(KEPT_KEYS)
    if trust.customer_certificate_covers(helpers.get_conf(trust.BOX_ADDRESS_KEY)):
        kept.add(trust.BOX_ADDRESS_KEY)
    configuration = helpers.get_conf()
    if configuration.has_section(SECTION):
        forgotten = [key for key in configuration.options(SECTION) if key.startswith(OWN_KEYS) and key not in kept]
        if forgotten:
            helpers.update_conf(dict.fromkeys(forgotten, ''))


def reset_file():
    """The file by which a person with the card in hand asks the box to forget its server, or None."""
    for partition in BOOT_PARTITIONS:
        for name in RESET_FILES:
            candidate = Path(partition, name)
            if candidate.is_file():
                return candidate
    return None


def reset(helpers, marker):
    """Forget the server, its credential - and with it the lock - and what the layer kept for it; delete `marker`.

    What the layer kept is what `clear_viindoo_configuration` forgets.

    Runs before core is loaded, so that core starts as the box it has become;
    `helpers` is core's helper module, which the caller loaded on its own.
    """
    helpers.disconnect_from_server()
    clear_viindoo_configuration(helpers)
    partition = str(marker.parent)
    read_only = os.statvfs(partition).f_flag & os.ST_RDONLY
    if read_only:
        subprocess.run(['sudo', '-n', 'mount', '-o', 'remount,rw', partition], check=False, timeout=30)
    subprocess.run(['sudo', '-n', 'rm', '-f', '--', str(marker)], check=False, timeout=30)
    if read_only:
        subprocess.run(['sudo', '-n', 'mount', '-o', 'remount,ro', partition], check=False, timeout=30)
    if marker.exists():
        raise OSError(f'{marker} could not be deleted: the box resets at each start until it is removed')
