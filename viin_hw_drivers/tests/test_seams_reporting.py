"""What the server learns of the box, and the live channel of the box: rows 5, 6 and 8 of the seams of the layer."""
import time

import common
import services_common as box

CREDENTIAL = 'suite-credential-reports'
DEVICE = 'box_suite_device'
OWN_NAME = 'box.shop.example'
# The seams the box applies once it has started, but those that a test of this file moves.
SEAMS = (1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 15, 17, 18)


def report_changes_made_while_the_server_is_down(port):
    """Start a box whose server is down, change its devices and its address, then bring the server up.

    Gives 'first_report' (what the server got first once it was up: the devices and the address)
    and 'tried_meanwhile', whether the box tried to tell the server while it was down.
    """
    common.start()
    reporting = common.layer('reporting')
    # A box tries again within a minute; the suite does not wait that long.
    reporting.RETRY_LONGEST = 2
    changed_at = time.time()
    box.plug(DEVICE)
    common.store_box_configuration(viin_box_address=OWN_NAME)
    # Core looks at its devices and its address every three seconds: two looks see both changes.
    time.sleep(7)
    common.wait_until(lambda: (reporting.outcome()['at'] or 0) > changed_at, 'the box to report the change')
    tried = reporting.outcome()['state'] == reporting.UNREACHABLE
    server = box.LiveChannelServer(port=port, channel='')
    common.wait_until(server.reports, 'the server to hear from the box', 60)
    first = server.reports()[0]
    return {'tried_meanwhile': tried,
            'first_report': {'devices': sorted(first['devices']), 'ip': first['iot_box']['ip']}}


def bring_the_server_up_after_the_box_started(port):
    """Start a box whose server is down, then bring the server up; give what the box did without a restart."""
    common.start()
    reporting = common.layer('reporting')
    reporting.RETRY_LONGEST = 2
    before = reporting.channel()['state']
    server = box.LiveChannelServer(port=port)
    try:
        common.wait_until(server.subscriptions, 'the box to subscribe to its live channel', 60)
    except AssertionError:
        pass
    return {'channel_before': before, 'subscribed': bool(server.subscriptions()), 'restarts': len(common.restarts)}


def reconnect_the_live_channel(port):
    """Start a box beside its server, have the server drop the live channel, and give what came after.

    Gives 'after_drop': what the server received after it dropped the channel, in order, each as
    ('report', devices reported) or ('subscribe', ''), until the box subscribed again.
    """
    server = box.LiveChannelServer(port=port)
    common.start()
    common.wait_until(server.subscriptions, 'the box to subscribe to its live channel', 60)
    box.plug(DEVICE)
    common.wait_until(lambda: any(DEVICE in report['devices'] for report in server.reports()), 'the new device')
    server.drop()
    common.wait_until(lambda: server.kinds()[server.kinds().index('drop'):].count('subscribe'),
                      'the box to subscribe again', 60)
    after = server.happened[server.kinds().index('drop') + 1:]
    return {'after_drop': [(kind, sorted(what['devices']) if kind == 'report' else '') for kind, what in after]}


def serve_the_server_as_a_stock_box_does(port):
    """Start a box with a network card beside its server, then have the server print on its receipt printer.

    Gives the 'subscription' the box sent, the 'identifier' its reports carry, the 'printers' it
    reported with their kind, the 'completions' the server got and whether the receipt was
    'printed'.
    """
    common.give_a_hardware_address()
    server = box.LiveChannelServer(port=port)
    common.start()
    common.wait_until(server.subscriptions, 'the box to subscribe to its live channel', 60)
    box.wait_for_devices(box.RECEIPT_PRINTER, box.LABEL_PRINTER, box.OFFICE_PRINTER)
    printers = (box.RECEIPT_PRINTER, box.LABEL_PRINTER, box.OFFICE_PRINTER)
    common.wait_until(lambda: any(set(printers) <= set(report['devices']) for report in server.reports()),
                      'the box to report its printers')
    reported = next(report for report in reversed(server.reports()) if set(printers) <= set(report['devices']))
    subscription = server.subscriptions()[-1]
    before = box.print_jobs(box.RECEIPT_PRINTER)
    server.send(box.print_command(
        subscription['mac_address'], box.RECEIPT_PRINTER, 'box-suite-print-1', b'Box suite receipt\n'))
    try:
        common.wait_until(server.completions, 'the box to confirm the print', 30)
        printed = box.new_print_job(box.RECEIPT_PRINTER, before)
    except AssertionError:
        printed = b''
    return {
        'subscription': subscription,
        'identifier': reported['iot_box']['identifier'],
        'printers': {printer: reported['devices'][printer]['subtype'] for printer in printers},
        'completions': server.completions(),
        'printed': b'Box suite receipt' in printed,
    }


