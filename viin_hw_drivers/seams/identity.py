"""Seams of identity: what the box says of itself to its server.

Its address, the software release it runs and its hardware address. Core's
device manager computes all three when core is imported, before any seam
applies, so each seam also corrects the value the manager already holds: the
first report of every start carries what the box says of itself, not core's.
"""
import array
import fcntl
import functools
import inspect
import ipaddress
import logging
import re
import socket
import struct
import sys
from pathlib import Path

import netifaces

from .. import trust
from . import CORE, FunctionSeam, SeamError, core, register

_logger = logging.getLogger(__name__)

MANAGER = f'{CORE}.main'
HELPERS = f'{CORE}.tools.helpers'
LIVE_CHANNEL = f'{CORE}.websocket_client'

# Where the address the box reports comes from, in the order it is chosen.
OWN_NAME = 'own_name'            # the name the installer gave the box
VIINDOO_NAME = 'viindoo_name'    # the name Viindoo issued for the box
ADDRESS = 'address'              # an IPv4 address of the site's network
LOCAL_NAME = 'local_name'        # the box has no such address: its name on the local network
CORE_VALUE = 'core'              # the seam is not in place: what core computes

# Where the hardware address the box reports comes from.
WIRED = 'wired'                  # the wired interface the board is built with
WIRELESS = 'wireless'            # a board without one: the wireless interface it is built with
# CORE_VALUE: neither can be read, or the seam is not in place.

NETWORK_INTERFACES = Path('/sys/class/net')
DEVICE_TREE = Path('/sys/firmware/devicetree/base')
ETHERNET_TYPE = '1'
SIOCETHTOOL = 0x8946
ETHTOOL_GPERMADDR = 0x20
MAX_ADDRESS_LENGTH = 32
IFREQ_SIZE = 40

_HOST_NAME = re.compile(r'(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*')
_HARDWARE_ADDRESS = re.compile(r'[0-9a-f]{2}(:[0-9a-f]{2}){5}')
_NO_HARDWARE_ADDRESS = ('00:00:00:00:00:00', 'ff:ff:ff:ff:ff:ff')

# The functions of core each seam wraps, for the public functions below.
_originals = {}
_said = {}


def _helpers():
    return core('tools.helpers')


def _names(function):
    return set(inspect.unwrap(function).__code__.co_names)


def _tell_once(what, value, message, *arguments):
    """Log `message` when what the box says of `what` is not what it said last."""
    if _said.get(what) != value:
        _said[what] = value
        _logger.info(message, *arguments)


def browsable(address):
    """Whether a browser on the site's network can be sent to `address`.

    A host name is, an IPv4 address of a network is. A link-local, loopback,
    unspecified, multicast or reserved address is not, and no IPv6 address is:
    the server writes the address into links without brackets.
    """
    if not isinstance(address, str) or not address:
        return False
    try:
        found = ipaddress.ip_address(address)
    except ValueError:
        return bool(_HOST_NAME.fullmatch(address)) and address != 'localhost' and not address.endswith('.localhost')
    return found.version == 4 and not (
        found.is_link_local or found.is_loopback or found.is_unspecified or found.is_multicast or found.is_reserved)


def _is_ip_address(value):
    try:
        ipaddress.ip_address(value)
    except (TypeError, ValueError):
        return False
    return True


def _site_addresses():
    """The IPv4 addresses of the site's network the box holds, those of its built-in interfaces first."""
    built_in = [name for name, _wireless in _built_in_interfaces()]
    others = sorted(set(netifaces.interfaces()) - set(built_in))
    for name in built_in + others:
        try:
            entries = netifaces.ifaddresses(name).get(netifaces.AF_INET, ())
        except ValueError:
            continue
        for entry in entries:
            if browsable(entry.get('addr')):
                yield entry['addr']


def _choose_address(core_value):
    helpers = _helpers()
    for source, key in ((OWN_NAME, trust.BOX_ADDRESS_KEY), (VIINDOO_NAME, trust.MANAGED_NAME_KEY)):
        # Spelled as the lock spells the names by which a browser reaches the box.
        name = (helpers.get_conf(key) or '').strip().lower()
        if browsable(name):
            return {'address': name, 'source': source, 'core': core_value}
    # Only an address: core derives a name from the certificate zone of its vendor, which the box never reports.
    if _is_ip_address(core_value) and browsable(core_value):
        return {'address': core_value, 'source': ADDRESS, 'core': core_value}
    site = next(_site_addresses(), '')
    if site:
        return {'address': site, 'source': ADDRESS, 'core': core_value}
    return {'address': f'{helpers.get_hostname().lower()}.local', 'source': LOCAL_NAME, 'core': core_value}


