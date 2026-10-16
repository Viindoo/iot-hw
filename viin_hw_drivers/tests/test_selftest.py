"""The self-test of the box: each layer it depends on, the diagnostic file, and the clock it takes from its server."""
import ctypes
import datetime
import json
import logging
import socket
import subprocess
import tempfile
import threading
import types
from unittest import mock

import common
import services_common as box
import wifi_common

CREDENTIAL = 'suite-credential-selftest'
CERTIFICATE = '/viin_hw_homepage/certificate'
SITE_ADDRESS = '192.168.1.50'
ROUTER = '192.168.1.1'
OWN_NAME = 'box.shop.example'
VIINDOO_NAME = 'k3x9.iot.viindoo.example'
# The wildcard name of the certificates of core's vendor, as core keeps it once a server handed it over,
# and the name core derives from it for a box at SITE_ADDRESS.
VENDOR_ZONE = '*.vendor-zone.example'
NAME_IN_THE_VENDOR_ZONE = '192-168-1-50.vendor-zone.example'
NAME_ENDS = '2027-01-31T00:00:00Z'
# The end dates of the certificate the web server serves and of the pair installed for the Viindoo name, far enough
# away that neither is ending, and different so that a line cannot name the wrong one by accident.
INSTALLED_ENDS = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=400)).strftime(
    '%Y-%m-%dT%H:%M:%SZ')
NAME_PAIR_ENDS = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=300)).strftime(
    '%Y-%m-%dT%H:%M:%SZ')
DAY = datetime.timedelta(days=1)
EARLIER_ADDRESS = '10.11.12.1'
WIFI_PASSWORD = 'box-suite-wifi-password-4R7'
WIFI_JOIN = '/hw_posbox_homepage/update_wifi'
WIFI_FORGET = '/hw_posbox_homepage/wifi_clear'
# The name of the profile of the network the box keeps.
WIFI_PROFILE = 'viindoo-wifi'
# A handler file of core that cannot be loaded, and a seam of the layer whose symbol moved in core.
BROKEN_HANDLER_AND_MOVED_SEAM = {
    'from functools import reduce': 'from functools import reduce\nraise ImportError("box suite")',
    'def _get_domain(self):': 'def _get_domain(self, refresh=False):',
}


def ends_in(days):
    """The end date of a certificate that ends `days` days from now, before now when negative, as lines carry it."""
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')


def lines_of(result):
    return {line['id']: [line['state'], line['reason']] for line in result['lines']}


def self_test_of_a_box_beside_a_server(port, up, channel):
    """Start a box whose server listens on `port` when `up`, giving `channel` to a report; give its self-test."""
    if up:
        common.FakeServer(port=port, channel=channel)
    common.start()
    reporting = common.layer('reporting')
    common.wait_until(lambda: reporting.outcome()['state'] != reporting.NEVER, 'the first report of the box')
    if up and channel:
        common.wait_until(lambda: reporting.channel()['state'] == reporting.RECONNECTING,
                          'the live channel to fail on a server that has none')
    return common.layer('selftest').run(wait=20, refresh=True)


def self_test_beside_a_server(port, up, channel):
    """The state and the reason of each line of the self-test of a box beside a server (see above)."""
    return lines_of(self_test_of_a_box_beside_a_server(port, up, channel))


def actions_beside_a_server(port, up, channel):
    """The action of each line of the self-test of a box beside a server (see above)."""
    return {line['id']: line['action'] for line in self_test_of_a_box_beside_a_server(port, up, channel)['lines']}


def self_test_of_a_box_whose_core_moved():
    """Start a box whose core holds a handler file that cannot be loaded and moved a symbol of a seam."""
    common.start(common.configuration_file(
        tempfile.mkdtemp(prefix='box-suite-'), core_changes=BROKEN_HANDLER_AND_MOVED_SEAM))
    handlers = common.layer('seams.handlers')
    common.wait_until(lambda: handlers.handler_files()['loaded'], 'core to load its device handlers')
    return lines_of(common.layer('selftest').run(wait=0))


def browser_address_line_after_a_start(address):
    """Start a box that holds `address` and gives it to browsers. Meant for `common.in_new_process`.

    Gives 'accept_again', what its browser address line says of it, and 'certificate', the fingerprint of the
    certificate installed once the line was worked out.
    """
    chosen = {'address': address, 'source': 'address', 'core': address}
    with box.network({'eth0': address}):
        common.start()
        with mock.patch.object(common.layer('seams.identity'), 'reported_address', return_value=chosen):
            lines = common.layer('selftest').run(wait=0, refresh=True)['lines']
        line = next(line for line in lines if line['id'] == 'browser_address')
        return {'accept_again': line['details'].get('accept_again'),
                'certificate': common.fingerprint(common.web_certificate())}


class _UnsynchronisedLibrary:
    """The C library, as far as the clock of a box that no time server keeps."""

    def __init__(self, library):
        self._library = library

    def adjtimex(self, timex):
        timex._obj.status = 0x0040
        return 5

    def __getattr__(self, name):
        return getattr(self._library, name)


