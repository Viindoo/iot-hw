"""The lock of the box settings.

A box that is connected to a server is locked: what changes its settings asks
for the settings code, which the administrators of that server read there and
which the box works out from its own credential. Nothing is stored for it.

Locked or not, the box refuses a change of its settings that a browser marks as
asked by a page of another web site.
"""
import base64
import functools
import hashlib
import hmac
import ipaddress
import logging
import re
import time

import netifaces

from odoo.http import request

from . import trust
from .seams import core

_logger = logging.getLogger(__name__)

SETTINGS_CODE_LABEL = b'viin-iot-box-settings-code'
PROOF_LABEL = b'viin-iot-box-unlock-proof'
ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
PROOF_LIFETIME = 15 * 60
PROOF_HEADER = 'X-Viindoo-Unlock'
REFUSAL = {'status': 'locked', 'reason': 'unlock_required'}
# The refusal no unlock proof lifts: the request is one a browser sent for a page of another web site.
OTHER_SITE = {'status': 'forbidden', 'reason': 'other_site'}
DEFAULT_PORTS = {'http': 80, 'https': 443}
# What `Sec-Fetch-Site` says of a request no other web site asked for: a page of the address itself, or a person.
OWN_SITE = ('same-origin', 'none')

# A person types the code: the letters the alphabet leaves out are read as the digits they look like.
_AS_TYPED = str.maketrans({'O': '0', 'I': '1', 'L': '1', ' ': None, '-': None})
# A host and a port written so that a browser, a command line and this file all read the same ones.
_AUTHORITY = r'(?P<host>[A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])(?::(?P<port>[0-9]{1,5}))?'
_HOST = re.compile(_AUTHORITY)
_ADDRESS = re.compile(rf'(?P<scheme>[A-Za-z]+)://{_AUTHORITY}(?P<rest>[/?#].*)?', re.DOTALL)
# What may follow the host of an address that is kept: core expands a percent sign with the other values of the
# configuration, the credential among them, when it reads the address back.
_PLAIN = re.compile(r"[A-Za-z0-9!#$&'()*+,\-./:;=?@_~]*")


def _token():
    return core('tools.helpers').get_token() or ''


def _same(left, right):
    return hmac.compare_digest(left.encode(errors='replace'), right.encode(errors='replace'))


def is_locked():
    return bool(_token())


def _host(name):
    """`name` in the one spelling every reader gives it - lower case, an address at its shortest - or None.

    A browser takes a name that ends in a number for an address, in spellings
    this file does not follow: such a name counts only as four decimal parts.
    """
    name = name.lower()
    try:
        if name.startswith('['):
            return str(ipaddress.IPv6Address(name[1:-1]))
        last = name.rpartition('.')[2]
        if last.isdigit() or last.startswith('0x'):
            return name if str(ipaddress.IPv4Address(name)) == name else None
    except ValueError:
        return None
    return name


def origin(address, plain=False):
    """(scheme, host, port) of a web address, None unless every reader of `address` is sure to find the same three.

    The address is http or https and names its host and port bare: no user
    part, no backslash, no space. With `plain`, what follows the host is free
    of percent signs and of every character a reader may drop or rewrite.
    """
    found = _ADDRESS.fullmatch(address) if isinstance(address, str) else None
    scheme = found and found['scheme'].lower()
    if scheme not in DEFAULT_PORTS or (plain and not _PLAIN.fullmatch(found['rest'] or '')):
        return None
    host, port = _host(found['host']), int(found['port'] or DEFAULT_PORTS[scheme])
    return (scheme, host, port) if host and 0 < port < 65536 else None


def box_hosts():
    """The names and addresses by which a browser reaches this box, each as `origin` spells a host."""
    helpers = core('tools.helpers')
    name = helpers.get_hostname().lower()
    # The installer's name for the box and the name Viindoo issued for it: the box reports them as its address.
    found = {'localhost', name, f'{name}.local'}
    found.update(
        (helpers.get_conf(key) or '').strip().lower() for key in (trust.BOX_ADDRESS_KEY, trust.MANAGED_NAME_KEY))
    for interface in netifaces.interfaces():
        for family in (netifaces.AF_INET, netifaces.AF_INET6):
            for entry in netifaces.ifaddresses(interface).get(family, ()):
                found.add(str(ipaddress.ip_address(entry['addr'].split('%')[0])))
    return found - {''}


