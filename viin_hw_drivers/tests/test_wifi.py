"""The Wi-Fi network a person has the box join: what reaches NetworkManager, what the box keeps on its card to join
it again as it starts, what it does when a join fails, what it refuses, and what it keeps secret."""
import configparser
import contextlib
import hashlib
import json
import logging
import os
import shutil
import string
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import common
import services_common as box
import wifi_common

WIFI = '/hw_posbox_homepage/update_wifi'
WIFI_CLEAR = '/hw_posbox_homepage/wifi_clear'
CREDENTIAL = 'suite-credential-wifi'
PASSWORD = 'not-a-real-password'
# The name of the profile of the network the box keeps, which the program that runs as the box starts looks for.
PROFILE = 'viindoo-wifi'
START_PROGRAM = box.SYSTEM_FILES / 'usr' / 'local' / 'sbin' / 'viin-box-wifi'
START_UNIT = box.SYSTEM_FILES / 'etc' / 'systemd' / 'system' / 'viin-box-wifi.service'
INSTALLER = box.SYSTEM_FILES / 'apply.sh'
# Core's script that rc.local runs as the box starts: it starts the access point of the box when the box has no
# address.
ACCESS_POINT_SCRIPT = 'point_of_sale/tools/posbox/configuration/wireless_ap.sh'
SITE_ADDRESS = '192.168.1.50'
# Every sign of ASCII, then letters up to the longest password WPA takes: 63 characters.
EVERY_SIGN = (string.punctuation + string.ascii_letters)[:63]

# Name and password of networks a person may join, each with a character or a form a careless keeper loses or changes.
LEGITIMATE = {
    'plain password': ('Workshop', PASSWORD),
    'shortest password': ('Workshop', 'pa55word'),
    'longest password, every sign': ('Workshop', EVERY_SIGN),
    'slash': ('Workshop', 'pass/word/1'),
    'ampersand': ('Workshop', 'pass&word&2'),
    'backslash': ('Workshop', 'pass\\word\\3'),
    'equals sign': ('Workshop', 'pass=word==4'),
    'percent sign': ('Workshop', '100%pass%(word)s'),
    'hash sign': ('Workshop', '#pass#word'),
    'semicolon': ('Workshop', 'pass;word;5'),
    'quotes': ('Workshop', 'it\'s "the" password'),
    'spaces at either end of the password': ('Workshop', '  pass word  '),
    'hexadecimal key': ('Workshop', '0123456789abcdef' * 4),
    'text that reads as an escape': ('Workshop', '\\040literal\\n'),
    'backslash at the end': ('Workshop', 'ends with a backslash\\'),
    'colon': ('Workshop', 'a:b=c:d'),
    'password in Vietnamese': ('Workshop', 'Mật khẩu số 3'),
    'open network': ('Workshop', ''),
    'name with spaces': ('Shop floor 2', PASSWORD),
    'name with quotes': ('Bob\'s "shop"', PASSWORD),
    'name with signs of a command line': ('Shop;$HOME&`x`\\', PASSWORD),
    'name in Vietnamese': ('Xưởng in số 3', PASSWORD),
    'name of 32 bytes': ('É' * 16, PASSWORD),
    'spaces at either end of the name': (' Shop ', PASSWORD),
}


def as_typed(password):
    """The password of a network as NetworkManager is to hold it: none for an open network."""
    return password or None


class WifiCase(common.BoxCase):
    """A box without a server, with a radio, whose person may join a network without an unlock proof."""

    answer_delay = 0

    def setUp(self):
        super().setUp()
        self.network = wifi_common.network_manager(self, answer_delay=self.answer_delay)
        common.store_box_configuration(wifi_ssid='', wifi_password='')
        self.card, self.netplan_card = wifi_common.card_copies()

    def join(self, name, password, proof=None, **parameters):
        """The answer of the route, once the join it started has ended."""
        with wifi_common.settled():
            return self.call(WIFI, proof=proof, essid=name, password=password, **parameters)

    def kept(self):
        """(name, password) of the network the box joins again as it starts: the one of its copy on the card."""
        copy = wifi_common.on_the_card(self.card)
        return copy and (copy['ssid'], copy.get('psk'))

    def keep_earlier(self, name, password):
        """Arrange a network the box joined and kept before: NetworkManager holds it and its copy is on the card."""
        profile = self.network.hold_profile(PROFILE, name, password)
        wifi_common.put_on_the_card(self.card, profile)
        return profile


