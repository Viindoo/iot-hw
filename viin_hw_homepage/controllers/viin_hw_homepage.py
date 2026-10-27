"""The route by which an installer adds a network printer that does not announce itself on the network of the box.

Such a printer - typically one that takes raw jobs on port 9100 - is never
found by the print server alone. The box gives it a print queue whose name
makes core's printer interface take it for the kind the installer chose; core
finds the queue at its next look at the printers and the server learns of the
printer then. What the caller sends is never logged nor sent back.
"""
import functools
import inspect
import ipaddress
import logging
import re
import traceback

from odoo import http
from odoo.http import request

from odoo.addons.viin_hw_drivers import device_tests, lock, seams

_logger = logging.getLogger(__name__)

BOX_ERROR = {'status': 'failed', 'reason': 'box_error'}
DEFAULT_PORT = 9100
# What the installer chooses, the kind core then gives the printer, and the word a queue name needs for that kind.
KINDS = {
    'label': (device_tests.LABEL, device_tests.LABEL_MARK),
    'receipt': (device_tests.RECEIPT, device_tests.RECEIPT_NAMES[0].upper()),
    'office': (device_tests.OFFICE, ''),
}
MAX_ADDRESS = 255
MAX_NAME = 60
MAX_COPIES = 99
DEFAULT_NAME = 'Network_printer'

_IPV6 = re.compile(r'\[(?P<host>[0-9A-Fa-f:.]+)\](?::(?P<port>[0-9]{1,5}))?')
_OTHER = re.compile(r'(?P<host>[A-Za-z0-9.-]+)(?::(?P<port>[0-9]{1,5}))?')
_LABEL = re.compile(r'(?!-)[A-Za-z0-9-]{1,63}(?<!-)')
_NOT_IN_NAME = re.compile(r'[^A-Za-z0-9_-]+')


def _contained(endpoint):
    """Decorator of a route: an error the route did not foresee is answered BOX_ERROR, quoting nothing it was sent."""
    @functools.wraps(endpoint)
    def contained(*args, **kwargs):
        try:
            return endpoint(*args, **kwargs)
        except Exception as error:
            origin = traceback.extract_tb(error.__traceback__)[-1]
            _logger.error(
                '%s failed on this box: %s at %s:%s',
                request.httprequest.path, type(error).__name__, origin.filename, origin.lineno)
            return dict(BOX_ERROR)

    return contained


def _refused(reason):
    return {'status': 'refused', 'reason': reason}


def _ip(text):
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _host(text):
    """`text` as the host of a printer address, None when it is none: no name or address of the box itself."""
    found = _ip(text)
    if found is None:
        if text.replace('.', '').isdigit():
            return None
        labels = text.rstrip('.').split('.')
        if len(text) > 253 or not all(map(_LABEL.fullmatch, labels)) or text.lower().rstrip('.') == 'localhost':
            return None
        return text.lower().rstrip('.')
    if found.is_loopback or found.is_unspecified or found.is_multicast:
        return None
    return f'[{found}]' if found.version == 6 else str(found)


def device_uri(address):
    """The print-server address of a printer that takes raw jobs at `address`, None when `address` is not one.

    `address` is a host name, an IPv4 address or an IPv6 address, with or
    without a port (an IPv6 address with a port in brackets); the port is
    DEFAULT_PORT when none is given.
    """
    if not isinstance(address, str) or len(address) > MAX_ADDRESS:
        return None
    text = address.strip()
    bare = _ip(text)
    if bare is not None and bare.version == 6:
        found = {'host': text, 'port': None}
    else:
        found = _IPV6.fullmatch(text) or _OTHER.fullmatch(text)
    if not found:
        return None
    host = _host(found['host'])
    port = int(found['port'] or DEFAULT_PORT)
    if not host or not 0 < port < 65536:
        return None
    return f'socket://{host}:{port}'


def kind_by_name(queue):
    """The kind core's printer interface gives a printer known only by the name of its queue."""
    lowered = queue.lower()
    if any(word in lowered for word in device_tests.RECEIPT_NAMES):
        return device_tests.RECEIPT
    if any(word in lowered for word in device_tests.LABEL_NAMES):
        return device_tests.LABEL
    return device_tests.OFFICE


