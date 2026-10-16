"""The readings the box confirms to its own server before a page gets them, and the start each event names."""
import itertools
import json
import math
import socket
import threading
import time
import uuid
from unittest import mock

import common
import services_common as box

CREDENTIAL = 'suite-credential-readings-7Q4V'
ACTION = '/hw_drivers/action'
EVENT = '/hw_drivers/event'
READING = '/iot/reading'
SESSION = 'box-suite-readings-session'
LISTENER = 'box-suite-readings-listener'
INSTRUMENT = 'box_suite_instrument'
BOOT_ID = 'viin_boot_id'
# A weight no other line of the log holds by chance.
WEIGHT = 48.271953
# How long the box waits for its server at most before a page gets a reading the server asked for.
CAP = 3
# What a busy software box adds to a wait the box itself bounds, and the share of CAP within which a read that has
# nothing to wait for is confirmed (or left unconfirmed). Both bound the time the box spends in the confirmation of
# a read, measured from inside the box: the round trip of the request that carried the read is not part of it.
SLACK = 1.0
AT_ONCE = CAP / 2

_instrument_classes = []


def event_manager():
    return common.core('event_manager').event_manager


def plug_instrument(identifier):
    """Plug, into the box started in this process, a device that reads what its `shows` holds; return it.

    Its `read_once` action puts `shows` in the data of the device, as a driver puts what it read
    there, and tells the pages that listen to it.
    """
    if not _instrument_classes:
        class Instrument(common.core('driver').Driver):

            def __init__(self, identifier):
                super().__init__(identifier, None)
                self.device_type = 'scale'
                self.device_name = 'Box suite instrument'
                self.shows = {'value': WEIGHT}
                self._actions['read_once'] = self._read_once

            def _read_once(self, data):
                self.data = dict(self.shows)
                event_manager().device_changed(self)

        _instrument_classes.append(Instrument)
    instrument = _instrument_classes[0](identifier)
    common.core('main').iot_devices[identifier] = instrument
    return instrument


def events_of(request):
    """The events core holds for the read whose action carried the value-request token `request`."""
    return [event for event in list(event_manager().events)
            if (event.get('request_data') or {}).get('viin_iot_request') == request]


def start_named_by_an_event():
    """Start a box, read a device once and give the start its event names. Meant for `common.in_new_process`."""
    common.start()
    plug_instrument(INSTRUMENT)
    common.call_route(
        ACTION, session_id=SESSION, device_identifier=INSTRUMENT, data=json.dumps({'action': 'read_once'}))
    return [event for event in event_manager().events if event['device_identifier'] == INSTRUMENT][-1].get(BOOT_ID)


class ServerThatTakesReadings(common.FakeServer):
    """A server that answers each confirmation of a reading with `status`.

    `released_when_received` tells, per confirmation, whether a page could already have the
    reading when it came: the event in core's list, or handed to the page waiting for it.
    """

    def __init__(self, status=200, **options):
        self.status = status
        self.released_when_received = []
        super().__init__(**options)

    @property
    def confirmations(self):
        return [request['body'] for request in self.requests if request['path'] == READING]

    def answer_to(self, method, path, body):
        if path != READING:
            return super().answer_to(method, path, body)
        request = body['params'].get('request') if isinstance(body, dict) else None
        handed = [session['result'] for session in list(event_manager().sessions.values())]
        self.released_when_received.append(bool(events_of(request)) or any(
            (event.get('request_data') or {}).get('viin_iot_request') == request for event in handed if event))
        return self.status, common.rpc_result(True)


class ServerThatTakesItsTime(ServerThatTakesReadings):
    """A server that answers each confirmation of a reading `delay` seconds after it came."""

    def __init__(self, delay, **options):
        self.delay = delay
        super().__init__(**options)

    def answer_to(self, method, path, body):
        if path == READING:
            time.sleep(self.delay)
        return super().answer_to(method, path, body)


