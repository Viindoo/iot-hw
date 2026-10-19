"""What the box does with each route of core: close it, lock it, keep it from other web sites, or leave it to core.

Core is not changed. For each controller of core that has a route to change,
this file makes a controller that inherits from it - the way Odoo lets a module
change the routes of another. What decides is the address of a route, whatever
core names the method that serves it.

ROUTES is the table of the routes of the three box modules of core, and of the
addresses by which the `web` module of core manages databases: of those, one the
table does not list is not served, and `CoreRoutes` reports every difference
between the table and what core registers. Two kinds of routes are outside the
table and served as core serves them: the other routes of `web`, and the routes
that the device driver files of core define, each for its own device.

The route by which a page uses a device stays core's, but for what the box
keeps of it: a choice in SETTING_ACTIONS is a setting, and so is an address of
the screen that is neither a page of the server of the box nor one of the box.
"""
import collections
import functools
import importlib
import inspect
import json
import logging
from urllib.parse import urlsplit

from odoo import http
from odoo.tools import config

from odoo.addons.viin_hw_drivers import lock, pairing, seams, trust, wifi

_logger = logging.getLogger(__name__)

CLOSED = 'closed'        # not a route of the box: its address answers "not found"
REPLACED = 'replaced'    # not a route of the box until a controller of the layer defines its method again
LOCKED = 'locked'        # a setting: `lock.protected` says which requests it runs for
STOCK = 'stock'          # served as core serves it
# From the fate that gives least to the one that gives most.
FATES = (CLOSED, REPLACED, LOCKED, STOCK)

# A row of ROUTES: the method of core that serves the address, the fate of the route, and whether the route
# answers the pages of another web site where core lets it. Without `other_sites` it allows no other origin.
Route = collections.namedtuple('Route', ('method', 'fate', 'other_sites'), defaults=(False,))

# The controller files core loads with its box modules: each of their routes is in ROUTES, or is not served.
BOX_CONTROLLERS = (
    'odoo.addons.hw_drivers.controllers',
    'odoo.addons.hw_escpos.controllers',
    'odoo.addons.hw_posbox_homepage.controllers',
)
# The module the page of core is built on. Of its routes, ROUTES has the ones under DATABASES and no other.
WEB_CONTROLLERS = 'odoo.addons.web.controllers'
DATABASES = '/web/database'

CONNECT = '/hw_posbox_homepage/connect_to_server'
DISCONNECT = '/hw_posbox_homepage/server_clear'
JOIN_WIFI = '/hw_posbox_homepage/update_wifi'
FORGET_WIFI = '/hw_posbox_homepage/wifi_clear'
# The parameters of core's JOIN_WIFI method, in its order: the layer serves the route with these and no other. Core
# taking one more, one less, or these in another order is a difference of the seam.
WIFI_PARAMETERS = ('self', 'essid', 'password', 'persistent')
DEVICE_ACTION = '/hw_drivers/action'
# The parameter in which DEVICE_ACTION takes, as a JSON text, the action a device is asked for.
ACTION_PARAMETER = 'data'
# The actions of DEVICE_ACTION that are settings of the box, kept across its restarts: whether a keyboard device
# is used as a scanner, and its layout. They are locked like the routes that are settings.
SETTING_ACTIONS = ('update_is_scanner', 'update_layout')
# The action of DEVICE_ACTION that gives the screen of the box the address it shows, and the key of that address.
# The box keeps the address across its restarts: it is a setting unless `_is_own_page`.
SCREEN_ACTION = 'update_url'
SCREEN_ADDRESS = 'url'
# What DEVICE_ACTION answers on a box without a server, where no unlock proof exists, for an address of the screen
# that is a setting.
OTHER_ADDRESS = {'status': 'forbidden', 'reason': 'other_address'}
# The port of the web server of the box, per scheme; it hands requests on to the box service.
WEB_SERVER_PORTS = {'http': 80, 'https': 443}

