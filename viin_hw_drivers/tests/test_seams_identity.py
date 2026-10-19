"""What the box says of itself to its server: its address, its release and its hardware address (rows 7, 11, 17)."""
import json
import tempfile
from pathlib import Path
from unittest import mock

import common
import services_common as box

CREDENTIAL = 'suite-credential-identity'
OWN_NAME = 'box.shop.example'
VIINDOO_NAME = 'k3x9.iot.viindoo.example'
SITE_ADDRESS = '192.168.1.50'
LINK_LOCAL = '169.254.7.7'
# The wildcard name of the certificates of core's vendor, as core keeps it once a server handed it over.
VENDOR_ZONE = '*.vendor-zone.example'
# The software box has no network: core finds the box at the address of the site, as it does on one.
CORE_FINDS_THE_SITE_ADDRESS = {
    'def get_ip():': f'def get_ip():\n    return {SITE_ADDRESS!r}\n\n\ndef _address_of_the_machine():'}
HOST_NAME = 'box-suite-identity'
IMAGE_VERSION = Path('/var/odoo/iotbox_version')
SEAMS = (1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 15, 17, 18)

WIRED = 'dc:a6:32:00:00:01'
WIRELESS = 'dc:a6:32:00:00:02'
DONGLE = '00:e0:4c:00:00:03'
DEVICE = 'box_suite_identity_device'


def release_mark():
    return f'+viin.{box.pointer_release()}'


def first_report_of_a_start(port):
    """Start a box beside its server; give what the first report of the start told the server of the box."""
    server = common.FakeServer(port=port, channel='box-suite-channel')
    common.start()
    common.wait_until(lambda: common.reported_credentials(server), 'the first report of the box')
    first = next(request for request in server.requests if request['path'] == '/iot/setup')
    return {key: first['body']['params']['iot_box'][key] for key in ('ip', 'version')}


def first_report_of_a_start_on_the_site_network(port):
    """Start a box whose core finds it at the address of the site; give what its first report told the server."""
    configuration = common.configuration_file(
        tempfile.mkdtemp(prefix='box-suite-'), core_changes=CORE_FINDS_THE_SITE_ADDRESS)
    with box.network({'eth0': SITE_ADDRESS}):
        server = common.FakeServer(port=port, channel='box-suite-channel')
        common.start(configuration)
        common.wait_until(lambda: common.reported_credentials(server), 'the first report of the box')
    first = next(request for request in server.requests if request['path'] == '/iot/setup')
    return {'ip': first['body']['params']['iot_box']['ip']}


class IdentityCase(common.BoxCase):

    def reported(self):
        """What the box tells a server of itself in its next report, as core's device loop sends it."""
        server = self.server(channel='box-suite-channel')
        manager = common.core('main').manager
        # Core's device loop looks at the address, the version and the hardware address of the box every three seconds.
        manager._get_changes_to_send()
        self.report_to(server)
        reports = [request for request in server.requests if request['path'] == '/iot/setup']
        return reports[-1]['body']['params']['iot_box']


class TestReportedAddress(IdentityCase):

    def test_name_the_installer_gave_the_box_is_reported_before_its_viindoo_name(self):
        common.store_box_configuration(viin_box_address=OWN_NAME, viin_managed_name=VIINDOO_NAME)

        self.assertEqual(self.reported()['ip'], OWN_NAME)

    def test_viindoo_name_is_reported_when_the_installer_gave_no_name(self):
        common.store_box_configuration(viin_managed_name=VIINDOO_NAME)

        self.assertEqual(self.reported()['ip'], VIINDOO_NAME)

    def test_address_of_the_site_is_reported_when_the_box_has_no_name(self):
        helpers = common.core('tools.helpers')
        with mock.patch.object(helpers, 'get_ip', return_value=SITE_ADDRESS):
            self.assertEqual(self.reported()['ip'], SITE_ADDRESS)

    def test_link_local_address_is_never_reported(self):
        helpers = common.core('tools.helpers')
        with mock.patch.object(helpers, 'get_ip', return_value=LINK_LOCAL), box.network({'eth0': SITE_ADDRESS}):
            self.assertEqual(self.reported()['ip'], SITE_ADDRESS)

    def test_box_without_an_address_of_the_site_reports_its_name_on_the_local_network(self):
        with common.named(HOST_NAME):
            self.assertEqual(self.reported()['ip'], f'{HOST_NAME}.local')

    def test_name_core_derives_from_the_certificate_zone_of_its_vendor_is_never_reported(self):
        common.store_box_configuration(subject=VENDOR_ZONE)
        helpers = common.core('tools.helpers')
        with mock.patch.object(helpers, 'get_ip', return_value=SITE_ADDRESS), box.network({'eth0': SITE_ADDRESS}):
            self.assertEqual(self.reported()['ip'], SITE_ADDRESS)

    def test_box_in_the_certificate_zone_of_core_without_an_address_of_the_site_reports_its_local_name(self):
        common.store_box_configuration(subject=VENDOR_ZONE)
        helpers = common.core('tools.helpers')
        with mock.patch.object(helpers, 'get_ip', return_value=LINK_LOCAL), box.network({'eth0': LINK_LOCAL}), \
                common.named(HOST_NAME):
            self.assertEqual(self.reported()['ip'], f'{HOST_NAME}.local')


