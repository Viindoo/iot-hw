"""Seam of the readings the box confirms: the event a device sends to the pages that listen to it."""
import inspect

from .. import attestation, pairing
from . import CORE, FunctionSeam, SeamError, core, register

ACTION_ROUTE = 'controllers.driver'


def _constants(code):
    """Every constant of `code`, those grouped in tuples included."""
    found, pending = set(), list(code.co_consts)
    while pending:
        constant = pending.pop()
        if isinstance(constant, tuple):
            pending.extend(constant)
        elif isinstance(constant, str):
            found.add(constant)
    return found


@register
class ConfirmedReadings(FunctionSeam):
    number = 9
    module = f'{CORE}.event_manager'
    name = 'EventManager.device_changed'
    parameters = ('self', 'device')
    feature = ('the box tells its server the value of each reading the server asked it for, before the page gets '
               'it; each event names the start of the box service it comes from')
    # Core builds the event from the device's data and the data of the action under way, then keeps it and
    # wakes the pages that wait: what the layer adds is read, and sent, before that happens.
    uses = frozenset({'data', 'device_identifier', 'request', 'params', 'events', 'append', 'sessions', 'set'})
    keys = frozenset({'data', 'request_data'})

    def check(self):
        super().check()
        original = inspect.unwrap(self._resolve()[2])
        missing = sorted(self.uses - set(original.__code__.co_names))
        missing += sorted(self.keys - _constants(original.__code__))
        if missing:
            raise SeamError('no longer builds and hands out its event as the layer expects: %s' % ', '.join(missing))
        if 'request' not in original.__globals__:
            raise SeamError(f'{self.module} no longer holds the request under way')
        try:
            action = core(ACTION_ROUTE).DriverController.action
        except (KeyError, AttributeError):
            raise SeamError(f'{CORE}.{ACTION_ROUTE}.DriverController.action does not exist') from None
        if 'data' not in inspect.signature(action).parameters:
            raise SeamError('the action route no longer takes the data of the action as `data`')

    def apply(self):
        super().apply()
        # Only a box whose events are confirmed may say so when it pairs.
        pairing.declare_confirms_readings()

    def replacement(self, original):
        request = original.__globals__['request']

        def device_changed(event_manager, device):
            # One copy of the device's data: the value told to the server is the value the page gets, even when
            # the driver changes its data while the server is answered.
            reading = attestation.seen(device)
            try:
                attestation.confirm(reading, request)
            finally:
                released = original(event_manager, reading)
            return released

        return device_changed