def clock_a_paired_box_sets_at_its_start():
    """Start a paired box whose clock no time server keeps; give the times it asked the system to set its clock to."""
    load = ctypes.CDLL

    def library(name, *arguments, **options):
        found = load(name, *arguments, **options)
        return _UnsynchronisedLibrary(found) if name is None else found

    ctypes.CDLL = library
    with box.clock_commands() as asked:
        common.start()
        try:
            common.wait_until(lambda: asked, 'the box to set its clock', 20)
        except AssertionError:
            pass
        return list(asked)


class SelfTestCase(common.BoxCase):

    def lines(self, wait=20):
        return lines_of(common.layer('selftest').run(wait=wait, refresh=True))

    def line(self, identifier, wait=20):
        """The whole line of the self-test with the id `identifier`: state, reason, action and details."""
        lines = common.layer('selftest').run(wait=wait, refresh=True)['lines']
        return next(line for line in lines if line['id'] == identifier)


class TestNetworkLine(SelfTestCase):

    def test_box_without_an_address_names_the_network_as_failing(self):
        self.assertEqual(self.lines()['network'], ['failed', 'no_address'])

    def test_box_with_a_link_local_address_only_names_the_network_as_failing(self):
        with box.network({'eth0': '169.254.3.3'}):
            self.assertEqual(self.lines()['network'], ['failed', 'link_local_only'])

    def test_box_with_an_address_and_a_router_has_its_network_in_order(self):
        with box.network({'eth0': SITE_ADDRESS}, gateway=ROUTER):
            self.assertEqual(self.lines()['network'][0], 'ok')


class TestServerLines(SelfTestCase):

    def test_box_whose_server_is_down_names_the_server_as_failing(self):
        server = self.box_with_server(CREDENTIAL)
        server.close()

        self.assertEqual(self.lines()['server'][0], 'failed')

    def test_box_whose_server_answers_has_its_server_in_order(self):
        self.box_with_server(CREDENTIAL)

        self.assertEqual(self.lines()['server'], ['ok', 'reachable'])

    def test_box_whose_server_name_does_not_resolve_names_the_name_resolution_as_failing(self):
        common.store_box_configuration(remote_server='http://box-suite.invalid:8069', token=CREDENTIAL)

        self.assertEqual(self.lines()['name_resolution'][0], 'failed')


class TestCertificateLine(SelfTestCase):

    def test_server_whose_certificate_nobody_vouches_for_names_the_certificate_as_failing(self):
        server = self.server(certificate=common.make_certificate('localhost'))
        common.store_box_configuration(remote_server=server.url, token=CREDENTIAL)

        self.assertEqual(self.lines()['certificate'], ['failed', 'untrusted'])

    def test_server_whose_certificate_nobody_vouches_for_is_met_by_pairing_again(self):
        server = self.server(certificate=common.make_certificate('localhost'))
        common.store_box_configuration(remote_server=server.url, token=CREDENTIAL)

        self.assertEqual(self.line('certificate')['action'], 'pair_again')

    def test_server_whose_certificate_the_installer_trusted_has_its_certificate_in_order(self):
        self.box_with_server(CREDENTIAL, certificate=common.make_certificate('localhost'))

        self.assertEqual(self.lines()['certificate'], ['ok', 'trusted_by_you'])

    def test_server_reached_without_https_is_warned_about(self):
        self.box_with_server(CREDENTIAL)

        self.assertEqual(self.lines()['certificate'][0], 'warning')


class TestClockLine(SelfTestCase):

    def setUp(self):
        super().setUp()
        self.server_of_the_box = self.box_with_server(CREDENTIAL)
        self.server_of_the_box.clock = common.utc_now() + DAY

    def test_clock_of_a_box_no_time_server_keeps_is_failing_when_its_server_says_another_time(self):
        with mock.patch.object(common.layer('selftest'), 'clock_synchronised', return_value=False):
            line = self.lines()['clock']

        self.assertEqual(line, ['failed', 'wrong'])

    def test_clock_a_time_server_keeps_is_not_blamed_when_its_server_says_another_time(self):
        with mock.patch.object(common.layer('selftest'), 'clock_synchronised', return_value=True):
            line = self.lines()['clock']

        self.assertEqual(line[0], 'warning')
        self.assertNotEqual(line[1], 'wrong')


class ServerAtAPortCase(common.BoxStateCase):

    def lines_beside(self, up, channel='box-suite-channel', child=self_test_beside_a_server):
        port = common.closed_port()
        common.store_box_configuration(remote_server=f'http://127.0.0.1:{port}', token=CREDENTIAL)
        return common.in_new_process(child, port=port, up=up, channel=channel)


class TestRegistrationAndLiveChannelLines(ServerAtAPortCase):

    def test_box_whose_server_took_its_report_but_whose_live_channel_fails_names_the_live_channel(self):
        lines = self.lines_beside(up=True)

        self.assertEqual(lines['registration'], ['ok', 'accepted'])
        self.assertEqual(lines['live_channel'], ['failed', 'reconnecting'])

    def test_box_whose_server_refuses_its_report_names_the_registration_as_failing(self):
        lines = self.lines_beside(up=True, channel='')

        self.assertEqual(lines['registration'], ['failed', 'refused'])

    def test_box_whose_server_refuses_its_report_is_told_to_reset_it_then_pair_it(self):
        actions = self.lines_beside(up=True, channel='', child=actions_beside_a_server)

        self.assertEqual(actions['registration'], 'reset_then_pair')

    def test_box_whose_server_is_down_names_the_registration_and_the_server_as_failing(self):
        lines = self.lines_beside(up=False)

        self.assertEqual(lines['registration'], ['failed', 'unreachable'])
        self.assertEqual(lines['server'][0], 'failed')