class TestFirstReportOfAStart(common.BoxStateCase):

    def test_first_report_of_a_start_carries_the_name_and_the_release_the_layer_gives(self):
        port = common.closed_port()
        common.store_box_configuration(
            remote_server=f'http://127.0.0.1:{port}', token=CREDENTIAL, viin_box_address=OWN_NAME)

        first = common.in_new_process(first_report_of_a_start, port=port)

        self.assertEqual(first['ip'], OWN_NAME)
        self.assertTrue(first['version'].endswith(release_mark()), first['version'])

    def test_first_report_of_a_start_carries_the_address_of_the_site_and_not_a_name_in_the_zone_of_core(self):
        port = common.closed_port()
        common.store_box_configuration(
            remote_server=f'http://127.0.0.1:{port}', token=CREDENTIAL, subject=VENDOR_ZONE)

        first = common.in_new_process(first_report_of_a_start_on_the_site_network, port=port)

        self.assertEqual(first['ip'], SITE_ADDRESS)


class TestReportedVersion(IdentityCase):

    def test_detailed_version_names_the_release_the_box_runs(self):
        helpers = common.core('tools.helpers')

        self.assertTrue(helpers.get_version(detailed_version=True).endswith(release_mark()))
        self.assertTrue(self.reported()['version'].endswith(release_mark()))

    def test_short_version_stays_the_image_version_core_reads_as_a_number(self):
        helpers = common.core('tools.helpers')

        self.assertEqual(helpers.get_version(), f'L{IMAGE_VERSION.read_text().strip()}')


class HardwareAddressCase(IdentityCase):

    def board(self, device_tree=True):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix='box-suite-'))
        return box.FakeBoard(directory, device_tree)

    def reported_on(self, board):
        # Core, alone, gives the address of whichever interface carries the traffic at that moment.
        with board.patched(), box.network({'wlan0': '192.168.1.60'}):
            return self.reported()['identifier']


class TestReportedHardwareAddress(HardwareAddressCase):

    def test_built_in_wired_interface_gives_the_address_whichever_interface_carries_the_traffic(self):
        board = self.board()
        board.interface('wlan0', WIRELESS, wireless=True)
        board.interface('eth0', WIRED)
        board.interface('eth1', DONGLE, on='usb')

        self.assertEqual(self.reported_on(board), WIRED)

    def test_board_without_a_built_in_wired_interface_gives_the_address_of_its_built_in_wireless_one(self):
        board = self.board()
        board.interface('wlan0', WIRELESS, wireless=True)
        board.interface('eth1', DONGLE, on='usb')

        self.assertEqual(self.reported_on(board), WIRELESS)

    def test_wired_interface_on_the_internal_hub_of_the_board_counts_as_built_in(self):
        board = self.board()
        board.interface('eth0', WIRED, on='board_usb')
        board.interface('wlan0', WIRELESS, wireless=True)

        self.assertEqual(self.reported_on(board), WIRED)

    def test_board_without_a_built_in_interface_gives_the_address_core_finds(self):
        board = self.board()
        board.interface('eth1', DONGLE, on='usb')

        self.assertEqual(self.reported_on(board), box.SUITE_HARDWARE_ADDRESS)


class TestCommandsOfTheLiveChannel(HardwareAddressCase):

    def setUp(self):
        super().setUp()
        self.device = box.plug(DEVICE)
        self.addCleanup(common.core('main').iot_devices.pop, DEVICE, None)
        board = self.board()
        board.interface('eth0', WIRED)
        board.interface('wlan0', WIRELESS, wireless=True)
        self.enterContext(board.patched())
        self.enterContext(box.network({'wlan0': '192.168.1.60'}))

    def command_to(self, address, number):
        common.core('websocket_client').on_message(None, json.dumps([{'message': {'type': 'iot_action', 'payload': {
            'iotDevice': {'iotIdentifiers': [address], 'identifiers': [{'identifier': DEVICE}]},
            'action': 'box_suite', 'iot_idempotent_id': f'box-suite-command-{number}-{id(self)}'}}}]))

    def test_command_sent_to_the_hardware_address_the_box_reports_reaches_its_device(self):
        self.command_to(WIRED, 1)

        self.assertEqual(self.device.asked, ['box_suite'])

    def test_command_sent_to_the_address_of_the_interface_in_use_is_left_to_the_box_it_names(self):
        self.command_to(box.SUITE_HARDWARE_ADDRESS, 2)

        self.assertEqual(self.device.asked, [])


class TestReportedAddressSeamMoved(common.MovedSeamCase):
    number = 7
    core_changes = {'def _get_domain(self):': 'def _get_domain(self, refresh=False):'}
    other_seams = tuple(seam for seam in SEAMS if seam != 7)


class TestReportedVersionSeamMoved(common.MovedSeamCase):
    number = 11
    core_changes = {'def get_version(detailed_version=False):': 'def get_version(detailed_version=False, short=True):'}
    other_seams = tuple(seam for seam in SEAMS if seam != 11)


class TestReportedHardwareAddressSeamMoved(common.MovedSeamCase):
    number = 17
    core_changes = {'def get_mac_address():': 'def get_mac_address(interface=None):'}
    other_seams = tuple(seam for seam in SEAMS if seam != 17)