class ServerThatNeverAnswers:
    """A server that reads the request on each connection and never answers; it lets go after `holds` seconds.

    `paths` lists the path of each request it read.
    """

    def __init__(self, holds=20):
        self.paths = []
        self._holds = holds
        self._listener = socket.socket()
        self._listener.bind(('127.0.0.1', 0))
        self._listener.listen()
        self.url = f'http://127.0.0.1:{self._listener.getsockname()[1]}'
        threading.Thread(target=self._take, daemon=True).start()

    def _take(self):
        while True:
            try:
                connection, _address = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._hold, args=(connection,), daemon=True).start()

    def _hold(self, connection):
        with connection:
            connection.settimeout(self._holds)
            try:
                head = connection.recv(65536).split(b'\r\n', 1)[0].split()
                if len(head) > 1:
                    self.paths.append(head[1].decode(errors='replace'))
                time.sleep(self._holds)
            except OSError:
                pass

    def close(self):
        self._listener.close()


class ReadingsCase(common.BoxCase):

    def setUp(self):
        super().setUp()
        self.request_token = f'box-suite-request-{uuid.uuid4().hex}'
        self.instrument = plug_instrument(INSTRUMENT)
        self.addCleanup(common.core('main').iot_devices.pop, INSTRUMENT, None)
        self.confirm_seconds = {}
        self.time_the_confirmations()

    def time_the_confirmations(self):
        """Record, per device, the seconds each confirmation of a read took from its start to its end in the box."""
        attestation = common.layer('attestation')
        original = attestation.confirm

        def timed(device, request):
            began = time.monotonic()
            try:
                return original(device, request)
            finally:
                self.confirm_seconds.setdefault(device.device_identifier, []).append(time.monotonic() - began)

        self.enterContext(mock.patch.object(attestation, 'confirm', timed))

    def paired_with(self, server):
        """The box holds `server` as its server, with CREDENTIAL, as a paired box does after its restart."""
        common.store_box_configuration(remote_server=server.url, token=CREDENTIAL)
        return server

    def read(self, identifier=INSTRUMENT, **marks):
        """(answer of the action route, seconds the box spent confirming it) of a read a page asks of the device,
        `marks` in its data."""
        data = json.dumps({'action': 'read_once', **marks})
        answer = self.call(ACTION, session_id=SESSION, device_identifier=identifier, data=data)
        return answer, self.confirm_seconds[identifier][-1]

    def marked_read(self, identifier=INSTRUMENT):
        """A read the server asked to be confirmed: its value-request token and the confirmation mark."""
        return self.read(identifier, viin_iot_request=self.request_token, viin_iot_attest=True)

    def released(self):
        """The values of the events a page can get for the read of this test."""
        return [event['value'] for event in events_of(self.request_token)]

    def scale(self, weight):
        """The simulated scale with `weight` on it, the file of the scale put back when the test ends."""
        [scale] = box.wait_for_devices(box.SCALE)
        box.keep_files(self, box.SCALE_FILE)
        box.SCALE_FILE.write_text(f'{weight}\n')
        return scale


