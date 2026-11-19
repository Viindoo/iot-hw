"""Seams of the device handlers: the handler files of the layer, the default screen page, the status ticket of a
receipt printer, the owner of a device, the listing of the devices of an interface and the USB keyboards and
scanners of core.

Core loads its handler files from one directory, which an update of core empties of every file core does not
ship. The files of the layer stay in the layer and are loaded right after those of core.
"""
import ast
import contextlib
import copy
import functools
import importlib
import importlib.util
import inspect
import ipaddress
import json
import logging
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import unquote, urlsplit

import jinja2

from odoo import http
from odoo.tools.func import lazy_property
from odoo.tools.misc import file_path

from . import CORE, FunctionSeam, PartsSeam, Seam, SeamError, core, register, report_problem
from . import apply as apply_seams

_logger = logging.getLogger(__name__)

LAYER = __name__.rsplit('.', 2)[0]
LAYER_DIRECTORY = Path(__file__).resolve().parent.parent
HANDLER_KINDS = ('interfaces', 'drivers')
CORE_HANDLERS = f'{CORE}.iot_handlers'
LAYER_HANDLERS = f'{LAYER}.iot_handlers'
SCREEN_HANDLER = 'hw_drivers/iot_handlers/drivers/DisplayDriver_L.py'
SCREEN_PAGE = 'pos_display_template'
# What the screen page of the layer reads of what core hands its own page.
SCREEN_PAGE_CONTEXT = ('display_ifaces', 'display_identifier', 'hostname')
SCREEN_PAGE_SEAM = 14
PRINTER_HANDLER = 'hw_drivers/iot_handlers/drivers/PrinterDriver_L.py'
PRINTER_DRIVER = 'PrinterDriver'
STATUS_TICKET = 'print_status'
STATUS_TICKET_SEAM = 19
ACTION_ERRORS_SEAM = 20
PRINTER_LISTING_SEAM = 22
KEYBOARD_DEVICES_SEAM = 26
PRINTER_INTERFACE = 'PrinterInterface'
PRINTER_INTERFACE_HANDLER = 'hw_drivers/iot_handlers/interfaces/PrinterInterface_L.py'
# What core's status ticket says of the box, each as the text core prints and the one a Viindoo box prints instead.
STATUS_TICKET_WORDS = {
    'title': ('IoTBox Status', 'Viindoo IoT Box'),
    'box without a network': ('the IoTBox is correc-\ntly connected', 'the box is correctly\nconnected'),
}
# The libraries by which a file can call another machine over HTTP. The layer verifies the calls made through
# the plain name `urllib3`, and no other.
HTTP_LIBRARIES = ('urllib3', 'requests', 'http.client', 'urllib.request', 'httpx', 'aiohttp', 'websocket')
MAX_ERROR = 300
# The directory the box service is given under /run before each start: in memory, kept while the box is on.
OWNERS_FILE = Path('/run/odoo/viin-device-owners.json')
MAX_OWNER = 256

_files = {'loaded': False, 'core': [], 'layer': [], 'failed': []}
_files_lock = threading.Lock()


def handler_files():
    """The handler files of this box.

    :return: ``{'loaded', 'core', 'layer', 'failed'}``; `loaded` is whether core has loaded the handlers yet,
        `core` and `layer` the files found in each tree, each as ``<interfaces or drivers>/<file name>``, `failed`
        a list of ``{'file', 'origin' (core or layer), 'error'}`` for those that could not be loaded
    :rtype: dict
    """
    with _files_lock:
        return {
            'loaded': _files['loaded'],
            'core': list(_files['core']),
            'layer': list(_files['layer']),
            'failed': [dict(failure) for failure in _files['failed']],
        }


def _describe(error):
    return f'{type(error).__name__}: {error}'[:MAX_ERROR]


def _core_files(kind):
    """The files core loads from its handler directory `kind` on this machine, as core lists them."""
    directory = file_path(f'hw_drivers/iot_handlers/{kind}')
    return sorted(core('tools.helpers').list_file_by_os(directory)), Path(directory)


def _layer_files(kind):
    directory = LAYER_DIRECTORY / 'iot_handlers' / kind
    if not directory.is_dir():
        return []
    return sorted(
        directory / name for name in core('tools.helpers').list_file_by_os(directory)
        if name.endswith('.py') and not name.startswith('_'))