def _base_name(name, uri):
    """The start of the queue name: the name the installer gave, else one made of the address; '' when unusable."""
    if name is None or name == '':
        host = uri.removeprefix('socket://').rsplit(':', 1)[0]
        return f'{DEFAULT_NAME}_{_NOT_IN_NAME.sub("_", host)}'.strip('_')[:MAX_NAME]
    if not isinstance(name, str) or len(name) > MAX_NAME:
        return ''
    return _NOT_IN_NAME.sub('_', name.strip()).strip('_-')


def _queue_name(base, mark, taken):
    """A queue name not in `taken` (lower case) that starts with `base` and ends with `mark`; '' when none is left."""
    for copy in range(1, MAX_COPIES + 1):
        start = base if copy == 1 else f'{base}_{copy}'
        name = f'{start}_{mark}' if mark else start
        if name.lower() not in taken:
            return name
    return ''


def _printer_interface():
    """(core's printer interface, its print-server connection, the lock that serialises it), or None."""
    for interface in seams.core('main').interfaces.values():
        # The layer wraps `get_devices`: the connection is a name of the file of core that defines it.
        namespace = getattr(inspect.unwrap(getattr(interface, 'get_devices', None)), '__globals__', {})
        if getattr(interface, 'connection_type', '') == 'printer' and {'conn', 'cups_lock'} <= namespace.keys():
            return interface, namespace['conn'], namespace['cups_lock']
    return None


def add_network_printer(address, kind, name=None):
    """Give the printer that takes raw jobs at `address` a print queue core takes for a printer of `kind`.

    {'status': 'added', 'queue', 'identifier', 'kind', 'appears_within'}:
    `identifier` is the one core gives the device, `appears_within` the
    seconds within which core finds it. {'status': 'refused', 'reason':
    invalid_address, invalid_kind, invalid_name or already_added (+ `queue`,
    the queue the box already has for that address)}; {'status': 'failed',
    'reason': print_server_unavailable or print_server_error}.
    """
    if not isinstance(kind, str) or kind not in KINDS:
        return _refused('invalid_kind')
    uri = device_uri(address)
    if not uri:
        return _refused('invalid_address')
    wanted, mark = KINDS[kind]
    base = _base_name(name, uri)
    if mark and mark.lower() in base.lower():
        mark = ''
    if not base or kind_by_name(f'{base}_{mark}' if mark else base) != wanted:
        return _refused('invalid_name')
    found = _printer_interface()
    if not found:
        return {'status': 'failed', 'reason': 'print_server_unavailable'}
    interface, connection, guard = found
    try:
        with guard:
            queues = connection.getPrinters()
        existing = next((queue for queue, attributes in sorted(queues.items())
                         if str(attributes.get('device-uri', '')).lower() == uri.lower()), None)
        if existing:
            return dict(_refused('already_added'), queue=existing)
        queue = _queue_name(base, mark, {queue.lower() for queue in queues})
        if not queue:
            return _refused('invalid_name')
        with guard, seams.core('tools.helpers').writable():
            connection.addPrinter(name=queue, device=uri)
            connection.enablePrinter(queue)
            connection.acceptJobs(queue)
            connection.setPrinterUsersAllowed(queue, ['all'])
    except Exception as error:
        _logger.warning('The print server refused a new network printer: %s', type(error).__name__)
        return {'status': 'failed', 'reason': 'print_server_error'}
    _logger.info('A network printer was given the print queue %s, which core takes for a %s', queue, wanted)
    return {
        'status': 'added',
        'queue': queue,
        'identifier': interface.get_identifier(None, queue),
        'kind': wanted,
        'appears_within': interface._loop_delay,
    }


class BoxPrinters(http.Controller):

    @http.route('/viin_hw_homepage/printer_add', type='json', auth='none', methods=['POST'], save_session=False)
    @_contained
    @lock.protected
    def printer_add(self, address=None, kind=None, name=None):
        """Add a network printer that does not announce itself: `add_network_printer`. It changes a setting."""
        return add_network_printer(address, kind, name)