def _hold_the_device_loop(manager):
    """Stop the device loop of core `manager` at the start of its next pass, for the rest of the process.

    Once the box has a server the loop reports by itself: a report of it that ends after the clock jump would be the
    one the server took last, at a moment the test does not choose.
    """
    held = threading.Event()
    look = manager._get_changes_to_send

    def changes_to_send():
        if threading.current_thread() is manager:
            held.set()
            threading.Event().wait()
        return look()

    manager._get_changes_to_send = changes_to_send
    common.wait_until(held.is_set, 'the device loop of core to start a pass')


def moments_seen_after_the_clock_of_the_box_jumps(port, paired_by_report):
    """Note a moment on a clock the test drives, correct the wall clock by two days, and give the lines 5 s later."""
    common.start()
    reporting = common.layer('reporting')
    manager = common.core('main').manager
    _hold_the_device_loop(manager)
    common.store_box_configuration(remote_server=f'http://127.0.0.1:{port}', token=CREDENTIAL)
    clock = types.SimpleNamespace(wall=1_800_000_000.0, mono=1000.0)
    reporting.time = types.SimpleNamespace(time=lambda: clock.wall, monotonic=lambda: clock.mono)
    if paired_by_report:
        server = common.FakeServer(port=port, channel='box-suite-channel')
        manager.server_url = server.url
        manager.send_alldevices()
    else:
        reporting.channel_opened()
    clock.wall += 2 * 86400
    clock.mono += 5
    lines = common.layer('selftest').run(wait=0, refresh=True)['lines']
    return {line['id']: line['details'] for line in lines}


class TestMomentsOfTheBoxStayRightWhenItsClockIsCorrected(common.BoxStateCase):

    def test_moment_the_server_accepted_the_box_is_5_seconds_ago_when_the_clock_jumped_two_days_after_it(self):
        details = common.in_new_process(
            moments_seen_after_the_clock_of_the_box_jumps, port=common.closed_port(), paired_by_report=True)

        self.assertEqual(details['registration']['accepted_age'], 5)

    def test_moment_the_live_channel_took_its_state_is_5_seconds_ago_when_the_clock_jumped_two_days_after_it(self):
        details = common.in_new_process(
            moments_seen_after_the_clock_of_the_box_jumps, port=common.closed_port(), paired_by_report=False)

        self.assertEqual(details['live_channel']['since_age'], 5)