class TestConfirmedReading(ReadingsCase):

    def test_marked_read_is_confirmed_to_the_paired_server_with_the_box_token_in_the_body(self):
        server = self.paired_with(self.server(ServerThatTakesReadings))
        self.scale(WEIGHT)
        before = time.time()

        answer, _seconds = self.marked_read(box.SCALE)

        self.assertIs(answer, True)
        self.assertEqual(len(server.confirmations), 1, server.calls)
        body = server.confirmations[0]
        self.assertEqual((body['jsonrpc'], body['method']), ('2.0', 'call'))
        params = body['params']
        self.assertEqual(set(params), {'token', 'iot_mac', 'device_identifier', 'request', 'value', 'time'})
        self.assertEqual(params['token'], CREDENTIAL)
        self.assertEqual(params['device_identifier'], box.SCALE)
        self.assertEqual(params['request'], self.request_token)
        self.assertEqual(params['value'], WEIGHT)
        self.assertTrue(before <= params['time'] <= time.time(), params['time'])

    def test_confirmation_reaches_the_server_before_a_page_can_get_the_reading(self):
        server = self.paired_with(self.server(ServerThatTakesReadings))
        waiting = {}
        listener = threading.Thread(target=lambda: waiting.update(event=common.call_route(EVENT, listener={
            'session_id': LISTENER, 'devices': [INSTRUMENT], 'last_event': time.time()})), daemon=True)
        listener.start()
        common.wait_until(lambda: LISTENER in event_manager().sessions, 'the page to wait for the instrument')

        self.marked_read()
        listener.join(30)

        self.assertEqual(server.released_when_received, [False])
        self.assertEqual(waiting['event']['value'], WEIGHT)
        self.assertEqual(waiting['event']['request_data']['viin_iot_request'], self.request_token)

    def test_unit_the_driver_gives_is_confirmed_exactly_as_the_driver_wrote_it(self):
        server = self.paired_with(self.server(ServerThatTakesReadings))
        self.instrument.shows = {'value': WEIGHT, 'unit': ' Kg'}

        self.marked_read()

        self.assertEqual([body['params'].get('unit') for body in server.confirmations], [' Kg'])

    def test_unit_is_left_out_when_the_driver_gives_no_text_for_it(self):
        server = self.paired_with(self.server(ServerThatTakesReadings))
        for shows in ({'value': WEIGHT}, {'value': WEIGHT, 'unit': None}, {'value': WEIGHT, 'unit': ''},
                      {'value': WEIGHT, 'unit': 1}):
            with self.subTest(shows=shows):
                self.instrument.shows = shows
                confirmed = len(server.confirmations)

                self.marked_read()

                self.assertEqual(len(server.confirmations), confirmed + 1)
                self.assertNotIn('unit', server.confirmations[-1]['params'])

    def test_reading_that_is_not_a_finite_number_is_not_confirmed(self):
        server = self.paired_with(self.server(ServerThatTakesReadings))
        for value in (True, '48.2', math.inf, math.nan):
            with self.subTest(value=value):
                self.instrument.shows = {'value': value}
                confirmed = len(server.confirmations)

                answer, _seconds = self.marked_read()

                self.assertIs(answer, True)
                self.assertEqual(len(server.confirmations), confirmed)

    def test_value_told_to_the_server_is_the_value_the_page_gets_though_the_driver_changes_its_data_at_every_read(self):
        server = self.paired_with(self.server(ServerThatTakesReadings))
        reads = itertools.count()

        class DriftingInstrument(type(self.instrument)):
            @property
            def data(self):
                return {'value': WEIGHT + next(reads)}

            @data.setter
            def data(self, _value):
                pass

        common.core('main').iot_devices[INSTRUMENT] = DriftingInstrument(INSTRUMENT)

        self.marked_read()

        self.assertEqual(len(server.confirmations), 1)
        self.assertEqual([body['params']['value'] for body in server.confirmations], self.released())


