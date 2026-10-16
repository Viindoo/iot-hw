"""The lock of the box settings: the settings code, the unlock proof, what the box does with each route of core -
close it, lock it, keep it from other web sites, or leave it to core - and what it changes for no page of another
web site."""
import contextlib
import errno
import ipaddress
import json
import logging
import socket
import time
from unittest import mock

import common
import wifi_common

CERTIFICATE = '/viin_hw_homepage/certificate'
PAIR = '/viin_hw_homepage/pair'
UNLOCK = '/viin_hw_homepage/unlock'
CONNECT = '/hw_posbox_homepage/connect_to_server'
DISCONNECT = '/hw_posbox_homepage/server_clear'
WIFI = '/hw_posbox_homepage/update_wifi'
WIFI_CLEAR = '/hw_posbox_homepage/wifi_clear'
LOG_LEVEL = '/hw_posbox_homepage/log_levels_update'
RESTART = '/hw_posbox_homepage/restart_odoo_service'
SHELL_PASSWORD = '/hw_posbox_homepage/generate_password'
# The routes of core the box must not serve: the services of the vendor of core, software from elsewhere, and
# the management of databases, which a box has none of.
CLOSED = (
    '/hw_posbox_homepage/save_credential',
    '/hw_posbox_homepage/clear_credential',
    '/hw_drivers/check_certificate',
    '/hw_posbox_homepage/six_payment_terminal_add',
    '/hw_posbox_homepage/six_payment_terminal_clear',
    '/hw_posbox_homepage/enable_ngrok',
    '/hw_proxy/perform_upgrade',
    '/hw_proxy/perform_flashing_create_partition',
    '/hw_proxy/perform_flashing_download_raspios',
    '/hw_proxy/perform_flashing_copy_raspios',
    '/hw_posbox_homepage/load_iot_handlers',
    '/hw_posbox_homepage/clear_iot_handlers',
    '/web/database/selector',
    '/web/database/manager',
    '/web/database/create',
    '/web/database/duplicate',
    '/web/database/drop',
    '/web/database/backup',
    '/web/database/restore',
    '/web/database/change_password',
    '/web/database/list',
)
# The page of the box, what it reads, and the log of the box: for the page of the box itself, and no other.
PAGE_AND_LOG = (
    '/',
    '/logs',
    '/hw_posbox_homepage/data',
    '/hw_posbox_homepage/ping',
    '/hw_posbox_homepage/wifi',
    '/hw_posbox_homepage/iot_logs',
    '/hw_posbox_homepage/log_levels',
    '/hw_drivers/download_logs',
    '/hw_proxy/get_version',
)
# The routes by which the pages of a server use the devices of the box from the browser of a user: to the box,
# those pages are another web site.
DEVICE_ROUTES = (
    '/hw_proxy/hello',
    '/hw_proxy/handshake',
    '/hw_proxy/status_json',
    '/hw_drivers/action',
    '/hw_drivers/event',
    '/hw_proxy/open_cashbox',
    '/hw_proxy/print_receipt',
    '/hw_proxy/print_xml_receipt',
)
ACTION = '/hw_drivers/action'
EVENT = '/hw_drivers/event'
# The header by which a box lets the pages of other web sites read what it answers.
OTHER_SITES_MAY_READ = 'Access-Control-Allow-Origin'

# A credential and the settings code of the box that holds it, worked out apart from the box software: the code
# the administrators of the server read there.
VECTOR_CREDENTIAL = 'abc'
VECTOR_SETTINGS_CODE = 'R4HF8KC1'
# Another credential with its settings code, worked out the same way; the code holds a 0 and two 1.
OTHER_CREDENTIAL = 'box-suite-credential-003'
OTHER_SETTINGS_CODE = 'S10ME1Y4'

CODE = 'K7Q2-M9XD-4TWP'
PAIRED_ELSEWHERE = {'status': 'paired', 'token': 'suite-credential-elsewhere', 'box_name': 'Packing line 2'}
# What core's own page sends to connect a box: a server address that carries the credential.
CORE_CONNECTION = 'http://other-server.example?token=other-credential&db_uuid=&enterprise_code='
CUSTOMER_HOST = 'box.customer.example'
# The name Viindoo issued for the box, as the client of that name stores it once the box has checked it leads to itself.
VIINDOO_NAME = 'box-7k2q.iot.example'
# The reason by which the box refuses, as locked, a request that carries no valid unlock proof.
LOCKED = 'unlock_required'
# What a JSON route that changes a setting answers to a request that carries no valid unlock proof, and to a page of
# another web site.
LOCKED_ANSWER = {'status': 'locked', 'reason': LOCKED}
OTHER_SITE_ANSWER = {'status': 'forbidden', 'reason': 'other_site'}

# An address by which the browsers of a site reach the box and that the box does not hold itself - the network of
# the site hands their requests on to it - and the page of the box as those browsers name it.
BOX = 'http://192.0.2.10:8069'
PAGE_OF_THE_BOX = f'{BOX}/'
PAGE_OF_ANOTHER_WEB_SITE = f'{common.ANOTHER_WEB_SITE}/page'
# What a browser adds of its own to a request that a page of another web site made it send, case by case.
SENT_FOR_ANOTHER_WEB_SITE = (
    {'Sec-Fetch-Site': 'cross-site'},
    {'Sec-Fetch-Site': 'same-site'},
    {'Origin': common.ANOTHER_WEB_SITE},
    {'Origin': 'null'},
    {'Referer': PAGE_OF_ANOTHER_WEB_SITE},
    {'Sec-Fetch-Site': 'cross-site', 'Origin': common.ANOTHER_WEB_SITE, 'Referer': PAGE_OF_ANOTHER_WEB_SITE},
    {'Sec-Fetch-Site': 'same-origin', 'Origin': common.ANOTHER_WEB_SITE},
    {'Sec-Fetch-Site': 'cross-site', 'Origin': BOX, 'Referer': PAGE_OF_THE_BOX},
    {'Origin': BOX, 'Referer': PAGE_OF_ANOTHER_WEB_SITE},
)
# What reaches the box with a request that no page of another web site asked for, case by case: a script adds
# nothing, a browser tells that a person typed the address or that the page of the box sent the request. The
# last two name the box at another port and by another scheme than the request does.
SENT_FOR_NO_OTHER_WEB_SITE = (
    {},
    {'Sec-Fetch-Site': 'same-origin'},
    {'Sec-Fetch-Site': 'none'},
    {'Origin': BOX},
    {'Referer': PAGE_OF_THE_BOX},
    {'Sec-Fetch-Site': 'same-origin', 'Origin': BOX, 'Referer': PAGE_OF_THE_BOX},
    {'Origin': 'http://192.0.2.10'},
    {'Origin': 'https://192.0.2.10:8069'},
)
SENT_BY_THE_PAGE_OF_THE_BOX = {'Sec-Fetch-Site': 'same-origin', 'Origin': BOX, 'Referer': PAGE_OF_THE_BOX}