class TestDriversAndLayerLines(common.BoxStateCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.lines = common.in_new_process(self_test_of_a_box_whose_core_moved)

    def test_handler_file_that_cannot_be_loaded_names_the_drivers_as_failing(self):
        self.assertEqual(self.lines['drivers'], ['failed', 'failed_to_load'])

    def test_seam_whose_symbol_moved_names_the_layer_as_failing(self):
        self.assertEqual(self.lines['layer'], ['failed', 'not_in_place'])


class TestBrowserAddressLine(SelfTestCase):

    def address_line(self):
        lines = common.layer('selftest').run(wait=0, refresh=True)['lines']
        return next(line for line in lines if line['id'] == 'browser_address')

    def test_self_test_shows_that_browsers_are_given_the_name_of_the_installer(self):
        common.store_box_configuration(viin_box_address=OWN_NAME)

        line = self.address_line()

        self.assertEqual((line['details']['address'], line['details']['kind']), (OWN_NAME, 'own_name'))

    def test_self_test_shows_that_browsers_are_given_an_address_of_the_site(self):
        with mock.patch.object(common.core('tools.helpers'), 'get_ip', return_value=SITE_ADDRESS):
            line = self.address_line()

        self.assertEqual((line['details']['address'], line['details']['kind']), (SITE_ADDRESS, 'address'))

    def test_box_without_an_address_of_the_site_warns_that_browsers_get_its_local_name(self):
        line = self.address_line()

        self.assertEqual(line['details']['kind'], 'local_name')
        self.assertEqual(line['state'], 'warning')

    def test_self_test_shows_that_browsers_get_the_address_of_the_site_when_core_knows_the_zone_of_its_vendor(self):
        common.store_box_configuration(subject=VENDOR_ZONE)
        with mock.patch.object(common.core('tools.helpers'), 'get_ip', return_value=SITE_ADDRESS), \
                box.network({'eth0': SITE_ADDRESS}):
            line = self.address_line()

        self.assertEqual((line['details']['address'], line['details']['kind']), (SITE_ADDRESS, 'address'))

    def test_page_data_give_browsers_the_address_of_the_site_when_core_knows_the_zone_of_its_vendor(self):
        common.store_box_configuration(subject=VENDOR_ZONE)
        with mock.patch.object(common.core('tools.helpers'), 'get_ip', return_value=SITE_ADDRESS), \
                box.network({'eth0': SITE_ADDRESS}):
            given = self.request(box.STATUS).get_json()['advanced']['box_address']

        self.assertEqual((given['address'], given['kind']), (SITE_ADDRESS, 'address'))

    def healthy_line_for(self, source, address, installed_kind):
        """The line of a box that serves a certificate of `installed_kind` and gives browsers `address` of `source`."""
        return self.line_for(source, address, installed_kind, INSTALLED_ENDS, NAME_PAIR_ENDS)

    def line_for(self, source, address, installed_kind, installed_ends, name_ends):
        """The line of a box that gives browsers `address` of `source`, serves a certificate of `installed_kind`
        issued for that address and ending at `installed_ends`, and holds a pair of the Viindoo name whose
        certificate ends at `name_ends`, or one it cannot read when None."""
        trust, identity = common.layer('trust'), common.layer('seams.identity')
        installed = {'kind': installed_kind, 'expiry': installed_ends, 'subject': 'CN=box', 'names': [address]}
        name = None if name_ends is None else dict(installed, expiry=name_ends)
        chosen = {'address': address, 'source': source, 'core': address}
        with mock.patch.object(trust, 'box_certificate', return_value=installed), \
                mock.patch.object(trust, 'name_certificate', return_value=name), \
                mock.patch.object(identity, 'reported_address', return_value=chosen):
            return self.address_line()

    def viindoo_name_line(self, installed_kind='box', installed_ends=INSTALLED_ENDS, name_ends=NAME_PAIR_ENDS):
        """`line_for` a box that gives browsers its Viindoo name: (state, reason, action), and the line."""
        line = self.line_for('viindoo_name', VIINDOO_NAME, installed_kind, installed_ends, name_ends)
        return (line['state'], line['reason'], line['action']), line

    def test_viindoo_name_whose_certificate_ended_fails_and_asks_for_a_restart(self):
        name_ended = ends_in(-1)
        cases = {'own certificate valid': INSTALLED_ENDS, 'own certificate ended': ends_in(-2)}
        for case, installed_ends in cases.items():
            with self.subTest(case):
                verdict, line = self.viindoo_name_line(installed_ends=installed_ends, name_ends=name_ended)

                self.assertEqual(verdict, ('failed', 'certificate_expired', 'restart_box'))
                self.assertEqual(
                    (line['details'].get('certificate_kind'), line['details'].get('certificate_ends')),
                    ('viindoo_name', name_ended))

    def test_viindoo_name_whose_certificate_cannot_be_read_is_warned_about_and_asks_for_a_restart(self):
        verdict, _line = self.viindoo_name_line(name_ends=None)

        self.assertEqual(verdict, ('warning', 'name_certificate_unreadable', 'restart_box'))

    def test_viindoo_name_box_whose_own_certificate_ended_warns_for_browsers_that_open_it_by_its_address(self):
        cases = {'certificate the box made': ('box', 'restart_box'),
                 'certificate of the customer': ('customer', 'renew_own_certificate')}
        for case, (installed_kind, action) in cases.items():
            with self.subTest(case):
                verdict, _line = self.viindoo_name_line(installed_kind, installed_ends=ends_in(-1))

                self.assertEqual(verdict, ('warning', 'address_certificate_expired', action))

    def test_viindoo_name_box_whose_own_certificate_ends_soon_warns_for_browsers_that_open_it_by_its_address(self):
        cases = {'certificate the box made': ('box', 'restart_box'),
                 'certificate of the customer': ('customer', 'renew_own_certificate')}
        for case, (installed_kind, action) in cases.items():
            with self.subTest(case):
                verdict, _line = self.viindoo_name_line(installed_kind, installed_ends=ends_in(10))

                self.assertEqual(verdict, ('warning', 'address_certificate_ending', action))

    def test_viindoo_name_whose_certificate_is_valid_is_in_order_until_the_end_of_that_certificate(self):
        for case, name_ends in {'ends in a year': ends_in(365), 'ends in ten days': ends_in(10)}.items():
            with self.subTest(case):
                verdict, line = self.viindoo_name_line(name_ends=name_ends)

                self.assertEqual(verdict, ('ok', 'viindoo_name', ''))
                self.assertEqual(
                    (line['details'].get('certificate_kind'), line['details'].get('certificate_ends')),
                    ('viindoo_name', name_ends))

    def test_certificate_of_the_image_is_warned_about_whatever_address_browsers_are_given(self):
        for source, address in {'viindoo_name': VIINDOO_NAME, 'address': SITE_ADDRESS}.items():
            with self.subTest(source):
                line = self.line_for(source, address, 'shipped', INSTALLED_ENDS, NAME_PAIR_ENDS)

                self.assertEqual((line['state'], line['reason'], line['action']),
                                 ('warning', 'shared_certificate', 'restart_box'))

    def test_certificate_browsers_get_at_an_address_of_the_site_is_judged_by_its_own_end(self):
        cases = {
            'certificate the box made, ended': ('box', ends_in(-1), ('failed', 'certificate_expired', 'restart_box')),
            'certificate of the customer, ended': (
                'customer', ends_in(-1), ('failed', 'certificate_expired', 'renew_own_certificate')),
            'certificate the box made, ending': ('box', ends_in(10), ('warning', 'certificate_ending', 'restart_box')),
            'certificate of the customer, ending': (
                'customer', ends_in(10), ('warning', 'certificate_ending', 'renew_own_certificate')),
        }
        for case, (installed_kind, installed_ends, expected) in cases.items():
            with self.subTest(case):
                line = self.line_for('address', SITE_ADDRESS, installed_kind, installed_ends, ends_in(-1))

                self.assertEqual((line['state'], line['reason'], line['action']), expected)

    def test_healthy_line_names_the_certificate_browsers_get_and_its_end_date(self):
        cases = {
            'certificate the box made': ('address', SITE_ADDRESS, 'box', ('box', INSTALLED_ENDS)),
            'certificate of the customer': ('own_name', OWN_NAME, 'customer', ('customer', INSTALLED_ENDS)),
            'certificate of the Viindoo name': ('viindoo_name', VIINDOO_NAME, 'box', ('viindoo_name', NAME_PAIR_ENDS)),
        }
        for case, (source, address, installed_kind, expected) in cases.items():
            with self.subTest(case):
                line = self.healthy_line_for(source, address, installed_kind)

                self.assertEqual(line['state'], 'ok', line)
                self.assertEqual((line['details']['certificate_kind'], line['details']['certificate_ends']), expected)

    def test_certificate_the_web_server_refused_to_take_asks_for_a_restart_only_of_browsers_that_get_it(self):
        trust = common.layer('trust')
        self.keep_web_certificate()
        # A look that serves what the box holds leaves nothing waiting for the web server after the test.
        self.addCleanup(trust.refresh_box_certificate)
        common.install_web_certificate(box.box_made_certificate(self.own_names(SITE_ADDRESS), lasting_days=59))
        common.serve_web_certificate()
        with box.network({'eth0': SITE_ADDRESS}), common.failing('nginx'), \
                self.assertRaises((OSError, subprocess.SubprocessError)):
            trust.refresh_box_certificate()
        cases = {
            'the Viindoo name': ('viindoo_name', VIINDOO_NAME, ['ok', 'viindoo_name', '']),
            'an address of the site': ('address', SITE_ADDRESS, ['warning', 'certificate_not_served', 'restart_box']),
        }
        for case, (source, address, expected) in cases.items():
            with self.subTest(case):
                line = self.healthy_line_for(source, address, 'box')

                self.assertEqual([line['state'], line['reason'], line['action']], expected)

    def own_names(self, *addresses):
        host = socket.gethostname()
        return [host, f'{host}.local', *addresses]

    def line_for_address_with(self, certificate=None, address=SITE_ADDRESS):
        """The browser address line of a box that holds `address` and gives it to browsers; the web server serves
        `certificate` when one is given."""
        if certificate:
            self.keep_web_certificate()
            common.install_web_certificate(certificate)
        chosen = {'address': address, 'source': 'address', 'core': address}
        with mock.patch.object(common.layer('seams.identity'), 'reported_address', return_value=chosen), \
                box.network({'eth0': address}):
            return self.address_line()

    def test_own_certificate_made_at_another_address_and_about_to_be_made_again_is_pending_with_no_action(self):
        line = self.line_for_address_with(box.box_made_certificate(self.own_names(EARLIER_ADDRESS)))

        self.assertEqual((line['state'], line['reason'], line['action']), ('pending', 'certificate_being_made', ''))

    def test_own_certificate_whose_remaking_failed_asks_for_a_restart(self):
        self.keep_web_certificate()
        common.install_web_certificate(box.box_made_certificate(self.own_names(EARLIER_ADDRESS)))
        trust = common.layer('trust')
        # A look that succeeds clears the memory of the one that failed.
        self.addCleanup(trust.refresh_box_certificate)
        with box.network({'eth0': SITE_ADDRESS}), common.failing('sudo'), self.assertRaises(Exception):
            trust.refresh_box_certificate()

        line = self.line_for_address_with()

        self.assertEqual((line['state'], line['reason'], line['action']),
                         ('warning', 'certificate_not_for_address', 'restart_box'))

    def test_certificate_of_the_customer_that_does_not_cover_the_address_is_theirs_to_renew(self):
        customer = common.make_certificate(OWN_NAME, valid_until=common.utc_now() + 400 * DAY)

        line = self.line_for_address_with(customer)

        self.assertEqual((line['state'], line['reason'], line['action']),
                         ('warning', 'certificate_not_for_address', 'renew_own_certificate'))

    def serving(self, certificate):
        """Have the web server of the box present `certificate`, a `Certificate`, or none when it is None."""
        self.keep_web_certificate()
        box.keep_files(self, common.core('tools.helpers').path_file(common.layer('trust').RENEWAL_FILE))
        if certificate:
            common.install_web_certificate(certificate)
        else:
            common.as_root('rm', '-f', str(common.WEB_CERTIFICATE))

    def while_the_box_holds_the_site_address(self, action):
        with box.network({'eth0': SITE_ADDRESS}):
            action()

    def test_certificate_the_box_made_in_place_of_one_it_had_made_is_to_be_accepted_again_for_seven_days(self):
        trust = common.layer('trust')
        self.serving(box.box_made_certificate(self.own_names(EARLIER_ADDRESS)))
        self.while_the_box_holds_the_site_address(trust.refresh_box_certificate)
        made = common.utc_now()
        cases = {'the day it was made': (0, True), 'six days later': (6, True), 'eight days later': (8, False)}
        for case, (days, accept_again) in cases.items():
            with self.subTest(case), mock.patch.object(trust, 'utc_now', return_value=made + days * DAY):
                line = self.line_for_address_with()

                self.assertEqual(line['state'], 'ok', line)
                self.assertIs(line['details']['accept_again'], accept_again)
                self.assertTrue(line['details']['certificate_made_at'])

    def test_certificate_the_box_made_in_place_of_one_it_had_not_made_is_never_to_be_accepted_again(self):
        trust = common.layer('trust')
        replaced = {
            'its first certificate, in place of the one of the image': (
                common.make_certificate(common.SHIPPED_COMMON_NAME), trust.refresh_box_certificate),
            'in place of none': (None, trust.refresh_box_certificate),
            'in place of the certificate of the customer': (
                common.make_certificate(OWN_NAME, valid_until=common.utc_now() + 400 * DAY),
                trust.restore_box_certificate),
        }
        for case, (certificate, made_by) in replaced.items():
            with self.subTest(case):
                self.serving(certificate)
                self.while_the_box_holds_the_site_address(made_by)

                line = self.line_for_address_with()

                self.assertEqual(line['state'], 'ok', line)
                self.assertIs(line['details']['accept_again'], False)
                self.assertTrue(line['details']['certificate_made_at'])

    def test_box_certificate_put_back_after_the_customer_one_is_never_to_be_accepted_again_after_a_renewal(self):
        self.serving(box.box_made_certificate(self.own_names(EARLIER_ADDRESS)))
        self.while_the_box_holds_the_site_address(common.layer('trust').refresh_box_certificate)
        renewed = self.line_for_address_with()
        customer = common.make_certificate(OWN_NAME, valid_until=common.utc_now() + 400 * DAY)
        installed = self.call(CERTIFICATE, certificate=customer.pem, key=customer.key_pem, host_name=OWN_NAME)
        self.while_the_box_holds_the_site_address(lambda: self.call(CERTIFICATE, restore=True))

        line = self.line_for_address_with()

        self.assertIs(renewed['details']['accept_again'], True)
        self.assertEqual(installed.get('status'), 'installed', installed)
        self.assertEqual((line['state'], line['details']['certificate_kind']), ('ok', 'box'), line)
        self.assertIs(line['details']['accept_again'], False)

    def test_certificate_the_box_made_in_place_of_one_it_had_made_is_still_to_be_accepted_again_after_a_restart(self):
        self.serving(box.box_made_certificate(self.own_names(EARLIER_ADDRESS)))
        self.while_the_box_holds_the_site_address(common.layer('trust').refresh_box_certificate)
        made = common.fingerprint(common.web_certificate())

        restarted = common.in_new_process(browser_address_line_after_a_start, address=SITE_ADDRESS)

        self.assertEqual(restarted['certificate'], made)
        self.assertIs(restarted['accept_again'], True)

    def test_certificate_of_the_customer_is_never_to_be_accepted_again(self):
        customer = common.make_certificate(SITE_ADDRESS, valid_until=common.utc_now() + 400 * DAY)

        line = self.line_for_address_with(customer)

        self.assertEqual(line['state'], 'ok', line)
        self.assertNotIn('accept_again', line['details'])

    def test_line_answers_a_state_with_an_instruction_whatever_name_it_is_given(self):
        identity = common.layer('seams.identity')
        for kind in ('own_name', 'viindoo_name', 'address', 'local_name'):
            chosen = {'address': NAME_IN_THE_VENDOR_ZONE, 'source': kind, 'core': NAME_IN_THE_VENDOR_ZONE}
            with self.subTest(kind=kind), mock.patch.object(identity, 'reported_address', return_value=chosen):
                line = self.address_line()

                self.assertNotEqual(line['reason'], 'box_error')
                self.assertIn(line['state'], ('ok', 'warning', 'failed'))
                self.assertTrue(line['state'] == 'ok' or line['action'], line)


class TestBrowserAddressLineWhileTheWebServerRefusesACertificate(common.BoxStateCase):

    def looks(self):
        """What `follow_a_certificate_the_web_server_refused_to_take` gives, once for the tests of the class."""
        cls = type(self)
        if '_looks' not in vars(cls):
            self.keep_web_certificate()
            cls._looks = common.in_new_process(
                box.follow_a_certificate_the_web_server_refused_to_take, address=SITE_ADDRESS)
        return cls._looks

    def test_certificate_the_web_server_refused_to_take_asks_for_a_restart_until_a_reload_serves_it(self):
        looks = self.looks()

        self.assertEqual(looks['meanwhile']['line'], ['warning', 'certificate_not_served', 'restart_box'])
        self.assertEqual(looks['after']['line'][0], 'ok', looks['after'])


class TestViindooNameLine(SelfTestCase):

    def setUp(self):
        super().setUp()
        box.keep_files(self, box.STATE_FILE)
        common.store_box_configuration(remote_server='http://127.0.0.1:9', token=CREDENTIAL)

    def answered(self, **record):
        """The server last answered the question of the box for a name with `record`."""
        common.layer('update').change_state(lambda state: state.update(public_name=record))

    def test_customer_server_that_offers_no_name_is_neutral(self):
        self.answered(status='unavailable', reason='no_provider')

        self.assertEqual(self.lines(wait=0)['viindoo_name'][0], 'neutral')

    def test_name_switched_off_by_an_administrator_is_neutral(self):
        self.answered(status='unavailable', reason='disabled')

        self.assertEqual(self.lines(wait=0)['viindoo_name'][0], 'neutral')

    def test_name_is_neutral_for_a_box_with_the_certificate_of_the_installer(self):
        self.keep_web_certificate()
        common.install_web_certificate(common.make_certificate(OWN_NAME, valid_until=common.utc_now() + 400 * DAY))
        common.store_box_configuration(viin_box_address=OWN_NAME)
        self.answered(status='refused', reason='address_not_private')

        line = self.line('viindoo_name', wait=0)

        self.assertEqual((line['state'], line['reason'], line['action']), ('neutral', 'own_certificate', ''))

    def test_name_is_neutral_and_asks_nothing_for_a_box_given_a_typed_name_and_serving_its_own_certificate(self):
        host = socket.gethostname()
        self.keep_web_certificate()
        common.install_web_certificate(box.box_made_certificate([host, f'{host}.local', OWN_NAME]))
        common.store_box_configuration(viin_box_address=OWN_NAME)
        self.answered(status='refused', reason='address_not_private')

        line = self.line('viindoo_name', wait=0)

        self.assertEqual((line['state'], line['reason'], line['action']), ('neutral', 'own_name', ''))

    def test_name_refused_for_an_address_that_is_not_private_is_warned_about(self):
        self.answered(status='refused', reason='address_not_private')

        self.assertEqual(self.lines(wait=0)['viindoo_name'], ['warning', 'address_not_private'])

    def line_of_the_name(self, **record):
        self.answered(**record)
        return self.line('viindoo_name', wait=0)

    def test_box_the_server_does_not_know_is_told_to_reset_it_then_pair_it(self):
        line = self.line_of_the_name(status='unavailable', reason='box_unknown')

        self.assertEqual((line['state'], line['reason'], line['action']), ('warning', 'box_unknown', 'reset_then_pair'))

    def test_name_that_ended_asks_one_thing_of_the_person_by_what_the_server_last_answered(self):
        answers = {
            'server not reached': ('unreachable:no_answer', 'check_server'),
            'limit reached': ('refused:limit_reached', 'ask_administrator'),
            'address not private': ('refused:address_not_private', 'use_own_certificate'),
            'any other answer': ('refused:server_error', 'contact_support'),
            'no answer kept': ('', 'contact_support'),
        }
        for case, (last_answer, action) in answers.items():
            with self.subTest(case):
                line = self.line_of_the_name(
                    status='issued', fqdn=VIINDOO_NAME, not_after='2020-01-31T00:00:00Z', check='ok',
                    last_answer=last_answer)

                self.assertEqual((line['state'], line['reason'], line['action']), ('warning', 'expired', action))

    def test_name_the_box_could_not_check_on_its_own_site_is_met_by_restarting_the_box(self):
        line = self.line_of_the_name(
            status='issued', fqdn=VIINDOO_NAME, not_after=NAME_ENDS, check='wrong_certificate')

        self.assertEqual((line['state'], line['reason'], line['action']),
                         ('warning', 'name_check_failed', 'restart_for_name'))

    def test_page_data_carries_the_name_its_end_and_its_state(self):
        common.store_box_configuration(viin_managed_name=VIINDOO_NAME)
        self.answered(status='issued', fqdn=VIINDOO_NAME, not_after=NAME_ENDS, check='ok')

        data = self.request(box.PAGE_DATA).get_json()

        name = data.get('viindoo', {}).get('advanced', {}).get('viindoo_name', {})
        self.assertEqual(
            (name.get('name'), name.get('not_after'), name.get('state')), (VIINDOO_NAME, NAME_ENDS, 'issued'))


class TestBoxLog(SelfTestCase):

    def test_failing_line_is_written_once_to_the_box_log_however_often_the_self_test_runs(self):
        with box.network({'eth0': SITE_ADDRESS}, gateway=ROUTER):
            self.lines(wait=0)

        with self.assertLogs(common.layer('selftest')._logger, logging.WARNING) as logged:
            self.lines(wait=0)
            self.lines(wait=0)

        network = [record for record in logged.records if record.args and record.args[0] == 'network']
        self.assertEqual(len(network), 1)


class TestDiagnosticFile(common.BoxCase):

    def test_diagnostic_file_holds_no_credential_settings_code_key_wifi_password_or_unlock_proof(self):
        key = common.make_certificate('box-suite-key').key_pem
        with common.box_log():
            self.box_with_server(CREDENTIAL)
        common.store_box_configuration(wifi_password=WIFI_PASSWORD)
        _answer, proof = self.unlock(box.settings_code(CREDENTIAL))
        self.request('/hw_posbox_homepage/ping', proof=proof)
        # What a command of core may print to the box log about what it was given.
        logging.getLogger(f'{common.CORE}.tools.helpers').error(
            'box suite: %s %s %s', WIFI_PASSWORD, box.settings_code(CREDENTIAL), key)

        response = self.request(box.DIAGNOSTIC)

        text = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response.headers.get('Content-Disposition', ''))
        secrets = {'credential': CREDENTIAL, 'settings code': box.settings_code(CREDENTIAL), 'unlock proof': proof,
                   'private key': key.splitlines()[1], 'wifi password': WIFI_PASSWORD}
        self.assertEqual([name for name, secret in secrets.items() if secret in text], [])


