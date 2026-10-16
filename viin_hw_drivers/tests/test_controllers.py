"""The routes of the box services: status, diagnostic file, clock and software."""
import datetime
from unittest import mock

import common
import services_common as box

CREDENTIAL = 'suite-credential-routes'
DAY = datetime.timedelta(days=1)
# The layers the self-test names, as the page tells them apart.
LAYERS = ('network', 'clock', 'server', 'certificate', 'registration', 'live_channel', 'software', 'drivers', 'layer')


class TestStatusAndDiagnosticFile(common.BoxCase):

    def test_status_names_each_layer_the_box_depends_on(self):
        response = self.request(box.STATUS)

        self.assertEqual(response.status_code, 200)
        named = {line['id'] for line in response.get_json()['selftest']['lines']}
        self.assertLessEqual(set(LAYERS), named)

    def test_status_is_not_given_to_a_page_of_another_web_site(self):
        response = self.request(box.STATUS, sent=box.FROM_ANOTHER_WEB_SITE)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json().get('reason'), box.OTHER_SITE)

    def test_diagnostic_file_is_not_given_to_a_page_of_another_web_site(self):
        response = self.request(box.DIAGNOSTIC, sent=box.FROM_ANOTHER_WEB_SITE)

        self.assertEqual(response.status_code, 403)
        self.assertNotIn('attachment', response.headers.get('Content-Disposition', ''))


class TestSoftwareRoute(common.BoxCase):

    def setUp(self):
        super().setUp()
        box.keep_files(self, box.STATE_FILE, box.CHANNEL_FILE)
        # Where the box software comes from is out of reach: a look finds nothing to move to.
        self.git = self.enterContext(box.git_commands())
        self.addCleanup(self.wait_for_the_look)
        self.server_of_the_box = self.box_with_server(CREDENTIAL)
        _answer, self.proof = self.unlock(box.settings_code(CREDENTIAL))

    @staticmethod
    def wait_for_the_look():
        update = common.layer('update')
        common.wait_until(lambda: not update.status()['busy'], 'the look for a release to end')

    def automatic_updates(self):
        return common.box_configuration().get('viin_auto_update')

    def test_look_for_a_release_needs_no_unlock_proof(self):
        self.assertEqual(self.call(box.UPDATE, action='check').get('status'), 'started')

    def test_look_for_a_release_gives_git_no_trust_but_that_of_the_system(self):
        # What would make git trust a server for another reason than the authorities of the system.
        trusting = {
            'GIT_SSL_NO_VERIFY': '1', 'GIT_SSL_CAINFO': '/tmp/box-suite.pem', 'SSL_CERT_FILE': '/tmp/box-suite.pem'}
        with mock.patch.dict('os.environ', trusting):
            self.call(box.UPDATE, action='check')
            self.wait_for_the_look()

        self.assertTrue(self.git)
        given = [set(environment or {}) & set(trusting) for environment in self.git.environments]
        self.assertEqual([names for names in given if names], [])
        self.assertNotIn(None, self.git.environments)

    def test_release_is_not_applied_without_the_unlock_proof(self):
        self.assertEqual(self.call(box.UPDATE, action='apply').get('reason'), box.LOCKED)

    def test_release_is_applied_with_the_unlock_proof(self):
        self.assertEqual(self.call(box.UPDATE, proof=self.proof, action='apply').get('status'), 'started')

    def test_automatic_updates_are_not_turned_off_without_the_unlock_proof(self):
        before = self.automatic_updates()

        answer = self.call(box.UPDATE, action='auto_update', enabled=False)

        self.assertEqual(answer.get('reason'), box.LOCKED)
        self.assertEqual(self.automatic_updates(), before)

    def test_automatic_updates_are_turned_off_with_the_unlock_proof(self):
        answer = self.call(box.UPDATE, proof=self.proof, action='auto_update', enabled=False)

        self.assertEqual(answer.get('status'), 'saved')
        self.assertEqual(self.automatic_updates(), '0')
        self.assertIs(answer['update']['auto_update'], False)

    def test_page_of_another_web_site_changes_nothing_even_with_the_unlock_proof(self):
        before = self.automatic_updates()

        response = self.request(box.UPDATE, {'action': 'auto_update', 'enabled': False}, proof=self.proof,
                                sent=box.FROM_ANOTHER_WEB_SITE)

        self.assertEqual(response.get_json()['result'].get('reason'), box.OTHER_SITE)
        self.assertEqual(self.automatic_updates(), before)

    def test_box_offers_no_way_to_choose_another_channel(self):
        for action in ('channel', 'set_channel', 'follow'):
            with self.subTest(action=action):
                answer = self.call(box.UPDATE, proof=self.proof, action=action, channel=f'box-candidate/{box.SERIES}')

                self.assertEqual(answer.get('status'), 'refused')
                self.assertFalse(box.CHANNEL_FILE.exists())
                self.assertEqual(common.layer('update').status()['channel'],
                                 f'refs/heads/{box.release_channel(box.SERIES)}')