class ServerAtAPortCase(common.BoxStateCase):
    """A box whose server is at a port of this machine, the child process listening there when it says so."""

    def configure_a_server(self):
        port = common.closed_port()
        common.store_box_configuration(remote_server=f'http://127.0.0.1:{port}', token=CREDENTIAL)
        return port


class TestChangesMadeWhileTheServerIsDown(ServerAtAPortCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.happened = None

    def setUp(self):
        super().setUp()
        if type(self).happened is None:
            type(self).happened = common.in_new_process(
                report_changes_made_while_the_server_is_down, port=self.configure_a_server())

    def test_device_plugged_while_the_server_is_down_is_known_to_the_server_at_its_first_contact(self):
        self.assertTrue(self.happened['tried_meanwhile'])
        self.assertIn(DEVICE, self.happened['first_report']['devices'])

    def test_address_changed_while_the_server_is_down_is_known_to_the_server_at_its_first_contact(self):
        self.assertEqual(self.happened['first_report']['ip'], OWN_NAME)


class TestServerDownWhenTheBoxStarted(ServerAtAPortCase):

    def test_box_gets_its_live_channel_once_the_server_is_up_without_a_restart(self):
        happened = common.in_new_process(bring_the_server_up_after_the_box_started, port=self.configure_a_server())

        self.assertNotEqual(happened['channel_before'], 'connected')
        self.assertTrue(happened['subscribed'])
        self.assertEqual(happened['restarts'], 0)


class TestLiveChannelThatReconnects(ServerAtAPortCase):

    def test_box_reports_all_its_devices_before_it_subscribes_again(self):
        happened = common.in_new_process(reconnect_the_live_channel, port=self.configure_a_server())

        after = happened['after_drop']
        self.assertEqual([kind for kind, _devices in after][-1], 'subscribe')
        self.assertEqual(after[0][0], 'report', after)
        self.assertIn(DEVICE, after[0][1])


class TestWhatAStockBoxDoesForTheServer(ServerAtAPortCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.happened = None

    def setUp(self):
        super().setUp()
        if type(self).happened is None:
            type(self).happened = common.in_new_process(serve_the_server_as_a_stock_box_does,
                                                        port=self.configure_a_server())

    def test_box_subscribes_to_its_channel_under_the_hardware_address_it_reports(self):
        subscription = self.happened['subscription']

        self.assertEqual(subscription['channels'], ['box-suite-channel'])
        self.assertEqual(subscription['mac_address'], self.happened['identifier'])

    def test_box_reports_the_kind_of_each_printer(self):
        self.assertEqual(self.happened['printers'], {
            box.RECEIPT_PRINTER: 'receipt_printer', box.LABEL_PRINTER: 'label_printer',
            box.OFFICE_PRINTER: 'office_printer'})

    def test_print_command_of_the_server_prints_and_is_confirmed_with_its_print_id(self):
        completions = self.happened['completions']

        self.assertTrue(self.happened['printed'])
        self.assertEqual(
            [(completion['print_id'], completion['device_identifier']) for completion in completions],
            [('box-suite-print-1', box.RECEIPT_PRINTER)])
        self.assertEqual(completions[0]['iot_mac'], self.happened['identifier'])


class TestReportOutcomeSeamMoved(common.MovedSeamCase):
    number = 5
    core_changes = {
        'def send_alldevices(self, iot_client=None):': 'def send_alldevices(self, iot_client=None, force=False):'}
    other_seams = tuple(seam for seam in SEAMS if seam != 5)


class TestReportOwedSeamMoved(common.MovedSeamCase):
    number = 6
    core_changes = {'def _get_changes_to_send(self):': 'def _get_changes_to_send(self, force=False):'}
    other_seams = tuple(seam for seam in SEAMS if seam != 6)


class TestLiveChannelSeamMoved(common.MovedSeamCase):
    number = 8
    core_changes = {
        'def on_close(ws, close_status_code, close_msg):': 'def on_close(ws, close_status_code, close_msg, why=None):'}
    other_seams = tuple(seam for seam in SEAMS if seam != 8)