class _FailedFiles(logging.Handler):
    """Collects the handler files core says it could not load: core logs each, and keeps none."""

    def __init__(self, names):
        super().__init__(logging.ERROR)
        self._names = names
        self._thread = threading.get_ident()
        self.failed = {}

    def emit(self, record):
        if record.thread != self._thread or not record.exc_info or not isinstance(record.args, tuple):
            return
        for argument in record.args:
            if isinstance(argument, str) and argument in self._names:
                self.failed[argument] = _describe(record.exc_info[1])


def _load_layer_file(kind, path):
    name = f'{LAYER_HANDLERS}.{kind}.{path.stem}'
    # A handler file that another one imported is loaded already, and must not register its devices twice.
    if name in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise


def _load_layer_files():
    for kind in HANDLER_KINDS:
        for path in _layer_files(kind):
            listed = f'{kind}/{path.name}'
            with _files_lock:
                _files['layer'].append(listed)
            try:
                _load_layer_file(kind, path)
            except Exception as error:
                _logger.exception('Unable to load the handler file %s of the Viindoo layer', listed)
                with _files_lock:
                    _files['failed'].append({'file': listed, 'origin': 'layer', 'error': _describe(error)})


def _namespace(handler):
    """The namespace of the file that defines the class `handler`: core keeps no module of the files it loads."""
    for member in vars(handler).values():
        function = inspect.unwrap(getattr(member, '__func__', member))
        namespace = getattr(function, '__globals__', None)
        if namespace is not None and namespace.get('__name__') == handler.__module__:
            return namespace
    return None


def _handler_namespaces():
    """The namespaces of the loaded handler files.

    :return: a dict of the name of a loaded handler file to the list of its namespaces; a file that another one
        imports is loaded twice by core
    """
    main = core('main')
    handlers, pending = [*main.drivers, *main.interfaces.values()], [http.Controller]
    while pending:
        controller = pending.pop()
        handlers.append(controller)
        pending.extend(controller.__subclasses__())
    found = {}
    for handler in handlers:
        if not handler.__module__.startswith((f'{CORE_HANDLERS}.', f'{LAYER_HANDLERS}.')):
            continue
        namespace = _namespace(handler)
        known = found.setdefault(handler.__module__, [])
        if namespace is not None and all(namespace is not other for other in known):
            known.append(namespace)
    return found


class _FileNamespace:
    """The namespace of a loaded handler file, as a module holds it."""

    def __init__(self, namespace):
        self.__dict__ = namespace


def _verify_server_calls():
    """Have the handler files that call the server verify it, as core's own reports do."""
    verify = importlib.import_module(f'{__package__}.trust').verify_server_calls
    unverified = [
        name for name, namespaces in _handler_namespaces().items() for namespace in namespaces
        if 'urllib3' in namespace and not verify(_FileNamespace(namespace))]
    if unverified:
        raise SeamError('these handler files call other machines without verifying them: %s' % ', '.join(unverified))


def _after_core_files(failed):
    with _files_lock:
        _files.update(loaded=True, core=[], layer=[], failed=[])
    for kind in HANDLER_KINDS:
        names, _directory = _core_files(kind)
        with _files_lock:
            _files['core'].extend(f'{kind}/{name}' for name in names)
            _files['failed'].extend(
                {'file': f'{kind}/{name}', 'origin': 'core', 'error': failed[name]} for name in names if name in failed)


def _step(what, step, *arguments):
    """Run one thing the layer does once core has loaded its handler files; what fails costs that thing only."""
    try:
        step(*arguments)
    except Exception as error:
        _logger.critical(
            'Viindoo layer integrity: %s failed once the device handlers were loaded.', what, exc_info=True)
        report_problem(what, _describe(error))


def _unverifiable_calls(source):
    """How the handler file `source` reaches an HTTP library other than through the plain name `urllib3`."""
    def is_http(name):
        return any(name == library or name.startswith(f'{library}.') for library in HTTP_LIBRARIES)

    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.extend(
                f'import {alias.name} as {alias.asname}' if alias.asname else f'import {alias.name}'
                for alias in node.names
                if is_http(alias.name) and not (alias.name.split('.')[0] == 'urllib3' and not alias.asname))
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.extend(
                f'from {node.module} import {alias.name}' for alias in node.names
                if is_http(node.module) or is_http(f'{node.module}.{alias.name}'))
    return found


