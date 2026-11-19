"""The connections a client leaves idle, and the stop of the web server of the box service."""
import socket
import threading
import time
from unittest import mock

import common

# What a request in progress may take longer than the box lets a connection stay silent.
BEYOND_THE_IDLE_LIMIT = 5
IDLE_CONNECTIONS = 20
BODY_PIECES = (b'abc', b'def', b'ghi')
PIECE_PAUSE = 5


def application(environ, start_response):
    """A web application with a route that takes the time the query string says, and one that echoes its body."""
    if environ['PATH_INFO'] == '/take':
        time.sleep(float(environ['QUERY_STRING']))
    body = environ['wsgi.input'].read(int(environ.get('CONTENT_LENGTH') or 0))
    start_response('200 OK', [('Content-Type', 'text/plain')])
    return [b'answered:' + body]


def request_threads():
    """List the threads that serve a connection of a web server of core."""
    return [thread for thread in threading.enumerate() if getattr(thread, 'type', None) == 'http']


class ConnectionsOfAWebServerOfCore(common.BoxCase):
    """Core's web server, with the layer in place, on a port of the machine."""

    def setUp(self):
        super().setUp()
        from odoo.service.server import ThreadedWSGIServerReloadable

        self.server = ThreadedWSGIServerReloadable('127.0.0.1', 0, application)
        self.port = self.server.server_port
        serving = threading.Thread(target=self.server.serve_forever, name='box-suite-web-server', daemon=True)
        serving.start()
        self.addCleanup(self.close_server, serving)
        self.limit = common.layer('seams.service').IDLE_LIMIT
        self.before = len(request_threads())

    def close_server(self, serving):
        self.server.shutdown()
        serving.join(10)
        self.server.server_close()

    def connect(self):
        connection = socket.create_connection(('127.0.0.1', self.port), timeout=60)
        self.addCleanup(connection.close)
        return connection

    def ask(self, request):
        """Send `request` on a connection of its own.

        :param request: the bytes to send
        :return: the whole answer, as bytes
        """
        connection = self.connect()
        connection.sendall(request)
        answer = b''
        while True:
            piece = connection.recv(4096)
            if not piece:
                return answer
            answer += piece


class TestIdleConnections(ConnectionsOfAWebServerOfCore):

    def test_connections_a_client_leaves_idle_are_closed_and_their_threads_end(self):
        idle = [self.connect() for _number in range(IDLE_CONNECTIONS)]
        common.wait_until(
            lambda: len(request_threads()) >= self.before + IDLE_CONNECTIONS, 'the server to take the connections')
        began = time.monotonic()

        for connection in idle:
            self.assertEqual(connection.recv(1), b'', 'the server holds a connection that sent nothing')

        self.assertLess(time.monotonic() - began, self.limit + 10)
        common.wait_until(lambda: len(request_threads()) == self.before, 'the threads of the connections to end', 10)

    def test_request_made_while_connections_are_idle_is_answered(self):
        idle = [self.connect() for _number in range(IDLE_CONNECTIONS)]
        common.wait_until(
            lambda: len(request_threads()) >= self.before + IDLE_CONNECTIONS, 'the server to take the connections')

        answer = self.ask(b'GET /fast HTTP/1.0\r\n\r\n')

        self.assertTrue(answer.startswith(b'HTTP/1.0 200'), answer)
        self.assertEqual(len(idle), IDLE_CONNECTIONS)

    def test_idle_connection_closed_by_the_box_leaves_no_line_at_warning_level_or_above(self):
        with self.assertNoLogs('werkzeug', 'WARNING'):
            connection = self.connect()
            self.assertEqual(connection.recv(1), b'')
            common.wait_until(lambda: len(request_threads()) == self.before, 'the thread of the connection to end')


class TestRequestsThatTakeLong(ConnectionsOfAWebServerOfCore):

    def test_request_that_waits_for_its_answer_longer_than_the_idle_limit_is_answered_not_cut(self):
        wait = self.limit + BEYOND_THE_IDLE_LIMIT

        answer = self.ask(f'GET /take?{wait} HTTP/1.0\r\n\r\n'.encode())

        self.assertTrue(answer.startswith(b'HTTP/1.0 200'), answer)

    def test_request_whose_body_comes_in_pieces_a_few_seconds_apart_is_answered(self):
        connection = self.connect()
        connection.sendall(b'POST /echo HTTP/1.0\r\nContent-Length: %d\r\n\r\n' % sum(map(len, BODY_PIECES)))
        for number, piece in enumerate(BODY_PIECES):
            if number:
                time.sleep(PIECE_PAUSE)
            connection.sendall(piece)
        answer = b''
        while True:
            more = connection.recv(4096)
            if not more:
                break
            answer += more

        self.assertTrue(answer.startswith(b'HTTP/1.0 200'), answer)
        self.assertTrue(answer.endswith(b'answered:' + b''.join(BODY_PIECES)), answer)


class TestLogOfAReadThatTimedOut(common.BoxCase):
    """The request handler of core, as the layer leaves it: a read that timed out says nothing at warning level (the
    connection of a client that sent nothing is the normal case), and every other error of the handler is logged."""

    def handler(self):
        from odoo.service.server import RequestHandler

        handler = RequestHandler.__new__(RequestHandler)
        handler.client_address = ('192.0.2.1', 40000)
        return handler

    def test_read_that_timed_out_leaves_no_line_at_warning_level_or_above(self):
        with self.assertNoLogs('werkzeug', 'WARNING'):
            self.handler().log_error('Request timed out: %r', TimeoutError('timed out'))

    def test_error_of_another_kind_is_logged_as_core_logs_it(self):
        with self.assertLogs('werkzeug', 'ERROR'):
            self.handler().log_error('Bad request version (%r)', 'HTTP/9')


class TestBoundedStop(common.BoxCase):
    """A plain stop ends the process at STOP_DEADLINE, as core's second signal would; a reload does not."""

    def setUp(self):
        super().setUp()
        self.service = common.layer('seams.service')
        self.server_module = __import__('odoo.service.server', fromlist=['server_phoenix'])
        self.ended = threading.Event()
        self.stopped = []
        self.enterContext(mock.patch.object(self.service, 'end_process', self.ended.set))
        self.enterContext(mock.patch.object(self.service, 'STOP_DEADLINE', 0.3))

    def stop(self, phoenix):
        original = mock.Mock(side_effect=lambda _server: self.stopped.append('stop of core') or 'stopped')
        replacement = common.layer('seams')._integrity.seams[24].replacement(original)
        with mock.patch.object(self.server_module, 'server_phoenix', phoenix):
            return replacement(object())

    def test_plain_stop_runs_the_stop_of_core_and_ends_the_process_at_the_deadline(self):
        answer = self.stop(phoenix=False)

        self.assertEqual((answer, self.stopped), ('stopped', ['stop of core']))
        self.assertTrue(self.ended.wait(5), 'the process was not ended at the deadline')

    def test_reload_runs_the_stop_of_core_and_ends_nothing(self):
        answer = self.stop(phoenix=True)

        self.assertEqual((answer, self.stopped), ('stopped', ['stop of core']))
        self.assertFalse(self.ended.wait(1.5), 'a reload must not end the process')


class TestIdleConnectionsSeamMoved(common.MovedSeamCase):
    number = 23
    changed_signatures = ('odoo.service.server.RequestHandler.log_error',)
    other_seams = (1, 2, 3, 24)


class TestBoundedStopSeamMoved(common.MovedSeamCase):
    number = 24
    changed_signatures = ('odoo.service.server.ThreadedServer.stop',)
    other_seams = (1, 2, 3, 23)
