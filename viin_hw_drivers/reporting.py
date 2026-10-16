"""What the box tells its server, and what became of it.

Core reports the box and its devices when one of them changes, forgets a report
the server did not get, and opens its live channel only if the server answered
when the box started. This file keeps what became of each report, has a report
that is still owed sent again, lets any part of the layer ask for one, and
opens the live channel as soon as the server gives one.
"""
import logging
import threading
import time

from . import trust
from .seams import core

_logger = logging.getLogger(__name__)

# What became of a report.
NEVER = 'never'                # the box has sent none since it started
ACCEPTED = 'accepted'          # the server took it, and answered the channel of the box
REFUSED = 'refused'            # the server answered, and did not take it: it does not know this box
UNREACHABLE = 'unreachable'    # no answer the box could read came back

# The live channel, by which the server sends its print and screen commands.
OFF = 'off'                    # the box had no server when it started
WAITING = 'waiting'            # the server has not given a channel yet
CONNECTING = 'connecting'
CONNECTED = 'connected'
RECONNECTING = 'reconnecting'

# Core waits for the answer to a report without any limit: a server that goes down with a report on its way
# would hold the device loop, and with it every later report, until the box is restarted.
REPORT_TIMEOUT = 30
# A report the server did not take is sent again after RETRY_FIRST seconds, then each time after twice as long,
# RETRY_LONGEST at most: core logs every failed report in full, and a server may stay down for a night.
RETRY_FIRST = 5
RETRY_LONGEST = 60
# How many reports may wait at once for an answer that did not come in time.
MAX_UNANSWERED = 3
MAX_ERROR = 200


def _age(stamp):
    """Whole seconds on the monotonic clock since `stamp`; None when there is no stamp."""
    return None if stamp is None else max(0, int(time.monotonic() - stamp))


class _Answer:
    """Stands for the live channel client in one report of core.

    Core gives the channel the server answered to the client it is handed, and
    gives nothing when no answer it could read came back: that is all core lets
    out of what became of a report.
    """

    @property
    def state(self):
        if 'iot_channel' not in vars(self):
            return UNREACHABLE
        return ACCEPTED if self.iot_channel and isinstance(self.iot_channel, str) else REFUSED


class _Ledger:
    """Which reports ended how, and whether the server is still owed one."""

    def __init__(self):
        self._lock = threading.Lock()
        self._begun = 0
        self._asked = 0
        # (number, state, time, monotonic time) of the latest report that ended. The wall clock of the box is wrong
        # after every start until a time server or the server corrects it; the monotonic time is what an age is
        # worked out from.
        self._last = (0, NEVER, None, None)
        # (number, how many reports had been asked for when it began, time, monotonic time) of the latest report
        # the server took.
        self._accepted = (0, 0, None, None)
        self._failed = 0
        self._retry_at = 0.0
        self._delay = RETRY_FIRST
        self._waiting = 0

    def begin(self):
        """(number of a report that starts, how many reports the layer had asked for by then)."""
        with self._lock:
            self._begun += 1
            return self._begun, self._asked

    def end(self, number, asked, state):
        """Record how the report `number` ended; a report may end twice, the second time for good."""
        with self._lock:
            before = self._last[1]
            if number >= self._last[0]:
                self._last = (number, state, time.time(), time.monotonic())
            if state == ACCEPTED:
                if number > self._accepted[0]:
                    self._accepted = (number, asked, time.time(), time.monotonic())
                self._delay = RETRY_FIRST
            elif number > self._failed:
                self._failed = number
                self._retry_at = time.monotonic() + self._delay
                self._delay = min(self._delay * 2, RETRY_LONGEST)
            after = self._last[1]
        if after != before and after == REFUSED:
            _logger.warning('The server answered the report of this box without taking it: it does not know this box')
        elif after != before and after == ACCEPTED:
            _logger.info('The server took the report of this box')

    def ask(self):
        with self._lock:
            self._asked += 1
            self._retry_at = 0.0

    def unanswered(self):
        with self._lock:
            self._waiting += 1

    def answered(self):
        with self._lock:
            self._waiting -= 1

    def waiting(self):
        """How many reports the box stopped waiting for are still on their way."""
        with self._lock:
            return self._waiting

    def _owed(self):
        return self._asked > self._accepted[1] or self._failed > self._accepted[0]

    def due(self):
        with self._lock:
            return self._owed() and time.monotonic() >= self._retry_at

    def outcome(self):
        with self._lock:
            return {
                'state': self._last[1],
                'at': self._last[2],
                'at_age': _age(self._last[3]),
                'accepted_at': self._accepted[2],
                'accepted_age': _age(self._accepted[3]),
                'owed': self._owed(),
            }


class _Channel:

    def __init__(self):
        self.lock = threading.Lock()
        self.client = None
        self.state = OFF
        self.since = None
        self.since_stamp = None
        self.error = ''

    def turn(self, state, error=''):
        with self.lock:
            if self.state != state:
                self.state, self.since, self.since_stamp = state, time.time(), time.monotonic()
            self.error = error


_ledger = _Ledger()
_channel = _Channel()


def outcome():
    """What became of the reports of the box to its server since the box started.

    {'state': never, accepted, refused or unreachable - how the latest report ended;
    'at': when it ended; 'accepted_at': when the server last took a report;
    'owed': whether a report is still to be sent; 'at_age' and 'accepted_age': how many whole seconds ago those
    two moments were, on the monotonic clock, so they stay right when the clock of the box is corrected later}.
    Times are seconds since 1970 on the clock of the box, None for never; an age is None for never too.
    """
    return _ledger.outcome()


