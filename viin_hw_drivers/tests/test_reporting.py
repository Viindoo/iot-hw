"""What became of the reports of the box to its server, and the reports a part of the layer asks for."""
import time

import common
import services_common as box

# How long the suite has a report wait for a server that does not answer, instead of the half minute of a box.
SHORT_WAIT = 2


def follow_the_reports_of_a_box():
    """Start a box, have it report to servers that take, refuse, are down or never answer, and say what it kept.

    Gives the `outcome` the box kept after each report ('taken', 'refused', 'down', 'silent'),
    'sent_unasked' (reports the box sent while nothing changed and nobody asked),
    'sent_when_asked' (those it sent once a part of the layer asked for one) and
    'waited_for_silent', the seconds the report to the silent server held its caller.
    """
    common.start()
    reporting = common.layer('reporting')
    manager = common.core('main').manager
    taking = common.FakeServer(channel='box-suite-channel')
    happened = {}

    def report_to(server_url):
        manager.server_url = server_url
        manager.send_alldevices()
        return reporting.outcome()

    happened['refused'] = report_to(common.FakeServer().url)
    happened['down'] = report_to(f'http://127.0.0.1:{common.closed_port()}')
    happened['taken'] = report_to(taking.url)

    def reports():
        return len([request for request in taking.requests if request['path'] == '/iot/setup'])

    # Core's device loop runs every three seconds: two of its passes see whatever was still changing.
    time.sleep(7)
    settled = reports()
    time.sleep(7)
    happened['sent_unasked'] = reports() - settled
    before = reports()
    reporting.request_report()
    try:
        common.wait_until(lambda: reports() > before, 'the report a part of the layer asked for', 15)
    except AssertionError:
        pass
    happened['sent_when_asked'] = reports() - before

    silent = box.SilentServer()
    reporting.REPORT_TIMEOUT = SHORT_WAIT
    started = time.monotonic()
    happened['silent'] = report_to(silent.url)
    happened['waited_for_silent'] = time.monotonic() - started
    return happened


class TestReportOutcome(common.BoxStateCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.happened = common.in_new_process(follow_the_reports_of_a_box)

    def test_report_the_server_takes_is_known_as_accepted_and_nothing_is_owed(self):
        outcome = self.happened['taken']

        self.assertEqual(outcome['state'], 'accepted')
        self.assertIs(outcome['owed'], False)
        self.assertIsNotNone(outcome['accepted_at'])

    def test_report_a_server_answers_without_taking_it_is_known_as_refused_and_stays_owed(self):
        outcome = self.happened['refused']

        self.assertEqual(outcome['state'], 'refused')
        self.assertIs(outcome['owed'], True)

    def test_report_to_a_server_that_is_down_is_known_as_unreachable_and_stays_owed(self):
        outcome = self.happened['down']

        self.assertEqual(outcome['state'], 'unreachable')
        self.assertIs(outcome['owed'], True)

    def test_report_asked_for_by_a_part_of_the_layer_is_sent_though_nothing_changed(self):
        self.assertEqual(self.happened['sent_unasked'], 0)
        self.assertGreaterEqual(self.happened['sent_when_asked'], 1)

    def test_server_that_never_answers_holds_the_report_no_longer_than_the_box_waits(self):
        self.assertLess(self.happened['waited_for_silent'], SHORT_WAIT + 5)
        self.assertEqual(self.happened['silent']['state'], 'unreachable')
        self.assertIs(self.happened['silent']['owed'], True)