ROUTES = {
    # Online services of the vendor of core: its customer credential, its certificates, a remote tunnel, a payment
    # terminal.
    '/hw_posbox_homepage/save_credential': Route('save_credential', CLOSED),
    '/hw_posbox_homepage/clear_credential': Route('clear_credential', CLOSED),
    '/hw_drivers/check_certificate': Route('check_certificate', CLOSED),
    '/hw_posbox_homepage/enable_ngrok': Route('enable_remote_connection', CLOSED),
    '/hw_posbox_homepage/six_payment_terminal_add': Route('add_six_terminal', CLOSED),
    '/hw_posbox_homepage/six_payment_terminal_clear': Route('clear_six_terminal', CLOSED),
    # Software of the box taken from elsewhere than a Viindoo release: the image, core, the driver files.
    '/hw_proxy/perform_upgrade': Route('perform_upgrade', CLOSED),
    '/hw_proxy/perform_flashing_create_partition': Route('perform_flashing_create_partition', CLOSED),
    '/hw_proxy/perform_flashing_download_raspios': Route('perform_flashing_download_raspios', CLOSED),
    '/hw_proxy/perform_flashing_copy_raspios': Route('perform_flashing_copy_raspios', CLOSED),
    '/hw_posbox_homepage/load_iot_handlers': Route('load_iot_log_level', CLOSED),
    '/hw_posbox_homepage/clear_iot_handlers': Route('clear_iot_handlers', CLOSED),
    # Database management: a box has no database.
    f'{DATABASES}/selector': Route('selector', CLOSED),
    f'{DATABASES}/manager': Route('manager', CLOSED),
    f'{DATABASES}/create': Route('create', CLOSED),
    f'{DATABASES}/duplicate': Route('duplicate', CLOSED),
    f'{DATABASES}/drop': Route('drop', CLOSED),
    f'{DATABASES}/backup': Route('backup', CLOSED),
    f'{DATABASES}/restore': Route('restore', CLOSED),
    f'{DATABASES}/change_password': Route('change_password', CLOSED),
    f'{DATABASES}/list': Route('list', CLOSED),
    # Core's own version check asks its vendor, and its update follows the code branch of the server.
    '/hw_posbox_homepage/version_info': Route('get_version_info', REPLACED),
    '/hw_posbox_homepage/update_git_tree': Route('update_git_tree', REPLACED),
    # Settings: the server of the box, its network, its log levels, a restart, shell access.
    CONNECT: Route('connect_to_odoo_server', LOCKED),
    DISCONNECT: Route('clear_server_configuration', LOCKED),
    JOIN_WIFI: Route('update_wifi', LOCKED),
    FORGET_WIFI: Route('clear_wifi_configuration', LOCKED),
    '/hw_posbox_homepage/log_levels_update': Route('update_log_level', LOCKED),
    '/hw_posbox_homepage/restart_odoo_service': Route('odoo_service_restart', LOCKED),
    '/hw_posbox_homepage/generate_password': Route('generate_password', LOCKED),
    # The page of the box and what it reads.
    '/': Route('index', STOCK),
    '/logs': Route('logs_page', STOCK),
    '/hw_posbox_homepage/data': Route('get_homepage_data', STOCK),
    '/hw_posbox_homepage/ping': Route('ping', STOCK),
    '/hw_posbox_homepage/wifi': Route('get_available_wifi', STOCK),
    '/hw_posbox_homepage/iot_logs': Route('get_iot_logs', STOCK),
    '/hw_posbox_homepage/log_levels': Route('log_levels', STOCK),
    '/hw_drivers/download_logs': Route('download_logs', STOCK),
    '/hw_proxy/get_version': Route('check_version', STOCK),
    # Devices: the pages of the server call these from the browser of a user, as another web site than the box.
    '/hw_proxy/hello': Route('hello', STOCK, other_sites=True),
    '/hw_proxy/handshake': Route('handshake', STOCK, other_sites=True),
    '/hw_proxy/status_json': Route('status_json', STOCK, other_sites=True),
    DEVICE_ACTION: Route('action', STOCK, other_sites=True),
    '/hw_drivers/event': Route('event', STOCK, other_sites=True),
    '/hw_proxy/open_cashbox': Route('open_cashbox', STOCK, other_sites=True),
    '/hw_proxy/print_receipt': Route('print_receipt', STOCK, other_sites=True),
    '/hw_proxy/print_xml_receipt': Route('print_xml_receipt', STOCK, other_sites=True),
}


