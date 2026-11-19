"""The listing of the devices of an interface, and the USB keyboards and scanners of core: a device whose driver
cannot start is tried again at each look, a keyboard waits for its display, and a device plugged back reads its own
input devices."""
import inspect
import logging
import os
import stat
import tempfile
import types
from pathlib import Path
from unittest import mock

import evdev
import usb.util

import common

SEAM_LOGGER = 'odoo.addons.viin_hw_drivers.seams.handlers'
SCANNER = (0xbeef, 0x0001)
OTHER_SCANNER = (0xbeef, 0x0002)
DISPLAY = 4242
# The scan codes a stand-in scanner sends, and the characters the stand-in of the display library makes of them.
KEY_1, KEY_ENTER = 2, 28
CHARACTERS = {KEY_1: '1'}
SERIES_OF_SCANS = (KEY_1, KEY_ENTER)


def identifier_of(vendor_and_product):
    return 'usb_%04x:%04x' % vendor_and_product


class DevicesCase(common.BoxCase):

    def setUp(self):
        super().setUp()
        self.main = common.core('main')
        self.addCleanup(self.unplug_what_the_case_listed)
        self.listed_by_the_case = set()
        # A failure is logged in full once per interface, device and kind of error: each case starts with none met.
        failures = common.layer('seams.handlers')._failures
        failures.clear()
        self.addCleanup(failures.clear)

    def listed(self, identifier):
        self.listed_by_the_case.add(identifier)
        return self.main.iot_devices.get(identifier)

    def unplug_what_the_case_listed(self):
        for identifier in self.listed_by_the_case:
            device = self.main.iot_devices.pop(identifier, None)
            if device:
                device._stopped.set()


class StandInInterfaceAndDriver:
    """An interface and a driver that supports its devices.

    A device named in `failing` makes the driver raise at creation, one in `disconnect_fails` at disconnection.
    """

    def __init__(self, case):
        interface_module, driver_module = common.core('interface'), common.core('driver')
        self.starts = {}
        self.supported_calls = []
        self.failing = {}
        self.disconnect_fails = set()
        stand_in = self

        class StandInInterface(interface_module.Interface):
            connection_type = 'box_suite_stand_in'

        class StandInDriver(driver_module.Driver):
            connection_type = 'box_suite_stand_in'

            @classmethod
            def supported(cls, device):
                stand_in.supported_calls.append(device['name'])
                return device['kind'] == 'supported'

            def __init__(self, identifier, device):
                stand_in.starts[identifier] = stand_in.starts.get(identifier, 0) + 1
                if identifier in stand_in.failing:
                    raise stand_in.failing[identifier]
                super().__init__(identifier, device)

            def disconnect(self):
                if self.device_identifier in stand_in.disconnect_fails:
                    raise RuntimeError('cannot disconnect')
                super().disconnect()

            def run(self):
                self._stopped.wait()

        main = common.core('main')
        case.addCleanup(main.interfaces.pop, 'StandInInterface', None)
        case.addCleanup(lambda: main.drivers.remove(StandInDriver))
        self.interface = StandInInterface()


def device(name, kind='supported'):
    return {'name': name, 'kind': kind}


class TestDeviceListing(DevicesCase):

    def setUp(self):
        super().setUp()
        self.stand_in = StandInInterfaceAndDriver(self)
        self.interface = self.stand_in.interface

    def look(self, *names, kind='supported'):
        found = {name: device(name, kind) for name in names}
        self.interface.update_iot_devices(found)
        return [self.listed(name) for name in names]

    def test_layer_listing_is_the_one_every_interface_runs(self):
        update = common.core('interface').Interface.update_iot_devices

        self.assertTrue(getattr(update, 'viin_tries_again', False))

    def test_device_whose_driver_raises_at_creation_does_not_keep_the_other_devices_from_being_listed(self):
        self.stand_in.failing['bad'] = RuntimeError('cannot start')

        with self.assertLogs(SEAM_LOGGER, 'WARNING'):
            good, bad, other = self.look('good', 'bad', 'other')

        self.assertIsNotNone(good)
        self.assertIsNone(bad)
        self.assertIsNotNone(other)

    def test_device_whose_driver_raised_is_tried_again_at_each_look_and_listed_once_it_starts(self):
        self.stand_in.failing['bad'] = RuntimeError('cannot start')
        with self.assertLogs(SEAM_LOGGER, 'WARNING'):
            for _look in range(3):
                self.look('good', 'bad')

        self.assertEqual(self.stand_in.starts['bad'], 3)
        self.assertEqual(self.stand_in.starts['good'], 1)
        del self.stand_in.failing['bad']

        [good, bad] = self.look('good', 'bad')

        self.assertIsNotNone(bad)
        self.assertEqual(self.stand_in.starts['good'], 1)
        self.assertIs(self.main.iot_devices['good'], good)

    def test_failure_is_logged_once_with_its_traceback_however_many_looks_repeat(self):
        self.stand_in.failing['bad'] = RuntimeError('cannot start')

        with self.assertLogs(SEAM_LOGGER, 'DEBUG') as logged:
            for _look in range(4):
                self.look('bad')

        warnings = [record for record in logged.records if record.levelno >= logging.WARNING]
        self.assertEqual(len(warnings), 1)
        self.assertTrue(warnings[0].exc_info)
        self.assertEqual(len(logged.records), 4)

    def test_failure_of_another_kind_for_the_same_device_is_logged_in_full_again(self):
        self.stand_in.failing['bad'] = RuntimeError('cannot start')
        with self.assertLogs(SEAM_LOGGER, 'WARNING'):
            self.look('bad')
        self.stand_in.failing['bad'] = OSError(19, 'No such device')

        with self.assertLogs(SEAM_LOGGER, 'WARNING') as logged:
            self.look('bad')

        self.assertTrue(logged.records[0].exc_info)

    def test_device_no_driver_supports_stays_detected_and_no_driver_is_asked_again(self):
        for _look in range(3):
            [unsupported] = self.look('unsupported', kind='unsupported')

        self.assertIsNone(unsupported)
        self.assertEqual(self.stand_in.supported_calls, ['unsupported'])

    def test_device_that_is_gone_is_disconnected_and_one_that_comes_back_is_listed_anew(self):
        [first] = self.look('device')

        self.look()
        self.assertIsNone(self.main.iot_devices.get('device'))
        [second] = self.look('device')

        self.assertIsNot(first, second)
        self.assertIs(self.main.iot_devices['device'], second)

    def test_device_whose_disconnection_raises_does_not_keep_a_new_device_from_being_listed(self):
        self.look('old')
        self.stand_in.disconnect_fails.add('old')

        with self.assertLogs(SEAM_LOGGER, 'WARNING'):
            [new] = self.look('new')

        self.assertIsNotNone(new)