class TestNetworkTheBoxKeeps(WifiCase):

    def test_network_joined_and_kept_reaches_network_manager_as_typed_and_is_joined_again_as_typed(self):
        for case, (name, password) in LEGITIMATE.items():
            with self.subTest(case):
                self.network.change(networks={name: as_typed(password)})
                joins = len(self.network.joined())

                answer = self.join(name, password, persistent=True)

                self.assertEqual(answer.get('status'), 'success', answer)
                self.assertEqual(self.network.joined()[joins:], [(name, as_typed(password))])
                self.assertEqual(self.network.world()['active_ssid'], name)
                self.assertEqual(self.kept(), (name, as_typed(password)))

    def test_network_kept_is_on_the_card_for_root_alone(self):
        self.network.change(networks={'Workshop': PASSWORD})

        self.join('Workshop', PASSWORD, persistent=True)

        self.assertTrue(self.card.exists())
        found = self.card.stat()
        self.assertEqual((found.st_uid, found.st_mode & 0o777), (0, 0o600))

    def test_network_kept_on_a_system_whose_profiles_netplan_holds_is_kept_where_netplan_reads_it(self):
        self.network.change(networks={'Workshop': PASSWORD}, netplan=True)

        self.join('Workshop', PASSWORD, persistent=True)

        copy = wifi_common.on_the_card(self.netplan_card) or {}
        self.assertEqual((copy.get('ssid'), copy.get('psk')), ('Workshop', PASSWORD))
        self.assertIsNone(wifi_common.on_the_card(self.card))

    def test_network_joined_without_keeping_it_leaves_the_network_kept_before(self):
        for case, parameters in {'not to keep': {'persistent': False}, 'keeping left out': {}}.items():
            with self.subTest(case):
                self.keep_earlier('Office', 'office-password')
                self.network.change(networks={'Office': 'office-password', 'Workshop': PASSWORD})

                self.join('Workshop', PASSWORD, **parameters)

                self.assertEqual(self.network.world()['active_ssid'], 'Workshop')
                self.assertEqual(self.kept(), ('Office', 'office-password'))
                self.assertIsNone(wifi_common.on_the_card(self.netplan_card))

    def test_network_kept_takes_the_place_of_the_one_core_keeps(self):
        common.store_box_configuration(wifi_ssid='Network core keeps', wifi_password='kept-by-core')
        self.network.change(networks={'Workshop': PASSWORD})

        self.join('Workshop', PASSWORD, persistent=True)

        stored = common.box_configuration()
        self.assertEqual((stored.get('wifi_ssid', ''), stored.get('wifi_password', '')), ('', ''))
        self.assertEqual(self.kept(), ('Workshop', PASSWORD))

    def test_network_joined_restarts_the_box_service_once(self):
        self.network.change(networks={'Workshop': PASSWORD})

        self.join('Workshop', PASSWORD, persistent=True)

        self.assertEqual(len(common.restarts), 1)

    def test_box_on_its_own_access_point_leaves_it_to_join_the_network(self):
        self.network.change(networks={'Workshop': PASSWORD}, on_access_point=True, access_point_running=True)

        self.join('Workshop', PASSWORD, persistent=True)

        world = self.network.world()
        self.assertEqual(world['active_ssid'], 'Workshop')
        self.assertFalse(world['on_access_point'])
        self.assertEqual(self.kept(), ('Workshop', PASSWORD))

    def test_of_several_joins_asked_in_a_row_the_last_one_asked_is_the_one_that_runs(self):
        networks = {'First': 'first-password', 'Second': 'second-password', 'Third': 'third-password'}
        self.network.change(networks=networks, hold=True)

        with wifi_common.settled():
            self.call(WIFI, essid='First', password=networks['First'], persistent=True)
            common.wait_until(self.network.joins, 'the first join to reach NetworkManager')
            for name in ('Second', 'Third'):
                self.call(WIFI, essid=name, password=networks[name], persistent=True)
            self.network.release()

        self.assertEqual(self.network.joined(), [('First', 'first-password'), ('Third', 'third-password')])
        self.assertEqual(self.network.world()['active_ssid'], 'Third')
        self.assertEqual(self.kept(), ('Third', 'third-password'))


class TestJoinThatFails(WifiCase):

    def test_password_typed_wrong_keeps_nothing_and_leaves_the_network_kept_before(self):
        common.store_box_configuration(wifi_ssid='Network core keeps', wifi_password='kept-by-core')
        configuration = common.CONFIGURATION.read_bytes()
        self.keep_earlier('Office', 'office-password')
        self.network.change(networks={'Office': 'office-password', 'Workshop': PASSWORD})

        self.join('Workshop', 'typed-wrong', persistent=True)

        self.assertEqual(self.kept(), ('Office', 'office-password'))
        self.assertEqual(self.network.held().get(PROFILE, {}).get('ssid'), 'Office')
        self.assertEqual(common.CONFIGURATION.read_bytes(), configuration)

    def test_join_that_fails_brings_back_the_network_kept_before_and_starts_no_access_point(self):
        self.keep_earlier('Office', 'office-password')
        self.network.change(networks={'Office': 'office-password', 'Workshop': PASSWORD})

        self.join('Workshop', 'typed-wrong', persistent=True)

        self.assertEqual(self.network.world()['active_ssid'], 'Office')
        self.assertEqual(self.network.access_point_starts(), [])

    def test_join_that_fails_on_a_box_that_keeps_no_network_starts_the_access_point(self):
        self.network.change(networks={'Workshop': PASSWORD})

        self.join('Workshop', 'typed-wrong', persistent=True)

        self.assertEqual(self.network.access_point_starts(), [['force']])
        self.assertIsNone(self.kept())

    def test_join_that_fails_on_a_box_left_without_an_address_keeps_network_manager_off_the_radio_of_the_access_point(
            self):
        self.keep_earlier('Office', 'office-password')
        self.network.change(networks={'Workshop': PASSWORD}, addresses='')

        self.join('Workshop', 'typed-wrong', persistent=True)

        self.assertEqual(self.network.world()['radio_taken_from_the_access_point'], [[]])
        # In /etc alone, which lives in memory: the box joins the network kept again as it starts.
        self.assertIs(wifi_common.on_the_card(self.card)['autoconnect'], True)

    def test_join_that_fails_on_a_box_with_another_address_leaves_network_manager_to_join_the_network_kept(self):
        self.keep_earlier('Office', 'office-password')
        self.network.change(networks={'Workshop': PASSWORD}, addresses=f'{SITE_ADDRESS} ')

        self.join('Workshop', 'typed-wrong', persistent=True)

        self.assertTrue(self.network.held()[PROFILE]['autoconnect'])
        self.assertEqual(self.network.access_point_starts(), [['force']])

    def test_join_that_fails_starts_the_access_point_when_the_network_kept_is_out_of_reach_too(self):
        self.keep_earlier('Office', 'office-password')
        self.network.change(networks={'Workshop': PASSWORD})

        self.join('Workshop', 'typed-wrong', persistent=True)

        self.assertEqual(self.network.access_point_starts(), [['force']])
        self.assertEqual(self.kept(), ('Office', 'office-password'))