def _defined_in(controller, *packages):
    return controller.__module__.startswith(tuple(f'{package}.' for package in packages))


def _is_core(controller):
    return _defined_in(controller, *BOX_CONTROLLERS, WEB_CONTROLLERS)


def _manages_databases(address):
    return address == DATABASES or address.startswith(f'{DATABASES}/')


def _core_controllers():
    """(the controller classes of core that ROUTES may be about, why one of their packages could not be imported)."""
    problems = []
    for package in (*BOX_CONTROLLERS, WEB_CONTROLLERS):
        try:
            importlib.import_module(package)
        except Exception as error:
            problems.append(f'{package} cannot be imported ({type(error).__name__})')
    found, pending = [], [http.Controller]
    while pending:
        controller = pending.pop()
        pending.extend(controller.__subclasses__())
        if _is_core(controller) and controller not in found:
            found.append(controller)
    return sorted(found, key=lambda controller: (controller.__module__, controller.__qualname__)), problems


def _addresses(controller, name):
    """Where Odoo serves the method `name` of `controller`: the addresses of the nearest class that gives any."""
    for owner in controller.__mro__:
        routing = getattr(vars(owner).get(name), 'original_routing', None) or {}
        if routing.get('routes'):
            return tuple(routing['routes'])
    return ()


def _endpoints(controller):
    """[(method, addresses)] for each route ROUTES is about that `controller` itself defines, or defines again."""
    whole = _defined_in(controller, *BOX_CONTROLLERS)
    found = []
    for name, member in vars(controller).items():
        addresses = _addresses(controller, name) if inspect.isfunction(member) else ()
        if addresses and (whole or any(map(_manages_databases, addresses))):
            found.append((name, addresses))
    return found


def _fate(addresses):
    """The fate of a method served at `addresses`: of the fates of its addresses, the one that gives least."""
    fates = {ROUTES[address].fate if address in ROUTES else CLOSED for address in addresses}
    return min(fates, key=FATES.index)


def _answers_other_sites(addresses):
    return all(address in ROUTES and ROUTES[address].other_sites for address in addresses)


def _route(addresses):
    """The route decorator of the layer for a method served at `addresses`.

    It leaves the routing of core as it is, but for a method that does not
    answer other web sites: there it allows no other origin, whatever core does.
    """
    return http.route() if _answers_other_sites(addresses) else http.route(cors=None)


class _NotServed:
    """Takes the place of a method of core that the box must not serve.

    Odoo routes an attribute of a controller only when it is a method. Read on a
    controller, this one is none - unless `replaceable`, and a class of the layer
    defines the method again: then it is that one. Read on a class it is
    `routing`, because Odoo reads the routing of every class that defines the name.
    """

    def __init__(self, routing, replaceable):
        self._routing = routing
        self._replaceable = replaceable
        self._owner = self._name = None

    def __set_name__(self, owner, name):
        self._owner, self._name = owner, name

    def __get__(self, controller, owner=None):
        if controller is None:
            return self._routing
        if self._replaceable:
            order = type(controller).__mro__
            for following in order[order.index(self._owner) + 1:]:
                if self._name in vars(following):
                    return None if _is_core(following) else getattr(super(self._owner, controller), self._name)
        return None


