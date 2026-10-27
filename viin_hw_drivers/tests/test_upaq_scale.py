"""The UPA-Q scale (UpaQScaleDriver): found at 9600 baud by a weight line, a genuine Adam and a Toledo left to
the drivers of core, core's own scale classes untouched. The serial ports are scripted doubles."""
import threading
import types
from unittest import mock

import serial

import common

UPA_Q_PORT = '/dev/ttyBoxUpaQ'
ADAM_PORT = '/dev/ttyBoxAdam'
TOLEDO_PORT = '/dev/ttyBoxToledo'
BROKEN_PORT = '/dev/ttyBoxBroken'
REQUEST = b'P\r\n'
WEIGHT_LINE = b'  1.234kg\r\n'
TOLEDO_PROBE = b'Ehello'
TOLEDO_ANSWER = b'\x02E\rhello'


class ScriptedPorts:
    """Serial ports whose scale answers the request for the weight with a script, at one speed only.

    `serial.Serial` is replaced for as long as the object is entered: core's drivers and the layer's open
    their ports through it. Every request written is kept, with the speed the port was opened at.
    """

    def __init__(self):
        self.scripts = {}
        self.writes = []
        self._lock = threading.Lock()

    def script(self, path, baudrate, request, answer):
        self.scripts[path] = (baudrate, request, answer)

    def silence(self, path):
        self.scripts.pop(path, None)

    def writes_to(self, path, data):
        return [write for write in self.writes if write[0] == path and write[2] == data]

    def __enter__(self):
        patch = mock.patch.object(serial, 'Serial', self._open)
        patch.start()
        self._patch = patch
        return self

    def __exit__(self, *error):
        self._patch.stop()

    def _open(self, path, baudrate=9600, **_settings):
        if path == BROKEN_PORT:
            raise serial.SerialException(f'could not open port {path}')
        return _Port(self, path, baudrate)


class _Port:
    def __init__(self, ports, path, baudrate):
        self._ports, self.path, self.baudrate = ports, path, baudrate
        self._pending = b''
        self.is_open = True

    def isOpen(self):
        return self.is_open

    def close(self):
        self.is_open = False

    def write(self, data):
        with self._ports._lock:
            self._ports.writes.append((self.path, self.baudrate, bytes(data)))
        speed, request, answer = self._ports.scripts.get(self.path, (None, None, b''))
        if speed == self.baudrate and bytes(data).startswith(request):
            self._pending += answer
        return len(data)

    def read(self, size=1):
        taken, self._pending = self._pending[:size], self._pending[size:]
        return taken

    def read_until(self, terminator=b'\n', size=None):
        end = self._pending.find(terminator)
        end = len(self._pending) if end < 0 else end + len(terminator)
        return self.read(end)


class UpaQCase(common.BoxCase):

    def setUp(self):
        super().setUp()
        self.ports = ScriptedPorts()
        self.ports.__enter__()
        self.addCleanup(self.ports.__exit__)
        self.main = common.core('main')

    def plug(self, path):
        """Offer the port `path` to the serial interface of the box, as it finds a port; return the device taken."""
        interface = self.main.interfaces['SerialInterface']()
        interface.update_iot_devices({path: {'identifier': path}})
        device = self.main.iot_devices.get(path)
        if device:
            self.addCleanup(self.unplug, path)
        return device

    def unplug(self, path):
        device = self.main.iot_devices.get(path)
        if device:
            device.disconnect()

    def serial_drivers(self):
        return [driver.__name__ for driver in self.main.interfaces['SerialInterface']().drivers
                if driver.connection_type == 'serial']

    def read_once(self, device):
        """What the point of sale does to read the weight: the action `read_once`; the value the scale then holds."""
        device.action({'action': 'read_once'})
        return device.data['value']


class TestScaleThatAnswersAtNineThousandSixHundred(UpaQCase):

    def setUp(self):
        super().setUp()
        self.ports.script(UPA_Q_PORT, 9600, REQUEST, WEIGHT_LINE)

    def test_scale_is_found_as_a_upa_q_scale_of_ute_taiwan(self):
        device = self.plug(UPA_Q_PORT)

        self.assertEqual(type(device).__name__, 'UpaQScaleDriver')
        self.assertEqual(
            (device.device_type, device.device_name, device.device_manufacturer),
            ('scale', 'UPA-Q Serial Scale', 'UTE - TAIWAN'))

    def test_weight_the_scale_answers_is_the_value_the_point_of_sale_reads(self):
        device = self.plug(UPA_Q_PORT)

        self.assertEqual(self.read_once(device), 1.234)

    def test_scale_asked_to_stop_reading_holds_a_weight_of_zero(self):
        device = self.plug(UPA_Q_PORT)
        self.read_once(device)

        device.action({'action': 'stop_reading'})

        self.assertEqual(device.data['value'], 0)
        self.assertFalse(device._is_reading)