DEVICE = 'box_suite_keyboard'
SCREEN = 'box_suite_screen'
# The key under which core keeps the address of the screen in the configuration of the box.
KEPT_ADDRESS = 'browser-url'
# The programs by which core turns a screen and closes the browser on it, and the browsers it starts there: the
# software box has no screen.
SCREEN_TOOLS = ('xrandr', 'sync_touchscreen.sh', 'pkill')
BROWSERS = ('chromium', 'firefox')
# A page the box itself serves, at the port of the box service: not the one core shows on a screen given no other.
PAGE_ON_THE_BOX = 'http://localhost:8069/point_of_sale/display'
SESSION = 'box-suite-session'
SCAN = '8934567890123'
# A moment whose second is nearly over, and one whose second begins.
LATE_IN_ITS_SECOND = 1900000000.999
EARLY_IN_ITS_SECOND = 1900000000.0
_device_classes = []


def shell_password():
    """The record of the password of the box user, as the system keeps it."""
    return common.as_root('getent', 'shadow', common.BOX_USER).stdout


def joining(network, host_name=None):
    """What the Wi-Fi route answers once it started to join `network`: how a browser opens the box, by the host name of
    the box in lower case (the one of this machine when left out), and never its server."""
    host = (host_name or socket.gethostname()).lower()
    return {
        'status': 'success',
        'message': f'Connecting to {network}',
        'box': {'host_name': host, 'local_name': f'{host}.local', 'url': f'http://{host}.local'},
    }


@contextlib.contextmanager
def without_a_route_out():
    """Leave the box with no route to any host outside itself during the block, as a box reached on its own access
    point with no cable plugged in."""
    connect = socket.socket.connect

    def unreachable(sock, address):
        try:
            outside = sock.family in (socket.AF_INET, socket.AF_INET6) and not ipaddress.ip_address(
                address[0]).is_loopback
        except ValueError:
            outside = True
        if outside:
            raise OSError(errno.ENETUNREACH, 'Network is unreachable')
        return connect(sock, address)

    with mock.patch.object(socket.socket, 'connect', unreachable):
        yield


class SetClock:
    """Stands for the clock the lock reads: it tells the time a test sets, and only a test moves it."""

    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


def plug_device(case):
    """Plug a device into the box started in this process until the test `case` ends, and return it.

    It is a device as core holds them. It takes what core's keyboard, scale and printer devices
    are asked for, and `asked` lists the actions it ran.
    """
    main = common.core('main')
    if not _device_classes:
        class SuiteDevice(common.core('driver').Driver):

            def __init__(self):
                super().__init__(DEVICE, None)
                self.device_type = 'keyboard'
                self.device_name = 'Box suite keyboard'
                self.asked = []
                self._actions.update(dict.fromkeys(
                    ('update_is_scanner', 'update_layout', 'read_once', 'print_receipt'), self._run))

            def _run(self, data):
                self.asked.append(data['action'])

        _device_classes.append(SuiteDevice)
    device = _device_classes[0]()
    main.iot_devices[DEVICE] = device
    case.addCleanup(main.iot_devices.pop, DEVICE, None)
    return device


def plug_screen(case):
    """Plug a screen into the box started in this process until the test `case` ends, as the only screen it has.

    Core drives it as it drives the screen of a box, but for the programs the software box lacks:
    the browser is listed instead of started. Returns that list, one entry per start: the
    arguments of the browser, the address it opens first.
    """
    main = common.core('main')
    case.enterContext(common.without_running(*SCREEN_TOOLS))
    starts = case.enterContext(common.without_starting(*BROWSERS))
    others = {identifier: device for identifier, device in main.iot_devices.items() if device.device_type == 'display'}
    for identifier in others:
        del main.iot_devices[identifier]
    case.addCleanup(main.iot_devices.update, others)
    driver = next(driver for driver in main.drivers if driver.connection_type == 'display')
    main.iot_devices[SCREEN] = driver(SCREEN, {'identifier': SCREEN, 'name': 'Box suite screen', 'x_screen': '0'})
    case.addCleanup(main.iot_devices.pop, SCREEN, None)
    del starts[:]
    return starts


def kept_address():
    """The address the box keeps for its screen, which the screen opens when the box starts; None when it keeps none."""
    return common.box_configuration().get(KEPT_ADDRESS)


class TestSettingsCode(common.BoxCase):

    def test_settings_code_the_server_shows_for_the_credential_unlocks_the_box(self):
        self.box_with_server(VECTOR_CREDENTIAL)

        answer, proof = self.unlock(VECTOR_SETTINGS_CODE)

        self.assertEqual(answer.get('status'), 'unlocked', answer)
        self.assertEqual(self.request(RESTART, proof=proof).status_code, 200)

    def test_wrong_settings_code_gives_no_proof(self):
        self.box_with_server(VECTOR_CREDENTIAL)

        answer, proof = self.unlock('R4HF8KC2')

        self.assertEqual((answer.get('status'), answer.get('reason')), ('refused', 'invalid_code'), answer)
        self.assertIsNone(proof)

    def test_settings_code_of_another_box_gives_no_proof(self):
        self.box_with_server(VECTOR_CREDENTIAL)

        answer, proof = self.unlock(OTHER_SETTINGS_CODE)

        self.assertEqual((answer.get('status'), answer.get('reason')), ('refused', 'invalid_code'), answer)
        self.assertIsNone(proof)

    def test_settings_code_is_read_the_way_a_person_types_it(self):
        self.box_with_server(OTHER_CREDENTIAL)
        for typed in ('s10me1y4', 'S10M-E1Y4', ' S10M E1Y4 ', 'SIOMELY4', 'sio-mely4'):
            with self.subTest(typed=typed):
                answer, proof = self.unlock(typed)

                self.assertEqual(answer.get('status'), 'unlocked', answer)
                self.assertEqual(self.request(RESTART, proof=proof).status_code, 200)

    def test_box_without_a_server_gives_no_proof(self):
        answer, proof = self.unlock(VECTOR_SETTINGS_CODE)

        self.assertEqual(answer.get('status'), 'not_locked', answer)
        self.assertIsNone(proof)


class LockedBoxCase(common.BoxCase):

    def setUp(self):
        super().setUp()
        self.server_of_the_box = self.box_with_server(VECTOR_CREDENTIAL)
        _answer, self.proof = self.unlock(VECTOR_SETTINGS_CODE)
        self.kept = common.kept()

    def assertRefused(self, response):
        """`response` is the answer of a route of core that takes no JSON: the box refuses it as locked."""
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json().get('reason'), LOCKED)