def _lock(controller):
    """The controller that inherits from `controller` to give its routes their fate; None when core serves them all."""

    def original(self, name):
        return getattr(super(owner, self), name)

    def served(name, addresses, fate):
        run = _runner(addresses)

        def endpoint(self, *args, **kwargs):
            return run(original(self, name), *args, **kwargs)

        endpoint.__name__ = endpoint.__qualname__ = name
        if fate == LOCKED:
            endpoint = lock.protected(endpoint)
            if {CONNECT, DISCONNECT} & set(addresses):
                # The lock is asked twice: before the wait, so that a request it refuses never waits, and once
                # no other change of the server is under way, which is the answer that counts.
                endpoint = lock.protected(_one_at_a_time(endpoint))
        return _route(addresses)(endpoint)

    body = {}
    for name, addresses in _endpoints(controller):
        fate = _fate(addresses)
        if fate in (CLOSED, REPLACED):
            body[name] = _NotServed(_route(addresses)(vars(controller)[name]), replaceable=fate == REPLACED)
        elif fate == LOCKED or DEVICE_ACTION in addresses or not _answers_other_sites(addresses):
            body[name] = served(name, addresses, fate)
    if not body:
        return None
    owner = type(f'{controller.__name__}Lock', (controller,), {
        '__module__': __name__,
        '__doc__': f'The routes of {controller.__name__} that the box closes, locks or keeps from other web sites.',
        **body,
    })
    return owner


def _runner(addresses):
    """How the layer serves the route of core at `addresses`: a function of core's method and its arguments."""
    if DISCONNECT in addresses:
        return _leave_server
    if CONNECT in addresses:
        return _change_server
    if JOIN_WIFI in addresses:
        return _join_wifi
    if FORGET_WIFI in addresses:
        return _forget_wifi
    if DEVICE_ACTION in addresses:
        return _use_device
    return _as_core


def _as_core(method, *args, **kwargs):
    return method(*args, **kwargs)


def _one_at_a_time(endpoint):
    """`endpoint`, run while no other change of the server of the box is under way, a pairing attempt included."""
    @functools.wraps(endpoint)
    def alone(*args, **kwargs):
        with pairing.changing_server():
            return endpoint(*args, **kwargs)

    return alone


def _asked(data):
    """What a device is asked for in `data`, which core takes as a JSON text: a dict, empty when `data` holds none."""
    if isinstance(data, (str, bytes, bytearray)):
        try:
            data = json.loads(data)
        except ValueError:
            return {}
    return data if isinstance(data, dict) else {}


def _is_page(path):
    """Whether `path` leads, on the box, to no route that ROUTES closes or locks.

    A browser and the box fold a path before it is matched - doubled slashes,
    dot segments, a slash at the end: a path they may fold is no page.
    """
    folded = path.strip('/')
    if folded and {'', '.', '..'} & set(folded.split('/')):
        return False
    route = ROUTES.get(f'/{folded}')
    return route is None or route.fate == STOCK


def _is_own_page(address):
    """Whether `address` leads the browser of the screen to the server of the box, or to a page the box serves.

    The server is the one the box holds a credential of, at the very scheme,
    host and port the box knows it by. The box is one of `lock.box_hosts`, on
    the port of its web server or of the box service.
    """
    found = lock.origin(address, plain=True)
    if not found:
        return False
    scheme, host, port = found
    if lock.is_locked() and found == lock.origin(seams.core('tools.helpers').get_conf(trust.SERVER_KEY)):
        return True
    on_box = port == WEB_SERVER_PORTS[scheme] or (scheme, port) == ('http', config['http_port'])
    return on_box and host in lock.box_hosts() and _is_page(urlsplit(address).path)


def _screen_takes(address):
    """Whether the current request may give the screen of the box `address`.

    Without an address core opens again the one the box keeps. A request that
    proves an unlock gives any address; every other request, and so every
    request on a box without a server, gives one that `_is_own_page`.
    """
    return not address or (lock.is_locked() and lock.is_unlocked()) or _is_own_page(address)