class _HandlerLoader(FunctionSeam):
    module = f'{CORE}.tools.helpers'
    name = 'load_iot_handlers'
    parameters = ()

    def check(self):
        super().check()
        helpers, _attribute, loader = self._resolve()
        if not isinstance(getattr(helpers, '_logger', None), logging.Logger):
            raise SeamError(f'{self.module} has no logger to say which files it could not load')
        used = set(inspect.unwrap(loader).__code__.co_names)
        if not {'list_file_by_os', 'exec_module', 'exception'} <= used:
            raise SeamError('does not list its files with list_file_by_os, or does not log those it cannot load')
        try:
            listed = tuple(inspect.signature(helpers.list_file_by_os).parameters)
        except (AttributeError, TypeError, ValueError):
            raise SeamError(f'{self.module}.list_file_by_os does not exist') from None
        if len(listed) != 1:
            raise SeamError(f'{self.module}.list_file_by_os does not take one directory')

    def replacement(self, original):
        def load_iot_handlers():
            helpers = core('tools.helpers')
            names = set()
            with contextlib.suppress(Exception):
                names = {name for kind in HANDLER_KINDS for name in _core_files(kind)[0]}
            watch = _FailedFiles(names)
            helpers._logger.addHandler(watch)
            try:
                original()
            finally:
                helpers._logger.removeHandler(watch)
            _step('the list of handler files', _after_core_files, watch.failed)
            _step('the handler files of the layer', _load_layer_files)
            # The screen page and the printer driver of core exist at this point.
            apply_seams(only=(SCREEN_PAGE_SEAM, STATUS_TICKET_SEAM, PRINTER_LISTING_SEAM, KEYBOARD_DEVICES_SEAM))
            _step('verified calls of the handler files', _verify_server_calls)
            # Odoo routes the controllers of the handler files only once its routing is built again.
            _step('the routes of the handler files', lazy_property.reset_all, http.root)

        return load_iot_handlers


@register
class HandlerFiles(PartsSeam):
    number = 4
    target = f'{CORE}.tools.helpers.load_iot_handlers'
    feature = ('the box loads the device handlers of the Viindoo layer, knows which handler files could not be '
               'loaded, and verifies the server its handler files call')

    LOADER = 'load_iot_handlers'
    SERVER_CALLS = 'calls of the handler files to other machines'
    parts = (LOADER, SERVER_CALLS)

    def __init__(self):
        self._loader = _HandlerLoader()

    def check_part(self, part):
        if part == self.LOADER:
            self._loader.check()
            return
        unverifiable = []
        for kind in HANDLER_KINDS:
            names, directory = _core_files(kind)
            for name in names:
                with contextlib.suppress(OSError, SyntaxError, ValueError):
                    unverifiable.extend(
                        f'{kind}/{name}: {call}' for call in _unverifiable_calls((directory / name).read_bytes()))
        if unverifiable:
            raise SeamError('the layer cannot verify %s' % '; '.join(unverifiable))

    def apply_part(self, part):
        # The handler files get their verified calls once core has loaded them: the loader of the layer sees to it.
        if part == self.LOADER:
            self._loader.apply()


@functools.cache
def _screen_page():
    environment = jinja2.Environment(loader=jinja2.FileSystemLoader(LAYER_DIRECTORY / 'views'), autoescape=True)
    return environment.get_template('pos_display.html')


def _renders_screen_page(tree):
    """Whether the screen handler `tree` fills its page as the page of the layer expects."""
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'render'
                and isinstance(node.func.value, ast.Name) and node.func.value.id == SCREEN_PAGE
                and node.args and isinstance(node.args[0], ast.Dict)):
            given = {key.value for key in node.args[0].keys if isinstance(key, ast.Constant)}
            return set(SCREEN_PAGE_CONTEXT) <= given
    return False


@register
class DefaultScreenPage(Seam):
    number = SCREEN_PAGE_SEAM
    target = f'{SCREEN_PAGE} in {CORE_HANDLERS}.drivers.DisplayDriver_L'
    feature = 'a screen that no till uses shows the Viindoo page of the box'
    after = HandlerFiles.number

    def check(self):
        try:
            tree = ast.parse(Path(file_path(SCREEN_HANDLER)).read_bytes())
        except (OSError, SyntaxError, ValueError) as error:
            raise SeamError(f'{SCREEN_HANDLER} cannot be read ({type(error).__name__})') from None
        assigned = any(
            isinstance(node, ast.Assign) and any(
                isinstance(name, ast.Name) and name.id == SCREEN_PAGE for name in node.targets)
            for node in tree.body)
        if not assigned:
            raise SeamError(f'{SCREEN_HANDLER} does not keep its page in {SCREEN_PAGE}')
        if not _renders_screen_page(tree):
            raise SeamError('%s does not fill its page with %s' % (SCREEN_HANDLER, ', '.join(SCREEN_PAGE_CONTEXT)))
        try:
            _screen_page()
        except (OSError, jinja2.TemplateError) as error:
            raise SeamError(f'the screen page of the layer cannot be used ({_describe(error)})') from None

    def apply(self):
        namespaces = [
            namespace for namespace in _handler_namespaces().get(f'{CORE_HANDLERS}.drivers.DisplayDriver_L', ())
            if SCREEN_PAGE in namespace]
        if not namespaces:
            raise SeamError('core did not load its screen handler')
        for namespace in namespaces:
            namespace[SCREEN_PAGE] = _screen_page()