class TestSettingsWithoutTheUnlockProof(LockedBoxCase):

    def test_pairing_again_is_refused(self):
        other_server = self.server(answer=PAIRED_ELSEWHERE)

        answer = self.pair(link=other_server.link(CODE))

        self.assertEqual(answer.get('reason'), LOCKED, answer)
        self.assertEqual(other_server.requests, [])
        self.assertEqual(common.kept(), self.kept)

    def test_certificate_of_the_customer_is_not_installed(self):
        self.keep_web_certificate()
        installed = common.fingerprint(common.web_certificate())
        customer = common.make_certificate(CUSTOMER_HOST)

        answer = self.call(CERTIFICATE, certificate=customer.pem, key=customer.key_pem, host_name=CUSTOMER_HOST)

        self.assertEqual(answer.get('reason'), LOCKED, answer)
        self.assertEqual(common.fingerprint(common.web_certificate()), installed)
        self.assertEqual(common.kept(), self.kept)

    def test_box_is_neither_connected_to_another_server_nor_renamed(self):
        with common.without_running('rename_iot.sh') as renames:
            answer = self.call(CONNECT, token=CORE_CONNECTION, iotname='renamed-box')

        self.assertEqual(answer.get('reason'), LOCKED, answer)
        self.assertEqual(renames, [])
        self.assertEqual(common.kept(), self.kept)
        self.assertEqual(common.restarts, [])

    def test_box_is_not_disconnected(self):
        response = self.request(DISCONNECT)

        self.assertRefused(response)
        self.assertEqual(common.kept(), self.kept)
        self.assertEqual(common.restarts, [])

    def test_wifi_is_not_set(self):
        network = wifi_common.network_manager(self)
        network.change(networks={'Workshop': 'not-a-real-password'})

        with wifi_common.settled():
            answer = self.call(WIFI, essid='Workshop', password='not-a-real-password')

        self.assertEqual(answer, LOCKED_ANSWER)
        self.assertEqual(network.nmcli_calls(), [])

    def test_wifi_is_not_cleared(self):
        common.store_box_configuration(wifi_ssid='Workshop')

        response = self.request(WIFI_CLEAR)

        self.assertRefused(response)
        self.assertEqual(common.box_configuration().get('wifi_ssid'), 'Workshop')

    def test_log_level_is_not_changed(self):
        level = logging.getLogger('odoo').level
        self.addCleanup(logging.getLogger('odoo').setLevel, level)
        self.addCleanup(logging.getLogger('werkzeug').setLevel, logging.getLogger('werkzeug').level)

        answer = self.call(LOG_LEVEL, name='iot-logging-odoo', value='debug')

        self.assertEqual(answer.get('reason'), LOCKED, answer)
        self.assertEqual(logging.getLogger('odoo').level, level)

    def test_box_service_is_not_restarted(self):
        response = self.request(RESTART)

        self.assertRefused(response)
        self.assertEqual(common.restarts, [])

    def test_shell_password_is_not_changed(self):
        password = shell_password()

        response = self.request(SHELL_PASSWORD)

        self.assertRefused(response)
        self.assertEqual(shell_password(), password)


class TestSettingsWithTheUnlockProof(LockedBoxCase):

    def test_box_is_paired_again(self):
        other_server = self.server(answer=PAIRED_ELSEWHERE)

        answer = self.pair(proof=self.proof, link=other_server.link(CODE))

        self.assertEqual(answer.get('status'), 'paired', answer)
        self.assertEqual(common.box_configuration().get('remote_server'), other_server.url)

    def test_certificate_of_the_customer_is_installed(self):
        self.keep_web_certificate()
        customer = common.make_certificate(CUSTOMER_HOST)

        answer = self.call(
            CERTIFICATE, proof=self.proof, certificate=customer.pem, key=customer.key_pem, host_name=CUSTOMER_HOST)

        self.assertEqual(answer.get('status'), 'installed', answer)
        self.assertEqual(common.fingerprint(common.web_certificate()), customer.fingerprint)

    def test_connect_request_carrying_a_new_box_name_connects_to_the_server_and_changes_nothing_of_the_boxs_name(self):
        with common.without_running('rename_iot.sh') as renames:
            self.call(CONNECT, proof=self.proof, token=CORE_CONNECTION, iotname='renamed-box')

        self.assertEqual(renames, [])
        stored = common.box_configuration()
        self.assertEqual(stored.get('remote_server'), 'http://other-server.example')
        self.assertEqual(stored.get('token'), 'other-credential')

    def test_box_is_disconnected(self):
        response = self.request(DISCONNECT, proof=self.proof)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('token', common.box_configuration())

    def test_wifi_is_set(self):
        network = wifi_common.network_manager(self)
        network.change(networks={'Workshop': 'not-a-real-password'})

        with wifi_common.settled():
            answer = self.call(WIFI, proof=self.proof, essid='Workshop', password='not-a-real-password')

        self.assertEqual(answer, joining('Workshop'))
        self.assertEqual(network.joined(), [('Workshop', 'not-a-real-password')])

    def test_wifi_is_cleared(self):
        common.store_box_configuration(wifi_ssid='Workshop')

        response = self.request(WIFI_CLEAR, proof=self.proof)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('wifi_ssid', common.box_configuration())

    def test_log_level_is_changed(self):
        self.addCleanup(logging.getLogger('odoo').setLevel, logging.getLogger('odoo').level)
        self.addCleanup(logging.getLogger('werkzeug').setLevel, logging.getLogger('werkzeug').level)

        self.call(LOG_LEVEL, proof=self.proof, name='iot-logging-odoo', value='debug')

        self.assertEqual(logging.getLogger('odoo').level, logging.DEBUG)

    def test_box_service_is_restarted(self):
        response = self.request(RESTART, proof=self.proof)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(common.restarts), 1)

    def test_shell_password_is_changed(self):
        password = shell_password()

        response = self.request(SHELL_PASSWORD, proof=self.proof)

        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(shell_password(), password)


class TestJoiningAWifiOnAPairedBox(LockedBoxCase):

    def setUp(self):
        super().setUp()
        self.network = wifi_common.network_manager(self)

    def join(self, **parameters):
        """(answer of the route, the joins NetworkManager was asked for) for a box named `suite-box`."""
        joins = len(self.network.joined())
        with common.named('suite-box'), wifi_common.settled():
            answer = self.call(WIFI, proof=self.proof, **parameters)
        return answer, self.network.joined()[joins:]

    def test_answer_tells_how_to_open_the_box_and_never_names_the_server(self):
        answer, _joined = self.join(essid='Workshop', password='not-a-real-password')

        self.assertEqual(answer, joining('Workshop', 'suite-box'))
        self.assertNotIn(self.server_of_the_box.url, str(answer))

    def test_network_without_a_password_is_joined_as_an_open_network(self):
        self.network.change(networks={'Workshop': None})
        cases = {'password omitted': {}, 'password none': {'password': None}, 'password empty': {'password': ''}}
        for case, parameters in cases.items():
            with self.subTest(case):
                _answer, joined = self.join(essid='Workshop', **parameters)

                self.assertEqual(joined, [('Workshop', None)])

    def test_password_given_reaches_network_manager_unchanged(self):
        _answer, joined = self.join(essid='Workshop', password=' not a real password  ')

        self.assertEqual(joined, [('Workshop', ' not a real password  ')])


class TestJoiningAWifiOnABoxWithoutAServer(common.BoxCase):

    def test_box_without_a_server_and_without_a_route_out_answers_how_to_open_it_once_it_joins(self):
        network = wifi_common.network_manager(self)

        with common.named('Suite-Box'), without_a_route_out(), wifi_common.settled():
            answer = self.call(WIFI, essid='Workshop', password='not-a-real-password')

        self.assertEqual(answer, joining('Workshop', 'Suite-Box'))
        self.assertEqual(network.joined(), [('Workshop', 'not-a-real-password')])


