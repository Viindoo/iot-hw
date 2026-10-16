"""Seams of trust: what core sends to the server of the box goes over a verified connection.

A module of core calls a library by a name of its own. Each seam puts a stand-in
under that name, in that module and nowhere else: the certificates the
installer trusted for the server reach the connections to that server, and no
other library in the process ever sees them.
"""
import inspect
import logging
import sys

import certifi
import requests
import urllib3

from .. import trust
from . import CORE, PartsSeam, SeamError, register

_logger = logging.getLogger(__name__)


class _StandIn:
    """Stands for a library in a module of core: the library itself, but for what a subclass defines."""

    def __init__(self, library):
        self._library = library

    def __getattr__(self, name):
        return getattr(self._library, name)


class _ServerAuthorities(_StandIn):
    """Stands for certifi: its file of authorities is the one that verifies the server of the box."""

    def where(self):
        try:
            return trust.ca_bundle_path()
        except Exception:
            # Core reads this file where it catches no error. The public authorities still verify every
            # connection, and refuse the server whose certificate the installer trusted.
            _logger.exception(
                'The certificates trusted for the server cannot be used: public certificates only are accepted')
            return trust.PUBLIC_AUTHORITIES


class _VerifyingUrllib3(_StandIn):
    """Stands for urllib3: the pool managers it builds verify the servers they call."""

    def PoolManager(self, *arguments, **options):  # noqa: N802 - the name core calls
        return trust.pool_manager(*arguments, **options)


class _VerifyingSession(requests.Session):
    """Verifies every request it sends, redirections included, as `trust.pool_manager` does."""

    def send(self, request, **options):
        try:
            options['verify'] = trust.authorities_for(request.url)
        except Exception as error:
            # The kind of failure core's log sender expects from a server it cannot reach, and reports sparingly.
            raise requests.exceptions.ConnectionError(
                'the certificates trusted for the server cannot be used') from error
        return super().send(request, **options)


class _VerifyingRequests(_StandIn):
    """Stands for requests: the sessions it builds verify the servers they call."""

    def Session(self):  # noqa: N802 - the name core calls
        return _VerifyingSession()


def verify_server_calls(module):
    """Have a file of core that is loaded after the gate verify the servers it calls through urllib3.

    `module` gets the stand-in the reports of core get; calling it again changes
    nothing. False when the module does not name urllib3.
    """
    named = vars(module).get('urllib3')
    if named is urllib3:
        module.urllib3 = _VerifyingUrllib3(urllib3)
    return named is urllib3 or isinstance(named, _VerifyingUrllib3)


class _StandInSeam(PartsSeam):
    """Gives modules of core a stand-in for a library, one module at a time.

    `stand_ins` maps each module to (the name it calls the library by, the
    library, its stand-in, the function of the module that connects through it,
    what that function must still use of the library).
    """

    stand_ins = {}

    @property
    def parts(self):
        return tuple(self.stand_ins)

    def check_part(self, part):
        name, library, stand_in, caller, used = self.stand_ins[part]
        module = sys.modules.get(part)
        if module is None:
            raise SeamError('is not loaded')
        named = vars(module).get(name)
        if named is not library and not isinstance(named, stand_in):
            raise SeamError(f'does not name {name}')
        try:
            function = module
            for attribute in caller.split('.'):
                function = getattr(function, attribute)
            names = inspect.unwrap(function).__code__.co_names
        except AttributeError:
            raise SeamError(f'{caller} does not exist') from None
        if not {name, used} <= set(names):
            raise SeamError(f'{caller} no longer connects through {name}.{used}')

    def apply_part(self, part):
        name, library, stand_in, _caller, _used = self.stand_ins[part]
        module = sys.modules[part]
        if not isinstance(vars(module).get(name), stand_in):
            setattr(module, name, stand_in(library))


@register
class ServerAuthorities(_StandInSeam):
    number = 10
    target = f'certifi in {CORE}.websocket_client and {CORE}.tools.helpers, requests in {CORE}.server_logger'
    feature = ('a server whose certificate the installer trusted gets its live channel, hence its printing, '
               'the driver files and the log of the box; that certificate vouches for no other host')

    stand_ins = {
        f'{CORE}.websocket_client': ('certifi', certifi, _ServerAuthorities, 'WebsocketClient.run', 'where'),
        f'{CORE}.tools.helpers': ('certifi', certifi, _ServerAuthorities, 'download_iot_handlers', 'where'),
        f'{CORE}.server_logger': (
            'requests', requests, _VerifyingRequests, 'AsyncHTTPHandler._periodic_flush', 'Session'),
    }


@register
class VerifiedServerCalls(_StandInSeam):
    number = 12
    target = f'urllib3 in {CORE}.main and {CORE}.websocket_client'
    feature = ('core sends the credential of the box, and the results of its prints, to an https server over a '
               'verified connection only')

    stand_ins = {
        f'{CORE}.main': ('urllib3', urllib3, _VerifyingUrllib3, 'Manager.send_alldevices', 'PoolManager'),
        f'{CORE}.websocket_client': ('urllib3', urllib3, _VerifyingUrllib3, 'send_to_controller', 'PoolManager'),
    }