class TestWhyAJoinFailed(WifiCase):
    """The box says why a join did not come to be: the page tells it, and the self-test reads it."""

    def setUp(self):
        super().setUp()
        self.wifi = common.layer('wifi')

    def test_password_typed_wrong_is_reported_as_password_refused(self):
        self.network.change(networks={'Workshop': PASSWORD})

        self.join('Workshop', 'typed-wrong', persistent=True)

        self.assertEqual(self.wifi.last_join()['result'], 'password_refused')
        self.assertEqual(self.wifi.last_join()['ssid'], 'Workshop')

    def test_network_nobody_hears_is_reported_as_not_reached(self):
        self.network.change(networks={})

        self.join('Workshop', PASSWORD, persistent=True)

        self.assertEqual(self.wifi.last_join()['result'], 'not_reached')

    def test_network_joined_is_reported_as_joined_with_its_name_and_the_time(self):
        self.network.change(networks={'Workshop': PASSWORD})
        before = time.time()

        self.join('Workshop', PASSWORD, persistent=True)

        record = self.wifi.last_join()
        self.assertEqual((record['ssid'], record['result']), ('Workshop', 'joined'))
        self.assertTrue(before - 1 <= record['at'] <= time.time() + 1, record)

    def test_join_asked_while_the_box_is_busy_is_reported_as_busy(self):
        watch = wifi_common.WatchOfTheBox(self, self.network)
        self.enterContext(mock.patch.multiple(self.wifi, LOCK=watch.lock, HOLD_WAIT=1))
        self.network.change(networks={'Workshop': PASSWORD})

        with watch.program.locked() as held:
            self.assertTrue(held)
            self.join('Workshop', PASSWORD, persistent=True)

        self.assertEqual(self.wifi.last_join()['result'], 'busy')

    def test_the_latest_join_is_the_one_reported(self):
        self.network.change(networks={'Workshop': PASSWORD})
        self.join('Workshop', 'typed-wrong', persistent=True)

        self.join('Workshop', PASSWORD, persistent=True)

        self.assertEqual(self.wifi.last_join()['result'], 'joined')

    def test_no_join_yet_or_a_record_that_cannot_be_read_is_none(self):
        self.assertIsNone(self.wifi.last_join())
        for case, content in {'not a record': 'garbage', 'unknown result': '{"ssid": "A", "result": "x", "at": 1}',
                              'no time': '{"ssid": "A", "result": "joined"}'}.items():
            with self.subTest(case):
                self.wifi.LAST_JOIN.write_text(content)

                self.assertIsNone(self.wifi.last_join())

    def test_network_not_kept_is_not_brought_up_when_a_join_fails(self):
        self.network.change(networks={'Workshop': PASSWORD})

        self.join('Workshop', 'typed-wrong', persistent=True)

        words = ['connection', 'up', 'id', PROFILE]
        self.assertEqual([call for call in self.network.nmcli_calls()
                          if any(call[place:place + 4] == words for place in range(len(call)))], [])
        self.assertEqual(self.network.access_point_starts(), [['force']])

    def test_network_kept_is_brought_up_when_a_join_fails(self):
        self.keep_earlier('Office', 'office-password')
        self.network.change(networks={'Office': 'office-password', 'Workshop': PASSWORD})

        self.join('Workshop', 'typed-wrong', persistent=True)

        self.assertEqual(self.network.world()['active_ssid'], 'Office')
        self.assertEqual(self.wifi.last_join()['result'], 'password_refused')


class TestWhatTheBoxRefuses(WifiCase):

    def test_name_or_password_no_network_can_have_is_refused_and_nothing_is_joined(self):
        common.store_box_configuration(wifi_ssid='Network core keeps', wifi_password='kept-by-core')
        configuration = common.CONFIGURATION.read_bytes()
        self.keep_earlier('Office', 'office-password')
        refused = {
            'empty name': ('', PASSWORD, 'invalid_name'),
            'name longer than 32 bytes': ('É' * 17, PASSWORD, 'invalid_name'),
            'name that is no text': (42, PASSWORD, 'invalid_name'),
            'line break in the name': ('Workshop\nwifi_password = x', PASSWORD, 'invalid_name'),
            'tab in the name': ('Work\tshop', PASSWORD, 'invalid_name'),
            'null character in the name': ('Work\x00shop', PASSWORD, 'invalid_name'),
            'half of a character in the name': ('Workshop\udc80', PASSWORD, 'invalid_name'),
            'line break in the password': ('Workshop', 'x\ntouch made-by-a-line-break', 'invalid_password'),
            'tab in the password': ('Workshop', 'pass\tword', 'invalid_password'),
            'escape character in the password': ('Workshop', 'pass\x1bword', 'invalid_password'),
            'null character in the password': ('Workshop', 'pass\x00word', 'invalid_password'),
            'half of a character in the password': ('Workshop', 'pass\udc80word', 'invalid_password'),
            'password that is no text': ('Workshop', ['pass', 'word'], 'invalid_password'),
        }
        for case, (name, password, reason) in refused.items():
            with self.subTest(case):
                answer = self.join(name, password, persistent=True)

                self.assertEqual(answer, {'status': 'refused', 'reason': reason})
                self.assertEqual(self.network.nmcli_calls(), [])
                self.assertEqual(self.kept(), ('Office', 'office-password'))
                self.assertEqual(common.CONFIGURATION.read_bytes(), configuration)

    def test_attempt_to_run_a_command_or_to_add_a_setting_through_the_name_or_the_password_runs_nothing(self):
        # Short enough a folder for the name to hold the whole command in its 32 bytes.
        folder = tempfile.mkdtemp(prefix='w', dir='/tmp')
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        configuration = common.CONFIGURATION.read_bytes()
        attempts = {
            'command of the editor core keeps a network with': (
                f'x;touch {folder}/n1;#', f'x;touch {folder}/p1;#/e;s/a/b'),
            'command substitution': (f'$(touch {folder}/n2)', f'$(touch {folder}/p2)'),
            'backquotes': (f'`touch {folder}/n3`', f'`touch {folder}/p3`'),
            'quotes': (f'";touch {folder}/n4;"', f'\';touch {folder}/p4;\''),
            'setting of the configuration': ('Shop] [options', 'x = y ; wifi_ssid = z'),
        }
        for case, (name_attempt, password_attempt) in attempts.items():
            for name, password in ((name_attempt, PASSWORD), ('Workshop', password_attempt)):
                with self.subTest(case, name=name, password=password):
                    self.network.change(networks={name: password})

                    answer = self.join(name, password, persistent=True)

                    self.assertEqual(answer.get('status'), 'success', answer)
                    self.assertEqual(os.listdir(folder), [])
                    self.assertEqual(common.CONFIGURATION.read_bytes(), configuration)
                    self.assertEqual(self.network.joined()[-1], (name, password))
                    self.assertEqual(self.kept(), (name, password))