class TestUnlockProof(common.BoxCase):

    def setUp(self):
        super().setUp()
        self.box_with_server(VECTOR_CREDENTIAL)

    def unlocks(self, proof):
        """Whether a request that carries `proof` may change a setting; asked of the restart of the box service."""
        return self.request(RESTART, proof=proof).status_code == 200

    def test_proof_made_for_another_box_never_unlocks(self):
        proof = common.layer('lock').issue_proof(OTHER_CREDENTIAL)

        self.assertFalse(self.unlocks(proof))

    def test_proof_changed_in_any_one_place_never_unlocks(self):
        _answer, proof = self.unlock(VECTOR_SETTINGS_CODE)
        for place, character in enumerate(proof):
            changed = proof[:place] + ('0' if character != '0' else '1') + proof[place + 1:]
            with self.subTest(changed=changed):
                self.assertFalse(self.unlocks(changed))

    def test_proof_unlocks_until_15_minutes_have_passed(self):
        clock = SetClock(LATE_IN_ITS_SECOND)
        with mock.patch.object(common.layer('lock'), 'time', clock):
            _answer, proof = self.unlock(VECTOR_SETTINGS_CODE)
            clock.now += 14 * 60 + 59

            self.assertTrue(self.unlocks(proof))

    def test_proof_no_longer_unlocks_once_15_minutes_have_passed(self):
        clock = SetClock(EARLY_IN_ITS_SECOND)
        with mock.patch.object(common.layer('lock'), 'time', clock):
            _answer, proof = self.unlock(VECTOR_SETTINGS_CODE)
            clock.now += 15 * 60

            self.assertFalse(self.unlocks(proof))


class DeviceCase(LockedBoxCase):

    def setUp(self):
        super().setUp()
        self.device = plug_device(self)

    def ask_device(self, proof=None, **action):
        """What the box answers a page that asks the device for `action`, the way the pages of a server do."""
        return self.call(ACTION, proof=proof, session_id=SESSION, device_identifier=DEVICE, data=json.dumps(action))


class TestDeviceChoicesTheBoxKeepsWithoutTheUnlockProof(DeviceCase):

    def test_keyboard_is_not_made_a_scanner(self):
        answer = self.ask_device(action='update_is_scanner', is_scanner=True)

        self.assertEqual(answer, {'status': 'locked', 'reason': LOCKED})
        self.assertEqual(self.device.asked, [])

    def test_keyboard_layout_is_not_changed(self):
        answer = self.ask_device(action='update_layout', layout='us', variant='')

        self.assertEqual(answer, {'status': 'locked', 'reason': LOCKED})
        self.assertEqual(self.device.asked, [])


class TestDeviceChoicesTheBoxKeepsWithTheUnlockProof(DeviceCase):

    def test_keyboard_is_made_a_scanner(self):
        answer = self.ask_device(proof=self.proof, action='update_is_scanner', is_scanner=True)

        self.assertIs(answer, True)
        self.assertEqual(self.device.asked, ['update_is_scanner'])

    def test_keyboard_layout_is_changed(self):
        answer = self.ask_device(proof=self.proof, action='update_layout', layout='us', variant='')

        self.assertIs(answer, True)
        self.assertEqual(self.device.asked, ['update_layout'])


class TestDeviceUseWithoutTheUnlockProof(DeviceCase):

    def test_weight_is_read(self):
        answer = self.ask_device(action='read_once')

        self.assertIs(answer, True)
        self.assertEqual(self.device.asked, ['read_once'])

    def test_receipt_is_printed(self):
        answer = self.ask_device(action='print_receipt', receipt='')

        self.assertIs(answer, True)
        self.assertEqual(self.device.asked, ['print_receipt'])

    def test_scan_reaches_the_page_that_listens_for_it(self):
        self.device.data = {'value': SCAN}
        common.core('event_manager').event_manager.device_changed(self.device)

        answer = self.call(EVENT, listener={'session_id': SESSION, 'devices': [DEVICE], 'last_event': 0})

        self.assertEqual((answer.get('device_identifier'), answer.get('value')), (DEVICE, SCAN))


class Screen:
    """What a box case gains with a screen; a test class names it before its box case.

    `with_proof` says whether the pages that ask the screen send the unlock proof of the case.
    """
    with_proof = False

    def setUp(self):
        super().setUp()
        self.started = plug_screen(self)

    def ask_screen(self, *address, proof=None):
        """What the box answers a page that asks the screen to show an address or, given none, its page again."""
        action = {'action': 'update_url'}
        if address:
            action['url'] = address[0]
        proof = proof or (self.proof if self.with_proof else None)
        self.before_the_ask = (self.shown(), kept_address())
        return self.call(ACTION, proof=proof, session_id=SESSION, device_identifier=SCREEN, data=json.dumps(action))

    def shown(self):
        """The addresses the browser of the screen was started with since the screen was plugged, in order."""
        return [arguments[0] for arguments in self.started]

    def assertNotShown(self, answer):
        """The box refused, by `answer`, what the screen was last asked: no browser started, the kept address stays."""
        self.assertIsInstance(answer, dict, answer)
        self.assertEqual((self.shown(), kept_address()), self.before_the_ask)

    def assertNoneShown(self, *addresses):
        for address in addresses:
            with self.subTest(address=address):
                self.assertNotShown(self.ask_screen(address))


class PagesTheScreenShowsWhoeverAsks:

    def test_screen_shows_a_page_of_the_box_itself(self):
        for page in (PAGE_ON_THE_BOX, f'https://{socket.gethostname()}/point_of_sale/display/{SCREEN}'):
            with self.subTest(page=page):
                answer = self.ask_screen(page)

                self.assertIs(answer, True)
                self.assertEqual(self.shown()[-1:], [page])
                self.assertEqual(kept_address(), page)

    def test_screen_asked_without_an_address_shows_again_the_page_the_box_keeps(self):
        self.ask_screen(PAGE_ON_THE_BOX)

        answer = self.ask_screen()

        self.assertIs(answer, True)
        self.assertEqual(self.shown(), [PAGE_ON_THE_BOX, PAGE_ON_THE_BOX])


