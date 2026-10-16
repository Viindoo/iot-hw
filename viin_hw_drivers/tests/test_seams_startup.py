"""The seams of the start, the engine that applies every seam, and what the box does when core is not where
a seam expects it."""
import contextlib
import threading

import common
import services_common as box

# The seams of the security set: the gate, the trust in servers, the routes of core.
SEAMS = (1, 2, 3, 10, 12, 13, 18)
CREDENTIAL = 'suite-credential-5T8WQ2ZN'

# The numbers of the seams the file below declares: far from those of the layer.
WRAPPING_SEAM = 9001
LATER_SEAM = 9002
SEAM_FILE = 'viin_hw_drivers/seams/box_suite.py'
# A seam file as the layer may hold more of: a seam that wraps a function of core that keeps what it answered
# and counts its calls, each by the thread that made it, and a seam on a symbol that exists only once core has
# loaded its device handlers, which the first applies then.
SEAM_FILE_CONTENT = '''
import threading

from . import CORE, FunctionSeam, Seam, SeamError, register

MOVED = False
calls = []
applications = []
wrapping = []


@register
class ServerAddress(FunctionSeam):
    number = 9001
    module = f'{CORE}.tools.helpers'
    name = 'get_odoo_server_url'
    feature = 'the box suite counts how often core reads the address of its server'

    def __init__(self):
        super().__init__()
        wrapping.append(self)

    def replacement(self, original):
        def get_odoo_server_url():
            calls.append(threading.get_ident())
            return original()

        return get_odoo_server_url


@register
class SymbolOfALoadedHandler(Seam):
    number = 9002
    target = 'a symbol of the box suite that exists once core has loaded its device handlers'
    feature = 'the box suite follows a seam that is applied after the gate'
    after = 9001

    def check(self):
        if MOVED:
            raise SeamError('the symbol moved')

    def apply(self):
        applications.append(1)
'''
SEAM_FILES = {SEAM_FILE: SEAM_FILE_CONTENT}
MOVED_LATER_SEAM = {SEAM_FILE: SEAM_FILE_CONTENT.replace('MOVED = False', 'MOVED = True')}


def nightly_update_checks():
    """The actions by which core asks for its update check every night, on the box started in this process."""
    import schedule

    return [job for job in schedule.jobs if getattr(job.job_func, '__name__', '') == 'check_git_branch']


def use_the_update_check_a_file_of_the_layer_gives():
    """Start a box, give it an update check as a file of the layer does, and have core ask for its check.

    Meant for `common.in_new_process`: the check a box was given holds for as long as its process.
    The check given runs until it is told to end. Gives what happened, each as a number or a truth:

    * 'ended_when_core_got_its_answer' - the runs of the check that had ended when the nightly
      action of core, which asks for the check, came back;
    * 'started' - whether that ask started the check;
    * 'runs_after_a_second_ask' - the runs started once core had asked again while the first ran;
    * 'runs_after_the_first_ended' - the runs started once core had asked again after the first ended;
    * 'runs_after_a_start_that_failed' - the runs started once core had asked twice more, the
      first time on a machine that refused the check its thread.
    """
    common.start()
    helpers = common.core('tools.helpers')
    started, ended = [], []
    running, may_end = threading.Event(), threading.Event()

    def check():
        started.append(1)
        running.set()
        may_end.wait(30)
        ended.append(1)

    def runs_once_ended(runs):
        with contextlib.suppress(AssertionError):
            common.wait_until(lambda: len(ended) >= runs, 'the update check to end', timeout=10)
        return len(started)

    common.layer('seams.startup').set_update_check(check)
    for action in nightly_update_checks():
        action.run()
    happened = {'ended_when_core_got_its_answer': len(ended), 'started': running.wait(10)}
    helpers.check_git_branch()
    happened['runs_after_a_second_ask'] = len(started)
    may_end.set()
    runs_once_ended(1)
    helpers.check_git_branch()
    happened['runs_after_the_first_ended'] = runs_once_ended(2)

    asking = threading.current_thread()
    start_thread = threading.Thread.start

    def refuse_the_thread_of_the_ask(thread):
        if threading.current_thread() is asking:
            raise RuntimeError("can't start new thread")
        start_thread(thread)

    threading.Thread.start = refuse_the_thread_of_the_ask
    try:
        helpers.check_git_branch()
    finally:
        threading.Thread.start = start_thread
    helpers.check_git_branch()
    happened['runs_after_a_start_that_failed'] = runs_once_ended(3)
    return happened


