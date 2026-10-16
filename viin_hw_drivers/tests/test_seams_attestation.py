"""The readings the box confirms to its server, and the start each event names: row 9 of the seams of the layer."""
import json
import tempfile

import common
import services_common as box

CREDENTIAL = 'suite-credential-confirmed-readings'
PAIR = '/viin_hw_homepage/pair'
CODE = 'K7Q2-M9XD-4TWP'
PAIRED = {'status': 'paired', 'token': CREDENTIAL, 'box_name': 'Weighing station'}
ACTION = '/hw_drivers/action'
READING = '/iot/reading'
SESSION = 'box-suite-confirmed-readings-session'
REQUEST = 'box-suite-request-of-a-moved-core'
WEIGHT = 2.75
NUMBER = 9
# The seams the box applies once it has started, but this one.
SEAMS = (1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 15, 17, 18)
# Core's event manager takes one more parameter in its call for a device that changed.
SIGNATURE_MOVED = {'def device_changed(self, device):': 'def device_changed(self, device, origin=None):'}
# Core's event no longer carries the data of the action under way under the key the pages read.
EVENT_MOVED = {"'request_data': ": "'action_data': "}


def read_on_a_box_whose_event_seam_moved():
    """Start a box whose core moved SIGNATURE_MOVED, pair it, then have its server ask for a confirmed reading.

    Meant for `common.in_new_process`. Gives 'state' (of seam NUMBER), 'pair_request' (what the
    server got to pair the box), 'answer' (of the action route), 'events' (those core holds for
    the read) and 'confirmations' (what the server got on READING).
    """
    configuration = common.configuration_file(tempfile.mkdtemp(prefix='box-suite-'), core_changes=SIGNATURE_MOVED)
    common.start(configuration)
    server = common.FakeServer(answer=PAIRED)
    paired = common.call_route(PAIR, link=server.link(CODE))
    common.forget_cached_configuration()
    box.wait_for_devices(box.SCALE)
    box.SCALE_FILE.write_text(f'{WEIGHT}\n')
    answer = common.call_route(ACTION, session_id=SESSION, device_identifier=box.SCALE, data=json.dumps(
        {'action': 'read_once', 'viin_iot_request': REQUEST, 'viin_iot_attest': True}))
    events = [event for event in common.core('event_manager').event_manager.events
              if (event.get('request_data') or {}).get('viin_iot_request') == REQUEST]
    return {
        'state': common.seam_states(common.layer('seams').integrity()).get(NUMBER),
        'paired': paired.get('status'),
        'pair_request': server.pairing_requests[0],
        'answer': answer,
        'events': events,
        'confirmations': [request['body'] for request in server.requests if request['path'] == READING],
    }


class TestPairRequestOfABoxThatConfirmsReadings(common.BoxCase):

    def test_box_whose_confirmation_seam_is_applied_says_so_when_it_pairs(self):
        server = self.server(answer=PAIRED)

        answer = self.pair(link=server.link(CODE))

        self.assertEqual(common.seam_states(common.layer('seams').integrity()).get(NUMBER), 'applied')
        self.assertEqual(answer.get('status'), 'paired', answer)
        self.assertIs(server.pairing_requests[0].get('confirms_readings'), True)


class TestConfirmedReadingsSeamMoved(common.MovedSeamCase):
    number = NUMBER
    core_changes = SIGNATURE_MOVED
    other_seams = SEAMS


class TestConfirmedReadingsSeamWhoseEventMoved(common.MovedSeamCase):
    number = NUMBER
    core_changes = EVENT_MOVED
    other_seams = SEAMS


class TestBoxWhoseConfirmationSeamMoved(common.BoxStateCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.happened = None

    def setUp(self):
        super().setUp()
        box.keep_files(self, box.SCALE_FILE)
        if type(self).happened is None:
            type(self).happened = common.in_new_process(read_on_a_box_whose_event_seam_moved)

    def test_pair_request_does_not_say_that_the_box_confirms_readings(self):
        self.assertEqual(self.happened['state'], 'failed')
        self.assertEqual(self.happened['paired'], 'paired')
        self.assertNotIn('confirms_readings', self.happened['pair_request'])

    def test_reading_the_server_asked_for_goes_to_the_page_unconfirmed(self):
        self.assertIs(self.happened['answer'], True)
        self.assertEqual([event['value'] for event in self.happened['events']], [WEIGHT])
        self.assertEqual(self.happened['confirmations'], [])

    def test_events_stay_as_core_makes_them(self):
        self.assertTrue(self.happened['events'])
        for event in self.happened['events']:
            self.assertNotIn('viin_boot_id', event)