class AddressesTheScreenShowsForNoRequestWithoutTheUnlockProof:

    def test_address_with_a_user_part_before_its_host_is_not_shown(self):
        self.assertNoneShown(
            'http://localhost:8069@another-site.example/',
            'http://another-site.example@localhost:8069/',
            'http://shop@localhost:8069/point_of_sale/display',
        )

    def test_address_with_a_backslash_is_not_shown(self):
        self.assertNoneShown(
            'http://another-site.example\\@localhost:8069/',
            'http://localhost:8069\\@another-site.example/',
            'http:\\\\localhost:8069\\point_of_sale\\display',
        )

    def test_address_with_a_space_or_a_control_character_is_not_shown(self):
        self.assertNoneShown(
            ' http://localhost:8069/point_of_sale/display',
            'http://localhost:8069/point_of_sale/display ',
            'http://localhost:8069/point of sale/display',
            'http://local\thost:8069/point_of_sale/display',
            'http://localhost:8069/point_of_sale/display\n',
            'http://localhost:8069/point_of_sale/display\x00',
        )

    def test_address_whose_host_is_a_number_a_browser_reads_as_an_address_is_not_shown(self):
        self.assertNoneShown(
            'http://2130706433:8069/point_of_sale/display',
            'http://017700000001:8069/point_of_sale/display',
            'http://0x7f.0.0.1:8069/point_of_sale/display',
            'http://127.1:8069/point_of_sale/display',
        )

    def test_name_of_the_box_that_a_browser_reads_as_an_address_does_not_pass_for_the_box(self):
        with common.named('3232235777'):
            answer = self.ask_screen('http://3232235777:8069/point_of_sale/display')

        self.assertNotShown(answer)

    def test_address_of_another_scheme_than_the_two_of_the_web_is_not_shown(self):
        self.assertNoneShown(
            'ftp://localhost/point_of_sale/display',
            'file:///etc/hostname',
            'javascript:alert(1)',
            'data:text/html,page',
        )

    def test_what_is_no_address_is_not_shown(self):
        self.assertNoneShown(
            '--proxy-server=another-site.example',
            'localhost:8069',
            '//localhost:8069/point_of_sale/display',
            8069,
            [PAGE_ON_THE_BOX],
            {'page': PAGE_ON_THE_BOX},
        )

    def test_address_with_a_percent_sign_is_not_shown(self):
        self.assertNoneShown(
            f'{PAGE_ON_THE_BOX}?shop=%(token)s',
            f'{PAGE_ON_THE_BOX}%20',
        )

    def test_address_of_a_setting_of_the_box_is_not_shown(self):
        self.assertNoneShown(
            f'http://localhost:8069{WIFI_CLEAR}',
            f'http://localhost:8069{DISCONNECT}',
            f'http://localhost:8069{RESTART}',
            f'http://localhost:8069{SHELL_PASSWORD}',
        )

    def test_address_of_a_setting_is_not_shown_in_any_spelling_a_browser_or_the_box_folds_to_it(self):
        self.assertNoneShown(
            f'http://localhost:8069{WIFI_CLEAR}/',
            'http://localhost:8069//hw_posbox_homepage/wifi_clear',
            'http://localhost:8069/hw_posbox_homepage//wifi_clear',
            'http://localhost:8069/point_of_sale/../hw_posbox_homepage/wifi_clear',
            'http://localhost:8069/./hw_posbox_homepage/wifi_clear',
            'http://localhost:8069/hw_posbox_homepage/./wifi_clear',
            f'http://localhost:8069{WIFI_CLEAR}?again=1',
            f'http://localhost:8069{WIFI_CLEAR}#top',
            f'http://LOCALHOST:8069{WIFI_CLEAR}',
            f'http://127.0.0.1:8069{WIFI_CLEAR}',
            f'https://localhost{WIFI_CLEAR}',
            f'http://{socket.gethostname()}{WIFI_CLEAR}',
        )


class TestScreenOfALockedBoxWithoutTheUnlockProof(
        PagesTheScreenShowsWhoeverAsks, AddressesTheScreenShowsForNoRequestWithoutTheUnlockProof, Screen,
        LockedBoxCase):

    def test_screen_shows_a_page_of_the_server_of_the_box(self):
        page = f'{self.server_of_the_box.url}/pos_customer_display/3/till-access-key'

        answer = self.ask_screen(page)

        self.assertIs(answer, True)
        self.assertEqual(self.shown(), [page])
        self.assertEqual(kept_address(), page)

    def test_page_of_another_web_site_is_refused_as_any_setting_is(self):
        answer = self.ask_screen(f'{common.ANOTHER_WEB_SITE}/welcome')

        self.assertEqual(answer, {'status': 'locked', 'reason': LOCKED})
        self.assertNotShown(answer)

    def test_address_a_browser_does_not_read_as_a_page_of_the_server_of_the_box_is_not_shown(self):
        server = self.server_of_the_box

        self.assertNoneShown(
            f'{server.url}@another-site.example/',
            f'http://another-site.example\\@{server.host}:{server.port}/',
            f'{server.url}/pos_customer_display/3/%(token)s',
        )


class TestScreenOfALockedBoxWithTheUnlockProof(PagesTheScreenShowsWhoeverAsks, Screen, LockedBoxCase):
    with_proof = True

    def test_screen_shows_a_page_of_another_web_site(self):
        page = f'{common.ANOTHER_WEB_SITE}/welcome'

        answer = self.ask_screen(page)

        self.assertIs(answer, True)
        self.assertEqual(self.shown(), [page])
        self.assertEqual(kept_address(), page)


class TestScreenOfABoxWithoutAServer(
        PagesTheScreenShowsWhoeverAsks, AddressesTheScreenShowsForNoRequestWithoutTheUnlockProof, Screen,
        common.BoxCase):

    def leave_a_server(self):
        """Give the box a server, then disconnect it: (that server, the unlock proof the box gave meanwhile)."""
        server = self.box_with_server(VECTOR_CREDENTIAL)
        _answer, proof = self.unlock(VECTOR_SETTINGS_CODE)
        self.request(DISCONNECT, proof=proof)
        common.forget_cached_configuration()
        return server, proof

    def test_page_of_another_web_site_is_not_shown(self):
        answer = self.ask_screen(f'{common.ANOTHER_WEB_SITE}/welcome')

        self.assertNotShown(answer)

    def test_page_of_the_server_the_box_left_is_not_shown(self):
        server, _proof = self.leave_a_server()

        answer = self.ask_screen(f'{server.url}/pos_customer_display/3/till-access-key')

        self.assertNotShown(answer)

    def test_unlock_proof_of_the_server_the_box_left_shows_no_page_of_another_web_site(self):
        _server, proof = self.leave_a_server()

        answer = self.ask_screen(f'{common.ANOTHER_WEB_SITE}/welcome', proof=proof)

        self.assertNotShown(answer)

    def test_page_of_a_server_whose_address_the_box_keeps_without_a_credential_is_not_shown(self):
        # The page of core connects a box this way; given no credential, the box has no server.
        self.call(CONNECT, token='http://other-server.example?token=&db_uuid=&enterprise_code=')
        common.forget_cached_configuration()
        self.assertEqual(common.box_configuration().get('remote_server'), 'http://other-server.example')

        answer = self.ask_screen('http://other-server.example/pos_customer_display/3/till-access-key')

        self.assertNotShown(answer)


class TestBoxWithoutAServer(common.BoxCase):

    def test_settings_are_changed_without_a_proof(self):
        common.store_box_configuration(wifi_ssid='Workshop')

        cleared = self.request(WIFI_CLEAR)
        restarted = self.request(RESTART)

        self.assertEqual((cleared.status_code, restarted.status_code), (200, 200))
        self.assertNotIn('wifi_ssid', common.box_configuration())
        self.assertEqual(len(common.restarts), 1)

    def test_device_choices_the_box_keeps_are_made_without_a_proof(self):
        device = plug_device(self)

        answer = self.call(
            ACTION, session_id=SESSION, device_identifier=DEVICE,
            data=json.dumps({'action': 'update_is_scanner', 'is_scanner': True}))

        self.assertIs(answer, True)
        self.assertEqual(device.asked, ['update_is_scanner'])


class Settings:
    """What a box case gains to ask each setting of the box as the browsers of its site do; a test class names it
    before its box case.

    A box that has a server is asked with its unlock proof, so that its lock refuses nothing.
    """
    proof = None

    def setUp(self):
        super().setUp()
        self.before = common.save_state()
        self.kept = common.kept()

    def get(self, path, sent):
        return self.request(path, proof=self.proof, sent=sent, box=BOX)

    def post(self, path, sent, **parameters):
        """(the result of a JSON route, its response)."""
        response = self.request(path, parameters, proof=self.proof, sent=sent, box=BOX)
        return response.get_json().get('result'), response

    def as_before(self):
        """Give the box back what it kept about its server when the test began."""
        common.restore_state(self.before)
        del common.restarts[:]