class TestNetworkForgotten(WifiCase):

    def test_network_forgotten_is_removed_from_every_place_the_box_keeps_it(self):
        self.network.change(networks={'Workshop': PASSWORD})
        self.join('Workshop', PASSWORD, persistent=True)
        wifi_common.put_on_the_card(self.netplan_card, {'id': PROFILE, 'ssid': 'Office', 'psk': 'office-password'})
        common.store_box_configuration(wifi_ssid='Network core keeps', wifi_password='kept-by-core')

        response = self.request(WIFI_CLEAR)

        self.assertEqual(response.status_code, 200)
        self.assertEqual([path for path in (self.card, self.netplan_card) if path.exists()], [])
        stored = common.box_configuration()
        self.assertEqual((stored.get('wifi_ssid', ''), stored.get('wifi_password', '')), ('', ''))


class TestNetworkForgottenWhileItIsJoined(WifiCase):
    """Forget, pressed while a join runs, wins over the keep that join was asked for."""

    def test_network_forgotten_while_it_is_being_joined_is_not_kept(self):
        self.keep_earlier('Office', 'office-password')
        self.network.change(networks={'Workshop': PASSWORD}, hold=True)

        with wifi_common.settled():
            self.call(WIFI, essid='Workshop', password=PASSWORD, persistent=True)
            common.wait_until(self.network.joins, 'the join to reach NetworkManager')
            response = self.request(WIFI_CLEAR)
            self.network.release()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.network.world()['active_ssid'], 'Workshop')
        self.assertEqual([path for path in (self.card, self.netplan_card) if path.exists()], [])

    def test_network_joined_after_a_forget_is_kept(self):
        self.keep_earlier('Office', 'office-password')
        self.request(WIFI_CLEAR)
        self.network.change(networks={'Workshop': PASSWORD})

        self.join('Workshop', PASSWORD, persistent=True)

        self.assertEqual(self.kept(), ('Workshop', PASSWORD))


class TestNetworkFoundByItsName(WifiCase):
    """The box finds the network it is asked to join among those its radio hears, by the very name it was given."""

    # An access point whose address is spelled as a network name may be: a name of that spelling is no address.
    OTHER = {'bssid': 'AA:BB:CC:DD:EE:01', 'signal': 90, 'ssid': 'Other network'}

    def test_network_whose_name_nmcli_would_read_otherwise_is_joined_as_exactly_that_network(self):
        names = {
            'name of an option of nmcli': '--ask',
            'short option of nmcli': '-s',
            'whole option of nmcli': '--show-secrets',
            'name spelled as the address of an access point': self.OTHER['bssid'],
            'colon in the name': 'Shop:2',
            'backslash in the name': 'Shop\\2\\',
            'backslash before a colon in the name': 'Shop\\:2',
        }
        for case, name in names.items():
            with self.subTest(case, name=name):
                heard = {'bssid': '02:00:5E:00:53:10', 'signal': 60, 'ssid': name}
                self.network.change(networks={name: PASSWORD, self.OTHER['ssid']: PASSWORD},
                                    access_points=[self.OTHER, heard])
                joins = len(self.network.joined())

                self.join(name, PASSWORD, persistent=True)

                self.assertEqual(self.network.joined()[joins:], [(name, PASSWORD)])
                self.assertEqual(self.network.world()['active_ssid'], name)
                self.assertEqual(self.kept(), (name, PASSWORD))

    def test_strongest_access_point_announcing_the_name_is_joined_and_none_announcing_another_name(self):
        strongest = {'bssid': '02:00:5E:00:53:22', 'signal': 75, 'ssid': 'Shop:2'}
        heard = [
            {'bssid': '02:00:5E:00:53:21', 'signal': 40, 'ssid': 'Shop:2'},
            strongest,
            {'bssid': '02:00:5E:00:53:31', 'signal': 99, 'ssid': 'Shop:2-5G'},
            {'bssid': '02:00:5E:00:53:32', 'signal': 98, 'ssid': 'Shop'},
            {'bssid': '02:00:5E:00:53:33', 'signal': 97, 'ssid': 'Shop\\:2'},
            {'bssid': '02:00:5E:00:53:34', 'signal': 96, 'ssid': 'Shop:2 '},
        ]
        self.network.change(networks={found['ssid']: PASSWORD for found in heard}, access_points=heard)

        self.join('Shop:2', PASSWORD, persistent=True)

        world = self.network.world()
        self.assertEqual((world['active_bssid'], world['active_ssid']), (strongest['bssid'], 'Shop:2'))
        self.assertEqual(self.kept(), ('Shop:2', PASSWORD))

    def test_network_no_access_point_announces_is_joined_by_its_name(self):
        self.network.change(networks={'Hidden network': PASSWORD}, access_points=[])

        self.join('Hidden network', PASSWORD, persistent=True)

        self.assertEqual(self.network.world()['active_ssid'], 'Hidden network')
        self.assertEqual(self.kept(), ('Hidden network', PASSWORD))

    def test_hidden_network_whose_name_nmcli_would_take_for_its_option_is_joined_and_kept(self):
        for name in ('--ask', '-a', '-s', '--show-secrets'):
            with self.subTest(name=name):
                self.network.change(networks={name: PASSWORD}, access_points=[])
                joins = len(self.network.joined())

                answer = self.join(name, PASSWORD, persistent=True)

                self.assertEqual(answer.get('status'), 'success', answer)
                self.assertEqual(self.network.joined()[joins:], [(name, PASSWORD)])
                self.assertEqual(self.network.world()['active_ssid'], name)
                self.assertEqual(self.kept(), (name, PASSWORD))


