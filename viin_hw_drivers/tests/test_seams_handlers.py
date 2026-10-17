"""The device handlers the box loads, its default screen page, the status ticket of a receipt printer and the owner
of a device (rows 4, 14, 19 and 15)."""
import json
import tempfile
import time
import types
from unittest import mock

import common
import services_common as box

CREDENTIAL = 'suite-credential-handlers'
SESSION = 'box-suite-station'
REQUEST = 'box-suite-request-1'
SCREEN_PAGE = '/point_of_sale/display'
SEAMS = (1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 15, 17, 18)
# Core's driver files that call the server of the box themselves: the screen and the keyboard layouts.
CORE_DRIVERS_THAT_CALL_THE_SERVER = ('DisplayDriver', 'KeyboardUSBDriver')

LAYER_DEVICE = 'box_suite_device'
PAGE_DEVICE = 'box_suite_page_device'
# Handler files the layer may hold: an interface that finds one device, the driver of that device, and a file
# that cannot be loaded.
LAYER_HANDLERS = {
    'viin_hw_drivers/iot_handlers/interfaces/BoxSuiteInterface_L.py': '''
from odoo.addons.hw_drivers.interface import Interface


class BoxSuiteInterface(Interface):
    connection_type = 'box_suite'
    _loop_delay = 1

    def get_devices(self):
        return {'box_suite_device': {'kind': 'box_suite'}}
''',
    'viin_hw_drivers/iot_handlers/drivers/BoxSuiteDriver_L.py': '''
from odoo.addons.hw_drivers.driver import Driver


class BoxSuiteDriver(Driver):
    connection_type = 'box_suite'

    def __init__(self, identifier, device):
        super().__init__(identifier, device)
        self.device_type = 'keyboard'
        self.device_name = 'Box suite device'
        self._actions.update({'box_suite': lambda data: None})

    @classmethod
    def supported(cls, device):
        return True

    def run(self):
        self._stopped.wait()
''',
    'viin_hw_drivers/iot_handlers/drivers/BoxSuiteBroken_L.py': '''
raise ImportError('a handler file the box cannot load')
''',
}
BROKEN_LAYER_FILE = 'drivers/BoxSuiteBroken_L.py'
# A file of core's handlers that cannot be loaded once an update changed it.
CORE_FILE_THAT_FAILS = {'from functools import reduce': 'from functools import reduce\nraise ImportError("box suite")'}
BROKEN_CORE_FILE = 'drivers/L10nKeEDISerialDriver.py'
# A file of core's handlers that reaches the network by a library the layer cannot verify.
CORE_FILE_WITH_ANOTHER_LIBRARY = {'import struct': 'import struct\nimport requests'}
# The title of the status ticket a Viindoo box prints, and the name core gives the box on its own ticket.
TICKET_TITLE = b'Viindoo IoT Box'
CORE_BOX_NAME = b'IoTBox'
# The network interfaces of a box that has none but its loopback, as the printer driver of core reads them.
NO_NETWORK = types.SimpleNamespace(AF_INET=2, interfaces=lambda: [], ifaddresses=lambda _interface: {})


def _drivers_line():
    lines = common.layer('selftest').run(wait=0)['lines']
    line = next(line for line in lines if line['id'] == 'drivers')
    return [line['state'], line['reason']]


def start_with_handler_files_of_the_layer():
    """Start a box whose layer holds LAYER_HANDLERS; give its devices, the files it could not load, the drivers line."""
    common.start(box.with_layer_files(LAYER_HANDLERS))
    try:
        box.wait_for_devices(LAYER_DEVICE, timeout=30)
    except AssertionError:
        pass
    return {
        'devices': sorted(common.core('main').iot_devices),
        'failed': common.layer('seams.handlers').handler_files()['failed'],
        'drivers_line': _drivers_line(),
    }


def start_with_a_handler_file_of_core_that_cannot_be_loaded():
    """Start a box whose core holds a handler file that cannot be loaded; give the files it lists as failed."""
    common.start(common.configuration_file(tempfile.mkdtemp(prefix='box-suite-'), core_changes=CORE_FILE_THAT_FAILS))
    handlers = common.layer('seams.handlers')
    common.wait_until(lambda: handlers.handler_files()['loaded'], 'core to load its device handlers')
    return {'failed': handlers.handler_files()['failed'], 'drivers_line': _drivers_line()}