class SettingsAnotherWebSiteAsksFor(Settings):

    def test_box_is_not_paired(self):
        for sent in SENT_FOR_ANOTHER_WEB_SITE:
            with self.subTest(sent=sent):
                other_server = self.server(answer=PAIRED_ELSEWHERE)

                answer, _response = self.post(PAIR, sent, link=other_server.link(CODE))

                self.assertNotEqual(answer.get('status'), 'paired', answer)
                self.assertEqual(other_server.requests, [])
                self.assertEqual(common.kept(), self.kept)
            self.as_before()

    def test_no_unlock_proof_is_given(self):
        for sent in SENT_FOR_ANOTHER_WEB_SITE:
            with self.subTest(sent=sent):
                answer, response = self.post(UNLOCK, sent, code=VECTOR_SETTINGS_CODE)

                self.assertNotIn(answer.get('status'), ('unlocked', 'not_locked'), answer)
                self.assertNotIn(common.layer('lock').PROOF_HEADER, response.headers)

    def test_certificate_of_the_customer_is_not_installed(self):
        self.keep_web_certificate()
        customer = common.make_certificate(CUSTOMER_HOST)
        for sent in SENT_FOR_ANOTHER_WEB_SITE:
            with self.subTest(sent=sent):
                installed = common.fingerprint(common.web_certificate())

                answer, _response = self.post(
                    CERTIFICATE, sent, certificate=customer.pem, key=customer.key_pem, host_name=CUSTOMER_HOST)

                self.assertNotEqual(answer.get('status'), 'installed', answer)
                self.assertEqual(common.fingerprint(common.web_certificate()), installed)

    def test_box_is_neither_connected_to_a_server_nor_renamed(self):
        for sent in SENT_FOR_ANOTHER_WEB_SITE:
            with self.subTest(sent=sent):
                with common.without_running('rename_iot.sh') as renames:
                    self.post(CONNECT, sent, token=CORE_CONNECTION, iotname='renamed-box')

                self.assertEqual(renames, [])
                self.assertEqual(common.kept(), self.kept)
                self.assertEqual(common.restarts, [])
            self.as_before()

    def test_box_is_not_disconnected(self):
        for sent in SENT_FOR_ANOTHER_WEB_SITE:
            with self.subTest(sent=sent):
                response = self.get(DISCONNECT, sent)

                self.assertEqual(response.status_code, 403)
                self.assertEqual(common.kept(), self.kept)
                self.assertEqual(common.restarts, [])
            self.as_before()

    def test_wifi_is_not_set(self):
        network = wifi_common.network_manager(self)
        network.change(networks={'Workshop': 'not-a-real-password'})
        for sent in SENT_FOR_ANOTHER_WEB_SITE:
            with self.subTest(sent=sent):
                with wifi_common.settled():
                    answer, _response = self.post(WIFI, sent, essid='Workshop', password='not-a-real-password')

                self.assertEqual(answer, OTHER_SITE_ANSWER)
                self.assertEqual(network.nmcli_calls(), [])

    def test_wifi_is_not_cleared(self):
        for sent in SENT_FOR_ANOTHER_WEB_SITE:
            with self.subTest(sent=sent):
                common.store_box_configuration(wifi_ssid='Workshop')

                response = self.get(WIFI_CLEAR, sent)

                self.assertEqual(response.status_code, 403)
                self.assertEqual(common.box_configuration().get('wifi_ssid'), 'Workshop')

    def test_log_level_is_not_changed(self):
        logger = logging.getLogger('odoo')
        self.addCleanup(logger.setLevel, logger.level)
        self.addCleanup(logging.getLogger('werkzeug').setLevel, logging.getLogger('werkzeug').level)
        for sent in SENT_FOR_ANOTHER_WEB_SITE:
            with self.subTest(sent=sent):
                logger.setLevel(logging.INFO)

                self.post(LOG_LEVEL, sent, name='iot-logging-odoo', value='debug')

                self.assertEqual(logger.level, logging.INFO)
            self.as_before()

    def test_box_service_is_not_restarted(self):
        for sent in SENT_FOR_ANOTHER_WEB_SITE:
            with self.subTest(sent=sent):
                del common.restarts[:]

                response = self.get(RESTART, sent)

                self.assertEqual(response.status_code, 403)
                self.assertEqual(common.restarts, [])

    def test_shell_password_is_not_changed(self):
        for sent in SENT_FOR_ANOTHER_WEB_SITE:
            with self.subTest(sent=sent):
                password = shell_password()

                response = self.get(SHELL_PASSWORD, sent)

                self.assertEqual(response.status_code, 403)
                self.assertEqual(shell_password(), password)


class TestSettingsAnotherWebSiteAsksOfABoxWithoutAServer(SettingsAnotherWebSiteAsksFor, common.BoxCase):
    """A box without a server has no lock: only where a request comes from can keep its settings."""


class TestSettingsAnotherWebSiteAsksWithTheUnlockProof(SettingsAnotherWebSiteAsksFor, LockedBoxCase):
    """A box that has a server, asked with its unlock proof: the proof opens nothing to another web site."""


class SettingsNoOtherWebSiteAsksFor(Settings):

    def test_box_is_paired(self):
        other_server = self.server(answer=PAIRED_ELSEWHERE)
        for sent in SENT_FOR_NO_OTHER_WEB_SITE:
            with self.subTest(sent=sent):
                answer, _response = self.post(PAIR, sent, link=other_server.link(CODE))

                self.assertEqual(answer.get('status'), 'paired', answer)
                self.assertEqual(common.box_configuration().get('remote_server'), other_server.url)
            self.as_before()

    def test_certificate_of_the_customer_is_installed(self):
        self.keep_web_certificate()
        for sent in SENT_FOR_NO_OTHER_WEB_SITE:
            with self.subTest(sent=sent):
                customer = common.make_certificate(CUSTOMER_HOST)

                answer, _response = self.post(
                    CERTIFICATE, sent, certificate=customer.pem, key=customer.key_pem, host_name=CUSTOMER_HOST)

                self.assertEqual(answer.get('status'), 'installed', answer)
                self.assertEqual(common.fingerprint(common.web_certificate()), customer.fingerprint)

    def test_connect_request_carrying_a_new_box_name_connects_to_the_server_and_changes_nothing_of_the_boxs_name(self):
        for sent in SENT_FOR_NO_OTHER_WEB_SITE:
            with self.subTest(sent=sent):
                with common.without_running('rename_iot.sh') as renames:
                    self.post(CONNECT, sent, token=CORE_CONNECTION, iotname='renamed-box')

                self.assertEqual(renames, [])
                self.assertEqual(common.box_configuration().get('remote_server'), 'http://other-server.example')
            self.as_before()

    def test_box_is_left_without_a_server(self):
        for sent in SENT_FOR_NO_OTHER_WEB_SITE:
            with self.subTest(sent=sent):
                response = self.get(DISCONNECT, sent)

                self.assertEqual(response.status_code, 200)
                self.assertNotIn('token', common.box_configuration())
            self.as_before()

    def test_wifi_is_set(self):
        network = wifi_common.network_manager(self)
        for sent in SENT_FOR_NO_OTHER_WEB_SITE:
            with self.subTest(sent=sent):
                joins = len(network.joined())
                with wifi_common.settled():
                    answer, _response = self.post(WIFI, sent, essid='Workshop', password='not-a-real-password')

                self.assertEqual(answer, joining('Workshop'))
                self.assertEqual(network.joined()[joins:], [('Workshop', 'not-a-real-password')])

    def test_wifi_is_cleared(self):
        for sent in SENT_FOR_NO_OTHER_WEB_SITE:
            with self.subTest(sent=sent):
                common.store_box_configuration(wifi_ssid='Workshop')

                response = self.get(WIFI_CLEAR, sent)

                self.assertEqual(response.status_code, 200)
                self.assertNotIn('wifi_ssid', common.box_configuration())

    def test_log_level_is_changed(self):
        logger = logging.getLogger('odoo')
        self.addCleanup(logger.setLevel, logger.level)
        self.addCleanup(logging.getLogger('werkzeug').setLevel, logging.getLogger('werkzeug').level)
        for sent in SENT_FOR_NO_OTHER_WEB_SITE:
            with self.subTest(sent=sent):
                logger.setLevel(logging.INFO)

                self.post(LOG_LEVEL, sent, name='iot-logging-odoo', value='debug')

                self.assertEqual(logger.level, logging.DEBUG)
            # Core leaves a level alone when the configuration of the box already names it.
            self.as_before()

    def test_box_service_is_restarted(self):
        for sent in SENT_FOR_NO_OTHER_WEB_SITE:
            with self.subTest(sent=sent):
                del common.restarts[:]

                response = self.get(RESTART, sent)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(common.restarts), 1)

    def test_shell_password_is_changed(self):
        for sent in SENT_FOR_NO_OTHER_WEB_SITE:
            with self.subTest(sent=sent):
                password = shell_password()

                response = self.get(SHELL_PASSWORD, sent)

                self.assertEqual(response.status_code, 200)
                self.assertNotEqual(shell_password(), password)