class TestHiddenNetwork(WifiCase):
    """A network no access point announces: the box joins it by its name, keeps it, and joins it again."""

    def setUp(self):
        super().setUp()
        self.network.change(networks={'Hidden network': PASSWORD}, access_points=[])
        self.join('Hidden network', PASSWORD, persistent=True)

    def test_hidden_network_joined_and_kept_is_joined_again_as_the_box_starts(self):
        self.network.lose_the_network()

        done = subprocess.run([sys.executable, '-I', str(START_PROGRAM)], env=self.network.environment(),
                              capture_output=True, text=True, check=False, timeout=common.PROCESS_TIMEOUT)

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.network.world()['active_ssid'], 'Hidden network')
        self.assertEqual(self.kept(), ('Hidden network', PASSWORD))

    def test_hidden_network_kept_is_joined_again_by_network_manager_by_itself_once_it_is_back(self):
        self.network.lose_the_network()

        self.network.join_by_itself()

        self.assertEqual(self.network.world()['active_ssid'], 'Hidden network')


class TestBoxWhoseLocaleSettingsNameALocaleItLacks(WifiCase):
    """nmcli writes in the character set of its locale: the box reads every name whole, whatever locale its settings
    name."""

    def test_network_with_a_vietnamese_name_is_found_among_those_heard_joined_as_announced_and_kept(self):
        name = 'Xưởng in số 3'
        # WPA3 alone: only the protection its access point announces lets the box in.
        heard = {'bssid': '02:00:5E:00:53:40', 'signal': 70, 'ssid': name, 'security': 'WPA3',
                 'rsn_flags': 'pair_ccmp group_ccmp sae'}
        self.network.change(networks={name: PASSWORD}, access_points=[heard])
        wifi_common.with_settings_naming_a_locale_the_box_lacks(self)

        self.join(name, PASSWORD, persistent=True)

        world = self.network.world()
        self.assertEqual((world['active_ssid'], world['active_bssid']), (name, heard['bssid']))
        self.assertEqual(self.kept(), (name, PASSWORD))


class TestJoinAskedFromTheAccessPoint(WifiCase):
    """A radio that has just served the access point may hear nothing in its first scans: a network it hears once it
    listens is joined as its access points announce it, never taken for a hidden one."""

    def test_network_the_radio_hears_once_it_listens_is_joined_as_announced_and_kept(self):
        other = {'bssid': '02:00:5E:00:53:42', 'signal': 60, 'ssid': 'Neighbour'}
        cases = {
            'WPA2': ([{'ssid': 'Workshop', 'security': 'WPA2', 'rsn_flags': 'pair_ccmp group_ccmp psk'}], True),
            'WPA3 alone': ([{'ssid': 'Workshop', 'security': 'WPA3', 'rsn_flags': 'pair_ccmp group_ccmp sae'}], True),
            'hidden network among those heard': ([other], False),
        }
        for case, (announced, heard) in cases.items():
            with self.subTest(case):
                access_points = [{'bssid': '02:00:5E:00:53:41', 'signal': 70, **found} for found in announced]
                self.network.change(networks={'Workshop': PASSWORD, 'Neighbour': PASSWORD},
                                    access_points=access_points, on_access_point=True, access_point_running=True,
                                    active=None, active_ssid=None, active_bssid=None, empty_scans_after_access_point=2)

                self.join('Workshop', PASSWORD, persistent=True)

                world = self.network.world()
                self.assertEqual(world['active_ssid'], 'Workshop')
                self.assertEqual(world['active_bssid'], '02:00:5E:00:53:41' if heard else None)
                self.assertEqual(self.kept(), ('Workshop', PASSWORD))
                self.assertEqual(bool(wifi_common.on_the_card(self.card).get('hidden')), not heard)


class TestKeepThatFails(WifiCase):
    """The network kept before stays kept, all of it, when the box cannot keep the one it joined."""

    def setUp(self):
        super().setUp()
        common.store_box_configuration(wifi_ssid='Network core keeps', wifi_password='kept-by-core')
        self.keep_earlier('Office', 'office-password')
        self.network.change(networks={'Office': 'office-password', 'Workshop': PASSWORD})

    def assertKeptAsBefore(self):
        stored = common.box_configuration()
        self.assertEqual(self.kept(), ('Office', 'office-password'))
        self.assertEqual((stored.get('wifi_ssid'), stored.get('wifi_password')), ('Network core keeps', 'kept-by-core'))
        self.assertEqual([path for path in map(wifi_common.staged, wifi_common.card_copies()) if path.exists()], [])

    def test_keep_whose_copy_cannot_be_written_leaves_the_network_kept_before_as_it_was(self):
        for step in ('install', 'mv'):
            with self.subTest(failing=step):
                self.network.change(failing=[step])

                self.join('Workshop', PASSWORD, persistent=True)

                self.assertEqual(self.network.world()['active_ssid'], 'Workshop')
                self.assertKeptAsBefore()

    def test_keep_whose_profile_file_is_not_found_leaves_the_network_kept_before_as_it_was(self):
        self.network.change(files_listed=False)

        self.join('Workshop', PASSWORD, persistent=True)

        self.assertEqual(self.network.world()['active_ssid'], 'Workshop')
        self.assertKeptAsBefore()


class TestNetworkForgottenWhileACopyWasBeingWritten(WifiCase):

    def test_network_forgotten_leaves_no_copy_being_written_on_the_card(self):
        for path in wifi_common.card_copies():
            wifi_common.put_on_the_card(wifi_common.staged(path), {'id': PROFILE, 'ssid': 'Office', 'psk': PASSWORD})

        response = self.request(WIFI_CLEAR)

        self.assertEqual(response.status_code, 200)
        self.assertEqual([path for path in map(wifi_common.staged, wifi_common.card_copies()) if path.exists()], [])