class StandInInputDevice:
    """An input device as evdev gives it: it can be grabbed, and sends the keys of its scans."""

    def __init__(self, path, vendor_and_product, scans=()):
        self.path = path
        self.info = types.SimpleNamespace(vendor=vendor_and_product[0], product=vendor_and_product[1])
        self.scans = scans
        self.grabs = 0
        self.gone = False

    def grab(self):
        if self.gone:
            raise OSError(19, 'No such device')
        self.grabs += 1

    def read_loop(self):
        for scancode in self.scans:
            yield evdev.InputEvent(0, 0, evdev.ecodes.EV_KEY, scancode, 1)


class StandInDisplayLibrary:
    """The display library of core's keyboard driver: a display opens once `display` is not 0."""

    def __init__(self):
        self.display = 0
        self.opened = []

    def XOpenDisplay(self, name):
        self.opened.append((name, os.environ.get('XAUTHORITY')))
        return self.display

    def XCloseDisplay(self, display):
        return 0

    def XkbKeycodeToKeysym(self, display, keycode, group, modifiers):
        return keycode - 8

    def XkbTranslateKeySym(self, display, keysym, modifiers, buffer, size, returned):
        buffer._obj.value = CHARACTERS.get(keysym._obj.value, '').encode()
        return 0


class UsbDevice:
    def __init__(self, vendor_and_product):
        self.idVendor, self.idProduct = vendor_and_product
        self.iManufacturer, self.iProduct = 1, 2
        self._interfaces = [types.SimpleNamespace(bInterfaceClass=3, bInterfaceProtocol=1)]

    def __iter__(self):
        return iter([self._interfaces])


class KeyboardCase(DevicesCase):
    """Core's USB keyboard driver with the display library, the input devices and the layout program replaced."""

    def setUp(self):
        super().setUp()
        self.driver_class = next(
            driver for driver in self.main.drivers
            if driver.__name__ == 'KeyboardUSBDriver' and driver.__module__.endswith('KeyboardUSBDriver_L'))
        self.xlib = StandInDisplayLibrary()
        namespace = inspect.unwrap(vars(self.driver_class)['__init__']).__globals__
        self.enterContext(mock.patch.dict(namespace, xlib=self.xlib))
        self.enterContext(mock.patch.dict(os.environ))
        self.enterContext(mock.patch.object(self.driver_class, 'keyboard_layout_groups', []))
        self.enterContext(mock.patch.object(
            self.driver_class, 'available_layouts', [{'name': 'English', 'layout': 'us'}]))
        self.forget_the_display()
        self.present = []
        self.enterContext(mock.patch.object(evdev, 'list_devices', lambda: [found.path for found in self.present]))
        self.enterContext(mock.patch.object(
            evdev, 'InputDevice', lambda path: next(found for found in self.present if found.path == path)))
        self.enterContext(mock.patch.object(
            usb.util, 'get_string', lambda _device, index, langid=None: {1: 'Acme', 2: 'Barcode Scanner'}[index]))
        programs = Path(tempfile.mkdtemp(prefix='box-suite-'))
        self.addCleanup(common.as_root, 'rm', '-rf', '--', str(programs))
        layout_program = programs / 'setxkbmap'
        layout_program.write_text('#!/bin/sh\nexit 0\n')
        layout_program.chmod(layout_program.stat().st_mode | stat.S_IXUSR)
        os.environ['PATH'] = f'{programs}:{os.environ["PATH"]}'
        self.interface = self.main.interfaces['USBInterface']()

    def forget_the_display(self):
        """The class holds no open display, as at the start of a box that has none; what it held is put back."""
        driver_class = self.driver_class
        held = 'display' in vars(driver_class)
        display = vars(driver_class).get('display')

        def put_back():
            if held:
                driver_class.display = display
            elif 'display' in vars(driver_class):
                del driver_class.display

        self.addCleanup(put_back)
        if held:
            del driver_class.display

    def plug(self, vendor_and_product, scans=(), path=None):
        """Plug a keyboard-class device in: the system lists its input device.

        :return: the input device
        """
        found = StandInInputDevice(path or f'/dev/input/box-suite-{len(self.present)}', vendor_and_product, scans)
        self.present.append(found)
        return found

    def look(self, *devices):
        """Have the interface look at the USB devices `devices`.

        :param devices: (vendor, product) pairs
        :return: the devices listed, in the order of `devices`; ``None`` for one not listed
        """
        self.interface.update_iot_devices({identifier_of(found): UsbDevice(found) for found in devices})
        return [self.listed(identifier_of(found)) for found in devices]