def request_report():
    """Ask for a report of the box and its devices to the server, whatever changed or not.

    For a part of the layer that changed what the server shows of the box: the
    kind of a device, a name, a certificate. Returns at once; the device loop of
    core sends the report at its next pass, and again until the server took it.
    """
    _ledger.ask()


def is_due():
    """Whether the device loop has a report to send now, although nothing it watches changed."""
    return _ledger.due()


def report():
    """Report the box and all its devices to the server now, on the calling thread; returns `outcome()`.

    The report carries what the box has at this moment. It takes REPORT_TIMEOUT
    seconds at most and never raises: a report the server did not take stays
    owed, and the device loop sends it again.
    """
    try:
        manager = core('main').manager
        # Core reports what it last compared: this makes it look at the devices, the address and the version now.
        manager._get_changes_to_send()
        manager.send_alldevices(_channel.client)
    except Exception:
        _ledger.ask()
        _logger.exception('The box could not send its report to the server')
    return outcome()


class _Report:
    """One report of core, on a thread of its own: whoever asked for it waits REPORT_TIMEOUT at most."""

    def __init__(self, send, client):
        self._send = send
        self._client = client
        self._number, self._asked = _ledger.begin()
        self._answer = _Answer()
        self._handover = threading.Lock()
        self._ended = threading.Event()
        self._abandoned = False
        self.raised = None

    def _run(self):
        try:
            self._send(self._answer)
        except Exception as error:
            self.raised = error
        finally:
            with self._handover:
                if 'iot_channel' in vars(self._answer) and self._client is not None:
                    self._client.iot_channel = self._answer.iot_channel
                _ledger.end(self._number, self._asked, self._answer.state)
                self._ended.set()
            if self._abandoned:
                _ledger.answered()
                if self.raised:
                    _logger.error(
                        'A report of this box to its server failed after the box had stopped waiting for it',
                        exc_info=self.raised)
                open_channel()

    def send(self):
        """Send the report; False when the server has not answered in time, and the report goes on alone."""
        threading.Thread(target=self._run, name=f'ViinReport-{self._number}', daemon=True).start()
        self._ended.wait(REPORT_TIMEOUT)
        with self._handover:
            if not self._ended.is_set():
                self._abandoned = True
                _ledger.unanswered()
                _ledger.end(self._number, self._asked, UNREACHABLE)
        return not self._abandoned

    @staticmethod
    def skip():
        """Count a report as one the server did not get, without sending it."""
        _ledger.end(*_ledger.begin(), UNREACHABLE)


def deliver(send, client, has_server):
    """Run one report of core and keep what became of it.

    `send(client)` is the report of core, `client` the live channel client core
    hands it, None when it hands none. What `send` raises is raised here, unless
    it took longer than REPORT_TIMEOUT.
    """
    if not has_server:
        return send(client)
    first = client is not None and _hold(client)
    if _ledger.waiting() >= MAX_UNANSWERED:
        # Each of them holds a thread and a connection for as long as the server leaves it without an answer.
        _logger.warning(
            '%s reports of this box are still waiting for an answer of the server: no other is sent meanwhile',
            MAX_UNANSWERED)
        _Report.skip()
        return None
    report_of_core = _Report(send, client)
    if not report_of_core.send():
        _logger.warning(
            'The server did not answer the report of this box within %s seconds: the report will be sent again',
            REPORT_TIMEOUT)
        return None
    if report_of_core.raised:
        raise report_of_core.raised
    # After its first report core loads the device handlers, then starts the channel itself.
    if not first:
        open_channel()
    return None


def _hold(client):
    """Keep the live channel client core made; True when it is not the one already kept."""
    with _channel.lock:
        if _channel.client is client:
            return False
        _channel.client = client
    _channel.turn(WAITING)
    start, starting = client.start, threading.Lock()

    def start_once():
        # Core starts the channel too, when the server answered its first report: a thread starts once.
        with starting:
            if client.ident is None:
                _channel.turn(CONNECTING)
                start()

    client.start = start_once
    return True


def client():
    """The live channel client of core, a thread; None on a box that had no server when it started."""
    return _channel.client


def open_channel():
    """Start the live channel when the server gave one and it does not run yet; True when it runs."""
    kept = _channel.client
    if kept is None or not kept.iot_channel:
        return False
    if kept.ident is None:
        try:
            kept.start()
        except Exception:
            _logger.exception('The live channel of this box could not be started')
            return False
    return True


def channel():
    """The state of the live channel.

    {'state': off, waiting, connecting, connected or reconnecting; 'since': when
    it took that state, seconds since 1970 on the clock of the box, None while off; 'since_age': how many whole
    seconds ago that was, on the monotonic clock, None while off; 'error': what the channel last failed on, ''
    when connected}.
    """
    with _channel.lock:
        return {
            'state': _channel.state, 'since': _channel.since, 'since_age': _age(_channel.since_stamp),
            'error': _channel.error,
        }


def report_on_connection(connected):
    """Report the box and all its devices as the live channel client `connected` gets a connection.

    Called before the channel subscribes: the server takes a box that subscribes
    for a box that still has every device it had, so it must know better first.
    """
    try:
        _hold(connected)
    except Exception:
        _logger.exception('The live channel client of this box could not be kept')
    report()


def channel_opened():
    _channel.turn(CONNECTED)


def channel_lost(error):
    """Record that the live channel lost its connection; core's client connects again by itself."""
    text = ' '.join(str(error).split())[:MAX_ERROR]
    with _channel.lock:
        waiting = _channel.state in (OFF, WAITING)
    if not waiting:
        _channel.turn(RECONNECTING, text)


# What the server shows of the box may depend on the certificate the box serves.
trust.on_certificate_change(request_report)