def reported_address():
    """The address the box reports to its server, which the server hands to browsers.

    {'address', 'source', 'core'}: `source` is OWN_NAME, VIINDOO_NAME, ADDRESS,
    LOCAL_NAME, or CORE_VALUE when seam 7 is not in place; `core` is the value
    core computes, kept to show what was set aside.
    """
    manager = core('main').manager
    original = _originals.get('domain')
    if original is None:
        value = manager._get_domain()
        return {'address': value, 'source': CORE_VALUE, 'core': value}
    return _choose_address(original(manager))


@functools.cache
def release_mark():
    """What the detailed version of the box ends with: `+viin.<release>`, `+viin` when the layer names none."""
    try:
        # Not at the top: loading the seams to check them must not start what the update file starts.
        from .. import update

        name = update.release()
    except Exception:
        _logger.exception('The release the box runs cannot be read: the box reports none')
        name = ''
    return f'+viin.{name}' if name else '+viin'


def _bus(path):
    subsystem = path / 'subsystem'
    return subsystem.resolve().name if subsystem.exists() else ''


def _built_in(device):
    """Whether the network device is part of the board rather than something plugged into one of its ports.

    On a board the device tree describes, the board's own devices are those
    it describes; a device on the USB bus may be described at the level of
    the USB device rather than of its interface. Elsewhere, a device that is
    not on the USB bus.
    """
    device = device.resolve()
    if not DEVICE_TREE.is_dir():
        return _bus(device) != 'usb'
    described = [device, device.parent] if _bus(device) == 'usb' else [device]
    return any((path / 'of_node').exists() for path in described)


def _built_in_interfaces():
    """[(name, wireless)] of the Ethernet-type interfaces built into the board, the wired ones first."""
    try:
        names = sorted(entry.name for entry in NETWORK_INTERFACES.iterdir())
    except OSError:
        return []
    found = []
    for name in names:
        interface = NETWORK_INTERFACES / name
        try:
            kind = (interface / 'type').read_text(encoding='ascii').strip()
            if kind != ETHERNET_TYPE or not (interface / 'device').exists() or not _built_in(interface / 'device'):
                continue
        except OSError:
            continue
        found.append((name, (interface / 'wireless').exists() or (interface / 'phy80211').exists()))
    return sorted(found, key=lambda entry: entry[1])


def _spelled(octets):
    return ':'.join(f'{octet:02x}' for octet in octets)


def _permanent_address(name):
    """The address the interface's hardware carries, whatever address it uses now; '' when it gives none."""
    request = array.array('B', struct.pack('II', ETHTOOL_GPERMADDR, MAX_ADDRESS_LENGTH) + bytes(MAX_ADDRESS_LENGTH))
    pointer, _length = request.buffer_info()
    interface_request = struct.pack('16sP', name.encode()[:15], pointer).ljust(IFREQ_SIZE, b'\0')
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
            fcntl.ioctl(channel.fileno(), SIOCETHTOOL, interface_request)
    except OSError:
        return ''
    size = struct.unpack_from('I', request, 4)[0]
    return _spelled(request[8:8 + size]) if size == 6 else ''


def _interface_address(name):
    address = _permanent_address(name)
    if address in _NO_HARDWARE_ADDRESS or not address:
        try:
            address = (NETWORK_INTERFACES / name / 'address').read_text(encoding='ascii').strip().lower()
        except OSError:
            return ''
    return address if _HARDWARE_ADDRESS.fullmatch(address) and address not in _NO_HARDWARE_ADDRESS else ''


def _choose_hardware_address(original):
    for name, wireless in _built_in_interfaces():
        address = _interface_address(name)
        if address:
            return {'address': address, 'interface': name, 'source': WIRELESS if wireless else WIRED}
    return {'address': original(), 'interface': '', 'source': CORE_VALUE}


