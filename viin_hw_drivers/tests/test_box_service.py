"""The box service as a process of its own: how long it takes to stop, and what a real-time limit does to it."""
import time

import common
import services_common as box

# A stop ends within this many seconds, with status 0, whatever a client holds open.
STOP_BOUND = 10
# A real-time limit short enough to pass during the test.
SHORT_REAL_TIME_LIMIT = 5
# Core checks the age of its threads periodically: the test watches for several checks, with a request that waits
# for an event always open and older than the limit.
WATCH = 90
EVENT_WAIT = 50
NEW_EVENT_REQUEST_EVERY = 25
LIMIT_REACHED = 'virtual real time limit'


class TestStopOfTheBoxService(common.BoxStateCase):

    def started_with_clients(self, idle, event):
        service = box.box_service(self)
        before = service.threads()
        service.hold_idle_connections(idle)
        if event:
            service.wait_for_event(EVENT_WAIT)
        common.wait_until(
            lambda: service.threads() >= before + idle + bool(event), 'the box service to take the connections', 20)
        time.sleep(1)
        return service

    def test_stop_ends_within_the_bound_with_status_0_while_an_event_request_and_an_idle_connection_are_open(self):
        service = self.started_with_clients(idle=1, event=True)

        seconds, status = service.stop(wait=STOP_BOUND + 15)

        self.assertEqual(status, 0, f'the box service did not end by itself ({seconds:.1f} s)')
        self.assertLess(seconds, STOP_BOUND)

    def test_stop_ends_within_the_bound_while_idle_connections_alone_are_open(self):
        service = self.started_with_clients(idle=3, event=False)

        seconds, status = service.stop(wait=STOP_BOUND + 15)

        self.assertEqual(status, 0, f'the box service did not end by itself ({seconds:.1f} s)')
        self.assertLess(seconds, STOP_BOUND)

    def test_stop_with_no_client_ends_at_once_with_status_0(self):
        service = self.started_with_clients(idle=0, event=False)

        seconds, status = service.stop(wait=STOP_BOUND + 15)

        self.assertEqual(status, 0)
        self.assertLess(seconds, STOP_BOUND)


class TestRealTimeLimitInTheConfiguration(common.BoxStateCase):

    def test_box_service_whose_configuration_holds_a_short_real_time_limit_is_not_reloaded_by_it(self):
        service = box.box_service(self, limit_time_real=SHORT_REAL_TIME_LIMIT)
        service.hold_idle_connections(1)
        began = time.monotonic()
        next_request = 0
        while time.monotonic() - began < WATCH:
            if time.monotonic() - began >= next_request:
                service.wait_for_event(EVENT_WAIT, session=f'box-suite-station-{next_request}')
                next_request += NEW_EVENT_REQUEST_EVERY
            self.assertTrue(service.alive(), f'the box service ended:\n{service.log()[-3000:]}')
            time.sleep(1)

        self.assertNotIn(LIMIT_REACHED, service.log())
        self.assertEqual(service.hello(), 200)
