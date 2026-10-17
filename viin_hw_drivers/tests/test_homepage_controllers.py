"""What an installer reaches at the address of the box: its pages and their data, the version and update routes,
the log levels, the network printer it adds, and the web server in front of it all."""
import logging
import subprocess
import tempfile

import common
import services_common as box

CREDENTIAL = 'suite-credential-homepage'
PAGES = ('/', '/logs')
# What the page data of core holds for the services of the vendor of core: its pairing, its customer, a terminal.
VENDOR_KEYS = {'pairing_code', 'db_uuid', 'enterprise_code', 'six_terminal'}
BOX_LOGGER = 'iot-logging-root'
SOFTWARE_LOGGER = 'iot-logging-odoo'
DEVICE_ROUTE = '/hw_drivers/action'
SETTING_ROUTE = '/hw_posbox_homepage/update_wifi'
NAME = 'k3x9.iot.viindoo.example'
# What a browser asks before a public page may call a device of a private network.
PREFLIGHT = {
    'Origin': common.ANOTHER_WEB_SITE, 'Access-Control-Request-Method': 'POST',
    'Access-Control-Request-Headers': 'content-type', 'Access-Control-Request-Private-Network': 'true',
}
PRIVATE_NETWORK = 'access-control-allow-private-network'


class TestPagesOfTheBox(common.BoxCase):

    def test_pages_of_the_box_name_no_vendor_of_core(self):
        for page in PAGES:
            with self.subTest(page=page):
                response = self.request(page)

                self.assertEqual(response.status_code, 200)
                self.assertFalse(box.names_the_vendor(response.get_data(as_text=True)))

    def test_pages_of_the_box_load_nothing_from_outside_the_box(self):
        for page in PAGES:
            with self.subTest(page=page):
                self.assertEqual(box.outside_references(self.request(page).get_data(as_text=True)), [])


class TestIconOfTheTab(common.BoxCase):

    def test_browser_asking_for_the_icon_at_the_root_of_the_site_is_given_an_image_without_logging_in(self):
        response = self.request('/favicon.ico')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Content-Type'], 'image/png')
        self.assertTrue(response.get_data().startswith(b'\x89PNG\r\n\x1a\n'))


class TestPageData(common.BoxCase):

    def data(self):
        return self.request(box.PAGE_DATA).get_json()

    def test_page_data_holds_nothing_of_the_services_of_the_vendor_of_core(self):
        self.assertEqual(VENDOR_KEYS & set(self.data()), set())

    def test_page_data_carries_the_self_test_of_the_box(self):
        lines = self.data().get('viindoo', {}).get('selftest', {}).get('lines', [])

        self.assertIn('network', {line['id'] for line in lines})

    def test_page_data_offers_no_box_wide_debug_level_and_keeps_it_for_the_box_software(self):
        offered = self.data()['viindoo']['offered_log_levels']

        self.assertNotIn('debug', offered['root'])
        self.assertIn('warning', offered['root'])
        self.assertIn('debug', offered['odoo'])


class TestLogLevels(common.BoxCase):

    def setUp(self):
        super().setUp()
        for name in ('', 'odoo', 'werkzeug'):
            logger = logging.getLogger(name)
            self.addCleanup(logger.setLevel, logger.level)

    def set_level(self, name, value):
        return self.call(box.LOG_LEVELS_UPDATE, name=name, value=value)

    def test_box_wide_debug_level_is_refused_and_changes_nothing(self):
        configuration = common.CONFIGURATION.read_bytes()
        level = logging.getLogger().level

        answer = self.set_level(BOX_LOGGER, 'debug')

        self.assertEqual(answer.get('status'), 'refused')
        self.assertEqual(logging.getLogger().level, level)
        self.assertEqual(common.CONFIGURATION.read_bytes(), configuration)

    def test_box_wide_level_the_box_offers_is_set_as_on_a_stock_box(self):
        answer = self.set_level(BOX_LOGGER, 'error')

        self.assertEqual(answer.get('status'), 'success')
        self.assertEqual(logging.getLogger().level, logging.ERROR)

    def test_debug_level_of_the_box_software_is_still_set(self):
        answer = self.set_level(SOFTWARE_LOGGER, 'debug')

        self.assertEqual(answer.get('status'), 'success')
        self.assertEqual(logging.getLogger('odoo').level, logging.DEBUG)