def hardware_address():
    """The hardware address the box reports, the same whichever interface carries its traffic.

    {'address', 'interface', 'source'}: `source` is WIRED, WIRELESS, or
    CORE_VALUE - core's answer, when no built-in interface can be read or
    seam 17 is not in place; `interface` is '' then.
    """
    original = _originals.get('mac_address')
    if original is None:
        return {'address': _helpers().get_mac_address(), 'interface': '', 'source': CORE_VALUE}
    return _choose_hardware_address(original)


class _HeldByManager(FunctionSeam):
    """A function whose answer core's device manager keeps in `attribute` from the moment core is imported."""

    attribute = ''

    def _manager(self):
        try:
            return core('main').manager
        except (KeyError, AttributeError):
            raise SeamError("core's device manager does not exist") from None

    def check(self):
        super().check()
        manager = self._manager()
        if not hasattr(manager, self.attribute):
            raise SeamError(f'the device manager no longer keeps {self.attribute}')
        try:
            reported = _names(type(manager).send_alldevices)
        except AttributeError:
            raise SeamError('Manager.send_alldevices does not exist') from None
        if self.attribute not in reported:
            raise SeamError(f'Manager.send_alldevices no longer reports {self.attribute}')

    def current(self, manager):
        raise NotImplementedError

    def apply(self):
        super().apply()
        manager = self._manager()
        setattr(manager, self.attribute, self.current(manager))


@register
class ReportedAddress(_HeldByManager):
    number = 7
    module = MANAGER
    name = 'Manager._get_domain'
    parameters = ('self',)
    attribute = 'domain'
    feature = ("the box reports the installer's own name, else its Viindoo name, else an address of the site's "
               'network, and never one a browser cannot open')

    def replacement(self, original):
        _originals['domain'] = original

        def _get_domain(manager):
            chosen = _choose_address(original(manager))
            _tell_once('address', (chosen['source'], chosen['address']),
                       'The box reports the address %s to its server (%s)', chosen['address'], chosen['source'])
            return chosen['address']

        return _get_domain

    def current(self, manager):
        return manager._get_domain()


@register
class ReportedVersion(_HeldByManager):
    number = 11
    module = HELPERS
    name = 'get_version'
    parameters = ('detailed_version',)
    attribute = 'version'
    feature = 'the version the box reports names the Viindoo release it runs'

    def replacement(self, original):
        _originals['version'] = original

        # The short form is left as core gives it: core reads it as a number.
        def get_version(detailed_version=False):
            version = original(detailed_version=detailed_version)
            return f'{version}{release_mark()}' if detailed_version and version else version

        return get_version

    def current(self, manager):
        return _helpers().get_version(detailed_version=True)


@register
class ReportedHardwareAddress(_HeldByManager):
    number = 17
    module = HELPERS
    name = 'get_mac_address'
    parameters = ()
    attribute = 'mac_address'
    feature = ('the box reports one hardware address for its whole life, and obeys the live channel commands sent '
               'to that address, whether it is on cable or on Wi-Fi')

    def check(self):
        super().check()
        # A command reaches its devices only when the address it names is the one the box reports.
        try:
            live_channel = sys.modules[LIVE_CHANNEL]
            compared = _names(live_channel.on_message) & _names(live_channel.WebsocketClient.on_open)
        except (KeyError, AttributeError):
            raise SeamError(f'{LIVE_CHANNEL} has no on_message or WebsocketClient.on_open') from None
        if 'get_mac_address' not in compared:
            raise SeamError("the live channel no longer asks core's helpers for the hardware address of the box")
        bound = sorted(
            module_name for module_name, module in list(sys.modules.items())
            if module_name.startswith(CORE) and module_name != HELPERS and module is not None
            and 'get_mac_address' in vars(module))
        if bound:
            raise SeamError('these files of core keep get_mac_address under a name of their own: %s' % ', '.join(bound))

    def replacement(self, original):
        _originals['mac_address'] = original

        def get_mac_address():
            found = _choose_hardware_address(original)
            _tell_once('hardware address', (found['source'], found['interface'], found['address']),
                       'The box reports the hardware address %s to its server (%s %s)',
                       found['address'], found['source'], found['interface'] or 'interface of core')
            return found['address']

        return get_mac_address

    def current(self, manager):
        return _helpers().get_mac_address()