def _status_ticket_texts(tree):
    """The texts of the status ticket function of the printer handler `tree`.

    :return: the texts, or ``None`` when the handler has no such function
    """
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == PRINTER_DRIVER:
            for member in node.body:
                if isinstance(member, ast.FunctionDef) and member.name == STATUS_TICKET:
                    if [argument.arg for argument in member.args.args] != ['self']:
                        return None
                    return [constant.value.decode('ascii', 'replace') if isinstance(constant.value, bytes)
                            else constant.value for constant in ast.walk(member)
                            if isinstance(constant, ast.Constant) and isinstance(constant.value, (str, bytes))]
    return None


def _viindoo_ticket(original, words):
    """`print_status` printing the ticket core composes, with the words of a Viindoo box in place of `words`.

    The driver itself, which the point of sale prints through at the same time, is not changed.
    """
    def print_status(driver):
        composed = []
        twin = copy.copy(driver)
        twin.print_raw = lambda data, *_arguments, **_options: composed.append(data)
        original(twin)
        ticket = b''.join(composed)
        for core_words, layer_words in list(words.values()):
            ticket = ticket.replace(core_words.encode(), layer_words.encode(), 1)
        if ticket:
            driver.print_raw(ticket)

    print_status.viin_ticket_words = words
    return print_status


@register
class StatusTicket(PartsSeam):
    number = STATUS_TICKET_SEAM
    target = f'{PRINTER_DRIVER}.{STATUS_TICKET} in {CORE_HANDLERS}.drivers.PrinterDriver_L'
    feature = 'the status ticket of a receipt printer names the box a Viindoo IoT Box'
    after = HandlerFiles.number
    parts = tuple(STATUS_TICKET_WORDS)

    def __init__(self):
        self._words = {}

    def check_part(self, part):
        try:
            texts = _status_ticket_texts(ast.parse(Path(file_path(PRINTER_HANDLER)).read_bytes()))
        except (OSError, SyntaxError, ValueError) as error:
            raise SeamError(f'{PRINTER_HANDLER} cannot be read ({type(error).__name__})') from None
        if texts is None:
            raise SeamError(f'{PRINTER_HANDLER} has no {PRINTER_DRIVER}.{STATUS_TICKET}(self)')
        core_words = STATUS_TICKET_WORDS[part][0]
        if not any(core_words in text for text in texts):
            raise SeamError(f'{PRINTER_DRIVER}.{STATUS_TICKET} does not print {core_words!r}')

    def apply_part(self, part):
        drivers = [
            driver for driver in core('main').drivers
            if driver.__module__ == f'{CORE_HANDLERS}.drivers.PrinterDriver_L' and driver.__name__ == PRINTER_DRIVER]
        if not drivers:
            raise SeamError('core did not load its printer driver')
        for driver in drivers:
            function = vars(driver)[STATUS_TICKET]
            if getattr(function, 'viin_ticket_words', None) is not self._words:
                setattr(driver, STATUS_TICKET, functools.wraps(function)(_viindoo_ticket(function, self._words)))
        self._words[part] = STATUS_TICKET_WORDS[part]


class _Owners:
    """The session that last took each device, kept where a restart of the box service finds it again."""

    def __init__(self):
        self._lock = threading.Lock()
        self._known = None
        self._reported = False

    def _load(self):
        if self._known is None:
            try:
                stored = json.loads(OWNERS_FILE.read_text())
            except (OSError, ValueError):
                stored = {}
            self._known = {
                device: owner for device, owner in (stored.items() if isinstance(stored, dict) else ())
                if isinstance(owner, str) and 0 < len(owner) <= MAX_OWNER}
        return self._known

    def _store(self):
        temporary = OWNERS_FILE.with_name(f'{OWNERS_FILE.name}.new')
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as file:
                json.dump(self._known, file)
            os.replace(temporary, OWNERS_FILE)
        except OSError as error:
            if not self._reported:
                self._reported = True
                _logger.warning(
                    'The owners of the devices cannot be kept in %s (%s): after a restart of the box service a '
                    'device is given back to nobody', OWNERS_FILE, type(error).__name__)
                report_problem('device owners', _describe(error))

    def of(self, device):
        with self._lock:
            return self._load().get(device)

    def remember(self, device, owner):
        with self._lock:
            known = self._load()
            if not isinstance(owner, str) or not 0 < len(owner) <= MAX_OWNER:
                owner = None
            if known.get(device) == owner:
                return
            if owner is None:
                del known[device]
            else:
                known[device] = owner
            self._store()


