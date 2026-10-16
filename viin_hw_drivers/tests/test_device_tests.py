"""The test of each device from the box with no server, the scanner taken for a keyboard, and the office printer
that is a label printer."""
import json
import subprocess
import threading
import time
import types
from unittest import mock

import common
import services_common as box

CREDENTIAL = 'suite-credential-devices'
WEIGHT = 1.234
# What a page may wait for the device list while the print server does not answer, in seconds.
LONGEST_READ = 10


class DeviceTestCase(common.BoxCase):

    def tried(self, identifier, proof=None):
        return self.call(box.DEVICE_TEST, proof=proof, device_identifier=identifier)

    def printers(self):
        box.wait_for_devices(box.OFFICE_PRINTER, box.LABEL_PRINTER, box.RECEIPT_PRINTER)


class TestPrinterTests(DeviceTestCase):

    def setUp(self):
        super().setUp()
        self.printers()

    def test_office_printer_test_prints_on_that_printer(self):
        before = box.print_jobs(box.OFFICE_PRINTER)

        answer = self.tried(box.OFFICE_PRINTER)

        self.assertEqual((answer['status'], answer['kind']), ('ok', 'office_printer'), answer)
        self.assertTrue(box.new_print_job(box.OFFICE_PRINTER, before))

    def test_label_printer_test_prints_a_label_in_the_language_of_label_printers(self):
        before = box.print_jobs(box.LABEL_PRINTER)

        answer = self.tried(box.LABEL_PRINTER)

        printed = box.new_print_job(box.LABEL_PRINTER, before).strip()
        self.assertEqual((answer['status'], answer['kind']), ('ok', 'label_printer'), answer)
        self.assertTrue(printed.startswith(b'^XA') and printed.endswith(b'^XZ'), printed)

    def test_receipt_printer_test_prints_on_that_printer(self):
        before = box.print_jobs(box.RECEIPT_PRINTER)

        answer = self.tried(box.RECEIPT_PRINTER)

        self.assertEqual((answer['status'], answer['kind']), ('ok', 'receipt_printer'), answer)
        self.assertTrue(box.new_print_job(box.RECEIPT_PRINTER, before))

    def test_receipt_and_label_printer_tests_answer_for_the_job_the_print_server_numbered_for_the_test(self):
        for printer in (box.RECEIPT_PRINTER, box.LABEL_PRINTER):
            with self.subTest(printer=printer):
                before = box.print_jobs(printer)

                answer = self.tried(printer)

                printed = common.wait_until(
                    lambda printer=printer, before=before: set(box.print_jobs(printer)) - set(before), 'a print job')
                self.assertEqual((answer['status'], answer['detail']['reason']), ('ok', 'taken'), answer)
                self.assertIn(f'{printer}-job-{answer["detail"]["job"]}', printed)

    def test_printer_of_a_paired_box_is_tested_without_the_unlock_proof(self):
        self.box_with_server(CREDENTIAL)
        before = box.print_jobs(box.OFFICE_PRINTER)

        answer = self.tried(box.OFFICE_PRINTER)

        self.assertEqual(answer['status'], 'ok', answer)
        self.assertTrue(box.new_print_job(box.OFFICE_PRINTER, before))


class TestPrinterTestWhileCoreScansThePrinters(DeviceTestCase):

    def setUp(self):
        super().setUp()
        self.printers()

    def test_printer_test_does_not_wait_for_the_print_server_lock_core_holds_during_a_scan_of_the_printers(self):
        [printer] = box.wait_for_devices(box.OFFICE_PRINTER)
        scan_lock = type(printer).__init__.__globals__['cups_lock']
        before = box.print_jobs(box.OFFICE_PRINTER)

        with scan_lock:
            answer = self.tried(box.OFFICE_PRINTER)

        self.assertEqual((answer['status'], answer['kind']), ('ok', 'office_printer'), answer)
        self.assertTrue(box.new_print_job(box.OFFICE_PRINTER, before))


