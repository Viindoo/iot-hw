"""The gate: how the layer takes its place when the box starts, and where the suite itself may run."""
import builtins
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import common
import services_common as box

CORE_BEFORE_THE_LAYER = 'hw_drivers,viin_hw_drivers,hw_escpos,hw_posbox_homepage,viin_hw_homepage,web'
WITHOUT_THE_HOME_PAGE = 'viin_hw_drivers,hw_drivers,hw_escpos,hw_posbox_homepage,web'
CORE = 'odoo.addons.hw_drivers.'
PAIRING_CODE_OF_CORE = 'odoo.addons.hw_drivers.connection_manager.'
# The seams the gate applies before it lets the threads of core go.
GATE_SEAMS = {1, 2, 3, 10, 12, 18}
# The file of the layer that holds the threads of core while the layer takes its place.
GATE_FILE = 'seams.startup'
PING = common.PING
# The module of core that drives receipt printers itself; the box service imports it after the layer, and it imports
# the USB library as it is imported.
RECEIPT_PRINTER_MODULE = 'odoo.addons.hw_escpos.controllers.main'
# Seconds. How long either side of a start in step waits for the other one, at the most.
STEP_WAIT = 60


def start_report_in_step_with_the_usb_library():
    """`start_report` of a box where the device thread of core starts to import the USB library just before the box
    service does, for the receipt printer module of core: what a slow board, a Raspberry Pi, may do at any start.

    The device thread waits, at the start of the import, until the box service imports the library too, then a
    moment more. The report adds 'failed_handler_files', the handler files the box could not load, and
    'receipt_printer_module': whether the box service has that module.
    """
    import_ = builtins.__import__
    device_thread_imports, service_imports = threading.Event(), threading.Event()

    def in_step(name, globals_=None, locals_=None, fromlist=(), level=0):
        main = threading.current_thread() is threading.main_thread()
        if not level and not main and name == 'usb._version' and not device_thread_imports.is_set():
            # The first import the package of the library makes.
            device_thread_imports.set()
            service_imports.wait(STEP_WAIT)
            time.sleep(1)
        elif not level and main and name == 'usb.core' and 'usb.core' not in sys.modules and \
                '/hw_escpos/' in ((globals_ or {}).get('__file__') or ''):
            device_thread_imports.wait(STEP_WAIT)
            service_imports.set()
        return import_(name, globals_, locals_, fromlist, level)

    builtins.__import__ = in_step
    report = common.start_report()
    report['failed_handler_files'] = common.layer('seams.handlers').handler_files()['failed']
    report['receipt_printer_module'] = RECEIPT_PRINTER_MODULE in sys.modules
    return report


class TestDocumentedOrder(common.BoxStateCase):

    def test_no_thread_of_core_runs_before_the_layer_is_in_place(self):
        threads = self.started_box()['threads']

        core_threads = [thread for thread in threads if thread['thread'].startswith(CORE)]
        self.assertTrue(core_threads)
        for thread in core_threads:
            with self.subTest(thread=thread['thread']):
                self.assertLessEqual(GATE_SEAMS, set(thread['seams_applied'] or ()))

    def test_pairing_code_thread_of_core_never_runs(self):
        started = [thread['thread'] for thread in self.started_box()['threads']]

        self.assertTrue([name for name in started if name.startswith(CORE)])
        self.assertEqual([name for name in started if name.startswith(PAIRING_CODE_OF_CORE)], [])

    def test_box_without_a_server_contacts_no_host_but_the_publisher_of_its_software_when_it_starts(self):
        self.assertLessEqual(set(self.started_box()['asked']), {common.SOFTWARE_PUBLISHER})


class TestUsbLibraryImportedByCoreAndByTheBoxServiceAtOnce(common.BoxStateCase):

    def test_box_keeps_its_usb_devices_and_its_receipt_printer_module_whichever_imports_the_usb_library_first(self):
        started = common.in_new_process(start_report_in_step_with_the_usb_library)

        self.assertEqual(started['failed_handler_files'], [])
        self.assertTrue(started['receipt_printer_module'])