class TestKeyboardWithoutADisplay(KeyboardCase):

    def test_keyboard_device_is_not_listed_while_no_display_exists_and_listed_once_one_does(self):
        self.plug(SCANNER)
        with self.assertLogs(SEAM_LOGGER, 'DEBUG'):
            for _look in range(3):
                [found] = self.look(SCANNER)
                self.assertIsNone(found)
        self.xlib.display = DISPLAY

        [found] = self.look(SCANNER)

        self.assertIsNotNone(found)
        self.assertEqual(self.driver_class.display, DISPLAY)

    def test_start_before_the_display_is_up_leaves_no_warning_and_no_traceback_in_the_log(self):
        self.plug(SCANNER)

        with self.assertLogs(SEAM_LOGGER, 'DEBUG') as logged:
            for _look in range(4):
                self.look(SCANNER)
            self.xlib.display = DISPLAY
            [found] = self.look(SCANNER)

        self.assertIsNotNone(found)
        self.assertEqual([record for record in logged.records if record.levelno >= logging.WARNING], [])
        self.assertEqual([record for record in logged.records if record.exc_info], [])

    def test_display_is_opened_as_core_opens_it(self):
        self.xlib.display = DISPLAY
        self.plug(SCANNER)

        self.look(SCANNER)

        self.assertEqual(self.xlib.opened[0], (b':0.0', '/run/lightdm/pi/xauthority'))


class TestDeviceUnpluggedAndPluggedBack(KeyboardCase):

    def setUp(self):
        super().setUp()
        self.xlib.display = DISPLAY

    def test_scanner_plugged_back_is_listed_and_its_scans_are_read(self):
        first = self.plug(SCANNER, path='/dev/input/box-suite-first')
        [old] = self.look(SCANNER)
        self.assertIsNotNone(old)
        first.gone = True
        self.present.remove(first)
        self.look()
        self.assertIsNone(self.main.iot_devices.get(identifier_of(SCANNER)))
        self.plug(SCANNER, scans=SERIES_OF_SCANS, path='/dev/input/box-suite-second')

        [new] = self.look(SCANNER)

        self.assertIsNotNone(new)
        self.assertIsNot(new, old)
        common.wait_until(lambda: new.data.get('value') == '1', 'the scan to be read')

    def test_driver_made_for_a_device_plugged_back_holds_the_input_devices_of_that_device_only(self):
        first = self.plug(SCANNER, path='/dev/input/box-suite-first')
        [old] = self.look(SCANNER)
        first.gone = True
        self.present.remove(first)
        self.look()
        second = self.plug(SCANNER, path='/dev/input/box-suite-second')

        [new] = self.look(SCANNER)

        self.assertEqual(new.input_devices, [second])
        self.assertEqual(old.input_devices, [first])

    def test_device_plugged_after_the_one_plugged_back_is_listed(self):
        first = self.plug(SCANNER, path='/dev/input/box-suite-first')
        self.look(SCANNER)
        first.gone = True
        self.present.remove(first)
        self.look()
        self.plug(SCANNER, path='/dev/input/box-suite-second')
        self.look(SCANNER)
        self.plug(OTHER_SCANNER, path='/dev/input/box-suite-third')

        [_again, third] = self.look(SCANNER, OTHER_SCANNER)

        self.assertIsNotNone(third)


class TestDeviceListingSeamMoved(common.MovedSeamCase):
    number = 25
    core_changes = {
        'def update_iot_devices(self, devices={}):': 'def update_iot_devices(self, devices={}, scan=False):'}
    other_seams = (1, 2, 3, 23, 24)


class TestKeyboardDevicesSeamMoved(common.MovedSeamCase):
    number = 26
    core_changes = {
        "def __init__(self, identifier, device):\n        if not hasattr(KeyboardUSBDriver, 'display'):":
        "def __init__(self, identifier, device, options=None):\n        if not hasattr(KeyboardUSBDriver, 'display'):"}
    other_seams = (1, 2, 3, 23, 24, 25)