class TestReadsTheServerDidNotAsk(ReadingsCase):

    def setUp(self):
        super().setUp()
        self.server_of_the_box = self.paired_with(self.server(ServerThatTakesReadings))

    def test_read_the_page_tokenised_itself_is_not_confirmed(self):
        for marks in ({'viin_iot_request': self.request_token},
                      {'viin_iot_request': self.request_token, 'viin_iot_attest': False}):
            with self.subTest(marks=marks):
                confirmed = len(self.server_of_the_box.confirmations)

                answer, _seconds = self.read(**marks)

                self.assertIs(answer, True)
                self.assertEqual(len(self.server_of_the_box.confirmations), confirmed)

    def test_read_marked_without_a_value_request_token_is_not_confirmed(self):
        answer, _seconds = self.read(viin_iot_attest=True)

        self.assertIs(answer, True)
        self.assertEqual(self.server_of_the_box.confirmations, [])

    def test_read_of_a_page_of_core_is_not_confirmed(self):
        answer, _seconds = self.read()

        self.assertIs(answer, True)
        self.assertEqual(self.server_of_the_box.confirmations, [])

    def test_weight_the_scale_sends_by_itself_is_not_confirmed(self):
        self.scale(WEIGHT + 1)

        common.wait_until(lambda: [event for event in list(event_manager().events)
                                   if event['device_identifier'] == box.SCALE and event['value'] == WEIGHT + 1],
                          'the scale to send the new weight')

        self.assertEqual(self.server_of_the_box.confirmations, [])

    def test_device_test_of_the_box_is_not_confirmed(self):
        self.scale(WEIGHT)
        _answer, proof = self.unlock(box.settings_code(CREDENTIAL))

        answer = self.call(box.DEVICE_TEST, proof=proof, device_identifier=box.SCALE)

        self.assertEqual((answer['status'], answer['detail']['value']), ('ok', WEIGHT), answer)
        self.assertEqual(self.server_of_the_box.confirmations, [])

    def test_read_not_marked_is_not_held_up_by_a_server_that_does_not_answer(self):
        server = self.paired_with(self.server(ServerThatNeverAnswers))

        answer, _seconds = self.read(viin_iot_request=self.request_token)

        self.assertIs(answer, True)
        self.assertEqual(server.paths, [])


class TestServerThatCannotBeReached(ReadingsCase):

    def test_refused_connection_gives_the_page_the_reading_at_once(self):
        common.store_box_configuration(remote_server=f'http://127.0.0.1:{common.closed_port()}', token=CREDENTIAL)

        answer, seconds = self.marked_read()

        self.assertIs(answer, True)
        self.assertLess(seconds, AT_ONCE)
        self.assertEqual(self.released(), [WEIGHT])

    def test_server_that_never_answers_holds_the_reading_back_three_seconds_at_most(self):
        server = self.paired_with(self.server(ServerThatNeverAnswers))

        answer, seconds = self.marked_read()

        self.assertIs(answer, True)
        self.assertIn(READING, server.paths)
        self.assertLess(seconds, CAP + SLACK)
        self.assertEqual(self.released(), [WEIGHT])