class DrivenClock:
    """A clock the test moves: `sleep` moves it by what was asked, at once, so a wait of the box costs no time."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class PrintServerAtTheMercyOfThePrinter:
    """A print server whose test job and queue go through the states `script(seconds)` gives, from the moment of
    printing.

    `script` answers (job state, reasons of the queue) for the seconds that passed since the test page was queued.
    Jobs named in `older` were queued before the test and stay pending.
    """

    TEST_JOB = 101

    def __init__(self, clock, script, older=(), device_uri='socket://192.168.1.77:9100', message=''):
        self.clock, self.script, self.older, self.device_uri = clock, script, set(older), device_uri
        self.message = message
        self.printed_at = None
        self.cancelled = []

    def seconds(self):
        return self.clock.now - self.printed_at

    def getPrinterAttributes(self, queue, requested_attributes=None):  # noqa: N802 - the name pycups gives it
        reasons = self.script(self.seconds())[1] if self.printed_at is not None else []
        return {'printer-state': 3, 'printer-state-message': self.message, 'printer-is-accepting-jobs': True,
                'printer-state-reasons': reasons or ['none'], 'device-uri': self.device_uri}

    def getJobs(self, **_options):  # noqa: N802
        numbers = set(self.older) | ({self.TEST_JOB} if self.printed_at is not None else set())
        return {number: {'job-printer-uri': f'ipp://localhost/printers/{box.OFFICE_PRINTER}'} for number in numbers}

    def printTestPage(self, queue):  # noqa: N802
        self.printed_at = self.clock.now
        return self.TEST_JOB

    def getJobAttributes(self, number, requested_attributes=None):  # noqa: N802
        states = {'pending': 3, 'processing': 5, 'canceled': 7, 'completed': 9}
        return {'job-state': states[self.script(self.seconds())[0]]}

    def cancelJob(self, number):  # noqa: N802
        self.cancelled.append(number)


def never_reached(_seconds):
    return 'processing', ['connecting-to-device']


def never_connecting(_seconds):
    return 'processing', []


def connecting_for_the_job_ahead_then_never_reached(seconds):
    if seconds < 10:
        return 'pending', ['connecting-to-device']
    if seconds < 12:
        return 'processing', []
    return 'processing', ['connecting-to-device']


class PrintQueueSharedWithThePointOfSale(PrintServerAtTheMercyOfThePrinter):
    """The print queue `queue` of a printer the point of sale prints its tickets on.

    `run` stands for the command `lp`, which core's `print_raw` and so the point of sale run to print: it queues the
    print as the next job and names that job in its request id, `<queue>-<number>`, unless `numbered` is False.
    `ticket` queues a ticket of the point of sale; `after_the_test_print` runs right after the test print is queued.
    Every job of the queue is listed by `getJobs`; a ticket stays pending, the test print goes through the states
    `script` gives.

    `silent` makes the print server stop answering: `before_the_print` holds every question about the queue,
    `after_taking_the_print` holds `lp` once the test print is queued. A ticket sent while it is silent is queued
    when `answer_again` is called, before any held call is answered.
    """

    FIRST_JOB = 11

    def __init__(self, clock, queue, script=never_reached, numbered=True, silent=None):
        super().__init__(clock, script)
        self.queue, self.numbered, self.silent = queue, numbered, silent
        self.after_the_test_print = None
        self.answers = threading.Event()
        self.lock = threading.Lock()
        self.jobs, self.tickets, self.held_tickets, self.followed, self.sent = [], [], [], [], []
        self.test_job = None

    def commands(self):
        """What the box runs its commands through, with `lp` reaching this print server."""
        return types.SimpleNamespace(run=self.run, PIPE=subprocess.PIPE)

    def queued(self):
        with self.lock:
            self.jobs.append(self.FIRST_JOB + len(self.jobs))
            return self.jobs[-1]

    def test_print_queued(self):
        self.test_job = self.queued()
        self.printed_at = self.clock.now
        if self.after_the_test_print:
            self.after_the_test_print()

    def run(self, command, input=None, **_options):  # noqa: A002 - the name subprocess gives it
        assert command[:3] == ['lp', '-d', self.queue], command
        self.sent.append(input)
        self.test_print_queued()
        if self.silent == 'after_taking_the_print':
            self.answers.wait(30)
        given = f'request id is {self.queue}-{self.test_job} (0 file(s))\n' if self.numbered else ''
        return subprocess.CompletedProcess(command, 0, stdout=given.encode(), stderr=b'')

    def printTestPage(self, queue):  # noqa: N802
        self.sent.append(queue)
        self.test_print_queued()
        return self.test_job

    def ticket(self):
        with self.lock:
            if self.silent and not self.answers.is_set():
                self.held_tickets.append(None)
                return
        self.tickets.append(self.queued())

    def answer_again(self):
        with self.lock:
            held, self.held_tickets = len(self.held_tickets), []
        for _ticket in range(held):
            self.tickets.append(self.queued())
        self.answers.set()

    def getPrinterAttributes(self, queue, requested_attributes=None):  # noqa: N802
        if self.silent == 'before_the_print':
            self.answers.wait(30)
        return super().getPrinterAttributes(queue, requested_attributes)

    def getJobs(self, **_options):  # noqa: N802
        return {number: {'job-printer-uri': f'ipp://localhost/printers/{self.queue}'} for number in self.jobs}

    def getJobAttributes(self, number, requested_attributes=None):  # noqa: N802
        self.followed.append(number)
        if number in self.tickets:
            return {'job-state': 3}
        return super().getJobAttributes(number, requested_attributes)


class TestPrinterTestFollowsTheJobToThePrinter(DeviceTestCase):
    """A test print ends with an answer about the printer itself, not about the print server having the job."""

    WINDOW = 30

    def setUp(self):
        super().setUp()
        self.printers()
        self.tests = common.layer('device_tests')
        self.clock = DrivenClock()
        patched = mock.patch.object(self.tests, 'time', self.clock)
        patched.start()
        self.addCleanup(patched.stop)

    def tried_with(self, script, **options):
        self.server = PrintServerAtTheMercyOfThePrinter(self.clock, script, **options)
        patched = mock.patch.object(self.tests, '_own_connection', return_value=self.server)
        patched.start()
        self.addCleanup(patched.stop)
        began = self.clock.now
        answer = self.tried(box.OFFICE_PRINTER)
        self.waited = self.clock.now - began
        return answer

    def on_a_queue_shared_with_the_point_of_sale(self, printer, **options):
        server = PrintQueueSharedWithThePointOfSale(self.clock, printer, **options)
        for name, value in (('_own_connection', mock.Mock(return_value=server)), ('subprocess', server.commands())):
            patched = mock.patch.object(self.tests, name, value)
            patched.start()
            self.addCleanup(patched.stop)
        return server

    def test_ticket_queued_right_after_the_test_print_is_never_cancelled_nor_followed_as_the_test_job(self):
        for printer in (box.RECEIPT_PRINTER, box.LABEL_PRINTER, box.OFFICE_PRINTER):
            with self.subTest(printer=printer):
                server = self.on_a_queue_shared_with_the_point_of_sale(printer)
                server.after_the_test_print = server.ticket

                answer = self.tried(printer)

                self.assertEqual(
                    (answer['status'], answer['detail']['reason']), ('failed', 'printer_not_reached'), answer)
                self.assertTrue(server.tickets)
                self.assertEqual(server.cancelled, [server.test_job])
                self.assertEqual(set(server.followed), {server.test_job})

    def test_test_print_the_print_server_gives_no_job_number_for_is_answered_sent_and_no_job_is_followed_or_cancelled(
            self):
        for printer in (box.RECEIPT_PRINTER, box.LABEL_PRINTER):
            with self.subTest(printer=printer):
                server = self.on_a_queue_shared_with_the_point_of_sale(printer, numbered=False)
                server.after_the_test_print = server.ticket

                answer = self.tried(printer)

                self.assertEqual((answer['status'], answer['detail']['reason']), ('ok', 'sent_not_followed'), answer)
                self.assertEqual(len(server.sent), 1)
                self.assertEqual(server.cancelled, [])
                self.assertEqual(server.followed, [])

    def test_printer_that_never_answers_ends_the_test_within_the_window_as_not_reached_and_the_test_job_is_cancelled(
            self):
        answer = self.tried_with(never_reached)

        self.assertEqual((answer['status'], answer['detail']['reason']), ('failed', 'printer_not_reached'), answer)
        self.assertEqual(
            (answer['detail']['connection'], answer['detail']['address']), ('network', '192.168.1.77:9100'))
        self.assertLessEqual(self.waited, self.WINDOW + 1)
        self.assertEqual(self.server.cancelled, [self.server.TEST_JOB])

    def test_job_queued_before_the_test_is_not_cancelled_when_the_printer_is_not_reached(self):
        self.tried_with(never_reached, older=[77])

        self.assertEqual(self.server.cancelled, [self.server.TEST_JOB])

    def test_printer_reached_after_two_seconds_is_taken_well_before_the_window_ends(self):
        answer = self.tried_with(lambda seconds: ('processing', ['connecting-to-device'] if seconds < 2 else []))

        self.assertEqual((answer['status'], answer['detail']['reason']), ('ok', 'taken'), answer)
        self.assertLess(self.waited, 5)
        self.assertEqual(self.server.cancelled, [])

    def test_completed_job_is_taken_at_once(self):
        answer = self.tried_with(lambda _seconds: ('completed', []))

        self.assertEqual((answer['status'], answer['detail']['reason']), ('ok', 'taken'), answer)
        self.assertEqual(self.waited, 0)

    def test_job_processing_while_its_filters_run_is_not_taken_for_a_printer_that_then_never_connects(self):
        answer = self.tried_with(lambda seconds: ('processing', [] if seconds < 3 else ['connecting-to-device']))

        self.assertEqual((answer['status'], answer['detail']['reason']), ('failed', 'printer_not_reached'), answer)
        self.assertEqual(self.server.cancelled, [self.server.TEST_JOB])

    def test_queue_connecting_for_a_job_ahead_while_the_test_job_waits_is_no_evidence_that_the_printer_took_it(self):
        answer = self.tried_with(connecting_for_the_job_ahead_then_never_reached, older=[77])

        self.assertEqual((answer['status'], answer['detail']['reason']), ('failed', 'printer_not_reached'), answer)
        self.assertEqual(self.server.cancelled, [self.server.TEST_JOB])

    def test_job_processing_for_the_whole_window_with_a_queue_that_never_connects_is_not_taken(self):
        answer = self.tried_with(never_connecting, message='The printer is unreachable at this time.')

        self.assertEqual((answer['status'], answer['detail']['reason']), ('failed', 'printer_not_reached'), answer)
        self.assertLessEqual(self.waited, self.WINDOW + 1)
        self.assertEqual(self.server.cancelled, [self.server.TEST_JOB])

    def test_device_address_with_credentials_is_nowhere_in_the_answer_and_the_address_is_the_host_and_port(self):
        answer = self.tried_with(never_reached, device_uri='ipp://user:s3cret@10.0.0.9:631/ipp/print')

        self.assertEqual(answer['detail']['reason'], 'printer_not_reached', answer)
        self.assertEqual(answer['detail']['address'], '10.0.0.9:631')
        self.assertNotIn('s3cret', json.dumps(answer))
        self.assertNotIn('user:', json.dumps(answer))

    def test_connection_is_named_only_for_the_kinds_of_device_address_the_box_can_name(self):
        for device_uri, connection, address in (
                ('hp:/net/HP_LaserJet?ip=10.0.0.5', '', None),
                ('cups-pdf:/', '', None),
                ('dnssd://Brother%20HL._ipp._tcp.local/', '', None),
                ('implicitclass://Q/', '', None),
                ('socket://192.168.1.77:9100', 'network', '192.168.1.77:9100'),
                ('ipp://10.0.0.9:631/ipp/print', 'network', '10.0.0.9:631'),
                ('usb://HP/LaserJet?serial=ABC123', 'usb', None),
                ('serial:/dev/ttyUSB0?baud=9600', 'serial', None)):
            with self.subTest(device_uri=device_uri):
                answer = self.tried_with(never_reached, device_uri=device_uri)

                self.assertEqual(answer['detail']['reason'], 'printer_not_reached', answer)
                self.assertEqual(answer['detail']['connection'], connection)
                self.assertEqual(answer['detail'].get('address'), address)


class PrintServerThatStopsAnswering(PrintServerAtTheMercyOfThePrinter):
    """A print server that takes the test job and then holds every question about it until `release` is set.

    Each connection the box opens is a new object. The first is the one that holds. The later ones answer at once,
    as a print server whose printer completes every job, and note each job cancelled on them in `cancelled_on` as
    (number of the connection, job); a cancel on them waits for `cancel_release`, which is set until a test clears it.
    """

    def __init__(self, clock):
        super().__init__(clock, never_connecting)
        self.release = threading.Event()
        self.cancel_release = threading.Event()
        self.cancel_release.set()
        self.opened = 0
        self.cancelled_on = []
        self.answering = PrintServerAtTheMercyOfThePrinter(clock, lambda _seconds: ('completed', []))

    def connection(self):
        self.opened += 1
        if self.opened == 1:
            return self
        server, opened = self, self.opened

        class Later:
            def __getattr__(self, name):
                return getattr(server.answering, name)

            def cancelJob(self, number):  # noqa: N802
                server.cancel_release.wait(30)
                server.cancelled_on.append((opened, number))

        return Later()

    def getJobAttributes(self, number, requested_attributes=None):  # noqa: N802
        self.release.wait(30)
        return super().getJobAttributes(number, requested_attributes)


class PrintServerSlowToHandBackThePrint(PrintServerAtTheMercyOfThePrinter):
    """A print server that queues the test page at once, but answers the call that printed it only once `answer` is
    set. Every connection the box opens reaches it and is counted in `opened`."""

    def __init__(self, clock, older=()):
        super().__init__(clock, never_connecting, older=older)
        self.answer = threading.Event()
        self.opened = 0

    def connection(self):
        self.opened += 1
        return self

    def printTestPage(self, queue):  # noqa: N802
        number = super().printTestPage(queue)
        self.answer.wait(30)
        return number


class StartedThreads:
    """The threads `starter` starts while the block runs, in `threads`."""

    def __init__(self, starter):
        self.starter, self.threads = starter, []

    def __enter__(self):
        self._start = original = threading.Thread.start
        starter, threads = self.starter, self.threads

        def start(thread):
            if threading.current_thread() is starter:
                threads.append(thread)
            original(thread)

        threading.Thread.start = start
        return self

    def __exit__(self, *error):
        threading.Thread.start = self._start


class PrintServerWithoutAnswerCase(DeviceTestCase):
    """Tests of a device whose calls of the box are left without answer, by the print server or the device.

    The threads each test request starts are kept; `left_behind_ended` sets every event of `releases` and waits for
    those threads, so that no later test of the printer finds a call of this one still without answer.
    """

    BOUND = 10

    def setUp(self):
        super().setUp()
        self.printers()
        self.tests = common.layer('device_tests')
        self.started = []
        self.releases = []

    def patched(self, name, **options):
        patcher = mock.patch.object(self.tests, name, **options)
        patcher.start()
        self.addCleanup(patcher.stop)

    def left_behind_ended(self):
        for release in self.releases:
            release.set()
        for thread in self.started:
            thread.join(self.BOUND)
        self.assertEqual([thread for thread in self.started if thread.is_alive()], [], 'threads left without end')

    def tried_within_the_bound(self, identifier=box.OFFICE_PRINTER):
        """(answer, seconds, threads the request started) of a test of the device `identifier`.

        Fails when the request is still open after BOUND seconds.
        """
        found = {}
        asking = threading.Thread(target=lambda: found.update(answer=self.tried(identifier)), daemon=True)
        began = time.monotonic()
        with StartedThreads(asking) as started:
            asking.start()
            asking.join(self.BOUND)
        self.started += started.threads
        self.assertFalse(asking.is_alive(), f'the request is still open after {self.BOUND} s')
        return found['answer'], time.monotonic() - began, started.threads


class TestPrinterTestWhenThePrintServerStopsAnswering(PrintServerWithoutAnswerCase):
    """The request that tests a printer ends in bounded real time whatever the print server does."""

    def setUp(self):
        super().setUp()
        for name, value in (('FOLLOW_WINDOW', 0.2), ('FOLLOW_MARGIN', 0.2), ('CANCEL_WAIT', 1)):
            self.patched(name, new=value)
        self.server = PrintServerThatStopsAnswering(DrivenClock())
        self.releases += [self.server.release, self.server.cancel_release]
        self.patched('_own_connection', side_effect=self.server.connection)
        self.addCleanup(self.left_behind_ended)

    def test_follow_up_that_never_ends_answers_print_server_silent_in_the_bound_and_cancels_on_a_new_connection(self):
        answer, seconds, _started = self.tried_within_the_bound()

        self.assertEqual((answer['status'], answer['detail']['reason']), ('failed', 'print_server_silent'), answer)
        self.assertLess(seconds, self.BOUND)
        self.assertEqual(self.server.cancelled_on, [(2, self.server.TEST_JOB)])

    def test_printer_whose_follow_up_is_still_without_answer_is_answered_print_server_silent_without_asking(self):
        self.tried_within_the_bound()
        opened = self.server.opened

        answer, _seconds, started = self.tried_within_the_bound()

        self.assertEqual((answer['status'], answer['detail']['reason']), ('failed', 'print_server_silent'), answer)
        self.assertEqual(self.server.opened, opened)
        self.assertEqual(started, [])

    def test_printer_whose_cancel_of_its_test_job_is_still_without_answer_is_answered_print_server_silent(self):
        self.server.cancel_release.clear()
        _answer, _seconds, first = self.tried_within_the_bound()
        self.server.release.set()
        # Of the threads the first test started, only the one cancelling its test job is still without answer.
        common.wait_until(lambda: sum(thread.is_alive() for thread in first) == 1, 'the end of the follow-up', 10)
        opened = self.server.opened

        answer, _seconds, started = self.tried_within_the_bound()

        self.assertEqual((answer['status'], answer['detail']['reason']), ('failed', 'print_server_silent'), answer)
        self.assertEqual(self.server.opened, opened)
        self.assertEqual(started, [])

    def test_device_is_free_for_the_next_test_once_the_print_server_answers_the_follow_up_left_behind(self):
        self.tried_within_the_bound()
        self.left_behind_ended()

        answer, _seconds, _started = self.tried_within_the_bound()

        self.assertEqual((answer['status'], answer['detail']['reason']), ('ok', 'taken'), answer)


class TestPrinterTestWhenTheDeviceCallPassesTheTimeLimit(PrintServerWithoutAnswerCase):
    """The print server holds the test job, but the call that handed it over has not ended within TEST_TIMEOUT."""

    def setUp(self):
        super().setUp()
        for name, value in (('TEST_TIMEOUT', 0.3), ('CANCEL_WAIT', 1)):
            self.patched(name, new=value)
        self.server = PrintServerSlowToHandBackThePrint(DrivenClock(), older=[77])
        self.releases.append(self.server.answer)
        self.patched('_own_connection', side_effect=self.server.connection)
        self.addCleanup(self.left_behind_ended)

    def test_device_call_past_the_time_limit_answers_print_server_silent_and_cancels_no_unknown_job(self):
        answer, _seconds, _started = self.tried_within_the_bound()

        self.assertEqual((answer['status'], answer['detail']['reason']), ('failed', 'print_server_silent'), answer)
        self.assertEqual(self.server.cancelled, [])

    def test_test_job_the_device_call_hands_back_after_the_time_limit_is_cancelled_and_no_other_job(self):
        self.tried_within_the_bound()

        self.left_behind_ended()

        self.assertEqual(self.server.cancelled, [self.server.TEST_JOB])

    def test_printer_whose_device_call_is_still_without_answer_is_answered_print_server_silent_without_asking(self):
        self.tried_within_the_bound()
        opened = self.server.opened

        answer, _seconds, started = self.tried_within_the_bound()

        self.assertEqual((answer['status'], answer['detail']['reason']), ('failed', 'print_server_silent'), answer)
        self.assertEqual(self.server.opened, opened)
        self.assertEqual(started, [])


class TestReceiptPrinterTestWhenThePrintServerFallsSilent(PrintServerWithoutAnswerCase):
    """The request has answered at the time limit while the print server, silent, still holds a call of the test."""

    def setUp(self):
        super().setUp()
        self.patched('TEST_TIMEOUT', new=1)
        self.addCleanup(self.left_behind_ended)

    def silent(self, moment):
        self.server = PrintQueueSharedWithThePointOfSale(DrivenClock(), box.RECEIPT_PRINTER, silent=moment)
        self.releases.append(types.SimpleNamespace(set=self.server.answer_again))
        self.patched('_own_connection', side_effect=lambda: self.server)
        self.patched('subprocess', new=self.server.commands())

    def test_ticket_sent_while_the_print_server_is_silent_is_never_cancelled_by_the_test_left_behind(self):
        self.silent('after_taking_the_print')
        answer, _seconds, _started = self.tried_within_the_bound(box.RECEIPT_PRINTER)
        self.assertIsNotNone(self.server.test_job, 'the print server took no test print before it fell silent')
        self.server.ticket()

        self.left_behind_ended()

        self.assertEqual((answer['status'], answer['detail']['reason']), ('failed', 'print_server_silent'), answer)
        self.assertTrue(self.server.tickets)
        self.assertEqual(self.server.cancelled, [self.server.test_job])

    def test_test_print_is_not_sent_once_the_test_has_answered_while_the_print_server_was_silent(self):
        self.silent('before_the_print')
        answer, _seconds, _started = self.tried_within_the_bound(box.RECEIPT_PRINTER)

        self.left_behind_ended()

        self.assertEqual((answer['status'], answer['detail']['reason']), ('failed', 'print_server_silent'), answer)
        self.assertEqual(self.server.sent, [])
        self.assertEqual(self.server.jobs, [])


class TestScaleTestWhenTheReadingPassesTheTimeLimit(PrintServerWithoutAnswerCase):

    def test_scale_reading_past_the_time_limit_answers_timeout(self):
        [scale] = box.wait_for_devices(box.SCALE)
        reading = threading.Event()
        self.releases.append(reading)
        self.patched('TEST_TIMEOUT', new=0.3)
        held = mock.patch.object(scale, 'action', side_effect=lambda _data: reading.wait(30))
        held.start()
        self.addCleanup(held.stop)
        self.addCleanup(self.left_behind_ended)

        answer, _seconds, _started = self.tried_within_the_bound(box.SCALE)

        self.assertEqual((answer['status'], answer['detail']['reason']), ('failed', 'timeout'), answer)


class CoreDevice:
    """A device as a driver of core makes it: `device_connection` is what core derived, `dev` what core found."""

    device_manufacturer = ''
    _actions = {}

    def __init__(self, device_type, device_connection, dev=None, subtype=''):
        self.device_identifier = self.device_name = f'Suite_{device_type}'
        self.device_type, self.device_connection, self.dev = device_type, device_connection, dev or {}
        self.device_subtype = subtype


class TestDevicesPanelConnection(common.BoxCase):
    """The Devices panel names how a printer is connected as its print test does, never by its device address."""

    QUEUE = 'Suite_printer'

    def setUp(self):
        super().setUp()
        self.tests = common.layer('device_tests')

    def described_printer(self, device_uri, core_names, queued=True):
        # Core keeps the device address of a print queue in `device-uri` (its `url` is the queue name), and that
        # of a printer it only discovered in `url`.
        found = {'device-uri': device_uri, 'url': self.QUEUE} if queued else {'url': device_uri}
        return self.tests.describe(CoreDevice('printer', core_names, found, subtype='office_printer'))

    def test_printer_connection_is_named_by_the_kind_of_its_device_address_whatever_core_names_it(self):
        for device_uri, queued, core_names, connection in (
                ('socket://192.168.1.77:9100', True, 'network', 'network'),
                ('ipp://10.0.0.9:631/ipp/print', True, 'network', 'network'),
                ('ipps://printer.example.invalid/ipp/print', True, 'network', 'network'),
                ('http://10.0.0.8:631/printers/Office', True, 'network', 'network'),
                ('https://10.0.0.8/ipp/print', True, 'network', 'network'),
                ('lpd://10.0.0.7/queue', True, 'network', 'network'),
                ('usb://HP/LaserJet?serial=ABC123', True, 'direct', 'usb'),
                ('serial:/dev/ttyS0?baud=9600', True, 'network', 'serial'),
                ('hp:/net/HP_LaserJet?ip=10.0.0.5', True, 'network', ''),
                ('dnssd://Brother%20HL._ipp._tcp.local/', True, 'network', ''),
                ('implicitclass://Office/', True, 'network', ''),
                ('socket://', True, 'network', ''),
                ('usb://Zebra/ZD420?serial=XYZ', False, 'direct', 'usb'),
                ('socket://10.0.0.3:9100', False, 'network', 'network'),
                ('serial:/dev/ttyUSB0?baud=9600', False, 'serial', 'serial')):
            with self.subTest(device_uri=device_uri, queued=queued):
                self.assertEqual(self.described_printer(device_uri, core_names, queued)['connection'], connection)

    def test_devices_panel_names_no_device_address_nor_credential_of_a_printer(self):
        described = json.dumps(self.described_printer('ipp://user:s3cret@10.0.0.9:631/ipp/print', 'network'))

        self.assertNotIn('s3cret', described)
        self.assertNotIn('user:', described)
        self.assertNotIn('10.0.0.9', described)

    def test_connection_of_a_device_other_than_a_printer_is_the_one_core_gives(self):
        for device_type, core_names in (('scale', 'serial'), ('keyboard', 'direct'), ('display', 'hdmi')):
            with self.subTest(device_type=device_type):
                self.assertEqual(self.tests.describe(CoreDevice(device_type, core_names))['connection'], core_names)


class TestScaleTest(DeviceTestCase):

    def test_scale_test_reads_the_weight_on_the_scale_now(self):
        [scale] = box.wait_for_devices(box.SCALE)
        box.keep_files(self, box.SCALE_FILE)
        # The scale no longer sends its weight by itself, as when no page reads it: only the test reads it.
        scale.action({'action': 'stop_reading'})
        self.addCleanup(scale.action, {'action': 'start_reading'})
        box.SCALE_FILE.write_text(f'{WEIGHT}\n')

        answer = self.tried(box.SCALE)

        self.assertEqual(answer['status'], 'ok', answer)
        self.assertEqual(answer['detail']['value'], WEIGHT)

    def test_scale_test_gives_no_unit_when_the_scale_gives_none(self):
        [scale] = box.wait_for_devices(box.SCALE)
        box.keep_files(self, box.SCALE_FILE)
        scale.action({'action': 'stop_reading'})
        self.addCleanup(scale.action, {'action': 'start_reading'})
        box.SCALE_FILE.write_text(f'{WEIGHT}\n')

        answer = self.tried(box.SCALE)

        self.assertEqual(answer['status'], 'ok', answer)
        self.assertNotIn('unit', answer['detail'])

    def test_scale_test_gives_the_unit_the_scale_gives(self):
        [scale] = box.wait_for_devices(box.SCALE)
        scale.action({'action': 'stop_reading'})
        self.addCleanup(scale.action, {'action': 'start_reading'})
        patched = mock.patch.dict(scale.data)
        patched.start()
        self.addCleanup(patched.stop)
        read = mock.patch.object(scale, 'action', side_effect=lambda _data: scale.data.update(
            value=WEIGHT, unit='lb', status={'status': 'connected'}))
        read.start()
        self.addCleanup(read.stop)

        answer = self.tried(box.SCALE)

        self.assertEqual(answer['status'], 'ok', answer)
        self.assertEqual((answer['detail']['value'], answer['detail']['unit']), (WEIGHT, 'lb'))


class TestScannerTakenForAKeyboard(common.BoxCase):

    def setUp(self):
        super().setUp()
        [self.scanner, *_others] = box.wait_for_devices(
            box.SCANNER, box.SCALE, box.OFFICE_PRINTER, box.LABEL_PRINTER, box.RECEIPT_PRINTER)
        self.addCleanup(self.scanner.action, {'action': 'update_is_scanner', 'is_scanner': True})
        # A server that takes the reports of the box: none is owed when the scanner is switched.
        self.server_of_the_box = self.box_with_server(CREDENTIAL, channel='box-suite-channel')
        _answer, self.proof = self.unlock(box.settings_code(CREDENTIAL))

    def reported_kinds(self):
        return [request['body']['params']['devices'].get(box.SCANNER, {}).get('type')
                for request in self.server_of_the_box.requests if request['path'] == '/iot/setup']

    def test_scanner_is_used_as_a_keyboard_in_one_action_and_the_next_report_says_so(self):
        self.report_to(self.server_of_the_box)
        # Two passes of core's device loop: whatever the box was still finding is reported before the switch.
        time.sleep(7)
        reported = len(self.reported_kinds())

        answer = self.call(box.USE_AS, proof=self.proof, device_identifier=box.SCANNER, is_scanner=False)

        self.assertEqual(answer, {'status': 'switched', 'kind': 'keyboard'})
        common.wait_until(lambda: self.reported_kinds()[reported:], 'the next report of the box', 20)
        self.assertEqual(self.reported_kinds()[reported], 'keyboard')

    def test_scanner_is_not_switched_without_the_unlock_proof(self):
        answer = self.call(box.USE_AS, device_identifier=box.SCANNER, is_scanner=False)

        self.assertEqual(answer.get('reason'), box.LOCKED)
        self.assertEqual(self.scanner.device_type, 'scanner')

    def test_page_of_another_web_site_does_not_switch_the_scanner_even_with_the_unlock_proof(self):
        response = self.request(box.USE_AS, {'device_identifier': box.SCANNER, 'is_scanner': False}, proof=self.proof,
                                sent=box.FROM_ANOTHER_WEB_SITE)

        self.assertEqual(response.get_json()['result'].get('reason'), box.OTHER_SITE)
        self.assertEqual(self.scanner.device_type, 'scanner')


class TestOfficePrinterThatIsALabelPrinter(common.BoxCase):

    def setUp(self):
        super().setUp()
        box.wait_for_devices(box.OFFICE_PRINTER)
        self.addCleanup(self.put_back_the_print_queues)

    @staticmethod
    def put_back_the_print_queues():
        queues = box.print_queues()
        made = [line.split()[2].rstrip(':') for line in queues.splitlines()
                if line.startswith('device for ') and 'ZPL' in line.split()[2]]
        box.remove_print_queues(*made)
        if f'device for {box.OFFICE_PRINTER}:' not in queues:
            box.add_print_queue(box.OFFICE_PRINTER, f'softbox:/{box.OFFICE_PRINTER}')

    def test_office_printer_gets_a_print_queue_core_takes_for_a_label_printer(self):
        answer = self.call(box.LABEL_PRINTER_ROUTE, device_identifier=box.OFFICE_PRINTER)

        self.assertEqual(answer['status'], 'added', answer)
        self.assertEqual(box.kind_core_gives(answer['queue']), 'label_printer')

    def test_original_print_queue_of_the_office_printer_stays_and_still_prints_after_it_is_taken_for_a_label_printer(
            self):
        answer = self.call(box.LABEL_PRINTER_ROUTE, device_identifier=box.OFFICE_PRINTER)
        before = box.print_jobs(box.OFFICE_PRINTER)

        tried = self.call(box.DEVICE_TEST, device_identifier=box.OFFICE_PRINTER)

        self.assertNotEqual(answer['queue'], box.OFFICE_PRINTER)
        self.assertIn(f'device for {box.OFFICE_PRINTER}: ', box.print_queues())
        self.assertEqual((tried['status'], tried['kind']), ('ok', 'office_printer'), tried)
        self.assertTrue(box.new_print_job(box.OFFICE_PRINTER, before))

    def test_page_of_another_web_site_changes_no_print_queue(self):
        before = box.print_queues()

        response = self.request(box.LABEL_PRINTER_ROUTE, {'device_identifier': box.OFFICE_PRINTER},
                                sent=box.FROM_ANOTHER_WEB_SITE)

        self.assertEqual(response.get_json()['result'].get('reason'), box.OTHER_SITE)
        self.assertEqual(box.print_queues(), before)

    def test_locked_box_changes_no_print_queue_without_the_unlock_proof(self):
        self.box_with_server(CREDENTIAL)
        before = box.print_queues()

        answer = self.call(box.LABEL_PRINTER_ROUTE, device_identifier=box.OFFICE_PRINTER)

        self.assertEqual(answer.get('reason'), box.LOCKED)
        self.assertEqual(box.print_queues(), before)

    def label_queue_on_the_page(self):
        """The print queue the Devices panel names for the office printer as a label printer, '' when it names none."""
        devices = common.layer('selftest').status(wait=0)['devices']
        office = next(device for device in devices if device['identifier'] == box.OFFICE_PRINTER)
        return office['printer']['label_queue']

    def zpl_queues(self):
        return [line for line in box.print_queues().splitlines() if line.startswith('device for ') and 'ZPL' in line]

    def test_second_press_adds_no_second_label_queue_and_names_the_one_the_printer_has(self):
        first = self.call(box.LABEL_PRINTER_ROUTE, device_identifier=box.OFFICE_PRINTER)
        queues = box.print_queues()

        second = self.call(box.LABEL_PRINTER_ROUTE, device_identifier=box.OFFICE_PRINTER)

        self.assertEqual(first['status'], 'added', first)
        self.assertEqual(second, {'status': 'refused', 'reason': 'already_added', 'queue': first['queue']})
        self.assertEqual(box.print_queues(), queues)
        self.assertEqual(len(self.zpl_queues()), 1)

    def test_devices_panel_names_the_label_queue_of_the_office_printer_only_once_it_has_one(self):
        self.assertEqual(self.label_queue_on_the_page(), '')

        added = self.call(box.LABEL_PRINTER_ROUTE, device_identifier=box.OFFICE_PRINTER)

        self.assertEqual(self.label_queue_on_the_page(), added['queue'])


class TestDeviceListWhileThePrintServerIsSlow(common.BoxCase):
    """The page reads the device list about every 15 seconds: the print server never holds a read for long."""

    class PrintServer:
        """What the box asks for its print queues: counts the questions, and holds them while `hang` is set."""

        def __init__(self, queues, hang):
            self.queues, self.hang, self.asked = queues, hang, 0
            self.release = threading.Event()

        def getPrinters(self):  # noqa: N802 - the name pycups gives it
            self.asked += 1
            if self.hang:
                self.release.wait(60)
            return self.queues

    def setUp(self):
        super().setUp()
        box.wait_for_devices(box.OFFICE_PRINTER)
        self.tests = common.layer('device_tests')
        self.addCleanup(self.forget_the_answer)
        self.forget_the_answer()

    def forget_the_answer(self):
        # A question still on its way is ended too: its late answer must never pass for a fresh one in a later test.
        with self.tests._known_lock:
            self.tests._known.update(queues=None, at=None, thread=None)
            self.tests._known['generation'] += 1

    def with_print_server(self, hang, queues=None):
        server = self.PrintServer(queues or {}, hang)
        self.addCleanup(server.release.set)
        patched = mock.patch.object(self.tests, '_own_connection', return_value=server)
        patched.start()
        self.addCleanup(patched.stop)
        return server

    def office_printer(self):
        return next(device for device in self.tests.devices() if device['identifier'] == box.OFFICE_PRINTER)

    def test_device_list_is_given_within_the_wait_for_a_print_server_that_does_not_answer(self):
        self.with_print_server(hang=True)

        started = time.monotonic()
        office = self.office_printer()

        self.assertLess(time.monotonic() - started, LONGEST_READ)
        self.assertEqual(office['printer']['label_queue'], '')

    def test_print_server_is_asked_once_for_reads_within_a_poll_period(self):
        server = self.with_print_server(hang=False)

        for _read in range(3):
            self.office_printer()

        self.assertEqual(server.asked, 1)

    def test_print_server_that_is_still_not_answering_is_not_asked_again_by_the_next_read(self):
        server = self.with_print_server(hang=True)

        self.office_printer()
        self.office_printer()

        self.assertEqual(server.asked, 1)

    def test_queue_just_added_for_a_label_printer_is_named_at_once_although_the_print_server_is_slow(self):
        added = self.call(box.LABEL_PRINTER_ROUTE, device_identifier=box.OFFICE_PRINTER)
        self.addCleanup(self.remove_label_queues)
        self.with_print_server(hang=True)

        office = self.office_printer()

        self.assertEqual(added['status'], 'added', added)
        self.assertEqual(office['printer']['label_queue'], added['queue'])

    @staticmethod
    def remove_label_queues():
        made = [line.split()[2].rstrip(':') for line in box.print_queues().splitlines()
                if line.startswith('device for ') and 'ZPL' in line.split()[2]]
        box.remove_print_queues(*made)