class TestNetworkOfALockedBox(WifiCase):

    def setUp(self):
        super().setUp()
        self.box_with_server(CREDENTIAL)
        _answer, self.proof = self.unlock(box.settings_code(CREDENTIAL))
        self.network.change(networks={'Workshop': PASSWORD})
        self.join('Workshop', PASSWORD, proof=self.proof, persistent=True)

    def test_network_the_box_keeps_is_not_forgotten_without_the_unlock_proof(self):
        response = self.request(WIFI_CLEAR)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.kept(), ('Workshop', PASSWORD))

    def test_network_the_box_keeps_is_forgotten_with_the_unlock_proof(self):
        response = self.request(WIFI_CLEAR, proof=self.proof)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.card.exists())


class TestAnswerOfTheRoute(WifiCase):
    """The box leaves the network the browser reached it on only once the browser has its answer."""

    answer_delay = None

    def test_answer_reaches_the_browser_before_the_box_leaves_its_network(self):
        self.network.change(networks={'Workshop': PASSWORD})

        with wifi_common.settled():
            self.call(WIFI, essid='Workshop', password=PASSWORD)
            answered = time.time()

        moments = [moment for moment, _command in self.network.moments()]
        self.assertTrue(moments)
        self.assertGreaterEqual(min(moments) - answered, 1)


class TestWhatTheBoxKeepsSecret(WifiCase):

    def test_password_reaches_neither_the_log_nor_an_answer_nor_the_diagnostic_file_nor_the_configuration(self):
        passwords = {
            'joined and kept': 'Suite-wifi=5T8\\x"y\'z',
            'joined and not kept': 'Suite-wifi%4R7\\x"y\'z',
            'typed wrong': 'Suite-wifi&6W9\\x"y\'z',
            'refused': 'Suite-wifi\x1b7Q2\\x"y\'z',
        }
        self.network.change(
            networks={'Workshop': passwords['joined and kept'], 'Office': passwords['joined and not kept']})
        with common.box_log(logging.DEBUG) as written:
            answers = [
                self.join('Workshop', passwords['joined and kept'], persistent=True),
                self.join('Office', passwords['joined and not kept']),
                self.join('Workshop', passwords['typed wrong'], persistent=True),
                self.join('Workshop', passwords['refused'], persistent=True),
            ]
            self.request(WIFI_CLEAR)
            log = written()
        diagnostic = self.request(box.DIAGNOSTIC).get_data(as_text=True)
        configuration = common.CONFIGURATION.read_text(encoding='utf-8')

        self.assertTrue(log)
        places = {'log': log, 'answers': json.dumps(answers), 'diagnostic file': diagnostic,
                  'configuration': configuration}
        for case, password in passwords.items():
            spellings = {password, repr(password)[1:-1], json.dumps(password)[1:-1]}
            for where, text in places.items():
                with self.subTest(case, where=where):
                    self.assertEqual([spelling for spelling in spellings if spelling in text], [])


class TestJoinAskedWhileTheWatchRuns(WifiCase):
    """`viin-box-wifi watch` brings the box back to the network it keeps; a join asked from the box page wins, unless
    the watch is changing the Wi-Fi already: the page is then told the box is busy."""

    def test_watch_leaves_the_wifi_alone_while_a_join_asked_from_the_box_page_runs(self):
        watch = wifi_common.WatchOfTheBox(self, self.network)
        self.enterContext(mock.patch.object(common.layer('wifi'), 'LOCK', watch.lock))
        self.network.hold_profile(PROFILE, 'Office', 'office-password')
        self.network.change(networks={'Workshop': PASSWORD}, addresses='', hold=True)
        watch.look(0)

        with wifi_common.settled():
            self.call(WIFI, essid='Workshop', password=PASSWORD, persistent=True)
            common.wait_until(self.network.joins, 'the join to reach NetworkManager')
            looked = len(watch.history)
            # Long enough off the network for the watch to join it again, and to start the access point.
            watch.looks(15 * 60, 30 * 60)
            self.network.release()

        changes = [call for _at, call in watch.history[looked:]
                   if call[:1] not in (['sudo'], ['hostname']) and 'show' not in call]
        self.assertEqual(changes, [])
        self.assertEqual(self.network.world()['active_ssid'], 'Workshop')
        self.assertEqual(self.kept(), ('Workshop', PASSWORD))

    def test_watch_leaves_the_wifi_alone_from_the_moment_a_join_is_asked_from_the_box_page(self):
        watch = wifi_common.WatchOfTheBox(self, self.network)
        self.enterContext(mock.patch.multiple(common.layer('wifi'), LOCK=watch.lock, ANSWER_DELAY=3))
        self.network.hold_profile(PROFILE, 'Office', 'office-password')
        self.network.change(networks={'Workshop': PASSWORD, 'Office': 'office-password'}, addresses='')
        watch.look(0)

        with wifi_common.settled():
            self.call(WIFI, essid='Workshop', password=PASSWORD, persistent=True)
            looked = len(watch.history)
            # Long enough off the network for the watch to join the network it keeps again, before the join starts.
            watch.looks(5 * 60, 6 * 60)

        changes = [call for _at, call in watch.history[looked:]
                   if call[:1] not in (['sudo'], ['hostname']) and 'show' not in call]
        self.assertEqual(changes, [])
        self.assertEqual(self.network.world()['active_ssid'], 'Workshop')
        self.assertEqual(self.kept(), ('Workshop', PASSWORD))

    def test_join_asked_while_the_watch_only_looks_is_not_answered_busy(self):
        watch = wifi_common.WatchOfTheBox(self, self.network)
        self.enterContext(mock.patch.object(common.layer('wifi'), 'LOCK', watch.lock))
        self.network.change(networks={'Workshop': PASSWORD})

        with wifi_common.settled():
            with self.hold_like_a_look(watch, 0.5):
                answer = self.call(WIFI, essid='Workshop', password=PASSWORD, persistent=True)

        self.assertEqual(answer.get('status'), 'success', answer)
        self.assertEqual(self.network.world()['active_ssid'], 'Workshop')
        self.assertEqual(self.kept(), ('Workshop', PASSWORD))

    @contextlib.contextmanager
    def hold_like_a_look(self, watch, seconds):
        """Until the block ends, `watch` holds the lock as a look of it does, for `seconds` at most."""
        held, over = threading.Event(), threading.Event()

        def look():
            with watch.program.locked() as got:
                held.set()
                if got:
                    over.wait(seconds)

        thread = threading.Thread(target=look)
        thread.start()
        self.assertTrue(held.wait(30))
        try:
            yield
        finally:
            over.set()
            thread.join()

    def test_join_asked_while_the_watch_changes_the_wifi_is_answered_busy_and_does_not_start(self):
        watch = wifi_common.WatchOfTheBox(self, self.network)
        self.enterContext(mock.patch.multiple(common.layer('wifi'), LOCK=watch.lock, HOLD_WAIT=1))
        self.network.change(networks={'Workshop': PASSWORD})

        with wifi_common.settled():
            with watch.program.locked() as held:
                self.assertTrue(held)
                answer = self.call(WIFI, essid='Workshop', password=PASSWORD, persistent=True)

        self.assertEqual((answer.get('status'), answer.get('reason')), ('failed', 'busy'), answer)
        self.assertEqual(self.network.joins(), [])
        self.assertEqual(self.join('Workshop', PASSWORD, persistent=True).get('status'), 'success')
        self.assertEqual(self.network.world()['active_ssid'], 'Workshop')
        self.assertEqual(self.kept(), ('Workshop', PASSWORD))