class TestSettingsNoOtherWebSiteAsksOfABoxWithoutAServer(SettingsNoOtherWebSiteAsksFor, common.BoxCase):

    def test_box_answers_that_it_is_not_locked(self):
        for sent in SENT_FOR_NO_OTHER_WEB_SITE:
            with self.subTest(sent=sent):
                answer, _response = self.post(UNLOCK, sent, code=VECTOR_SETTINGS_CODE)

                self.assertEqual(answer.get('status'), 'not_locked', answer)

    def test_page_that_writes_the_name_of_the_box_in_capitals_changes_a_setting(self):
        name = socket.gethostname()

        response = self.request(RESTART, sent={'Origin': f'http://{name.upper()}:8069'}, box=f'http://{name}:8069')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(common.restarts), 1)


class TestSettingsAskedByAPageOpenedByTheViindooNameOfTheBox(common.BoxCase):
    """The name Viindoo issued for the box is one by which a browser reaches the box: its own pages carry it."""

    def ask_restart(self, name):
        """Ask for a restart from the page of the box that a browser opened by `name`."""
        page = f'https://{name}'
        del common.restarts[:]
        sent = {'Sec-Fetch-Site': 'same-origin', 'Origin': page, 'Referer': f'{page}/'}
        return self.request(RESTART, sent=sent, box=BOX)

    def test_page_of_the_box_opened_by_its_viindoo_name_changes_a_setting(self):
        common.store_box_configuration(viin_managed_name=VIINDOO_NAME)

        response = self.ask_restart(VIINDOO_NAME)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(common.restarts), 1)

    def test_page_opened_by_the_viindoo_name_of_another_box_is_another_web_site(self):
        common.store_box_configuration(viin_managed_name=VIINDOO_NAME)

        response = self.ask_restart('box-other.iot.example')

        self.assertEqual(response.status_code, 403)
        self.assertEqual(common.restarts, [])

    def test_page_opened_by_a_viindoo_name_the_box_does_not_hold_is_another_web_site(self):
        response = self.ask_restart(VIINDOO_NAME)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(common.restarts, [])


class TestSettingsNoOtherWebSiteAsksWithTheUnlockProof(SettingsNoOtherWebSiteAsksFor, LockedBoxCase):

    def test_unlock_proof_is_given(self):
        for sent in SENT_FOR_NO_OTHER_WEB_SITE:
            with self.subTest(sent=sent):
                answer, response = self.post(UNLOCK, sent, code=VECTOR_SETTINGS_CODE)

                self.assertEqual(answer.get('status'), 'unlocked', answer)
                self.assertIn(common.layer('lock').PROOF_HEADER, response.headers)


class TestSettingsThePageOfALockedBoxAsksWithoutTheUnlockProof(LockedBoxCase):

    def test_lock_refuses_the_page_of_the_box_every_setting(self):
        for address in (DISCONNECT, WIFI_CLEAR, RESTART, SHELL_PASSWORD):
            with self.subTest(address=address):
                response = self.request(address, sent=SENT_BY_THE_PAGE_OF_THE_BOX, box=BOX)

                self.assertRefused(response)
        for address in (PAIR, CERTIFICATE, CONNECT, WIFI, LOG_LEVEL):
            with self.subTest(address=address):
                answer = self.request(address, {}, sent=SENT_BY_THE_PAGE_OF_THE_BOX, box=BOX).get_json()

                self.assertEqual((answer.get('result') or {}).get('reason'), LOCKED, answer)
        self.assertEqual(common.kept(), self.kept)
        self.assertEqual(common.restarts, [])


class TestSettingsAskedThroughTheWebServerOfTheBox(common.BoxCase):
    """The web server of the box hands a request on under an address of its own, not the one the browser called."""

    def set_wifi(self, sent):
        """Ask the box, through its web server, to join a Wi-Fi network: (its answer, the NetworkManager it asked)."""
        common.serve()
        network = wifi_common.network_manager(self)
        network.change(networks={'Workshop': 'not-a-real-password'})
        with wifi_common.settled():
            answer = common.through_web_server(WIFI, sent=sent, essid='Workshop', password='not-a-real-password')
        return answer, network

    def test_page_of_the_box_called_by_the_name_of_the_box_sets_the_wifi(self):
        page = f'https://{socket.gethostname()}'

        answer, network = self.set_wifi({
            'Sec-Fetch-Site': 'same-origin', 'Origin': page, 'Referer': f'{page}/'})

        self.assertEqual(answer, joining('Workshop'), common.why_no_answer())
        self.assertEqual(network.joined(), [('Workshop', 'not-a-real-password')])

    def test_page_of_another_web_site_sets_no_wifi(self):
        answer, network = self.set_wifi({
            'Sec-Fetch-Site': 'cross-site', 'Origin': common.ANOTHER_WEB_SITE, 'Referer': PAGE_OF_ANOTHER_WEB_SITE})

        self.assertEqual(answer, OTHER_SITE_ANSWER, common.why_no_answer())
        self.assertEqual(network.nmcli_calls(), [])