class TestScaleThatDoesNotAnswerAtNineThousandSixHundred(UpaQCase):

    def test_genuine_adam_at_4800_goes_to_core_after_one_request_at_9600(self):
        self.ports.script(ADAM_PORT, 4800, REQUEST, WEIGHT_LINE)

        device = self.plug(ADAM_PORT)

        self.assertEqual(
            (type(device).__name__, device.device_type, device.device_manufacturer),
            ('AdamEquipmentDriver', 'scale', 'Adam'))
        self.assertTrue(device.device_name.startswith('Adam Equipment Serial'), device.device_name)
        self.assertEqual([write[1] for write in self.ports.writes_to(ADAM_PORT, REQUEST)], [9600, 4800])

    def test_toledo_is_still_a_toledo_and_is_not_asked_for_a_weight_at_9600(self):
        self.ports.script(TOLEDO_PORT, 9600, TOLEDO_PROBE, TOLEDO_ANSWER)

        device = self.plug(TOLEDO_PORT)

        self.assertEqual(type(device).__name__, 'Toledo8217Driver')
        self.assertEqual(self.ports.writes_to(TOLEDO_PORT, REQUEST), [])

    def test_port_that_cannot_be_opened_is_taken_by_no_scale(self):
        self.assertIsNone(self.plug(BROKEN_PORT))

    def test_upa_q_that_stays_silent_is_found_again_with_a_load_on_the_pan_after_a_restart(self):
        silent = self.plug(UPA_Q_PORT)
        self.assertEqual(type(silent).__name__, 'AdamEquipmentDriver')
        self.unplug(UPA_Q_PORT)

        # A load on the pan, and the box restarted: the interface probes the port again.
        self.ports.script(UPA_Q_PORT, 9600, REQUEST, WEIGHT_LINE)
        device = self.plug(UPA_Q_PORT)

        self.assertEqual(type(device).__name__, 'UpaQScaleDriver')


class TestScaleDriversOfCore(UpaQCase):

    def adam(self):
        return next(driver for driver in self.main.drivers if driver.__name__ == 'AdamEquipmentDriver')

    def test_serial_drivers_are_offered_a_port_in_the_order_toledo_upa_q_adam(self):
        order = self.serial_drivers()

        self.assertLess(order.index('Toledo8217Driver'), order.index('UpaQScaleDriver'))
        self.assertLess(order.index('UpaQScaleDriver'), order.index('AdamEquipmentDriver'))

    def test_core_registers_each_scale_driver_once(self):
        names = [driver.__name__ for driver in self.main.drivers]

        self.assertEqual(
            [names.count(name) for name in ('Toledo8217Driver', 'AdamEquipmentDriver', 'UpaQScaleDriver')], [1, 1, 1])

    def test_adam_class_of_core_keeps_its_speed_and_its_name(self):
        protocol = self.adam()._protocol

        self.assertEqual((protocol.baudrate, protocol.name), (4800, 'Adam Equipment'))

    def test_upa_q_driver_is_an_adam_class_with_a_protocol_of_its_own(self):
        upa_q = next(driver for driver in self.main.drivers if driver.__name__ == 'UpaQScaleDriver')

        self.assertTrue(issubclass(upa_q, self.adam()))
        self.assertIsNot(upa_q._protocol, self.adam()._protocol)
        self.assertEqual(upa_q._protocol.baudrate, 9600)

    def test_scale_that_core_stops_reading_keeps_the_weight_it_holds(self):
        scale = types.SimpleNamespace(_is_reading=True, data={'value': 5.0})

        self.adam()._stop_reading_action(scale, {})

        self.assertEqual((scale._is_reading, scale.data['value']), (False, 5.0))


class TestUpaQDriverIsLoadedByTheLayer(common.BoxCase):

    def test_handler_file_of_the_upa_q_scale_is_loaded_without_failure(self):
        files = common.layer('seams.handlers').handler_files()

        self.assertIn('drivers/UpaQScaleDriver.py', files['layer'])
        self.assertEqual([failure for failure in files['failed'] if 'UpaQ' in failure['file']], [])
