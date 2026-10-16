"""Seams of reporting: what the box tells its server reaches it, and the live channel comes back by itself."""
import inspect

from .. import reporting
from . import CORE, FunctionSeam, PartsSeam, SeamError, register

MANAGER = f'{CORE}.main'
LIVE_CHANNEL = f'{CORE}.websocket_client'


def _names(function):
    return set(inspect.unwrap(function).__code__.co_names)


class _ManagerSeam(FunctionSeam):
    """A method of core's device manager, which its loop must still reach by the names in `loop`."""

    module = MANAGER
    loop = ()

    def check(self):
        super().check()
        manager = self._resolve()[0]
        try:
            missing = set(self.loop) - _names(manager.run)
        except AttributeError:
            raise SeamError('Manager.run does not exist') from None
        if missing:
            raise SeamError('Manager.run no longer uses %s' % ', '.join(sorted(missing)))


@register
class ReportOutcome(_ManagerSeam):
    number = 5
    name = 'Manager.send_alldevices'
    parameters = ('self', 'iot_client')
    loop = ('send_alldevices', 'iot_channel', 'start')
    feature = ('the box knows whether its server took its last report, and gets its live channel although the '
               'server was not there when it started')

    def check(self):
        super().check()
        if not {'server_url', 'iot_channel'} <= _names(self._resolve()[2]):
            raise SeamError('no longer gives the channel the server answers to the client it is handed')

    def replacement(self, original):
        def send_alldevices(manager, iot_client=None):
            return reporting.deliver(lambda client: original(manager, client), iot_client, bool(manager.server_url))

        return send_alldevices


@register
class ReportOwed(_ManagerSeam):
    number = 6
    name = 'Manager._get_changes_to_send'
    parameters = ('self',)
    loop = ('_get_changes_to_send', 'send_alldevices')
    feature = ('a report the server did not take, and one a part of the layer asks for, are sent without waiting '
               'for a device to change')

    def replacement(self, original):
        def _get_changes_to_send(manager):
            changed = original(manager)
            return changed or (bool(manager.server_url) and reporting.is_due())

        return _get_changes_to_send


def _report_then_subscribe(original):
    def on_open(client, ws):
        reporting.report_on_connection(client)
        original(client, ws)
        reporting.channel_opened()

    return on_open


def _note_closed(original):
    def on_close(ws, close_status_code, close_msg):
        reporting.channel_lost(f'closed ({close_status_code})')
        return original(ws, close_status_code, close_msg)

    return on_close


def _note_error(original):
    def on_error(ws, error):
        reporting.channel_lost(error)
        return original(ws, error)

    return on_error


class _Callback(FunctionSeam):
    """One of the functions core's live channel client hands to the library that runs its connection."""

    module = LIVE_CHANNEL

    def __init__(self, name, parameters, wrap):
        super().__init__()
        self.name = name
        self.parameters = parameters
        self._wrap = wrap

    def check(self):
        super().check()
        owner = self._resolve()[0]
        client = owner if inspect.isclass(owner) else getattr(owner, 'WebsocketClient', None)
        run = getattr(client, 'run', None)
        if run is None:
            raise SeamError('WebsocketClient.run does not exist')
        callback = self.name.rpartition('.')[2]
        if callback not in _names(run):
            raise SeamError(f'WebsocketClient.run no longer hands {callback} to its connection')

    def replacement(self, original):
        return self._wrap(original)


@register
class LiveChannel(PartsSeam):
    number = 8
    target = f'{LIVE_CHANNEL}: WebsocketClient.on_open, on_close, on_error'
    feature = ('each time the live channel connects, the server gets the devices the box has before the box '
               'subscribes; the box knows whether its live channel is connected')

    def __init__(self):
        self._callbacks = {callback.name: callback for callback in (
            _Callback('WebsocketClient.on_open', ('self', 'ws'), _report_then_subscribe),
            _Callback('on_close', ('ws', 'close_status_code', 'close_msg'), _note_closed),
            _Callback('on_error', ('ws', 'error'), _note_error),
        )}

    @property
    def parts(self):
        return tuple(self._callbacks)

    def check_part(self, part):
        self._callbacks[part].check()

    def apply_part(self, part):
        self._callbacks[part].apply()
