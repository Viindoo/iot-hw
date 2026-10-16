"""The box back on the Wi-Fi network it keeps, by itself, while it runs: after a router restart, a weak signal, a
power cut where the router comes up after the box; and its access point for a person who sets the box up, whom no
retry of the box ever cuts off, while a device that only stays on the access point holds the box off its network no
longer."""
import http.client
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import common
import wifi_common

PASSWORD = 'not-a-real-password'
PROFILE = 'viindoo-wifi'
SITE_ADDRESS = '192.168.1.50'
STATION = '02:00:5e:00:60:01'
# The software box has no radio and no network of its own: each device is told apart by an address of its own on the
# loopback interface, from which it reaches the box.
STATION_ADDRESS = '127.0.0.20'
OTHER_ADDRESS = '127.0.0.30'
ACCESS_POINT_ADDRESS = '10.11.12.1'
# The ways a browser reaches the box page: through the web server of the box, or straight to the box service on its
# own port, as core's status ticket prints it.
WEB_SERVER, BOX_SERVICE = 'web server', 'box service'
# What the box page reads regularly while it is shown, and what its log page reads.
STATUS = '/viin_hw/status'
LOGS = '/hw_posbox_homepage/iot_logs'
MINUTE = 60
# How long a router takes to restart, at most: the box must not start its access point meanwhile.
ROUTER_RESTART = 5 * MINUTE
# The longest the box waits on its access point before it tries the network it keeps again.
LONGEST_WAIT = 15 * MINUTE
# `viin-box-wifi watch` in a process of its own, with its files in the directory given and its looks a second apart.
WATCH_ALONE = """
import importlib.machinery, importlib.util, sys
from pathlib import Path
loader = importlib.machinery.SourceFileLoader('box_suite_wifi_watch', sys.argv[1])
program = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(program)
files = Path(sys.argv[2])
program.KEPT_FILES, program.LOCK = (files / 'viindoo-wifi.nmconnection',), files / 'lock'
program.LEASES, program.PAGE_USE = files / 'leases', files / 'page-use.json'
program.LOOK_PERIOD, program.REJOIN_PERIODS = 1, (1,)
sys.exit(program.watch())
"""


class WatchCase(unittest.TestCase):
    """A box that keeps the network `Workshop`, its watch looking after it."""

    def setUp(self):
        common.require_software_box()
        super().setUp()
        directory = tempfile.mkdtemp(prefix='box-suite-wifi-watch-')
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        self.network = wifi_common.NetworkManager(directory)
        self.watch = wifi_common.WatchOfTheBox(self, self.network)

    def keep(self, **settings):
        self.network.hold_profile(PROFILE, 'Workshop', PASSWORD, **settings)

    def joins_of_the_network_kept(self):
        return self.watch.when('connection', 'up', 'id', PROFILE)

    def assertLessAndLessOften(self, moments):
        waits = [later - earlier for earlier, later in zip(moments, moments[1:])]
        self.assertGreaterEqual(len(waits), 2, moments)
        self.assertEqual(waits, sorted(waits), moments)
        self.assertLess(waits[0], waits[-1], moments)


class TestBoxOffTheNetworkItKeeps(WatchCase):

    def test_network_kept_that_network_manager_gave_up_on_is_joined_again_less_and_less_often_until_it_is(self):
        self.keep(blocked=True)
        # A cable keeps an address: the access point stays off, and the box only tries the network again.
        self.network.change(networks={}, addresses=SITE_ADDRESS)
        self.watch.looks(0, 15 * MINUTE)
        tries = self.joins_of_the_network_kept()

        self.network.change(networks={'Workshop': PASSWORD})
        self.watch.looks(15 * MINUTE, 30 * MINUTE)

        self.assertEqual(self.network.world()['active_ssid'], 'Workshop')
        self.assertLessEqual(tries[0], ROUTER_RESTART)
        self.assertLessAndLessOften(tries)
        joined = self.joins_of_the_network_kept()[-1]
        self.watch.looks(30 * MINUTE, 40 * MINUTE)
        self.assertEqual(self.joins_of_the_network_kept()[-1], joined)

    def test_router_that_restarts_is_joined_again_without_the_access_point_starting(self):
        self.keep(blocked=True)
        self.network.change(networks={}, addresses='')
        self.watch.looks(0, 3 * MINUTE)

        self.network.change(networks={'Workshop': PASSWORD})
        self.watch.looks(3 * MINUTE, ROUTER_RESTART)

        self.assertEqual(self.network.world()['active_ssid'], 'Workshop')
        self.assertEqual(self.network.access_point_starts(), [])

    def test_box_lost_without_any_address_starts_its_access_point_once_a_router_restart_would_be_over(self):
        self.keep()
        self.network.change(networks={}, addresses='')

        self.watch.looks(0, ROUTER_RESTART)
        self.assertEqual(self.network.access_point_starts(), [])
        self.watch.looks(ROUTER_RESTART, 20 * MINUTE)

        world = self.network.world()
        self.assertTrue(world['access_point_running'])
        self.assertTrue(world['radio_taken_from_the_access_point'])
        self.assertEqual([taking for taking in world['radio_taken_from_the_access_point'] if taking], [])

    def test_box_with_another_address_never_starts_its_access_point(self):
        self.keep()
        self.network.change(networks={}, addresses=SITE_ADDRESS)

        self.watch.looks(0, 30 * MINUTE)

        self.assertEqual(self.network.access_point_starts(), [])