class TestPasswordOutOfEveryCommand(WifiCase):
    """Every command the box runs as root is logged with its arguments, and any process reads the arguments of
    another: the password of a network is never one of them."""

    def test_password_is_an_argument_of_no_command_the_box_runs(self):
        for case, (name, password) in LEGITIMATE.items():
            if not password:
                continue
            with self.subTest(case):
                self.network.change(networks={name: password})
                calls = len(self.network.calls())

                self.join(name, password, persistent=True)
                self.join(name, password[::-1], persistent=True)
                self.request(WIFI_CLEAR)

                self.assertEqual(self.network.world()['active_ssid'], name)
                typed = (password, password[::-1])
                self.assertEqual([call for call in self.network.calls()[calls:]
                                  if any(secret in word for secret in typed for word in call)], [])


class TestStartOfTheBox(unittest.TestCase):
    """The program that runs as the box starts, before core's rc.local, with a stand-in for NetworkManager and the
    radio of a box."""

    def setUp(self):
        common.require_software_box()
        directory = tempfile.mkdtemp(prefix='box-suite-wifi-start-')
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        self.network = wifi_common.NetworkManager(directory)

    def start(self):
        done = subprocess.run([sys.executable, '-I', str(START_PROGRAM)], env=self.network.environment(),
                              capture_output=True, text=True, check=False, timeout=common.PROCESS_TIMEOUT)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done

    def changes(self):
        """What the start asked of NetworkManager and of the radio, beside what it only read."""
        return [call for call in self.network.calls() if 'show' not in call and call != ['hostname', '-I']]

    def test_box_that_keeps_no_network_does_nothing_with_the_wifi(self):
        self.network.change(networks={'Workshop': PASSWORD})

        self.start()

        self.assertEqual(self.changes(), [])
        self.assertIsNone(self.network.world()['active'])

    def test_network_kept_is_joined_and_tried_again_at_each_later_start(self):
        self.network.hold_profile(PROFILE, 'Workshop', PASSWORD)
        self.network.change(networks={'Workshop': PASSWORD})

        self.start()

        self.assertEqual(self.network.world()['active_ssid'], 'Workshop')
        self.assertTrue(self.network.held()[PROFILE]['autoconnect'])

    def test_network_kept_out_of_reach_of_a_box_left_without_an_address_is_kept_off_the_radio_of_the_access_point(
            self):
        self.network.hold_profile(PROFILE, 'Workshop', PASSWORD)
        self.network.change(networks={}, addresses='')

        self.start()

        self.assertIsNone(self.network.world()['active'])
        self.assertFalse(self.network.held()[PROFILE]['autoconnect'])

    def test_network_kept_the_box_is_on_already_is_not_joined_again(self):
        self.network.hold_profile(PROFILE, 'Workshop', PASSWORD)
        self.network.change(networks={'Workshop': PASSWORD}, active=PROFILE, active_ssid='Workshop')

        self.start()

        self.assertEqual([call for call in self.network.nmcli_calls() if 'up' in call], [])
        self.assertEqual(self.network.world()['active_ssid'], 'Workshop')

    def test_network_kept_out_of_reach_of_a_box_with_another_address_is_tried_again(self):
        self.network.hold_profile(PROFILE, 'Workshop', PASSWORD)
        self.network.change(networks={}, addresses=f'{SITE_ADDRESS} ')

        self.start()

        self.assertTrue(self.network.held()[PROFILE]['autoconnect'])


def _unit(path):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(path)
    return parser


class TestStartProgramIsInstalled(common.BoxStateCase):
    """The installer of the system files of the box puts the start program and its unit in place."""

    PLACES = ('etc/systemd', 'usr/local/sbin')
    INSTALLED_PROGRAM = Path('/usr/local/sbin/viin-box-wifi')
    INSTALLED_UNIT = Path('/etc/systemd/system/viin-box-wifi.service')
    ENABLED = Path('/etc/systemd/system/multi-user.target.wants/viin-box-wifi.service')

    def lay_out_system_files_again(self):
        directory = tempfile.mkdtemp(prefix='box-suite-wifi-installer-')
        saved = str(Path(directory, 'before.tar'))
        common.as_root('tar', '-C', '/', '-cpf', saved, *self.PLACES)

        def put_back():
            common.as_root('rm', '-rf', *(f'/{place}' for place in self.PLACES))
            common.as_root('tar', '-C', '/', '-xpf', saved)
            common.as_root('rm', '-rf', directory)

        self.addCleanup(put_back)
        common.as_root('rm', '-f', str(self.INSTALLED_PROGRAM), str(self.INSTALLED_UNIT), str(self.ENABLED))
        return subprocess.run(['sudo', '-n', 'bash', str(INSTALLER)], capture_output=True, text=True, check=False,
                              timeout=common.PROCESS_TIMEOUT)

    def test_installer_puts_the_program_and_its_unit_in_place_and_starts_it_with_the_box(self):
        done = self.lay_out_system_files_again()

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.INSTALLED_PROGRAM.read_bytes(), START_PROGRAM.read_bytes())
        self.assertEqual(self.INSTALLED_PROGRAM.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.INSTALLED_UNIT.read_bytes(), START_UNIT.read_bytes())
        self.assertEqual(os.readlink(self.ENABLED) if self.ENABLED.is_symlink() else None, '../viin-box-wifi.service')
        self.assertEqual(_unit(self.INSTALLED_UNIT)['Service']['ExecStart'], str(self.INSTALLED_PROGRAM))

    def test_unit_runs_once_network_manager_runs_and_before_core_decides_on_the_access_point(self):
        unit = _unit(START_UNIT)['Unit']

        self.assertIn('rc-local.service', unit.get('Before', '').split())
        self.assertIn('NetworkManager.service', unit.get('After', '').split())


