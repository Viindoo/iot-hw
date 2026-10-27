"""The UPA-Q serial scale (UTE, Taiwan).

It speaks the request and the answer of an Adam Equipment scale, at 9600 baud
instead of 4800, so it is read by core's Adam class. Core ships no driver for
it: the class below is the one that finds it, by a weight line at 9600 baud,
before core's Adam driver takes a port by default.
"""
import contextlib
import logging
import re

import serial

from odoo.addons.hw_drivers.main import drivers

_logger = logging.getLogger(__name__)

# Core keeps no module of its handler files, and importing its scale file a second time would register its
# Toledo and Adam drivers again: the Adam class is taken from the drivers core has registered.
_ADAM = next(
    (driver for driver in drivers if driver.__name__ == 'AdamEquipmentDriver' and driver.connection_type == 'serial'),
    None)
if _ADAM is None:
    raise ImportError("core's Adam Equipment driver is not registered: the UPA-Q scale cannot be built on it")

# The same record as core's protocols, written out: nothing here depends on the values of core's Adam protocol.
UPA_Q_PROTOCOL = type(_ADAM._protocol)(
    name='UPA-Q',
    baudrate=9600,
    bytesize=serial.EIGHTBITS,
    stopbits=serial.STOPBITS_ONE,
    parity=serial.PARITY_NONE,
    timeout=0.2,
    writeTimeout=0.2,
    measureRegexp=rb'\s*([0-9.]+)kg',
    statusRegexp=None,
    commandTerminator=b'\r\n',
    commandDelay=0.2,
    measureDelay=0.5,
    newMeasureDelay=5,
    measureCommand=b'P',
    emptyAnswerValid=True,
)
# How long a port is given to answer the one request of the probe, and the most it is read for.
PROBE_TIMEOUT = 1
PROBE_READ_SIZE = 64


# No `priority` here: the driver metaclass gives a class its parent's priority plus 1, so this one is 2 (Adam is 1,
# Toledo 3). It is probed after Toledo and before Adam; a priority set on Adam would move it with no sign.
class UpaQScaleDriver(_ADAM):
    """Driver for the UPA-Q serial scale."""

    _protocol = UPA_Q_PROTOCOL

    def __init__(self, identifier, device):
        super().__init__(identifier, device)
        self.device_name = 'UPA-Q Serial Scale'
        self.device_manufacturer = 'UTE - TAIWAN'

    def _stop_reading_action(self, data):
        """Stops asking for the scale value, and clears the one held."""
        self._is_reading = False
        self.data['value'] = 0

    @classmethod
    def supported(cls, device):
        """Whether a weight line comes back, at 9600 baud, to one request for the weight.

        A port that answers nothing is left to the other drivers: a genuine
        Adam, which speaks at 4800, reaches core's Adam driver. A UPA-Q that
        stays silent for lack of a load on the pan reaches it too, and is
        found again at the next start of the box with a load on the pan.
        """
        protocol = cls._protocol
        try:
            with contextlib.closing(serial.Serial(
                    device['identifier'], baudrate=protocol.baudrate, bytesize=protocol.bytesize,
                    stopbits=protocol.stopbits, parity=protocol.parity, timeout=PROBE_TIMEOUT,
                    write_timeout=PROBE_TIMEOUT)) as connection:
                connection.write(protocol.measureCommand + protocol.commandTerminator)
                answer = connection.read(PROBE_READ_SIZE)
        except serial.SerialException:
            return False
        except Exception:
            _logger.exception('Error while probing %s with protocol %s', device, protocol.name)
            return False
        return re.search(protocol.measureRegexp, answer) is not None