def _use_device(action, *args, **kwargs):
    """Run core's device action, unless it changes a setting of the box and the request may not change the settings."""
    asked = _asked(kwargs.get(ACTION_PARAMETER))
    name = asked.get('action')
    if isinstance(name, str) and name in SETTING_ACTIONS and not lock.is_unlocked():
        return lock.refusal()
    if name == SCREEN_ACTION and not _screen_takes(asked.get(SCREEN_ADDRESS)):
        return lock.refusal() if lock.is_locked() else lock.refusal(OTHER_ADDRESS)
    return action(*args, **kwargs)


def _leave_server(disconnect, *args, **kwargs):
    """Run core's "disconnect", then make it true for the whole box.

    The layer forgets what it keeps for the server and keeps the settings of
    the box (`pairing.clear_viindoo_configuration`), so that the box can be
    connected elsewhere. Core keeps reporting to, and taking orders from, the
    server it had when it started: the box service restarts.
    """
    helpers = seams.core('tools.helpers')
    had_server = bool(helpers.get_conf('remote_server') or helpers.get_token())
    answer = disconnect(*args, **kwargs)
    try:
        pairing.clear_viindoo_configuration()
    finally:
        if had_server:
            helpers.odoo_restart(pairing.RESTART_DELAY)
    return answer


def _join_wifi(_core_join, essid, password=None, persistent=False):
    """Start to join a Wi-Fi network as `wifi.join` does, and answer how to open the box itself.

    Core's route also answers an address: its server's, or else the one the
    box has on its current network. A box without a server that has no route
    out - one reached on its own access point, without a cable - has no such
    address, and core's route fails there after the join has started. Neither
    address leads to the box once it has joined anyway: the box does not know
    its new address when it answers - the join runs in the background - but
    its host name is its own for life, and a browser on the network finds it
    by the name `<host name>.local`.

    A network without a password is joined without one, as is an empty or
    missing `password`; a password given is passed as it is. The answer holds
    core's `status` and `message`, and `box`: `host_name`, `local_name` and
    `url`, the address by which a browser opens the box. A name or a password
    `wifi.refusal` refuses is answered `refused` with its reason. A join asked
    while the box changes its Wi-Fi by itself is answered `failed`, reason
    `busy`, and is not started: it may be asked again a moment later.
    """
    password = '' if password is None else password
    reason = wifi.refusal(essid, password)
    if reason:
        return {'status': 'refused', 'reason': reason}
    if not wifi.join(essid, password, keep=bool(persistent)):
        return {'status': 'failed', 'reason': 'busy'}
    host_name = seams.core('tools.helpers').get_hostname().lower()
    return {
        'status': 'success',
        'message': f'Connecting to {essid}',
        'box': {'host_name': host_name, 'local_name': f'{host_name}.local', 'url': f'http://{host_name}.local'},
    }


def _forget_wifi(forget, *args, **kwargs):
    """Run core's "forget the saved network", which empties core's keys, then forget the network the box keeps."""
    answer = forget(*args, **kwargs)
    wifi.forget()
    return answer


# The parameter by which core's connect route renames the box; the box keeps the host name it was first given.
RENAME_PARAMETER = 'iotname'


def _change_server(connect, *args, **kwargs):
    """Run core's "connect" without its rename, then drop the certificates trusted for a server the box leaves.

    The host name of the box is its identity for life: `iotname` is removed
    from what core receives, whoever asks and whatever the lock says, so core's
    rename script never runs. The box trusts only what the installer trusted
    for its server; a call that leaves the server as it was (a refusal, the
    same server again) keeps them.
    """
    kwargs.pop(RENAME_PARAMETER, None)
    helpers = seams.core('tools.helpers')
    server = helpers.get_conf('remote_server')
    try:
        return connect(*args[:1], **kwargs)
    finally:
        if helpers.get_conf('remote_server') != server:
            trust.forget_certificates(helpers)