class TestWatchRunsTheProgramInstalled(common.BoxStateCase):
    """On a running system the installer of the system files - run after each update, and by the roll-back guard -
    has the watch run the program it installs, without waiting for the next start of the box."""

    PLACES = ('etc/systemd', 'usr/local/sbin')
    INSTALLED_PROGRAM = Path('/usr/local/sbin/viin-box-wifi')
    INSTALLED_UNIT = Path('/etc/systemd/system/viin-box-wifi-watch.service')
    RUNNING_SYSTEM = Path('/run/systemd/system')
    WATCH = 'viin-box-wifi-watch.service'

    def setUp(self):
        super().setUp()
        directory = Path(tempfile.mkdtemp(prefix='box-suite-wifi-watch-installer-'))
        saved = str(directory / 'before.tar')
        common.as_root('tar', '-C', '/', '-cpf', saved, *self.PLACES)

        def put_back():
            common.as_root('rm', '-rf', *(f'/{place}' for place in self.PLACES))
            common.as_root('tar', '-C', '/', '-xpf', saved)
            common.as_root('rm', '-rf', str(directory))

        self.addCleanup(put_back)
        if not self.RUNNING_SYSTEM.exists():
            common.as_root('mkdir', '-p', str(self.RUNNING_SYSTEM))
            self.addCleanup(common.as_root, 'rm', '-rf', str(self.RUNNING_SYSTEM))
        # systemd, as far as the installer asks it: each call, with the program installed at that moment.
        programs, self.asked = directory / 'bin', directory / 'asked'
        programs.mkdir()
        (programs / 'systemctl').write_text(
            '#!/bin/sh\n'
            f'echo "$*|$(sha256sum < {self.INSTALLED_PROGRAM} | cut -d" " -f1)" >> {self.asked}\n')
        (programs / 'systemctl').chmod(0o755)
        self.path = f'{programs}:{os.environ["PATH"]}'

    def install(self):
        """What the installer asked of systemd: (arguments, digest of the watch program installed then) each."""
        common.as_root('rm', '-f', str(self.asked))
        done = subprocess.run(['sudo', '-n', 'env', f'PATH={self.path}', 'bash', str(INSTALLER)], capture_output=True,
                              text=True, check=False, timeout=common.PROCESS_TIMEOUT)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        asked = self.asked.read_text().splitlines() if self.asked.exists() else []
        return [tuple(line.rsplit('|', 1)) for line in asked]

    def test_watch_runs_the_program_of_the_release_installed_once_it_is_in_place(self):
        release = hashlib.sha256(START_PROGRAM.read_bytes()).hexdigest()
        cases = {
            'program of the watch changed': (self.INSTALLED_PROGRAM, '#!/usr/bin/python3 -I\n'),
            'unit of the watch changed': (self.INSTALLED_UNIT, '[Service]\nExecStart=/bin/true\n'),
        }
        for case, (path, other) in cases.items():
            with self.subTest(case):
                self.install()
                common.as_root('tee', str(path), data=other.encode())

                asked = self.install()

                self.assertIn((f'--no-block restart {self.WATCH}', release), asked)
                reloaded = asked.index(next(call for call in asked if call[0] == 'daemon-reload'))
                self.assertLess(reloaded, asked.index((f'--no-block restart {self.WATCH}', release)))

    def test_watch_whose_program_and_unit_are_unchanged_keeps_running_as_it_is(self):
        self.install()

        asked = self.install()

        self.assertEqual([arguments for arguments, _program in asked], [f'--no-block start {self.WATCH}'])


class TestCoreAccessPointScriptMoved(common.BoxStateCase):
    """What the box relies on in core's access point script: a change there is reported by the seam of core's
    routes, line by line."""

    # Each line, and how an update of core might change it.
    CHANGED_LINES = {
        'FORCE_HOST_AP="${1}"': 'FORCE_HOST_AP="${2}"',
        'WIRED_IP=$(hostname -I)': 'WIRED_IP=$(ip -4 -o address show dev eth0)',
        'if [ -z "${WIRED_IP}" ] ; then': 'if [ -z "${WIRED_IP}" ] || [ -n "${FORCE_HOST_AP}" ] ; then',
        'service hostapd restart': '/usr/sbin/hostapd -B /etc/hostapd/hostapd.conf',
    }

    def test_standalone_check_names_each_line_of_the_access_point_script_that_core_no_longer_holds(self):
        for former, later in self.CHANGED_LINES.items():
            with self.subTest(line=former):
                check = common.seam_check(script_changes={ACCESS_POINT_SCRIPT: {former: later}})

                self.assertEqual(check.exit_code, 1, check.output)
                self.assertIs(check.holds.get(13), False, check.output)
                self.assertEqual(
                    [line for line in self.CHANGED_LINES if line in check.output], [former], check.output)


class TestCoreAccessPointScriptNoLongerTellsAnAddress(common.MovedSeamCase):
    number = 13
    script_changes = {ACCESS_POINT_SCRIPT: {'WIRED_IP=$(hostname -I)': 'WIRED_IP=$(ip -4 -o address show dev eth0)'}}
    other_seams = (1, 2, 3, 10, 12)