def use_the_seams_a_file_added_to_the_layer_declares():
    """Start a box whose layer holds SEAM_FILE, then use the two seams of that file.

    Meant for `common.in_new_process`. Gives:

    * 'integrity' - the state of the layer once the box started;
    * 'calls_counted' - how many calls the first seam counted for one call this thread made to the
      function it wraps, once the seam had been applied three times in all;
    * 'server_before_reset' and 'server_after_reset' - the address of its server as core reads
      it before and after core itself forgot that server, which resets what the wrapped
      function keeps; the second tells when core could not;
    * 'later_seam_once_applied' - the state of the second seam once the layer was asked twice
      to apply it, and 'later_seam_applications' how many times it changed its symbol.
    """
    happened = {'integrity': common.start_report(layer_files=SEAM_FILES)['integrity']}
    seams, added = common.layer('seams'), common.layer('seams.box_suite')
    helpers = common.core('tools.helpers')

    seams.apply(only=(WRAPPING_SEAM,))
    added.wrapping[0].apply()
    del added.calls[:]
    helpers.get_odoo_server_url()
    happened['calls_counted'] = added.calls.count(threading.get_ident())

    happened['server_before_reset'] = helpers.get_odoo_server_url()
    try:
        helpers.disconnect_from_server()
        happened['server_after_reset'] = helpers.get_odoo_server_url()
    except AttributeError as error:
        happened['server_after_reset'] = f'core could not reset it: {error}'

    seams.apply(only=(LATER_SEAM,))
    seams.apply(only=(LATER_SEAM,))
    happened['later_seam_once_applied'] = common.seam_states(seams.integrity()).get(LATER_SEAM)
    happened['later_seam_applications'] = len(added.applications)
    return happened


class TestNothingMoved(common.BoxCase):

    def test_standalone_check_passes_on_the_core_the_box_runs(self):
        check = common.seam_check()

        self.assertEqual(check.exit_code, 0, check.output)
        self.assertEqual({number: check.holds.get(number) for number in SEAMS}, dict.fromkeys(SEAMS, True))

    def test_started_box_reports_its_seams_applied(self):
        integrity = common.layer('seams').integrity()

        states = common.seam_states(integrity)
        self.assertEqual({number: states.get(number) for number in SEAMS}, dict.fromkeys(SEAMS, 'applied'))
        self.assertIs(integrity['ok'], True)


class TestUpdateCheckOfCore(common.BoxCase):

    def test_box_that_has_a_server_does_not_run_the_update_check_of_core_at_night(self):
        self.box_with_server(CREDENTIAL, kind=box.ServerOfASeries, series=common.OTHER_SERIES)
        nightly = nightly_update_checks()

        with common.core_update_possible() as updated:
            for action in nightly:
                action.run()

        self.assertTrue(nightly)
        self.assertEqual(updated, [])


class TestCertificateServiceOfTheVendorOfCore(common.BoxCase):
    """Core asks its vendor for a certificate, with the credential of the database, when the one it finds is the
    certificate of the image or about to end. A box that holds such a credential - one a connection to a server
    left, or a configuration kept from another box software - must never send it."""

    def setUp(self):
        super().setUp()
        self.keep_web_certificate()
        common.install_web_certificate(common.make_certificate(common.SHIPPED_COMMON_NAME))
        self.box_with_server(CREDENTIAL)
        common.store_box_configuration(db_uuid='suite-database', enterprise_code='suite-contract')

    def asked_by(self, step):
        """The hosts other than this machine that the box asked while `step` ran."""
        with common.AskedHosts() as asked:
            step()
        return [host for host in asked.hosts if host not in ('127.0.0.1', '::1', 'localhost')]

    def test_box_sends_the_credential_of_a_database_to_no_service_when_core_checks_its_certificate(self):
        helpers = common.core('tools.helpers')

        asked = self.asked_by(helpers.get_certificate_status)

        self.assertEqual(asked, [])

    def test_box_sends_the_credential_of_a_database_to_no_service_when_its_page_asks_for_its_data(self):
        asked = self.asked_by(lambda: self.request('/hw_posbox_homepage/data'))

        self.assertEqual(asked, [])


