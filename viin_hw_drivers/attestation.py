"""Confirmation of the readings the server asks for, told to it by the box itself.

When a page of the server reads a device with a value-request token and asks
for confirmation, the box sends the value it is about to hand to the browser to
its own server first. The server then accepts, for that request, only the value
the box confirmed: what a browser relays cannot be invented on the way. Every
other read - a till weighing every half second, a suggestion, a device test - is
left exactly as core makes it, with no call to the server.
"""
import json
import logging
import math
import threading
import time
import uuid

import urllib3

from . import trust
from .seams import core

_logger = logging.getLogger(__name__)

ROUTE = '/iot/reading'
# The keys the browser puts in the data of an action to ask for confirmation.
REQUEST_KEY = 'viin_iot_request'
CONFIRM_KEY = 'viin_iot_attest'
UNIT_KEY = 'unit'
BOOT_ID_KEY = 'viin_boot_id'
# How long a read the server asked for waits at most in all: for a free place, then for the server, name resolution
# and TLS included.
TIMEOUT = 3
# Confirmations at most under way at once: each holds a thread for as long as its server leaves it unanswered.
MAX_IN_FLIGHT = 3

# One value per start of the box service, kept nowhere: a page can tell events of two starts apart.
BOOT_ID = uuid.uuid4().hex

_in_flight = threading.BoundedSemaphore(MAX_IN_FLIGHT)


class _Seen:
    """The device as core builds its event from it: its own data plus BOOT_ID_KEY, the driver's dict untouched."""

    __slots__ = ('_device', 'data')

    def __init__(self, device, data):
        object.__setattr__(self, '_device', device)
        object.__setattr__(self, 'data', data)

    def __getattr__(self, name):
        return getattr(self._device, name)

    def __setattr__(self, name, value):
        setattr(self._device, name, value)


def seen(device):
    """What core's event manager is handed for `device`: `device` itself when its data cannot be copied."""
    try:
        return _Seen(device, {**device.data, BOOT_ID_KEY: BOOT_ID})
    except Exception:
        return device


def requested(request):
    """The value-request token of the action that asked to confirm this read; None for any other read.

    `request` is core's proxy of the HTTP request under way, unbound outside one.
    The data of an action that does not mention confirmation is not parsed.
    """
    try:
        raw = request.params.get('data') if request else None
        if not isinstance(raw, str) or CONFIRM_KEY not in raw:
            return None
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict) or data.get(CONFIRM_KEY) is not True:
        return None
    token = data.get(REQUEST_KEY)
    return token if isinstance(token, str) and token else None


def finite_number(value):
    """Whether `value` is a number JSON carries as one: not a boolean, a text, an infinity or NaN."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def reading(device, token):
    """The params of the confirmation of what `device` holds now, for the request `token`; None when no number.

    The unit is sent only as the driver's own text, exactly: the server compares
    it with the unit the browser relays, so a default here would refuse an
    honest reading.
    """
    data = device.data
    value = data.get('value')
    if not finite_number(value):
        return None
    params = {
        'device_identifier': device.device_identifier,
        'request': token,
        'value': value,
        'time': time.time(),
    }
    unit = data.get(UNIT_KEY)
    if isinstance(unit, str) and unit:
        params[UNIT_KEY] = unit
    return params


def _post(params):
    helpers = core('tools.helpers')
    server, credential = helpers.get_odoo_server_url(), helpers.get_token()
    if not server or not credential:
        _logger.debug('The box has no server to confirm a reading to')
        return
    params = dict(params, token=credential, iot_mac=core('main').manager.mac_address)
    body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params}, allow_nan=False)
    pool = trust.pool_manager(timeout=urllib3.Timeout(total=TIMEOUT), retries=False)
    try:
        response = pool.request(
            'POST', server.rstrip('/') + ROUTE, body=body, headers={'Content-Type': 'application/json'},
            redirect=False, preload_content=False)
        response.close()
    finally:
        pool.clear()
    if response.status != 200:
        _logger.warning('The server of the box answered a confirmation of a reading with HTTP %s', response.status)


def _send(params, ended):
    try:
        _post(params)
    except Exception as error:
        # The message of the error may quote what was sent.
        _logger.warning('A reading could not be confirmed to the server of the box (%s)', type(error).__name__)
    finally:
        _in_flight.release()
        ended.set()


def confirm(device, request):
    """Tell the server the value `device` holds when the action under way asked for it; never raises.

    Returns once the server answered, the call failed, or TIMEOUT seconds have
    passed since the read asked, whichever comes first - waiting for a free
    place among the confirmations under way is part of that time; a read that
    did not ask returns at once.
    """
    token = requested(request)
    if token is None:
        return
    deadline = time.monotonic() + TIMEOUT
    try:
        params = reading(device, token)
        if params is None:
            _logger.info('A reading asked of %s holds no number: nothing is confirmed', device.device_identifier)
            return
        if not _in_flight.acquire(timeout=max(0.0, deadline - time.monotonic())):
            _logger.warning(
                '%s confirmations of readings are still waiting for the server of the box: this one is not sent',
                MAX_IN_FLIGHT)
            return
        ended = threading.Event()
        try:
            threading.Thread(target=_send, args=(params, ended), name='ViinReadingConfirmation', daemon=True).start()
        except BaseException:
            _in_flight.release()
            raise
        if not ended.wait(max(0.0, deadline - time.monotonic())):
            _logger.warning(
                'The server of the box did not take the confirmation of a reading within %s seconds: '
                'the reading goes to the page unconfirmed', TIMEOUT)
    except Exception as error:
        _logger.warning('A reading could not be confirmed to the server of the box (%s)', type(error).__name__)