_owners = _Owners()


def device_owner(identifier):
    """The session that last took the device `identifier`.

    :return: the session, or ``None`` when none did since the box was switched on
    """
    return _owners.of(identifier)


def _give_owner_back(original):
    def initialise(driver, identifier, device):
        original(driver, identifier, device)
        try:
            owner = _owners.of(identifier)
            if owner is not None:
                driver.data['owner'] = owner
        except Exception:
            _logger.exception('The owner of the device %s could not be given back', identifier)

    return initialise


def _remember_owner(original):
    def action(controller, *arguments, **parameters):
        try:
            return original(controller, *arguments, **parameters)
        finally:
            # Core names the caller the owner of the device before it runs, or refuses, the action.
            try:
                session, identifier = (
                    parameters.get(name, arguments[position] if position < len(arguments) else None)
                    for position, name in enumerate(('session_id', 'device_identifier')))
                if isinstance(identifier, str) and identifier in core('main').iot_devices:
                    _owners.remember(identifier, session)
            except Exception:
                _logger.exception('The owner of a device could not be remembered')

    return action


class _Wrapped(FunctionSeam):

    def __init__(self, module, name, parameters, wrap):
        super().__init__()
        self.module = module
        self.name = name
        self.parameters = parameters
        self._wrap = wrap

    def replacement(self, original):
        return self._wrap(original)


@register
class DeviceOwner(PartsSeam):
    number = 15
    target = f'{CORE}.driver.Driver.__init__ and {CORE}.controllers.driver.DriverController.action'
    feature = 'after a restart of the box service a device is given back to the station that used it'

    def __init__(self):
        self._wrapped = {wrapped.name: wrapped for wrapped in (
            _Wrapped(f'{CORE}.driver', 'Driver.__init__', ('self', 'identifier', 'device'), _give_owner_back),
            _Wrapped(
                f'{CORE}.controllers.driver', 'DriverController.action',
                ('self', 'session_id', 'device_identifier', 'data'), _remember_owner),
        )}

    @property
    def parts(self):
        return tuple(self._wrapped)

    def check_part(self, part):
        self._wrapped[part].check()

    def apply_part(self, part):
        self._wrapped[part].apply()


# The device actions that failed, each as (device, kind of error): the first of each is logged in full.
MAX_FAILED_ACTIONS = 256
_failed_actions = set()
_failed_actions_lock = threading.Lock()


def _log_failed_action(identifier, error):
    key = (str(identifier)[:MAX_OWNER], type(error).__name__)
    with _failed_actions_lock:
        first = key not in _failed_actions
        if first:
            if len(_failed_actions) >= MAX_FAILED_ACTIONS:
                _failed_actions.clear()
            _failed_actions.add(key)
    if first:
        _logger.warning(
            'An action on the device %s failed (%s); the box answers that it was not done, and logs this kind of '
            'failure of this device in full once', key[0], key[1], exc_info=error)
    else:
        _logger.debug('An action on the device %s failed again (%s)', key[0], key[1])


def _answer_failed_action(original):
    def action(controller, *arguments, **parameters):
        try:
            return original(controller, *arguments, **parameters)
        except Exception as error:
            identifier = parameters.get('device_identifier', arguments[1] if len(arguments) > 1 else None)
            _log_failed_action(identifier, error)
            # What core answers for a device it does not know: the page reads it as an action not done.
            return False

    return action


@register
class ActionErrors(_Wrapped):
    number = ACTION_ERRORS_SEAM
    feature = ('an action on a device that fails is answered as not done, as core answers a device it does not '
               'know, instead of an error without the headers a page needs to read it')

    def __init__(self):
        super().__init__(
            f'{CORE}.controllers.driver', 'DriverController.action',
            ('self', 'session_id', 'device_identifier', 'data'), _answer_failed_action)


# One request to the mDNS daemon for the printers of the site's network, and how long it is given to answer.
MDNS_BROWSE = ('avahi-browse', '--all', '--resolve', '--terminate', '--parsable', '--no-db-lookup')
MDNS_TIMEOUT = 15
_ESCAPED_BYTE = re.compile(rb'\\(\d{3})')
_mdns_problem = {'told': False}