class TestUpdateCheckAFileOfTheLayerGives(common.BoxStateCase):

    @classmethod
    def happened(cls):
        if '_happened' not in vars(cls):
            cls._happened = common.in_new_process(use_the_update_check_a_file_of_the_layer_gives)
        return cls._happened

    def test_check_runs_when_core_asks_for_its_update_check(self):
        self.assertIs(self.happened()['started'], True)

    def test_whoever_asks_for_the_check_does_not_wait_for_it(self):
        self.assertEqual(self.happened()['ended_when_core_got_its_answer'], 0)

    def test_ask_that_comes_while_the_check_runs_starts_no_second_run(self):
        self.assertEqual(self.happened()['runs_after_a_second_ask'], 1)

    def test_check_runs_again_at_the_next_ask_once_it_has_ended(self):
        self.assertEqual(self.happened()['runs_after_the_first_ended'], 2)

    def test_check_runs_at_the_ask_that_follows_a_start_that_failed(self):
        self.assertEqual(self.happened()['runs_after_a_start_that_failed'], 3)


class TestSeamsOfAFileAddedToTheLayer(common.BoxStateCase):

    def happened(self):
        """What `use_the_seams_a_file_added_to_the_layer_declares` gives, once for the tests of the class."""
        cls = type(self)
        if '_happened' not in vars(cls):
            server = self.server()
            common.store_box_configuration(remote_server=server.url, token=CREDENTIAL)
            cls._server = server.url
            cls._happened = common.in_new_process(use_the_seams_a_file_added_to_the_layer_declares)
        return cls._happened

    def test_seam_on_a_symbol_of_a_loaded_handler_is_deferred_when_the_box_starts(self):
        states = common.seam_states(self.happened()['integrity'])

        self.assertEqual(states.get(WRAPPING_SEAM), 'applied')
        self.assertEqual(states.get(LATER_SEAM), common.DEFERRED)

    def test_deferred_seam_does_not_count_against_the_integrity_of_the_layer(self):
        self.assertIs(self.happened()['integrity']['ok'], True)

    def test_deferred_seam_is_applied_once_when_the_seam_that_carries_it_asks(self):
        happened = self.happened()

        self.assertEqual(happened['later_seam_once_applied'], 'applied')
        self.assertEqual(happened['later_seam_applications'], 1)

    def test_seam_applied_again_wraps_its_function_once(self):
        self.assertEqual(self.happened()['calls_counted'], 1)

    def test_function_a_seam_wraps_still_forgets_what_it_kept_when_core_resets_it(self):
        happened = self.happened()

        self.assertEqual(happened['server_before_reset'], self._server)
        self.assertIsNone(happened['server_after_reset'])

    def test_standalone_check_reports_the_deferred_seam_as_holding(self):
        check = common.seam_check(layer_files=SEAM_FILES)

        self.assertEqual(check.exit_code, 0, check.output)
        self.assertIs(check.holds.get(WRAPPING_SEAM), True, check.output)
        self.assertIs(check.holds.get(LATER_SEAM), True, check.output)
        self.assertIn(LATER_SEAM, check.deferred, check.output)


class TestSeamAppliedLaterWhoseSymbolMoved(common.MovedSeamCase):
    number = LATER_SEAM
    layer_files = MOVED_LATER_SEAM
    other_seams = (*SEAMS, WRAPPING_SEAM)


class TestThreadStartWithAnotherSignature(common.MovedSeamCase):
    number = 1
    changed_signatures = ('threading.Thread.start',)
    # Seam 2 withholds a thread the gate holds: it cannot hold without seam 1.
    other_seams = (3, 10, 12, 13, 18)


class TestPairingCodeThreadNoLongerStartedOnImport(common.MovedSeamCase):
    number = 2
    core_changes = {'connection_manager.start()': 'connection_manager.daemon = True'}
    other_seams = (1, 3, 10, 12, 13, 18)


class TestUpdateCheckRenamed(common.MovedSeamCase):
    number = 3
    core_changes = {'check_git_branch': 'check_code_branch'}
    other_seams = (1, 2, 10, 12, 13, 18)

    def test_box_without_a_server_still_contacts_no_host_but_the_publisher_of_its_software_when_it_starts(self):
        self.assertLessEqual(set(self.started_box()['asked']), {common.SOFTWARE_PUBLISHER})


class TestVendorCertificateRenamed(common.MovedSeamCase):
    number = 18
    core_changes = {'def load_certificate():': 'def load_certificate_of_the_vendor():'}
    other_seams = (1, 2, 3, 10, 12, 13)


class TestVendorCertificateWithAnotherSignature(common.MovedSeamCase):
    number = 18
    core_changes = {'def load_certificate():': 'def load_certificate(force):'}
    other_seams = (1, 2, 3, 10, 12, 13)


class TestUpdateCheckWithAnotherSignature(common.MovedSeamCase):
    number = 3
    core_changes = {'def check_git_branch():': 'def check_git_branch(force=False):'}
    other_seams = (1, 2, 10, 12, 13, 18)