class TestStartOfABoxThatHasAServer(common.BoxStateCase):

    def start_with(self, server):
        """What `common.start_report` says of the box once it started with `server` for its server."""
        self.keep_web_certificate()
        # Paired the way core pairs and still on the certificate of the image: what makes core ask its vendor for one.
        common.install_web_certificate(common.make_certificate(common.SHIPPED_COMMON_NAME))
        common.store_box_configuration(remote_server=server.url, token='suite-credential', db_uuid='suite-database')
        return common.in_new_process(common.start_report_of_a_box_whose_core_could_update)

    def test_box_contacts_no_host_but_its_server_and_the_publisher_of_its_software_when_it_starts(self):
        server = self.server()

        started = self.start_with(server)

        self.assertTrue(server.requests)
        self.assertIn(server.host, started['asked'])
        self.assertLessEqual(set(started['asked']), {server.host, common.SOFTWARE_PUBLISHER})

    def test_box_does_not_run_the_update_check_of_core_when_it_starts(self):
        server = self.server(kind=box.ServerOfASeries, series=common.OTHER_SERIES)

        started = self.start_with(server)

        # The server was asked for a report, so the start ran to its end: core's check is the step before it.
        self.assertIn('POST /iot/setup', server.calls)
        self.assertEqual(started['core_updated'], [])


class TestCoreListedBeforeTheLayer(common.BoxStateCase):
    start_options = {'server_wide_modules': CORE_BEFORE_THE_LAYER, 'probes': [PING]}

    def test_box_still_starts(self):
        self.assertEqual(self.started_box()['answers'], {PING: 200})

    def test_wrong_order_is_flagged(self):
        integrity = self.started_box()['integrity']

        self.assertIs(integrity['load_order_ok'], False)
        self.assertIs(integrity['ok'], False)


class TestHomePageModuleNotInPlace(common.BoxStateCase):
    start_options = {'server_wide_modules': WITHOUT_THE_HOME_PAGE, 'probes': [PING]}

    def test_box_still_starts(self):
        self.assertEqual(self.started_box()['answers'], {PING: 200})

    def test_integrity_state_names_the_missing_home_page_module(self):
        integrity = self.started_box()['integrity']

        self.assertIs(integrity['ok'], False)
        self.assertIn('viin_hw_homepage', [problem['source'] for problem in integrity['problems']])

    def test_standalone_check_names_the_missing_home_page_module(self):
        check = common.seam_check(server_wide_modules=WITHOUT_THE_HOME_PAGE)

        self.assertEqual(check.exit_code, 1, check.output)
        self.assertIn('viin_hw_homepage', check.failed_parts)


class TestGateThatCannotBeLoaded(common.BoxStateCase):
    start_options = {'without': [GATE_FILE], 'probes': [PING]}

    def test_box_still_starts(self):
        self.assertEqual(self.started_box()['answers'], {PING: 200})

    def test_integrity_is_not_ok_on_a_box_whose_gate_did_not_run(self):
        self.assertIs(self.started_box()['integrity']['ok'], False)


class TestSuiteOutsideTheSoftwareBox(unittest.TestCase):

    def setUp(self):
        super().setUp()
        common.require_software_box()
        # A real box has all that the software box has, but the file the software box leaves in its container.
        absent = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='box-suite-')), 'laid-out')
        self.enterContext(mock.patch.object(common, 'SOFTWARE_BOX', absent))

    def test_no_test_of_the_suite_runs(self):
        ran = []

        class TestOfTheSuite(common.BoxCase):

            def test_that_changes_the_machine(self):
                ran.append(self.id())

        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(TestOfTheSuite).run(result)

        self.assertEqual(ran, [])
        self.assertEqual(len(result.errors), 1)
        self.assertFalse(result.wasSuccessful())

    def test_no_box_is_started_in_the_process_of_the_tests(self):
        with self.assertRaises(common.NotTheSoftwareBox):
            common.start()

    def test_no_box_is_started_in_a_process_of_its_own(self):
        with self.assertRaises(common.NotTheSoftwareBox):
            common.in_new_process(common.start_report)

    def test_standalone_check_is_not_run(self):
        with self.assertRaises(common.NotTheSoftwareBox):
            common.seam_check()