class TestReadsWhileOthersWaitForTheServer(ReadingsCase):
    """At most MAX_IN_FLIGHT confirmations are under way at once; one more read waits for a place inside its own
    CAP seconds instead of being turned away."""

    UNDER_WAY = 3
    HOST = 'readings.box-suite.example'

    def setUp(self):
        super().setUp()
        self.instruments = [f'{INSTRUMENT}_{number}' for number in range(self.UNDER_WAY + 1)]
        for identifier in self.instruments:
            plug_instrument(identifier)
            self.addCleanup(common.core('main').iot_devices.pop, identifier, None)
        self.tokens = {identifier: f'{self.request_token}-{identifier}' for identifier in self.instruments}

    def read_on(self, identifier, results):
        answer = common.call_route(ACTION, session_id=f'{SESSION}-{identifier}', device_identifier=identifier,
                                   data=json.dumps({'action': 'read_once', 'viin_iot_request': self.tokens[identifier],
                                                    'viin_iot_attest': True}))
        results[identifier] = (answer, self.confirm_seconds[identifier][-1])

    def reads_under_way(self, results):
        """Start a read on each instrument but the last, on a thread each; return the threads."""
        threads = [threading.Thread(target=self.read_on, args=(identifier, results), daemon=True)
                   for identifier in self.instruments[:self.UNDER_WAY]]
        for thread in threads:
            thread.start()
        return threads

    def confirmed(self, server):
        return sorted(body['params']['request'] for body in server.confirmations)

    def test_read_that_finds_all_places_busy_with_a_server_that_answers_soon_is_sent_and_confirmed(self):
        server = self.paired_with(self.server(ServerThatTakesItsTime, delay=0.5))
        results = {}
        threads = self.reads_under_way(results)
        common.wait_until(lambda: len(server.confirmations) == self.UNDER_WAY, 'the confirmations to be under way', 10)

        last = self.instruments[-1]
        self.read_on(last, results)

        for thread in threads:
            thread.join(10)
        self.assertEqual(self.confirmed(server), sorted(self.tokens.values()))
        self.assertLess(results[last][1], CAP + SLACK)
        self.assertEqual([event['value'] for event in events_of(self.tokens[last])], [WEIGHT])
        self.assertEqual(server.released_when_received, [False] * (self.UNDER_WAY + 1))

    def test_read_that_finds_all_places_busy_for_good_goes_to_the_page_unconfirmed_within_three_seconds(self):
        server = self.server(ServerThatTakesReadings)
        stall, resolved = threading.Event(), []
        real = socket.getaddrinfo

        def getaddrinfo(host, port, *arguments, **options):
            if host != self.HOST:
                return real(host, port, *arguments, **options)
            resolved.append(host)
            if len(resolved) <= self.UNDER_WAY:
                # A name server that does not answer: the look-up outlasts the time the request itself may take.
                stall.wait(30)
                raise socket.gaierror(socket.EAI_AGAIN, 'Temporary failure in name resolution')
            return real('127.0.0.1', port, *arguments, **options)

        self.enterContext(mock.patch.object(socket, 'getaddrinfo', getaddrinfo))
        self.addCleanup(stall.set)
        common.store_box_configuration(remote_server=f'http://{self.HOST}:{server.port}', token=CREDENTIAL)
        results = {}
        threads = self.reads_under_way(results)
        common.wait_until(lambda: len(resolved) == self.UNDER_WAY, 'the look-ups to be under way', 10)

        last = self.instruments[-1]
        self.read_on(last, results)

        self.assertIs(results[last][0], True)
        self.assertLess(results[last][1], CAP + SLACK)
        self.assertEqual(len(resolved), self.UNDER_WAY)
        self.assertEqual([event['value'] for event in events_of(self.tokens[last])], [WEIGHT])
        self.assertEqual(server.confirmations, [])

        stall.set()
        for thread in threads:
            thread.join(10)
        self.read_on(last, results)
        common.wait_until(lambda: self.tokens[last] in self.confirmed(server), 'the next read to be confirmed', 10)
        self.assertEqual(len(resolved), self.UNDER_WAY + 1)

    def test_read_that_waits_for_a_place_has_only_what_is_left_of_its_three_seconds_for_the_server(self):
        server = self.server(ServerThatNeverAnswers)
        stall, resolved = threading.Event(), []
        real = socket.getaddrinfo

        def getaddrinfo(host, port, *arguments, **options):
            if host != self.HOST:
                return real(host, port, *arguments, **options)
            resolved.append(host)
            if len(resolved) <= self.UNDER_WAY:
                stall.wait(30)
                raise socket.gaierror(socket.EAI_AGAIN, 'Temporary failure in name resolution')
            return real('127.0.0.1', port, *arguments, **options)

        self.enterContext(mock.patch.object(socket, 'getaddrinfo', getaddrinfo))
        self.addCleanup(stall.set)
        port = server.url.rsplit(':', 1)[1]
        common.store_box_configuration(remote_server=f'http://{self.HOST}:{port}', token=CREDENTIAL)
        results = {}
        threads = self.reads_under_way(results)
        common.wait_until(lambda: len(resolved) == self.UNDER_WAY, 'the look-ups to be under way', 10)
        # The places come free two seconds into the wait of the last read, so a full CAP for the server would end it
        # at five seconds, a second past the bound.
        freeing = threading.Timer(2, stall.set)
        self.addCleanup(freeing.cancel)

        last = self.instruments[-1]
        freeing.start()
        self.read_on(last, results)

        self.assertIs(results[last][0], True)
        self.assertEqual(len(resolved), self.UNDER_WAY + 1)
        self.assertLess(results[last][1], CAP + SLACK)
        self.assertEqual([event['value'] for event in events_of(self.tokens[last])], [WEIGHT])
        for thread in threads:
            thread.join(10)