def start_when_core_calls_the_network_otherwise():
    """Start a box whose core reaches the network by a library the layer cannot verify, and whose layer holds
    LAYER_HANDLERS; give the state of the layer and the devices."""
    common.start(common.configuration_file(
        tempfile.mkdtemp(prefix='box-suite-'), core_changes=CORE_FILE_WITH_ANOTHER_LIBRARY, layer_files=LAYER_HANDLERS))
    try:
        box.wait_for_devices(LAYER_DEVICE, timeout=30)
    except AssertionError:
        pass
    return {'integrity': common.layer('seams').integrity(), 'devices': sorted(common.core('main').iot_devices)}


def take_the_device(session):
    """Start a box whose layer holds LAYER_HANDLERS and have the station `session` use its device."""
    common.start(box.with_layer_files(LAYER_HANDLERS))
    box.wait_for_devices(LAYER_DEVICE)
    return common.call_route('/hw_drivers/action', session_id=session, device_identifier=LAYER_DEVICE,
                             data=json.dumps({'action': 'box_suite'}))


def owner_after_a_restart():
    """Start the box of `take_the_device` again; give the owner its device has."""
    common.start(box.with_layer_files(LAYER_HANDLERS))
    [device] = box.wait_for_devices(LAYER_DEVICE)
    return device.data.get('owner')