class TestWifiNetworkInTheDiagnosticFile(common.BoxCase):
    """What the diagnostic file tells support of the Wi-Fi network the box keeps to join again as it starts."""

    def setUp(self):
        super().setUp()
        self.network = wifi_common.network_manager(self)
        self.card = wifi_common.card_copies()[0]

    def join(self, name):
        with wifi_common.settled():
            answer = self.call(WIFI_JOIN, essid=name, password=WIFI_PASSWORD, persistent=True)
        self.assertEqual(answer.get('status'), 'success', answer)

    def kept_wifi(self):
        response = self.request(box.DIAGNOSTIC)
        self.assertEqual(response.status_code, 200)
        return json.loads(response.get_data(as_text=True))['configuration']['kept_wifi'], response

    def test_diagnostic_file_names_the_network_the_box_keeps_and_never_its_password(self):
        names = {
            'plain name': 'Workshop',
            'signs nmcli writes with a backslash': 'Shop:2\\x\\',
            'name in Vietnamese': 'Xưởng in số 3',
            'spaces at either end of the name': ' Shop ',
        }
        for case, name in names.items():
            with self.subTest(case):
                self.network.change(networks={name: WIFI_PASSWORD})
                self.join(name)

                kept_wifi, response = self.kept_wifi()

                self.assertEqual(kept_wifi, {'card_copy': True, 'profile_network': name})
                self.assertNotIn(WIFI_PASSWORD, response.get_data(as_text=True))

    def test_diagnostic_file_names_the_network_kept_whole_whatever_locale_the_settings_of_the_box_name(self):
        name = 'Xưởng in số 3'
        wifi_common.put_on_the_card(self.card, self.network.hold_profile(WIFI_PROFILE, name, WIFI_PASSWORD))
        wifi_common.with_settings_naming_a_locale_the_box_lacks(self)

        kept_wifi, _response = self.kept_wifi()

        self.assertEqual(kept_wifi, {'card_copy': True, 'profile_network': name})

    def test_diagnostic_file_claims_no_network_kept_once_it_is_forgotten(self):
        self.network.change(networks={'Workshop': WIFI_PASSWORD})
        self.join('Workshop')

        self.request(WIFI_FORGET)

        kept_wifi, _response = self.kept_wifi()
        self.assertIs(kept_wifi['card_copy'], False)

    def test_diagnostic_file_is_whole_when_nmcli_fails_or_is_missing(self):
        wifi_common.put_on_the_card(self.card, self.network.hold_profile(WIFI_PROFILE, 'Workshop', None))
        cases = {
            'nmcli fails': lambda: self.network.change(broken=['nmcli']),
            'nmcli missing': lambda: (self.network.programs / 'nmcli').unlink(missing_ok=True),
        }
        for case, break_nmcli in cases.items():
            with self.subTest(case):
                break_nmcli()

                response = self.request(box.DIAGNOSTIC)

                self.assertEqual(response.status_code, 200)
                content = json.loads(response.get_data(as_text=True))
                self.assertEqual(content['configuration']['kept_wifi'], {'card_copy': True, 'profile_network': None})
                self.assertTrue(content['status']['selftest']['lines'])
                self.assertTrue(content['log'])


class TestClockAtTheStartOfAPairedBox(common.BoxCase):

    def test_paired_box_whose_clock_no_time_server_keeps_takes_its_server_time_whatever_its_certificate_dates(self):
        expired = common.make_certificate(
            'localhost', valid_from=common.utc_now() - 40 * DAY, valid_until=common.utc_now() - DAY)
        server = self.server(certificate=expired, clock=common.utc_now() + DAY)
        common.store_box_configuration(remote_server=server.url, token=CREDENTIAL)
        common.layer('trust').trust_certificate(expired.certificate)

        asked = common.in_new_process(clock_a_paired_box_sets_at_its_start)

        self.assertTrue(asked)
        self.assertAlmostEqual(asked[0], box.timestamp(server.clock), delta=120)

    def test_paired_box_takes_no_time_from_a_server_whose_certificate_is_for_another_name(self):
        other_name = common.make_certificate('other-host.example', names=['other-host.example'])
        server = self.server(certificate=other_name, clock=common.utc_now() + DAY)
        common.store_box_configuration(remote_server=server.url, token=CREDENTIAL)
        common.layer('trust').trust_certificate(other_name.certificate)

        self.assertEqual(common.in_new_process(clock_a_paired_box_sets_at_its_start), [])