class TestClockRoute(common.BoxCase):

    def clock(self, action, server_url=None):
        return self.call(box.CLOCK, action=action, server_url=server_url)

    def synchronised(self, answer):
        return self.enterContext(mock.patch.object(common.layer('selftest'), 'clock_synchronised',
                                                          return_value=answer))

    def test_box_without_a_server_shows_the_time_of_the_server_named_to_it_and_sets_nothing(self):
        server = self.server(clock=common.utc_now() + DAY)
        self.synchronised(False)

        with box.clock_commands() as asked:
            answer = self.clock('read', server.url)

        self.assertEqual((answer['status'], answer['verified']), ('read', False))
        self.assertAlmostEqual(answer['difference'], DAY.total_seconds(), delta=120)
        self.assertEqual(asked, [])

    def test_box_without_a_server_sets_its_clock_to_the_time_of_the_server_named_to_it(self):
        server = self.server(clock=common.utc_now() + DAY)
        self.synchronised(False)

        with box.clock_commands() as asked:
            self.clock('apply', server.url)

        self.assertEqual(len(asked), 1)
        self.assertAlmostEqual(asked[0], box.timestamp(server.clock), delta=120)

    def test_clock_a_time_server_keeps_is_never_set_from_a_server(self):
        server = self.server(clock=common.utc_now() + DAY)
        self.synchronised(True)

        with box.clock_commands() as asked:
            answer = self.clock('apply', server.url)

        self.assertEqual(answer.get('status'), 'refused')
        self.assertEqual(asked, [])

    def test_paired_box_reads_the_time_of_no_other_server_than_its_own(self):
        self.box_with_server(CREDENTIAL)
        other = self.server(clock=common.utc_now() + DAY)

        answer = self.clock('read', other.url)

        self.assertEqual(answer.get('status'), 'failed')
        self.assertEqual(other.requests, [])

    def test_paired_box_reads_the_time_of_its_server_whatever_the_dates_of_the_certificate_it_trusts(self):
        expired = common.make_certificate(
            'localhost', valid_from=common.utc_now() - 40 * DAY, valid_until=common.utc_now() - DAY)
        server = self.server(certificate=expired)
        common.store_box_configuration(remote_server=server.url, token=CREDENTIAL)
        common.layer('trust').trust_certificate(expired.certificate)

        answer = self.clock('read')

        self.assertEqual((answer.get('status'), answer.get('verified')), ('read', True), answer)

    def test_paired_box_reads_no_time_from_a_server_whose_certificate_is_for_another_name(self):
        other_name = common.make_certificate('other-host.example', names=['other-host.example'])
        server = self.server(certificate=other_name)
        common.store_box_configuration(remote_server=server.url, token=CREDENTIAL)
        common.layer('trust').trust_certificate(other_name.certificate)

        answer = self.clock('read')

        self.assertEqual(answer.get('status'), 'failed')