class TestHandlerFilesOfTheLayer(common.BoxStateCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.happened = common.in_new_process(start_with_handler_files_of_the_layer)

    def test_device_a_handler_file_of_the_layer_finds_is_a_device_of_the_box(self):
        self.assertIn(LAYER_DEVICE, self.happened['devices'])

    def test_handler_file_of_the_layer_that_cannot_be_loaded_is_named_as_failed(self):
        failed = {(failure['file'], failure['origin']) for failure in self.happened['failed']}

        self.assertIn((BROKEN_LAYER_FILE, 'layer'), failed)

    def test_self_test_names_the_drivers_as_the_failing_layer(self):
        self.assertEqual(self.happened['drivers_line'], ['failed', 'failed_to_load'])


class TestHandlerFileOfCoreThatCannotBeLoaded(common.BoxStateCase):

    def test_handler_file_of_core_that_cannot_be_loaded_is_named_as_failed(self):
        happened = common.in_new_process(start_with_a_handler_file_of_core_that_cannot_be_loaded)

        failed = {(failure['file'], failure['origin']) for failure in happened['failed']}
        self.assertIn((BROKEN_CORE_FILE, 'core'), failed)
        self.assertEqual(happened['drivers_line'], ['failed', 'failed_to_load'])


class TestServerCallsOfTheDriverFilesOfCore(common.BoxCase):

    def namespaces(self):
        """The namespace of each driver file of core that calls the server, as it is loaded in the box."""
        main = common.core('main')
        found = {driver.__name__: driver.__init__.__globals__ for driver in main.drivers
                 if driver.__name__ in CORE_DRIVERS_THAT_CALL_THE_SERVER}
        self.assertEqual(set(found), set(CORE_DRIVERS_THAT_CALL_THE_SERVER))
        return found

    def send(self, namespace, server):
        """Call the server the way those files of core do: through their urllib3, asking for no verification."""
        return namespace['urllib3'].PoolManager(cert_reqs='CERT_NONE').request(
            'POST', f'{server.url}/iot/box_suite', body=b'{}', retries=False, timeout=10)

    def test_driver_file_of_core_sends_nothing_to_a_server_whose_certificate_nobody_vouches_for(self):
        server = self.server(certificate=common.make_certificate('localhost'))
        common.store_box_configuration(remote_server=server.url, token=CREDENTIAL)
        for name, namespace in self.namespaces().items():
            with self.subTest(driver=name), self.assertRaises(Exception):
                self.send(namespace, server)

        self.assertEqual(server.requests, [])

    def test_driver_file_of_core_reaches_the_server_whose_certificate_the_installer_trusted(self):
        certificate = common.make_certificate('localhost')
        server = self.box_with_server(CREDENTIAL, certificate=certificate)
        for name, namespace in self.namespaces().items():
            with self.subTest(driver=name):
                self.send(namespace, server)

        self.assertEqual(len([request for request in server.requests if request['path'] == '/iot/box_suite']), 2)


class TestDefaultScreenPage(common.BoxCase):

    def test_screen_page_of_the_box_names_no_vendor_of_core(self):
        page = self.request(SCREEN_PAGE)

        self.assertEqual(page.status_code, 200)
        self.assertFalse(box.names_the_vendor(page.get_data(as_text=True)))

    def test_screen_page_of_the_box_loads_nothing_from_outside_the_box(self):
        page = self.request(SCREEN_PAGE)

        self.assertEqual(box.outside_references(page.get_data(as_text=True)), [])


class TestStatusTicket(common.BoxCase):
    """The status ticket a receipt printer prints as it is plugged in, and as the box page tests it."""

    def setUp(self):
        super().setUp()
        [self.printer] = box.wait_for_devices(box.RECEIPT_PRINTER)
        self.driver_file = common.layer('seams.handlers')._namespace(type(self.printer))

    def title(self, text):
        """`text` as core prints the title of a ticket on this printer."""
        return self.driver_file['RECEIPT_PRINTER_COMMANDS'][self.printer.receipt_protocol]['title'] % text

    def printed_status(self):
        before = box.print_jobs(box.RECEIPT_PRINTER)
        self.printer.print_status()
        return box.new_print_job(box.RECEIPT_PRINTER, before)

    def test_status_ticket_is_titled_viindoo_iot_box_and_never_names_the_box_as_core_does(self):
        printed = self.printed_status()

        self.assertIn(self.title(TICKET_TITLE), printed)
        self.assertNotIn(CORE_BOX_NAME, printed)

    def test_receipt_printer_tested_from_the_box_page_prints_the_ticket_titled_viindoo_iot_box(self):
        before = box.print_jobs(box.RECEIPT_PRINTER)

        answer = self.call(box.DEVICE_TEST, device_identifier=box.RECEIPT_PRINTER)

        printed = box.new_print_job(box.RECEIPT_PRINTER, before)
        self.assertEqual(answer['status'], 'ok', answer)
        self.assertIn(self.title(TICKET_TITLE), printed)
        self.assertNotIn(CORE_BOX_NAME, printed)

    def test_status_ticket_of_a_box_without_a_network_asks_to_check_the_cable_of_the_box(self):
        with mock.patch.dict(self.driver_file, ni=NO_NETWORK):
            printed = self.printed_status()

        self.assertIn(b'Please check that the box is correctly\nconnected with a network cable', printed)
        self.assertNotIn(CORE_BOX_NAME, printed)


class TestOwnerOfADevice(common.BoxStateCase):

    def test_device_is_given_back_to_the_station_that_used_it_after_a_restart_of_the_box_service(self):
        box.keep_files(self, box.OWNERS_FILE)
        box.OWNERS_FILE.unlink(missing_ok=True)

        self.assertIs(common.in_new_process(take_the_device, session=SESSION), True)
        self.assertEqual(common.in_new_process(owner_after_a_restart), SESSION)


class TestWhatAPageOfTheServerGetsFromADevice(common.BoxCase):

    def test_action_of_a_page_gives_its_owner_and_its_request_back_in_the_event_of_the_device(self):
        box.keep_files(self, box.OWNERS_FILE)
        box.plug(PAGE_DEVICE)
        self.addCleanup(common.core('main').iot_devices.pop, PAGE_DEVICE, None)
        since = time.time()

        answer = self.call('/hw_drivers/action', session_id=SESSION, device_identifier=PAGE_DEVICE,
                           data=json.dumps({'action': 'read_once', 'viin_iot_request': REQUEST}))
        event = self.call('/hw_drivers/event', listener={'session_id': SESSION, 'devices': [PAGE_DEVICE],
                                                         'last_event': since})

        self.assertIs(answer, True)
        self.assertEqual(event['owner'], SESSION)
        self.assertEqual(event['request_data']['viin_iot_request'], REQUEST)


class TestCoreThatReachesTheNetworkOtherwise(common.BoxStateCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.happened = common.in_new_process(start_when_core_calls_the_network_otherwise)

    def test_handler_files_of_the_layer_are_still_loaded(self):
        self.assertEqual(common.seam_states(self.happened['integrity']).get(4), 'failed')
        self.assertIn(LAYER_DEVICE, self.happened['devices'])


class TestHandlerFilesSeamMoved(common.MovedSeamCase):
    number = 4
    core_changes = CORE_FILE_WITH_ANOTHER_LIBRARY
    other_seams = tuple(seam for seam in SEAMS if seam != 4)


class TestScreenPageSeamMoved(common.MovedSeamCase):
    number = 14
    core_changes = {'pos_display_template': 'pos_display_page'}
    other_seams = SEAMS


class TestStatusTicketSeamMoved(common.MovedSeamCase):
    number = 19
    core_changes = {"b'IoTBox Status'": "b'IoT Box Status'"}
    other_seams = SEAMS


class TestDeviceOwnerSeamMoved(common.MovedSeamCase):
    number = 15
    core_changes = {'def action(self, session_id, device_identifier, data):':
                    'def action(self, session_id, device_identifier, data, options=None):'}
    other_seams = tuple(seam for seam in SEAMS if seam != 15)
