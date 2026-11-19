"""Seams of the web server of the box service: the connections a client leaves idle, and the stop.

Core serves each connection on a thread the interpreter waits for at exit, and gives a connection no read limit
outside its test mode: a client that opens a connection and sends nothing keeps the process from ending.
"""
import http.server
import inspect
import logging
import os
import socketserver
import sys
import threading

from . import FunctionSeam, PartsSeam, SeamError, register

_logger = logging.getLogger(__name__)

SERVER = 'odoo.service.server'
# Seconds a connection may stay silent before the box closes it. Only the read of a request line, of its headers and
# of its body counts: a request being served, and a request that waits for its answer, read nothing.
IDLE_LIMIT = 15
# Seconds a plain stop of the box service is given before the process ends as core's second stop signal ends it.
STOP_DEADLINE = 8
TIMED_OUT = 'Request timed out: %r'


@register
class IdleConnections(PartsSeam):
    number = 23
    target = f'{SERVER}.RequestHandler (timeout, log_error)'
    feature = ('a connection a client opens and leaves idle is closed after a short time, so that such connections '
               'neither pile up threads nor keep the box service from stopping')

    TIMEOUT = 'timeout'
    LOG_ERROR = 'log_error'
    parts = (TIMEOUT, LOG_ERROR)

    @staticmethod
    def _handler():
        try:
            return sys.modules[SERVER].RequestHandler
        except (KeyError, AttributeError):
            raise SeamError(f'{SERVER}.RequestHandler does not exist') from None

    def check_part(self, part):
        handler = self._handler()
        if part == self.TIMEOUT:
            if not issubclass(handler, socketserver.StreamRequestHandler) or not hasattr(handler, 'timeout'):
                raise SeamError('RequestHandler is not a stream request handler with a timeout')
            if 'timeout' not in socketserver.StreamRequestHandler.setup.__code__.co_names:
                raise SeamError('the stream request handler does not give its connection the timeout')
            return
        try:
            names = tuple(inspect.signature(handler.log_error).parameters)
        except (TypeError, ValueError):
            raise SeamError('RequestHandler.log_error is not a method') from None
        if names != ('self', 'format', 'args'):
            raise SeamError('signature changed: expected (self, format, args), found (%s)' % ', '.join(names))
        if TIMED_OUT not in http.server.BaseHTTPRequestHandler.handle_one_request.__code__.co_consts:
            raise SeamError('the request handler does not log "Request timed out" when a read times out')

    def apply_part(self, part):
        handler = self._handler()
        if part == self.TIMEOUT:
            handler.timeout = IDLE_LIMIT
            return
        original = handler.log_error
        if getattr(original, 'viin_idle_connections', False):
            return

        def log_error(request_handler, format, *args):
            if format == TIMED_OUT:
                _logger.debug(format, *args)
            else:
                original(request_handler, format, *args)

        log_error.viin_idle_connections = True
        handler.log_error = log_error


def end_process():
    """End the process at once, as core ends it on a second stop signal."""
    sys.stderr.write('The box service did not stop in time: it ends now.\n')
    sys.stderr.flush()
    os._exit(0)


def _stopping_in_time(original):
    def stop(server):
        if not sys.modules[SERVER].server_phoenix:
            deadline = threading.Timer(STOP_DEADLINE, end_process)
            deadline.name = 'ViinStopDeadline'
            deadline.daemon = True
            deadline.start()
        return original(server)

    return stop


@register
class BoundedStop(FunctionSeam):
    number = 24
    module = SERVER
    name = 'ThreadedServer.stop'
    parameters = ('self',)
    feature = 'the box service stops by its deadline, whatever a client holds open'

    def check(self):
        super().check()
        if not isinstance(getattr(sys.modules[SERVER], 'server_phoenix', None), bool):
            raise SeamError(f'{SERVER}.server_phoenix, which tells a reload from a stop, does not exist')

    def replacement(self, original):
        return _stopping_in_time(original)