def _browse_services():
    """What the mDNS daemon says of the services of the network, one `avahi-browse` line per service address."""
    return subprocess.run(MDNS_BROWSE, capture_output=True, timeout=MDNS_TIMEOUT, check=True).stdout


def _mdns_addresses():
    """The IPv4 addresses of the services the network announces.

    :return: a dict of ``<service name>.<type>.<domain>`` in lower case to the address; empty when the daemon
        cannot be asked, and a printer named by a service name then keeps that name as its address and is listed
        beside the same printer found by its IP
    """
    try:
        output = _browse_services()
    except (OSError, subprocess.SubprocessError) as error:
        if not _mdns_problem['told']:
            _mdns_problem['told'] = True
            _logger.warning(
                'The printers announced on the network cannot be matched to an address (%s): a printer found by '
                'its service name may be listed beside the same printer found by its address', _describe(error))
        return {}
    found = {}
    for line in output.splitlines():
        # =;<interface>;<protocol>;<name>;<type>;<domain>;<host>;<address>;<port>;<text>
        fields = line.split(b';')
        if len(fields) < 8 or fields[0] != b'=' or fields[2] != b'IPv4':
            continue
        name = _ESCAPED_BYTE.sub(lambda match: bytes([int(match.group(1)) & 0xFF]), fields[3])
        service = b'.'.join((name, fields[4], fields[5])).decode('utf-8', 'replace').lower()
        found.setdefault(service, fields[7].decode('ascii', 'replace'))
    return found


def _printer_host(entry):
    """The host of the address of the printer `entry`, as the URI of its print queue or of its device names it."""
    address = entry.get('device-uri') or entry.get('url')
    try:
        host = urlsplit(address).hostname if isinstance(address, str) else None
    except ValueError:
        return None
    # `hostname` lower-cases only what comes before a percent sign.
    return unquote(host).lower() if host else None


def _printer_addresses(listed):
    """The address of each printer of `listed` that has one.

    An address is the IP in the URI; a service name of the network (`<name>._ipp._tcp.local`) is matched to the IP
    its service announces, and any other host name is its own address.

    :return: a dict of identifier to address
    """
    hosts = {identifier: _printer_host(entry) for identifier, entry in listed.items()}
    announced = None
    addresses = {}
    for identifier, host in hosts.items():
        if not host:
            continue
        try:
            addresses[identifier] = str(ipaddress.ip_address(host))
            continue
        except ValueError:
            pass
        if host.endswith('.local') and announced is None:
            announced = _mdns_addresses()
        addresses[identifier] = (announced or {}).get(host, host)
    return addresses


def _one_entry_per_printer(listed):
    """The listing of the printers with one entry for each address: the rule of Odoo 19, in the same order.

    Of the entries that share an address, the one registered as a printer stays; else the one that announces a
    command language (`CMD:` or `ZPL` in its device id); else the first by identifier. An entry without an address
    is never left out. The entries are the ones core holds, and are not changed.
    """
    addresses = _printer_addresses(listed)
    registered = core('main').iot_devices
    chosen = {}
    for identifier in sorted(addresses, key=lambda name: (addresses[name], name)):
        entry = listed[identifier]
        address = addresses[identifier]
        rank = (
            identifier not in registered,
            not any(command in (entry.get('device-id') or '') for command in ('CMD:', 'ZPL')),
        )
        if address not in chosen or rank < chosen[address][0]:
            chosen[address] = (rank, identifier)
    kept = {identifier for _rank, identifier in chosen.values()}
    return {
        identifier: entry for identifier, entry in listed.items() if identifier not in addresses or identifier in kept}


def _listing_without_duplicates(original):
    def get_devices(interface):
        listed = original(interface)
        try:
            return _one_entry_per_printer(listed)
        except Exception:
            # Duplicated entries are a nuisance; a printer that vanishes from the listing is a defect.
            _logger.exception('The printers could not be matched by address: the listing of core stands')
            return listed

    get_devices.viin_one_entry_per_printer = True
    return get_devices


def _printer_listing_calls(tree):
    """Whether the printer interface `tree` has `get_devices(self)` and a dict of the printers it holds."""
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == PRINTER_INTERFACE:
            names = {
                target.id for member in node.body if isinstance(member, ast.Assign) for target in member.targets
                if isinstance(target, ast.Name)}
            listing = [
                member for member in node.body if isinstance(member, ast.FunctionDef) and member.name == 'get_devices']
            return ('printer_devices' in names and len(listing) == 1
                    and [argument.arg for argument in listing[0].args.args] == ['self'])
    return False