class TestNetworkForgotten(WatchCase):

    def test_network_forgotten_is_no_longer_looked_for(self):
        cases = {
            'box off the network': lambda: (self.keep(blocked=True), self.network.change(addresses='')),
            'box on its access point': lambda: (
                self.keep(autoconnect=False), self.network.change(on_access_point=True, access_point_running=True)),
        }
        for case, arrange in cases.items():
            with self.subTest(case):
                self.network.change(networks={'Workshop': PASSWORD}, active=None, active_ssid=None)
                self.watch.start_again()
                arrange()
                # The box page removes the copy on the card; the profile in /etc stays until the box restarts.
                self.watch.card_copy.unlink(missing_ok=True)
                looked = len(self.watch.history)

                self.watch.looks(0, 30 * MINUTE)

                changes = [call for _at, call in self.watch.history[looked:]
                           if 'up' in call or 'scan' in call or call[-2:] == ['hostapd', 'stop']
                           or 'wireless_ap.sh' in call[0]]
                self.assertEqual(changes, [])
                self.assertIsNone(self.network.world()['active'])


class TestBoxOnItsAccessPoint(WatchCase):
    """The box service is started in this process and served on its own port, behind the web server of the box."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        common.start()
        common.serve()

    def setUp(self):
        super().setUp()
        self.page_use = common.layer('page_use')
        self.enterContext(mock.patch.object(self.page_use, 'RECORD', self.watch.page_use))
        self.box_service_started()
        self.keep(autoconnect=False)
        self.network.change(on_access_point=True, access_point_running=True)

    def box_service_started(self):
        """The box service starts: nobody has used the box page since."""
        self.watch.page_use.unlink(missing_ok=True)
        self.page_use.begin()

    def use_the_page(self, address, way=WEB_SERVER, path=STATUS, host='127.0.0.1', sent=None):
        """The status of the HTTP answer to a read of the box from the device at `address`, the way `way`. `host`
        is the name the browser asked for, `sent` the headers it added."""
        import odoo

        port = 80 if way == WEB_SERVER else odoo.tools.config['http_port']
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=60, source_address=(address, 0))
        try:
            connection.request('GET', path, headers={'Host': host, **(sent or {})})
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()

    def uses_the_page(self, address, way=WEB_SERVER, path=STATUS, **options):
        self.assertEqual(self.use_the_page(address, way, path, **options), 200)

    def lease(self, ends):
        """An address the access point lent to STATION, as dnsmasq keeps it, that ends at `ends`."""
        self.watch.leases.write_text('', encoding='utf-8')
        self.watch.lend(STATION, STATION_ADDRESS, ends)

    def radio_taken(self, looked=0):
        """The calls of the looks after the first `looked` calls that took the radio from the access point."""
        return [call for _at, call in self.watch.history[looked:]
                if call[:1] == ['iw'] and 'scan' in call or call[-2:] == ['hostapd', 'stop']
                or call[:1] == ['nmcli'] and 'up' in call]

    def assertBackOnTheNetworkKept(self):
        world = self.network.world()
        self.assertEqual(world['active_ssid'], 'Workshop')
        self.assertFalse(world['access_point_running'])
        self.assertFalse(world['on_access_point'])
        self.assertTrue(self.network.held()[PROFILE]['autoconnect'])

    def test_box_rejoins_the_network_it_keeps_once_it_hears_it_and_restarts_the_box_service(self):
        cases = {
            'network announced': lambda: self.network.change(networks={'Workshop': PASSWORD}),
            'hidden network': lambda: self.network.change(networks={'Workshop': PASSWORD}, access_points=[]),
            'radio that cannot tell who is connected, lease over': lambda: (
                self.network.change(networks={'Workshop': PASSWORD}, stations=None), self.lease(int(time.time()) - 60)),
        }
        for case, arrange in cases.items():
            with self.subTest(case):
                self.network.change(active=None, active_ssid=None, on_access_point=True, access_point_running=True,
                                    access_points=None, stations=[])
                self.watch.start_again()
                arrange()

                self.watch.looks(0, 15 * MINUTE)

                self.assertBackOnTheNetworkKept()
                self.assertTrue(self.watch.when('service', 'odoo', 'restart'))

    def test_network_kept_with_a_vietnamese_name_is_heard_and_rejoined_whatever_locale_the_settings_of_the_box_name(
            self):
        name = 'Xưởng in số 3'
        self.network.hold_profile(PROFILE, name, PASSWORD, autoconnect=False)
        self.network.change(networks={name: PASSWORD})
        wifi_common.with_settings_naming_a_locale_the_box_lacks(self)

        self.watch.looks(0, 15 * MINUTE)

        self.assertEqual(self.network.world()['active_ssid'], name)
        self.assertFalse(self.network.world()['access_point_running'])

    def test_person_who_uses_the_box_page_through_the_access_point_is_never_cut_off(self):
        cases = {
            'page through the web server of the box': lambda: self.uses_the_page(STATION_ADDRESS, WEB_SERVER),
            'page straight from the box service, at the address of the status ticket': lambda: self.uses_the_page(
                STATION_ADDRESS, BOX_SERVICE),
            'log page straight from the box service': lambda: self.uses_the_page(STATION_ADDRESS, BOX_SERVICE, LOGS),
            'log page through the web server of the box': lambda: self.uses_the_page(STATION_ADDRESS, WEB_SERVER, LOGS),
            'radio that cannot tell who is connected': lambda: (
                self.uses_the_page(STATION_ADDRESS), self.network.change(stations=None)),
            'box service that keeps no record of the use of the page': lambda: (
                self.uses_the_page(STATION_ADDRESS), self.watch.page_use.unlink()),
        }
        for case, arrange in cases.items():
            with self.subTest(case):
                self.network.change(active=None, active_ssid=None, on_access_point=True, access_point_running=True,
                                    networks={'Workshop': PASSWORD}, stations=[STATION])
                self.watch.start_again()
                self.box_service_started()
                self.lease(int(time.time()) + 3600)
                arrange()
                looked = len(self.watch.history)

                self.watch.looks(0, 30 * MINUTE)

                self.assertEqual(self.radio_taken(looked), [])
                self.assertTrue(self.network.world()['access_point_running'])

    def test_person_who_uses_the_box_page_is_not_cut_off_by_a_restart_of_the_box_service(self):
        self.network.change(networks={'Workshop': PASSWORD}, stations=[STATION])
        self.lease(int(time.time()) + 3600)
        self.uses_the_page(STATION_ADDRESS)
        self.page_use.begin()
        looked = len(self.watch.history)

        self.watch.looks(0, 30 * MINUTE)

        self.assertEqual(self.radio_taken(looked), [])
        self.assertTrue(self.network.world()['access_point_running'])

    def test_person_who_connects_while_the_box_listens_is_not_cut_off(self):
        self.network.change(networks={'Workshop': PASSWORD}, connects_during_scan=STATION)

        self.watch.looks(0, 5 * MINUTE)

        self.assertTrue(self.watch.when('iw', 'dev', 'wlan0', 'scan'))
        self.assertEqual(self.watch.when('hostapd', 'stop'), [])
        self.assertTrue(self.network.world()['access_point_running'])

    def test_device_that_only_stays_on_the_access_point_holds_the_box_off_its_network_no_longer(self):
        cases = {
            'device that never opened the box page': lambda: None,
            'device that used the box page long ago': lambda: (
                self.uses_the_page(STATION_ADDRESS), setattr(self.watch.clock, 'ahead', 11 * MINUTE)),
            'device whose system asks other sites whether it reaches the internet': lambda: self.assertEqual(
                self.use_the_page(STATION_ADDRESS, path='/generate_204', host='connectivitycheck.example'), 302),
            'device whose browser opens another site, which the box sends on to its page': lambda: self.assertEqual(
                self.use_the_page(STATION_ADDRESS, path='/', host='another-site.example'), 302),
            'radio that cannot tell who is connected': lambda: self.network.change(stations=None),
            'person who used the box page from another address': lambda: self.uses_the_page(OTHER_ADDRESS),
            'device that names the one connected, straight to the box service': lambda: self.uses_the_page(
                OTHER_ADDRESS, BOX_SERVICE, sent={'X-Real-IP': STATION_ADDRESS}),
            'device that names the one connected, through the web server of the box': lambda: self.uses_the_page(
                OTHER_ADDRESS, WEB_SERVER, sent={'X-Real-IP': STATION_ADDRESS}),
        }
        for case, arrange in cases.items():
            with self.subTest(case):
                self.network.change(active=None, active_ssid=None, on_access_point=True, access_point_running=True,
                                    networks={'Workshop': PASSWORD}, stations=[STATION])
                self.watch.start_again()
                self.box_service_started()
                self.lease(int(time.time()) + 3600)
                arrange()

                self.watch.looks(0, 15 * MINUTE)

                self.assertBackOnTheNetworkKept()

    def test_device_that_drops_off_the_access_point_now_and_then_holds_the_box_off_its_network_no_longer(self):
        self.network.change(networks={'Workshop': PASSWORD}, stations=[STATION])
        self.lease(int(time.time()) + 3600)

        for at in range(0, 60 * MINUTE, self.watch.program.LOOK_PERIOD):
            if at and not at % (9 * MINUTE):
                # Away at this look, back while the box listens: a phone that connects again now and then.
                self.network.change(stations=[], connects_during_scan=STATION)
            self.watch.look(at)

        self.assertBackOnTheNetworkKept()

    def test_person_who_comes_back_after_a_while_has_the_time_to_open_the_box_page_again(self):
        self.network.change(networks={}, stations=[STATION])
        self.lease(int(time.time()) + 3600)
        self.watch.looks(0, 2 * MINUTE)
        self.network.change(stations=[])
        self.watch.looks(2 * MINUTE, 20 * MINUTE)
        self.network.change(networks={'Workshop': PASSWORD}, stations=[STATION])
        looked = len(self.watch.history)

        self.watch.looks(20 * MINUTE, 29 * MINUTE)

        self.assertTrue(self.network.world()['access_point_running'])
        self.assertEqual(self.radio_taken(looked), [])

    def test_network_kept_not_heard_is_listened_for_again_and_joined_once_it_is_back(self):
        self.network.change(networks={})
        self.watch.looks(0, LONGEST_WAIT - MINUTE)

        self.assertGreaterEqual(len(self.watch.when('iw', 'dev', 'wlan0', 'scan')), 2)
        self.assertEqual(self.watch.when('hostapd', 'stop'), [])

        self.network.change(networks={'Workshop': PASSWORD})
        self.watch.looks(LONGEST_WAIT - MINUTE, 40 * MINUTE)

        self.assertBackOnTheNetworkKept()

    def test_radio_that_hears_nothing_while_it_serves_the_access_point_still_brings_the_box_back(self):
        self.network.change(networks={'Workshop': PASSWORD}, deaf_on_access_point=True)

        self.watch.looks(0, 20 * MINUTE)

        self.assertBackOnTheNetworkKept()
        self.assertLessEqual(self.watch.when('hostapd', 'stop')[0], LONGEST_WAIT + MINUTE)

    def test_network_never_heard_is_tried_anyway_no_more_often_than_the_longest_wait(self):
        self.network.change(networks={})

        self.watch.looks(0, 60 * MINUTE)

        tries = self.watch.when('hostapd', 'stop')
        self.assertGreaterEqual(len(tries), 2)
        self.assertGreaterEqual(min(later - earlier for earlier, later in zip(tries, tries[1:])), LONGEST_WAIT)
        world = self.network.world()
        self.assertTrue(world['access_point_running'])
        self.assertEqual([taking for taking in world['radio_taken_from_the_access_point'] if taking], [])

    def test_cable_plugged_in_while_the_access_point_serves_stops_it_unless_somebody_sets_the_box_up(self):
        cases = {
            'nobody connected': (lambda: None, False),
            'person who uses the box page through the access point': (lambda: (
                self.network.change(stations=[STATION]), self.lease(int(time.time()) + 3600),
                self.uses_the_page(STATION_ADDRESS)), True),
        }
        for case, (arrange, serves) in cases.items():
            with self.subTest(case):
                self.network.change(networks={}, on_access_point=True, access_point_running=True,
                                    dnsmasq_running=True, stations=[], addresses=ACCESS_POINT_ADDRESS)
                self.watch.start_again()
                self.watch.look(0)
                arrange()
                self.network.change(addresses=f'{ACCESS_POINT_ADDRESS} {SITE_ADDRESS}')

                self.watch.looks(MINUTE, 30 * MINUTE if serves else 2 * MINUTE)

                world = self.network.world()
                self.assertEqual(
                    (world['access_point_running'], world['on_access_point'], world['dnsmasq_running']),
                    (serves, serves, serves))
                self.assertEqual(self.network.access_point_starts(), [])

    def test_join_that_fails_from_the_access_point_starts_it_again_and_is_tried_less_and_less_often(self):
        self.network.change(networks={'Workshop': 'password-changed-on-the-router'})

        self.watch.looks(0, 40 * MINUTE)

        world = self.network.world()
        self.assertTrue(world['access_point_running'])
        self.assertTrue(world['on_access_point'])
        self.assertLessAndLessOften(self.watch.when('hostapd', 'stop'))
        starts = world['radio_taken_from_the_access_point']
        self.assertEqual(len(starts), len(self.watch.when('hostapd', 'stop')))
        self.assertEqual([taking for taking in starts if taking], [])


class TestWifiHeldUntilWhatTheWatchStartedIsOver(WatchCase):
    """The access point the watch starts and the restart of the box service it asks for are over before the watch
    lets go of the Wi-Fi: a join asked from the box page meanwhile would be cut off by that restart."""

    def test_box_service_restarts_and_access_point_starts_while_the_watch_still_holds_the_wifi(self):
        on_access_point = {'on_access_point': True, 'access_point_running': True}
        cases = {
            'join from the access point': ({**on_access_point, 'networks': {'Workshop': PASSWORD}}, 'box service'),
            'join from the access point that fails': (
                {**on_access_point, 'networks': {'Workshop': 'password-changed-on-the-router'}}, 'access point script'),
            'box long without any address': ({'networks': {}, 'addresses': ''}, 'access point script'),
        }
        for case, (world, started) in cases.items():
            with self.subTest(case):
                self.keep(autoconnect=False)
                self.network.change(**{'on_access_point': False, 'access_point_running': False, 'active': None,
                                       'active_ssid': None, **world})
                self.watch.start_again()
                restarted = len(self.network.restarts())

                self.watch.looks(0, 15 * MINUTE)

                restarts = self.network.restarts()[restarted:]
                self.assertIn(started, [what for what, _held in restarts])
                self.assertEqual([what for what, held in restarts if not held], [])


class TestWatchAskedToStop(unittest.TestCase):
    """The watch is stopped and started again when an update installs another program of it: it ends the look it is
    in first, so that it never leaves the Wi-Fi halfway."""

    def setUp(self):
        common.require_software_box()
        directory = tempfile.mkdtemp(prefix='box-suite-wifi-watch-stop-')
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        self.network = wifi_common.NetworkManager(directory)
        self.files = self.network.directory / 'watch'
        self.files.mkdir()
        (self.files / 'viindoo-wifi.nmconnection').write_text('')

    def test_watch_asked_to_stop_during_a_join_ends_the_join_first(self):
        self.network.hold_profile(PROFILE, 'Workshop', PASSWORD, blocked=True)
        self.network.change(networks={'Workshop': PASSWORD}, addresses='', hold='every join')
        command = [sys.executable, '-I', '-c', WATCH_ALONE, str(wifi_common.WIFI_PROGRAM), str(self.files)]
        watch = subprocess.Popen(command, env=self.network.environment(), stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self.addCleanup(watch.kill)
        common.wait_until(lambda: ['connection', 'up', 'id', PROFILE] in [
            call[3:7] for call in self.network.calls() if call[:1] == ['nmcli']], 'the watch to join the network kept')

        watch.send_signal(signal.SIGTERM)
        time.sleep(1)
        self.assertIsNone(watch.poll())
        self.network.release()

        self.assertEqual(watch.wait(common.PROCESS_TIMEOUT), 0)
        self.assertEqual(self.network.world()['active_ssid'], 'Workshop')
        self.assertTrue(self.network.held()[PROFILE]['autoconnect'])