class TestVersionAndUpdateRoutes(common.BoxCase):

    def setUp(self):
        super().setUp()
        box.keep_files(self, box.STATE_FILE)

    @staticmethod
    def wait_for_the_look():
        update = common.layer('update')
        common.wait_until(lambda: not update.status()['busy'], 'the look for a release to end')

    def remotes_asked(self, commands):
        """The places the git commands of the box fetched from or asked, each as written in the command."""
        return [part for command in commands for part in command if '://' in part or part == 'origin']

    def test_version_route_answers_the_release_the_box_runs(self):
        core_commit = subprocess.run(['git', '-C', '/home/pi/odoo', 'rev-parse', 'HEAD'], capture_output=True,
                                     text=True, check=True).stdout.strip()

        with box.git_commands():
            response = self.request(f'{box.VERSION_INFO}?check=0')

        answer = response.get_json() or {}
        self.assertEqual((answer.get('currentCommitHash'), answer.get('release')), (core_commit, box.pointer_release()))
        self.assertIsInstance(answer.get('odooIsUpToDate'), bool)
        self.assertIsInstance(answer.get('imageIsUpToDate'), bool)

    def test_version_route_looks_for_a_release_where_viindoo_publishes_it_and_nowhere_else(self):
        with common.AskedHosts() as asked, box.git_commands() as commands:
            response = self.request(box.VERSION_INFO)
            self.wait_for_the_look()

        self.assertEqual(response.status_code, 200)
        remotes = self.remotes_asked(commands)
        self.assertTrue(remotes)
        self.assertLessEqual(set(remotes), {box.LAYER_REPOSITORY, box.CORE_REPOSITORY})
        self.assertLessEqual(set(asked.hosts) - {'127.0.0.1', 'localhost', '::1'}, {common.SOFTWARE_PUBLISHER})

    def test_version_route_does_not_answer_a_page_of_another_web_site(self):
        with box.git_commands() as commands:
            response = self.request(box.VERSION_INFO, sent=box.FROM_ANOTHER_WEB_SITE)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(commands, [])

    def test_update_route_is_refused_on_a_paired_box_without_the_unlock_proof(self):
        self.box_with_server(CREDENTIAL)

        with box.git_commands() as commands:
            answer = self.call(box.UPDATE_GIT_TREE)

        self.assertEqual(answer.get('reason'), box.LOCKED)
        self.assertEqual(commands, [])

    def test_update_route_starts_the_viindoo_update_with_the_unlock_proof_and_nothing_of_core(self):
        # A server on another series: what makes core's own update move the box to the branch of that series.
        self.box_with_server(CREDENTIAL, kind=box.ServerOfASeries, series='17.0')
        _answer, proof = self.unlock(box.settings_code(CREDENTIAL))

        with box.git_commands() as commands, common.without_running('posbox_update.sh') as update_of_core:
            answer = self.call(box.UPDATE_GIT_TREE, proof=proof)
            self.wait_for_the_look()

        self.assertEqual(answer.get('status'), 'started')
        remotes = self.remotes_asked(commands)
        self.assertTrue(remotes)
        self.assertLessEqual(set(remotes), {box.LAYER_REPOSITORY, box.CORE_REPOSITORY})
        self.assertEqual(update_of_core, [])

    def test_update_route_does_not_answer_a_page_of_another_web_site_even_with_the_unlock_proof(self):
        self.box_with_server(CREDENTIAL)
        _answer, proof = self.unlock(box.settings_code(CREDENTIAL))

        with box.git_commands() as commands:
            response = self.request(box.UPDATE_GIT_TREE, {}, proof=proof, sent=box.FROM_ANOTHER_WEB_SITE)

        self.assertEqual(response.get_json()['result'].get('reason'), box.OTHER_SITE)
        self.assertEqual(commands, [])