class TestClosedRoutes(common.BoxCase):

    def assertNotFound(self, proof=None):
        for address in CLOSED:
            with self.subTest(address=address):
                self.assertEqual(self.request(address, proof=proof).status_code, 404)
                self.assertEqual(self.request(address, {}, proof=proof).status_code, 404)

    def test_closed_routes_of_core_answer_not_found_on_a_box_without_a_server(self):
        self.assertNotFound()

    def test_closed_routes_of_core_answer_not_found_on_a_box_that_has_a_server(self):
        self.box_with_server(VECTOR_CREDENTIAL)
        _answer, proof = self.unlock(VECTOR_SETTINGS_CODE)

        self.assertNotFound(proof)


def from_another_web_site(**headers):
    """The headers of a request a browser sends for a page of another web site."""
    return {'Origin': common.ANOTHER_WEB_SITE, **headers}


class TestOtherWebSites(common.BoxCase):

    def assertNotReadable(self, response):
        """No page of another web site may read `response`: a browser hands it over only when the box says so."""
        self.assertNotIn(OTHER_SITES_MAY_READ, response.headers)

    def assertReadable(self, response):
        self.assertIn(response.headers.get(OTHER_SITES_MAY_READ), ('*', common.ANOTHER_WEB_SITE))

    def test_page_of_the_box_its_data_and_its_log_do_not_answer_another_web_site(self):
        for address in PAGE_AND_LOG:
            with self.subTest(address=address):
                self.assertNotReadable(self.client.get(address, headers=from_another_web_site()))

    def test_device_routes_answer_another_web_site(self):
        for address in DEVICE_ROUTES:
            with self.subTest(address=address):
                # What a browser asks before it lets a page of another site call a route.
                asked_first = self.client.open(address, method='OPTIONS', headers=from_another_web_site(**{
                    'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type'}))

                self.assertReadable(asked_first)

    def test_page_of_another_web_site_reads_the_answer_of_a_device_route(self):
        response = self.client.get('/hw_proxy/hello', headers=from_another_web_site())

        self.assertEqual(response.get_data(as_text=True), 'ping')
        self.assertReadable(response)

    def test_page_of_another_web_site_uses_a_device(self):
        device = plug_device(self)
        asked = {'session_id': SESSION, 'device_identifier': DEVICE, 'data': json.dumps({'action': 'read_once'})}

        response = self.request(ACTION, asked, sent=from_another_web_site(**{
            'Sec-Fetch-Site': 'cross-site', 'Referer': PAGE_OF_ANOTHER_WEB_SITE}))

        self.assertIs(response.get_json()['result'], True)
        self.assertEqual(device.asked, ['read_once'])
        self.assertReadable(response)

    def test_question_a_browser_asks_first_for_a_page_of_another_web_site_changes_no_setting(self):
        common.store_box_configuration(wifi_ssid='Workshop')
        password = shell_password()
        for address in (DISCONNECT, WIFI_CLEAR, RESTART, SHELL_PASSWORD):
            with self.subTest(address=address):
                asked_first = self.client.open(address, method='OPTIONS', headers=from_another_web_site(**{
                    'Access-Control-Request-Method': 'GET', 'Access-Control-Request-Headers': 'x-viindoo-unlock'}))

                self.assertNotReadable(asked_first)
        self.assertEqual(common.box_configuration().get('wifi_ssid'), 'Workshop')
        self.assertEqual(common.restarts, [])
        self.assertEqual(shell_password(), password)


class TestOtherWebSitesOnALockedBox(LockedBoxCase):

    def test_settings_do_not_answer_another_web_site(self):
        called_with_json = (CONNECT, WIFI, LOG_LEVEL)
        for address in (CONNECT, DISCONNECT, WIFI, WIFI_CLEAR, LOG_LEVEL, RESTART, SHELL_PASSWORD):
            with self.subTest(address=address):
                if address in called_with_json:
                    response = self.client.post(
                        address, json={'jsonrpc': '2.0', 'method': 'call', 'params': {}},
                        headers=from_another_web_site())
                else:
                    response = self.client.get(address, headers=from_another_web_site())

                self.assertNotIn(OTHER_SITES_MAY_READ, response.headers)
        self.assertEqual(common.kept(), self.kept)
        self.assertEqual(common.restarts, [])


class TestWhatTheLockLetsOut(common.BoxCase):

    def test_settings_code_and_unlock_proof_never_reach_the_box_log(self):
        self.box_with_server(OTHER_CREDENTIAL)

        with common.box_log() as written:
            self.unlock('S10ME1Y5')
            _answer, proof = self.unlock(OTHER_SETTINGS_CODE)
            self.request(RESTART, proof=proof)
            self.request(RESTART)
            log = written()

        self.assertTrue(proof)
        self.assertTrue(log)
        for secret in (OTHER_SETTINGS_CODE, proof, OTHER_CREDENTIAL):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, log)


class TestCoreAddedARoute(common.MovedSeamCase):
    number = 13
    added = '/hw_posbox_homepage/added_by_an_update'
    core_changes = {
        "    @http.route('/hw_posbox_homepage/ping', auth='none', type='http', cors='*')": (
            "    @http.route('/hw_posbox_homepage/added_by_an_update', auth='none', type='http', cors='*')\n"
            "    def added_by_an_update(self):\n"
            "        return 'served'\n"
            "\n"
            "    @http.route('/hw_posbox_homepage/ping', auth='none', type='http', cors='*')"
        ),
    }
    other_seams = (1, 2, 3, 10, 12)
    probes = (added,)

    def test_route_the_layer_does_not_list_is_not_served(self):
        self.assertEqual(self.started_box()['answers'][self.added], 404)


class TestCoreWifiRouteTakesOtherParameters(common.MovedSeamCase):
    number = 13
    core_changes = {
        '    def update_wifi(self, essid, password, persistent=False):': (
            '    def update_wifi(self, essid, password, remember=False):'),
    }
    other_seams = (1, 2, 3, 10, 12)


class TestCoreWifiRouteTakesOneMoreParameter(common.MovedSeamCase):
    number = 13
    core_changes = {
        '    def update_wifi(self, essid, password, persistent=False):': (
            '    def update_wifi(self, essid, password, persistent=False, hidden=False):'),
    }
    other_seams = (1, 2, 3, 10, 12)


class TestCoreWifiRouteTakesOneParameterLess(common.MovedSeamCase):
    number = 13
    core_changes = {
        '    def update_wifi(self, essid, password, persistent=False):': '    def update_wifi(self, essid, password):',
    }
    other_seams = (1, 2, 3, 10, 12)


class TestCoreWifiRouteTakesItsParametersInAnotherOrder(common.MovedSeamCase):
    number = 13
    core_changes = {
        '    def update_wifi(self, essid, password, persistent=False):': (
            '    def update_wifi(self, password, essid, persistent=False):'),
    }
    other_seams = (1, 2, 3, 10, 12)


class TestCoreAddedADatabaseAddress(common.MovedSeamCase):
    number = 13
    added = '/web/database/added_by_an_update'
    web_changes = {
        "    @http.route('/web/database/list', type='json', auth='none')": (
            "    @http.route('/web/database/added_by_an_update', type='http', auth='none')\n"
            "    def added_by_an_update(self):\n"
            "        return 'served'\n"
            "\n"
            "    @http.route('/web/database/list', type='json', auth='none')"
        ),
    }
    other_seams = (1, 2, 3, 10, 12)
    probes = (added,)

    def test_database_address_the_layer_does_not_list_is_not_served(self):
        self.assertEqual(self.started_box()['answers'][self.added], 404)