def from_other_site():
    """Whether the browser that sent the current request marks it as asked by a page of another web site.

    It does with a `Sec-Fetch-Site` other than `same-origin` or `none`, or with
    an `Origin` or a `Referer` whose host is neither the one the request
    addresses nor this box - the web server of the box hands requests on under
    a host of its own. A request without these headers is not marked: a script
    sends none, nor does a browser for an address a person types.
    """
    httprequest = request.httprequest
    site = httprequest.headers.get('Sec-Fetch-Site')
    if site is not None and site.strip().lower() not in OWN_SITE:
        return True
    senders = [origin(sender) for sender in map(httprequest.headers.get, ('Origin', 'Referer')) if sender is not None]
    if not senders:
        return False
    addressed = _HOST.fullmatch(httprequest.host or '')
    own = box_hosts()
    if addressed and _host(addressed['host']):
        own.add(_host(addressed['host']))
    return not all(sender and sender[1] in own for sender in senders)


def settings_code(token):
    """The settings code of the box whose credential is `token`: 8 symbols, 40 bits, one way."""
    digest = hmac.new(token.encode(), SETTINGS_CODE_LABEL, hashlib.sha256).digest()
    bits = int.from_bytes(digest[:5], 'big')
    return ''.join(ALPHABET[(bits >> shift) & 31] for shift in range(35, -1, -5))


def issue_proof(token):
    """An unlock proof for the box whose credential is `token`, valid PROOF_LIFETIME seconds from now."""
    return _sign(token, int(time.time()) + PROOF_LIFETIME)


def _sign(token, expires):
    key = hmac.new(token.encode(), PROOF_LABEL, hashlib.sha256).digest()
    signature = hmac.new(key, str(expires).encode(), hashlib.sha256).digest()
    return f'{expires}.{base64.urlsafe_b64encode(signature).rstrip(b"=").decode()}'


def proof_holds(proof, token):
    """Whether `proof` was issued for the box whose credential is `token` and has not run out."""
    proof = str(proof or '')
    expires = proof.partition('.')[0]
    if not token or not (expires.isascii() and expires.isdigit() and len(expires) <= 12):
        return False
    remaining = int(expires) - time.time()
    return _same(proof, _sign(token, int(expires))) and 0 < remaining <= PROOF_LIFETIME


def unlock(typed_code):
    """The unlock proof when `typed_code` is the settings code of this box, None otherwise.

    The caller sends the proof back in the header PROOF_HEADER of each request
    that changes a setting. A box that is not locked needs no proof and gets none.
    """
    token = _token()
    if not token:
        return None
    typed = str(typed_code or '').strip().upper().translate(_AS_TYPED)
    if not _same(typed, settings_code(token)):
        _logger.warning('A wrong settings code was given to unlock this box')
        return None
    return issue_proof(token)


def is_unlocked():
    """Whether the current request may change the settings: the box is not locked, or the request proves an unlock."""
    token = _token()
    return not token or proof_holds(request.httprequest.headers.get(PROOF_HEADER), token)


def refusal(answer=REFUSAL):
    """What a route answers to a request it does not serve: `answer`, with status 403 where it can."""
    if request.dispatcher.routing_type == 'json':
        return dict(answer)
    return request.make_json_response(answer, status=403)


def refusing_other_sites(endpoint):
    """Decorator of a controller method, under `@http.route`: a request `from_other_site` is answered OTHER_SITE."""
    @functools.wraps(endpoint)
    def guarded(*args, **kwargs):
        if from_other_site():
            _logger.warning(
                'A request that a browser sent for a page of another web site was refused on %s',
                request.httprequest.path)
            return refusal(OTHER_SITE)
        return endpoint(*args, **kwargs)

    return guarded


def protected(endpoint):
    """Decorator of a controller method, under `@http.route`, for what changes a setting.

    The box runs it for no request `from_other_site` and, when it is locked,
    for an unlocked request only.
    """
    @functools.wraps(endpoint)
    def guarded(*args, **kwargs):
        if is_unlocked():
            return endpoint(*args, **kwargs)
        return refusal()

    return refusing_other_sites(guarded)