class TestNetworkPrinter(common.BoxCase):

    def add(self, proof=None, **printer):
        answer = self.call(box.PRINTER_ADD, proof=proof, **printer)
        if answer.get('status') == 'added':
            self.addCleanup(box.remove_print_queues, answer['queue'])
        return answer

    def test_network_printer_is_found_as_the_kind_the_installer_chose(self):
        for kind, address, found in (('label', '192.0.2.21', 'label_printer'),
                                     ('receipt', '192.0.2.22', 'receipt_printer'),
                                     ('office', '192.0.2.23', 'office_printer')):
            with self.subTest(kind=kind):
                answer = self.add(address=address, kind=kind)

                self.assertEqual(answer.get('status'), 'added', answer)
                self.assertEqual(box.kind_core_gives(answer['queue']), found)

    def test_address_of_the_box_itself_is_not_taken_for_a_printer(self):
        before = box.print_queues()

        answer = self.add(address='127.0.0.1', kind='label')

        self.assertEqual(answer.get('status'), 'refused')
        self.assertEqual(box.print_queues(), before)

    def test_printer_added_once_is_not_added_again(self):
        self.add(address='192.0.2.24', kind='label')
        before = box.print_queues()

        answer = self.add(address='192.0.2.24:9100', kind='label', name='Another name')

        self.assertEqual(answer.get('status'), 'refused')
        self.assertEqual(box.print_queues(), before)

    def test_page_of_another_web_site_adds_no_printer(self):
        before = box.print_queues()

        response = self.request(box.PRINTER_ADD, {'address': '192.0.2.27', 'kind': 'label'},
                                sent=box.FROM_ANOTHER_WEB_SITE)

        self.assertEqual(response.get_json()['result'].get('reason'), box.OTHER_SITE)
        self.assertEqual(box.print_queues(), before)

    def test_locked_box_adds_no_printer_without_the_unlock_proof(self):
        self.box_with_server(CREDENTIAL)
        before = box.print_queues()

        answer = self.add(address='192.0.2.25', kind='label')

        self.assertEqual(answer.get('reason'), box.LOCKED)
        self.assertEqual(box.print_queues(), before)

    def test_locked_box_adds_a_printer_with_the_unlock_proof(self):
        self.box_with_server(CREDENTIAL)
        _answer, proof = self.unlock(box.settings_code(CREDENTIAL))

        answer = self.add(proof=proof, address='192.0.2.26', kind='label')

        self.assertEqual(answer.get('status'), 'added', answer)
        self.assertIn('socket://192.0.2.26:9100', box.print_queues())


class TestWebServer(common.BoxCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        common.serve()
        cls.box_certificate = common.make_certificate('box-suite-box', names=['box-suite-box', '127.0.0.1'])
        cls.name_certificate = common.make_certificate(NAME)

    def web_server(self, **options):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix='box-suite-'))
        server = box.WebServer(directory, self.box_certificate, **options)
        self.addCleanup(server.close)
        return server

    def test_browser_asking_before_a_public_page_uses_a_device_of_the_box_gets_the_private_network_answer(self):
        _status, headers, _body = self.web_server().ask('OPTIONS', DEVICE_ROUTE, PREFLIGHT)

        self.assertEqual(headers.get(PRIVATE_NETWORK), 'true')

    def test_browser_asking_before_a_public_page_changes_a_setting_gets_no_private_network_answer(self):
        _status, headers, _body = self.web_server().ask('OPTIONS', SETTING_ROUTE, PREFLIGHT)

        self.assertNotEqual(headers.get(PRIVATE_NETWORK), 'true')

    def test_name_of_the_box_is_answered_by_the_rules_its_address_is_answered_by(self):
        web_server = self.web_server(names={NAME: self.name_certificate})

        by_address = web_server.ask('OPTIONS', DEVICE_ROUTE, PREFLIGHT)[1].get(PRIVATE_NETWORK)
        by_name = web_server.ask('OPTIONS', DEVICE_ROUTE, PREFLIGHT, name=NAME)[1].get(PRIVATE_NETWORK)

        self.assertEqual((by_address, by_name), ('true', 'true'))

    def test_box_reached_by_its_address_keeps_its_own_certificate_whether_it_holds_a_name_or_not(self):
        without_a_name = self.web_server()
        with_a_name = self.web_server(names={NAME: self.name_certificate})

        self.assertEqual(without_a_name.presented(), self.box_certificate.fingerprint)
        self.assertEqual(with_a_name.presented(), self.box_certificate.fingerprint)
        self.assertEqual(with_a_name.presented(NAME), self.name_certificate.fingerprint)

    def test_page_shown_while_the_box_service_is_down_names_no_vendor_and_loads_nothing_from_outside(self):
        status, _headers, body = self.web_server(box_service_port=common.closed_port()).ask('GET', '/')

        page = body.decode()
        self.assertEqual(status, 502)
        self.assertTrue(page.strip())
        self.assertFalse(box.names_the_vendor(page))
        self.assertEqual(box.outside_references(page), [])

    def test_request_naming_another_host_without_a_certificate_is_sent_to_the_address_of_the_box(self):
        web_server = self.web_server()

        status, headers, _body = web_server.ask('GET', common.PING, name='site-of-someone.example', tls=False)
        served = web_server.ask('GET', common.PING, tls=False)[0]

        self.assertEqual(status, 302)
        self.assertTrue(headers.get('location', '').startswith('http://127.0.0.1/'), headers)
        self.assertEqual(served, 200)