def _parameter_changes(expected, found):
    """How the parameter names `found` differ from `expected`: the ones added, the ones removed, another order."""
    changes = []
    added = [parameter for parameter in found if parameter not in expected]
    removed = [parameter for parameter in expected if parameter not in found]
    if added:
        changes.append(f'adds {", ".join(added)}')
    if removed:
        changes.append(f'removes {", ".join(removed)}')
    kept = [parameter for parameter in found if parameter in expected]
    if kept != [parameter for parameter in expected if parameter in found]:
        changes.append(f'changes the order of {", ".join(kept)}')
    return changes


def _wifi_differences(name, method):
    """What `_join_wifi` relies on that core's JOIN_WIFI, served by `method`, no longer gives."""
    found = []
    parameters = tuple(inspect.signature(method).parameters)
    if parameters != WIFI_PARAMETERS:
        found.append(
            f'{JOIN_WIFI} ({name}) takes ({", ".join(parameters)}), not ({", ".join(WIFI_PARAMETERS)}): core '
            f'{" and ".join(_parameter_changes(WIFI_PARAMETERS, parameters))}, and the layer serves the route with '
            f'the parameters it knows')
    return found + wifi.core_differences()


def _differences():
    """What core registers that ROUTES does not expect, and what ROUTES expects that core does not give."""
    controllers, differences = _core_controllers()
    served = {}
    for controller in controllers:
        for name, addresses in _endpoints(controller):
            for address in addresses:
                served.setdefault(address, name)
            if (DEVICE_ACTION in addresses
                    and ACTION_PARAMETER not in inspect.signature(vars(controller)[name]).parameters):
                differences.append(
                    f'{DEVICE_ACTION} ({name}) does not take the action of a device in `{ACTION_PARAMETER}`: '
                    f'{", ".join(SETTING_ACTIONS)} are not locked and {SCREEN_ACTION} takes any address')
            if JOIN_WIFI in addresses:
                differences += _wifi_differences(name, vars(controller)[name])
    for address, name in sorted(served.items()):
        if address not in ROUTES:
            differences.append(f'{address} ({name}) is not listed: it answers "not found"')
        elif name != ROUTES[address].method:
            expected, fate = ROUTES[address][:2]
            differences.append(f'{address} is served by {name}, not by {expected}: it stays {fate}')
    differences += [
        f'{address} ({route.method}, {route.fate}) is no longer a route of core'
        for address, route in sorted(ROUTES.items()) if address not in served
    ]
    return differences


@seams.register
class CoreRoutes(seams.Seam):
    number = 13
    target = 'routes of the controllers of hw_drivers, hw_escpos and hw_posbox_homepage; database routes of web'
    feature = ('the routes of the box modules of core and the database addresses of web are those the layer '
               'lists, each closed, locked or kept from other web sites as listed; one it does not list answers '
               '"not found"; a setting is changed for no page of another web site; of the device route, the '
               'choices the box keeps for a keyboard device are locked, and the screen takes an address that is '
               'no page of the server of the box or of the box from an unlocked request only; the Wi-Fi route '
               'takes the parameters of core, and a network kept is joined as the box starts before core decides '
               'whether to start the access point, which announces the name of a Viindoo box')

    def check(self):
        differences = _differences()
        if differences:
            raise seams.SeamError('; '.join(differences))

    def apply(self):
        """Nothing is left to do: the controllers that settle the routes exist since this file was loaded."""


def _lock_all():
    locks = []
    for controller in _core_controllers()[0]:
        try:
            found = _lock(controller)
        except Exception as error:
            _logger.critical(
                'Viindoo layer integrity: the routes of %s could be neither closed nor locked.',
                controller.__name__, exc_info=True)
            seams.report_problem(f'routes of {controller.__name__}', f'{type(error).__name__}: {error}')
            continue
        if found:
            locks.append(found)
    return locks


# Odoo finds a controller through the class it inherits from, which does not keep it alive: this list does.
LOCKS = _lock_all()
seams.apply(only=(CoreRoutes.number,))