@register
class PrinterListing(Seam):
    number = PRINTER_LISTING_SEAM
    target = f'{PRINTER_INTERFACE}.get_devices in {CORE_HANDLERS}.interfaces.PrinterInterface_L'
    feature = 'a network printer that core finds by several addresses is listed once'
    after = HandlerFiles.number

    def check(self):
        try:
            tree = ast.parse(Path(file_path(PRINTER_INTERFACE_HANDLER)).read_bytes())
        except (OSError, SyntaxError, ValueError) as error:
            raise SeamError(f'{PRINTER_INTERFACE_HANDLER} cannot be read ({type(error).__name__})') from None
        if not _printer_listing_calls(tree):
            raise SeamError(
                f'{PRINTER_INTERFACE_HANDLER} has no {PRINTER_INTERFACE}.get_devices(self) and printer_devices')

    def apply(self):
        interface = core('main').interfaces.get(PRINTER_INTERFACE)
        if interface is None or interface.__module__ != f'{CORE_HANDLERS}.interfaces.PrinterInterface_L':
            raise SeamError('core did not load its printer interface')
        function = vars(interface)['get_devices']
        if not getattr(function, 'viin_one_entry_per_printer', False):
            interface.get_devices = functools.wraps(function)(_listing_without_duplicates(function))


KEYBOARD_HANDLER = 'hw_drivers/iot_handlers/drivers/KeyboardUSBDriver_L.py'
KEYBOARD_DRIVER = 'KeyboardUSBDriver'
# How core's keyboard driver opens the display it translates keys through. The layer opens it the same way, ahead of
# core, so that both mean the same display; the seam check pins both values in core's file.
DISPLAY_AUTHORITY = '/run/lightdm/pi/xauthority'
DISPLAY_NAME = ':0.0'
MAX_FAILURES = 256

_failures = set()
_failures_lock = threading.Lock()


class DisplayNotReady(Exception):
    """The display a USB keyboard or scanner reads its keys through does not exist yet."""


def _log_failure(interface, identifier, error):
    """Log the first failure of a device of an interface, per kind of error, in full: the next looks repeat it.

    The wait for the display is not a failure: it is said once at info level, with no traceback.
    """
    key = (type(interface).__name__, str(identifier), type(error).__name__)
    with _failures_lock:
        first = key not in _failures
        if first:
            if len(_failures) >= MAX_FAILURES:
                _failures.clear()
            _failures.add(key)
    if isinstance(error, DisplayNotReady):
        # A display that is not up yet is the expected wait of a box that starts before X: no fault to read.
        _logger.log(logging.INFO if first else logging.DEBUG,
                    'The device %s of the interface %s waits for the display', key[1], key[0])
    elif first:
        _logger.warning(
            'The device %s of the interface %s cannot be started (%s); it is tried again at each look, and this kind '
            'of failure of this device is logged in full once', key[1], key[0], key[2], exc_info=error)
    else:
        _logger.debug('The device %s of the interface %s cannot be started again (%s)', key[1], key[0], key[2])


def _forget_failures(interface, identifier):
    with _failures_lock:
        for key in [key for key in _failures if key[:2] == (type(interface).__name__, str(identifier))]:
            _failures.discard(key)


def _listing_that_tries_again(original):
    def update_iot_devices(interface, devices=None):
        devices = devices or {}
        detected = interface._detected_devices
        handled = {identifier: found for identifier, found in devices.items() if identifier in detected}
        # What core does for the devices it knows: the ones that are gone are disconnected.
        try:
            original(interface, dict(handled))
        except Exception as error:
            _log_failure(interface, 'known devices', error)
        for identifier in [identifier for identifier in devices if identifier not in detected]:
            attempt = {**handled, identifier: devices[identifier]}
            try:
                original(interface, attempt)
            except Exception as error:
                # Left out of what the interface holds as detected: the next look finds it new.
                _log_failure(interface, identifier, error)
            else:
                handled = attempt
                _forget_failures(interface, identifier)
            interface._detected_devices = handled.keys()

    update_iot_devices.viin_tries_again = True
    return update_iot_devices


@register
class DeviceListing(FunctionSeam):
    number = 25
    module = f'{CORE}.interface'
    name = 'Interface.update_iot_devices'
    parameters = ('self', 'devices')
    feature = ('a device whose driver cannot start is tried again at each look, and the other devices of the '
               'interface are listed')

    def check(self):
        super().check()
        code = getattr(self._resolve()[2], '__code__', None)
        used = set(code.co_names) if code else set()
        if not {'_detected_devices', 'supported', 'disconnect'} <= used:
            raise SeamError('does not keep its detected devices in _detected_devices, or does not ask each '
                            'driver whether it supports a device and disconnect the devices that are gone')

    def replacement(self, original):
        return _listing_that_tries_again(original)