class TestBoxWithoutAServer(ReadingsCase):

    def test_box_without_a_token_confirms_nothing(self):
        server = self.server(ServerThatTakesReadings)
        common.store_box_configuration(remote_server=server.url, token='')

        answer, _seconds = self.marked_read()

        self.assertIs(answer, True)
        self.assertEqual(server.confirmations, [])
        self.assertEqual(self.released(), [WEIGHT])

    def test_box_without_a_server_gives_the_page_the_reading_at_once(self):
        common.store_box_configuration(remote_server='', token=CREDENTIAL)

        answer, seconds = self.marked_read()

        self.assertIs(answer, True)
        self.assertLess(seconds, AT_ONCE)
        self.assertEqual(self.released(), [WEIGHT])


class TestStartOfTheBoxInEachEvent(ReadingsCase):

    def test_every_event_of_one_start_names_that_same_start(self):
        self.paired_with(self.server(ServerThatTakesReadings))
        since = time.time()
        self.scale(WEIGHT + 2)

        self.marked_read()
        self.read()
        common.wait_until(lambda: [event for event in list(event_manager().events)
                                   if event['device_identifier'] == box.SCALE and event['value'] == WEIGHT + 2],
                          'the scale to send the new weight')

        events = [event for event in list(event_manager().events)
                  if event['device_identifier'] in (INSTRUMENT, box.SCALE) and event['time'] >= since]
        self.assertGreaterEqual(len(events), 3)
        starts = {event.get(BOOT_ID) for event in events}
        self.assertEqual(len(starts), 1, starts)
        self.assertTrue(starts.pop())

    def test_driver_keeps_its_own_data_without_the_start_of_the_box(self):
        self.paired_with(self.server(ServerThatTakesReadings))
        self.instrument.shows = {'value': WEIGHT, 'unit': 'kg'}

        self.marked_read()
        self.read()

        self.assertEqual(self.instrument.data, {'value': WEIGHT, 'unit': 'kg'})

    def test_another_start_of_the_box_service_names_another_start(self):
        self.read()
        this_start = [event for event in event_manager().events if event['device_identifier'] == INSTRUMENT][-1]

        other_start = common.in_new_process(start_named_by_an_event)

        self.assertTrue(this_start.get(BOOT_ID))
        self.assertTrue(other_start)
        self.assertNotEqual(other_start, this_start[BOOT_ID])


class TestSecretsOfAConfirmation(ReadingsCase):

    def logged_during_a_marked_read(self):
        with common.box_log() as written:
            answer, _seconds = self.marked_read()
            log = written()
        self.assertIs(answer, True)
        return log

    def assertNoSecret(self, log):
        self.assertTrue(log)
        for secret in (CREDENTIAL, self.request_token, str(WEIGHT)):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, log)

    def test_confirmation_the_server_takes_leaves_no_secret_in_the_box_log(self):
        server = self.paired_with(self.server(ServerThatTakesReadings))

        log = self.logged_during_a_marked_read()

        self.assertEqual(len(server.confirmations), 1)
        self.assertNoSecret(log)

    def test_confirmation_the_server_refuses_with_an_error_leaves_no_secret_in_the_box_log(self):
        server = self.paired_with(self.server(ServerThatTakesReadings, status=500))

        log = self.logged_during_a_marked_read()

        self.assertEqual(len(server.confirmations), 1)
        self.assertNoSecret(log)

    def test_confirmation_to_a_server_that_refuses_the_connection_leaves_no_secret_in_the_box_log(self):
        common.store_box_configuration(remote_server=f'http://127.0.0.1:{common.closed_port()}', token=CREDENTIAL)

        self.assertNoSecret(self.logged_during_a_marked_read())

    def test_confirmation_to_a_server_that_never_answers_leaves_no_secret_in_the_box_log(self):
        server = self.paired_with(self.server(ServerThatNeverAnswers))

        log = self.logged_during_a_marked_read()

        self.assertIn(READING, server.paths)
        self.assertNoSecret(log)
