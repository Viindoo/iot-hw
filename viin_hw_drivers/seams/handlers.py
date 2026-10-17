"""Seams of the device handlers: the handler files of the layer, the default screen page, the status ticket of a
receipt printer, the owner of a device.

Core loads its handler files from one directory, which an update of core
empties of every file core does not ship. The files of the layer stay in the
layer and are loaded right after those of core.
"""
import ast
import contextlib
import copy
import functools
import importlib
import importlib.util
import inspect
import json
import logging
import os
import sys
import threading
from pathlib import Path

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
    """The handler files of this box, each as '<interfaces or drivers>/<file name>'.

    {'loaded': whether core has loaded the handlers yet; 'core' and 'layer': the
    files found in each tree; 'failed': [{'file', 'origin': core or layer,
    'error'}] for those that could not be loaded}.
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
    """{name of a loaded handler file: [its namespaces]}; a file that another one imports is loaded twice by core."""
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
            raise SeamError('no longer lists its files with list_file_by_os, or no longer logs those it cannot load')
        try:
            listed = tuple(inspect.signature(helpers.list_file_by_os).parameters)
        except (AttributeError, TypeError, ValueError):
            raise SeamError(f'{self.module}.list_file_by_os does not exist') from None
        if len(listed) != 1:
            raise SeamError(f'{self.module}.list_file_by_os no longer takes one directory')

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
            # The screen page and the printer driver of core exist from now on.
            apply_seams(only=(SCREEN_PAGE_SEAM, STATUS_TICKET_SEAM))
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
    """Whether the screen handler `tree` still fills its page as the page of the layer expects."""
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
            raise SeamError(f'{SCREEN_HANDLER} no longer keeps its page in {SCREEN_PAGE}')
        if not _renders_screen_page(tree):
            raise SeamError('%s no longer fills its page with %s' % (SCREEN_HANDLER, ', '.join(SCREEN_PAGE_CONTEXT)))
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
    """The texts of the status ticket function of the printer handler `tree`; None when it has no such function."""
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

    The ticket is composed on a copy of the driver whose `print_raw` keeps it,
    as the test of a receipt printer does: the driver itself, which the point
    of sale prints through at the same time, is not changed.
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
            raise SeamError(f'{PRINTER_HANDLER} no longer has {PRINTER_DRIVER}.{STATUS_TICKET}(self)')
        core_words = STATUS_TICKET_WORDS[part][0]
        if not any(core_words in text for text in texts):
            raise SeamError(f'{PRINTER_DRIVER}.{STATUS_TICKET} no longer prints {core_words!r}')

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
    """The session that last took the device `identifier`, None when none did since the box was switched on."""
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