def _keyboard_tree():
    try:
        return ast.parse(Path(file_path(KEYBOARD_HANDLER)).read_bytes())
    except (OSError, SyntaxError, ValueError) as error:
        raise SeamError(f'{KEYBOARD_HANDLER} cannot be read ({type(error).__name__})') from None


def _keyboard_class(tree):
    return next((node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == KEYBOARD_DRIVER), None)


def _assigned_names(body):
    return {
        target.id for member in body if isinstance(member, ast.Assign) for target in member.targets
        if isinstance(target, ast.Name)}


def _stores_display(node):
    """Whether `node` is the assignment target `KeyboardUSBDriver.display`."""
    if not (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)):
        return False
    return node.attr == 'display' and isinstance(node.value, ast.Name) and node.value.id == KEYBOARD_DRIVER


def _open_display(driver_class, xlib):
    """Keep on `driver_class` the display core keeps there, opened as core opens it.

    :raises DisplayNotReady: when no display opens
    """
    if getattr(driver_class, 'display', 0):
        return
    os.environ['XAUTHORITY'] = DISPLAY_AUTHORITY
    display = xlib.XOpenDisplay(bytes(DISPLAY_NAME, 'utf-8'))
    if not display:
        raise DisplayNotReady('no display is open yet: the box tries again at its next look')
    driver_class.display = display


def _with_display_and_own_input_devices(original, driver_class):
    def __init__(driver, identifier, device):
        _open_display(driver_class, original.__globals__['xlib'])
        # Core keeps the input devices of every driver in one list that is never emptied: a driver made after its
        # device was plugged back would walk the device that was unplugged.
        driver.input_devices = []
        original(driver, identifier, device)

    __init__.viin_own_input_devices = True
    return __init__


@register
class KeyboardDevices(Seam):
    number = KEYBOARD_DEVICES_SEAM
    target = f'{KEYBOARD_DRIVER}.__init__ in {CORE_HANDLERS}.drivers.KeyboardUSBDriver_L'
    feature = ('a USB keyboard or scanner is listed once the display exists, and a device plugged back reads its own '
               'input devices only')
    after = HandlerFiles.number

    def check(self):
        tree = _keyboard_tree()
        driver = _keyboard_class(tree)
        if driver is None:
            raise SeamError(f'{KEYBOARD_HANDLER} has no {KEYBOARD_DRIVER}')
        constructors = [
            member for member in driver.body if isinstance(member, ast.FunctionDef) and member.name == '__init__']
        if len(constructors) != 1 or [argument.arg for argument in constructors[0].args.args] != [
                'self', 'identifier', 'device']:
            raise SeamError(f'{KEYBOARD_DRIVER}.__init__ does not take (self, identifier, device)')
        if 'input_devices' not in _assigned_names(driver.body):
            raise SeamError(f'{KEYBOARD_DRIVER} does not keep its input devices in input_devices')
        if 'xlib' not in _assigned_names(tree.body):
            raise SeamError(f'{KEYBOARD_HANDLER} has no xlib')
        constants = {node.value for node in ast.walk(constructors[0]) if isinstance(node, ast.Constant)}
        opened = any(isinstance(node, ast.Attribute) and node.attr == 'XOpenDisplay' for node in ast.walk(tree))
        kept = any(_stores_display(node) for node in ast.walk(tree))
        if not (opened and kept and {DISPLAY_AUTHORITY, DISPLAY_NAME} <= constants):
            raise SeamError(f'{KEYBOARD_DRIVER} does not open its display as the layer does: XOpenDisplay of '
                            f'{DISPLAY_NAME} with {DISPLAY_AUTHORITY}, kept in {KEYBOARD_DRIVER}.display')

    def apply(self):
        module = f'{CORE_HANDLERS}.drivers.KeyboardUSBDriver_L'
        drivers = [
            driver for driver in core('main').drivers
            if driver.__module__ == module and driver.__name__ == KEYBOARD_DRIVER]
        if not drivers:
            raise SeamError('core did not load its keyboard driver')
        for driver in drivers:
            function = vars(driver)['__init__']
            if not getattr(function, 'viin_own_input_devices', False):
                driver.__init__ = functools.wraps(function)(_with_display_and_own_input_devices(function, driver))
