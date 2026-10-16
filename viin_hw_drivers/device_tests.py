"""The devices of the box as the installer sees them, and the test of each one with no server.

Each test runs what core itself runs for that device: the status ticket of a
receipt printer, a small label in ZPL for a label printer, the test page of
the print server for an office printer, one reading of a scale. The ticket and
the label go to the print queue through `lp`, as core's `print_raw` sends what
the point of sale prints, so that the box learns the number of its own test
job. A scanner, a keyboard and a footswitch are tested by the page of the box
itself, which listens to them as core lets any page do. A test changes no
setting.
"""
import copy
import logging
import re
import subprocess
import threading
import time
from urllib.parse import unquote, urlsplit

from . import reporting
from .seams import core

_logger = logging.getLogger(__name__)

# What a test answers.
OK = 'ok'
FAILED = 'failed'
UNSUPPORTED = 'unsupported'

RECEIPT = 'receipt_printer'
LABEL = 'label_printer'
OFFICE = 'office_printer'
SCANNER_ACTION = 'update_is_scanner'
SCALE_ACTION = 'read_once'
# A test that has not ended after this many seconds is answered as failed: `print_server_silent` for a printer,
# whose test calls only the print server of the box and never the printer itself, `timeout` for a scale. The
# device goes on on its own; a test not sent yet is not sent, and a test job the box knows by its number is
# cancelled, so that it neither prints later nor waits in front of real tickets. The seconds count from the start
# of the test itself, after the connection to the print server is open.
TEST_TIMEOUT = 15
# The job name of a test print sent through `lp`, for a person reading the print queue. The box itself knows its
# test job only by the number the print server gives it: by default the print server keeps the names of jobs
# private, so a name cannot tell the test from a ticket.
TEST_JOB_NAME = 'Viindoo IoT - test'
# How long, after the device call, the box follows its test job and the print queue to see whether the printer
# took the job, and how often it looks. The print server sets the queue reason below while it opens the printer
# for the job it is printing, and clears it once the printer is connected. A printer counts as reached only on
# positive evidence: the job completed, or, while the job itself is processing, the queue was seen connecting and
# later not. A reason seen while the job is still pending belongs to a job ahead of it, and a queue that never
# showed the reason proves nothing, whatever its message says.
FOLLOW_WINDOW = 30
FOLLOW_STEP = 0.5
CONNECTING = 'connecting-to-device'
# The follow-up runs in a thread of its own so that a print server that stops answering cannot hold the request:
# the request waits FOLLOW_WINDOW + FOLLOW_MARGIN seconds for it, then CANCEL_WAIT seconds for the cancelling of
# the test job on a connection of its own, and answers `print_server_silent`.
FOLLOW_MARGIN = 3
CANCEL_WAIT = 2
# The device address schemes of the print server whose host part is the network address of the printer.
NETWORK_SCHEMES = ('socket', 'ipp', 'ipps', 'http', 'https', 'lpd')
MAX_UNIT = 20
MAX_ADDRESS = 120
LABEL_TEXT = 'Viindoo IoT - test'
# What core looks for, in the order it looks, to tell a receipt printer and a label printer from an office one.
RECEIPT_COMMANDS = ('CMD:STAR;', 'CMD:ESC/POS;')
RECEIPT_NAMES = ('tm-m30',)
LABEL_COMMANDS = ('COMMAND SET:ZPL;', 'CMD:ESCLABEL;')
LABEL_NAMES = ('zpl',)
# The word core finds in the name of a print queue to take its printer for a label printer.
LABEL_MARK = 'ZPL'
MAX_QUEUE_NAME = 127
MAX_TEXT = 300
# The states of a print job and of a print queue, as the print server numbers them.
JOB_STATES = {3: 'pending', 4: 'held', 5: 'processing', 6: 'stopped', 7: 'canceled', 8: 'aborted', 9: 'completed'}
JOB_FAILURES = ('stopped', 'canceled', 'aborted')
QUEUE_STATES = {3: 'idle', 4: 'processing', 5: 'stopped'}

# Several requests may test the same device; a device does one test at a time.
_testing = {}
# Python cannot stop a call to the print server that does not answer: the threads a test of a printer left
# behind, per device. While one is alive the device is not tested again, so that presses pile up no threads and
# no late cancel of a test job lands inside a later test.
_left_behind = {}
_testing_lock = threading.Lock()
# Taking a printer for a label printer looks at the queues and then adds one: two presses must not interleave.
_queues_lock = threading.Lock()
# The page asks for the devices about every 15 seconds: the answer of the print server is kept for that long,
# and one request waits at most QUEUES_WAIT seconds for it, then shows the last answer it has.
QUEUES_FRESH = 15
QUEUES_WAIT = 3
# The last answer of the print server to `getPrinters` (None until there is one), when it came (monotonic, None
# when it is to be asked again), the thread now asking, and a count that ends an answer still on its way.
_known = {'queues': None, 'at': None, 'thread': None, 'generation': 0}
_known_lock = threading.Lock()


def _devices():
    return core('main').iot_devices


def _text(value, limit=MAX_TEXT):
    return ' '.join(str(value or '').split())[:limit]


def printer_kind(device):
    """(kind core gave the printer, why, what decided) - why is `command_set`, `name` or `default`.

    `command_set`: the printer announces a command language; `name`: its name
    says it; `default`: nothing said otherwise, so it is an office printer.
    """
    subtype = getattr(device, 'device_subtype', '') or ''
    announced = (device.dev or {}).get('device-id', '') if isinstance(device.dev, dict) else ''
    name = (device.device_name or '').lower()
    for kind, commands, names in ((RECEIPT, RECEIPT_COMMANDS, RECEIPT_NAMES), (LABEL, LABEL_COMMANDS, LABEL_NAMES)):
        found = next((command for command in commands if command in announced), '')
        if found:
            return subtype, ('command_set' if subtype == kind else 'unknown'), found
        found = next((word for word in names if word in name), '')
        if found:
            return subtype, ('name' if subtype == kind else 'unknown'), found
    return subtype, ('default' if subtype == OFFICE else 'unknown'), ''


def _scale_protocol(device):
    protocol = getattr(type(device), '_protocol', None)
    if protocol is None:
        return None
    return {
        'driver': type(device).__name__,
        'name': getattr(protocol, 'name', ''),
        'baudrate': getattr(protocol, 'baudrate', None),
        'bytesize': getattr(protocol, 'bytesize', None),
        'parity': getattr(protocol, 'parity', None),
        'stopbits': getattr(protocol, 'stopbits', None),
    }


def _how_tested(device):
    if device.device_type == 'printer':
        return 'box' if getattr(device, 'device_subtype', '') in (RECEIPT, LABEL, OFFICE) else 'none'
    if device.device_type == 'scale':
        return 'box' if SCALE_ACTION in device._actions else 'none'
    if device.device_type in ('scanner', 'keyboard'):
        return 'page'
    return 'none'


def describe(device, queues=None):
    """What the Devices panel of the box shows of one device.

    `queues` is what the print server answered to `getPrinters`; the `printer`
    entry of an office printer holds `label_queue`, the print queue that
    already stands for it as a label printer (empty when it has none).
    `connection` of a printer is `network`, `usb`, `serial` or '' (see `_connection_of`).
    """
    described = {
        'identifier': device.device_identifier,
        'name': _text(device.device_name),
        'type': device.device_type,
        'kind': kind_of(device),
        'connection': _connection_of(device),
        'manufacturer': _text(device.device_manufacturer),
        'test': _how_tested(device),
    }
    if device.device_type == 'printer':
        subtype, why, evidence = printer_kind(device)
        described['printer'] = {
            'kind': subtype,
            'why': why,
            'evidence': evidence,
            'can_become_label': subtype == OFFICE,
            'label_queue': _label_queue_of(device, queues) if subtype == OFFICE else '',
        }
    elif device.device_type == 'scale':
        described['scale'] = {'protocol': _scale_protocol(device)}
    elif device.device_type in ('scanner', 'keyboard'):
        described['keyboard'] = {
            'is_scanner': device.device_type == 'scanner',
            'can_switch': SCANNER_ACTION in device._actions,
        }
    return described


def _connection_of(device):
    """How a device is connected, as the Devices panel names it; never the device address of a printer.

    A printer is named as its print test names it (`_connected_by`), from its
    device address. Core sets `device-class` itself only for a print queue it
    takes from the list of queues (one whose name is not the one core derives
    from its address): `direct` when the address holds `usb`, else `network`,
    a serial or vendor address too; such a queue keeps its address in
    `device-uri`, and `url` is its name. A printer core discovered keeps the
    class the print server gives it (`serial`, `direct`, `network`), and its
    address is `url`.
    """
    if device.device_type != 'printer':
        return device.device_connection
    found = device.dev if isinstance(device.dev, dict) else {}
    return _connected_by(found.get('device-uri') or found.get('url'))[0]


def kind_of(device):
    """The kind of a device: the kind of a printer core derived, else the type core gives the device."""
    if device.device_type == 'printer':
        return getattr(device, 'device_subtype', '') or 'printer'
    return device.device_type


def devices():
    """Every device the box has, each as `describe` gives it, in the order of their identifiers."""
    found = dict(_devices())
    queues = None
    if any(getattr(device, 'device_subtype', '') == OFFICE for device in found.values()):
        queues = _known_queues()
    return [describe(found[identifier], queues) for identifier in sorted(found)]


def _known_queues():
    """What the print server answered to `getPrinters`, asked at most once per QUEUES_FRESH seconds.

    The question runs in a thread of its own and is waited for QUEUES_WAIT
    seconds; a print server that does not answer in time leaves the last answer
    (None if there is none) and the next call joins the question still on its
    way instead of opening another connection.
    """
    with _known_lock:
        at = _known['at']
        if at is not None and time.monotonic() - at < QUEUES_FRESH:
            return _known['queues']
        asking = _known['thread']
        if asking is None or not asking.is_alive():
            generation = _known['generation']
            asking = threading.Thread(target=_ask_queues, args=(generation,), name='ViinQueuesRead', daemon=True)
            _known['thread'] = asking
            asking.start()
    asking.join(QUEUES_WAIT)
    with _known_lock:
        return _known['queues']


def _ask_queues(generation):
    try:
        queues = _own_connection().getPrinters()
    except Exception as error:
        _logger.debug('The print queues cannot be read: %s', type(error).__name__)
        return
    _remember_queues(queues, generation)


def _remember_queues(queues, generation=None, fresh=True):
    """Keep `queues` as the answer of the print server; an answer older than a change of the queues is dropped."""
    with _known_lock:
        if generation is not None and generation != _known['generation']:
            return
        _known['queues'] = queues
        _known['at'] = time.monotonic() if fresh else None
        if not fresh:
            _known['generation'] += 1
            _known['thread'] = None


class _Sent:
    """A test job the print server holds; the box then follows it to see whether the printer took it."""

    def __init__(self, job):
        self.job = job


def _answer(result, kind='', **detail):
    return {'status': result, 'kind': kind, 'detail': detail}


def test(identifier):
    """Test the device `identifier` the way core uses it, with no server.

    {'status': ok, failed or unsupported, 'kind', 'detail'}; `detail` of a
    printer is what its print queue answered, of a scale the value (and the
    `unit`, when the driver gives one) and the status it read. Its `reason`
    says why a test is not ok, and what the box knows of the test print of an
    ok printer test.
    A printer test follows and cancels only the job whose number the print
    server gave for this test, never another print of the queue. It answers
    `taken` only on positive evidence that the printer took that job: the job
    completed, or, while the job itself was processing, the queue was seen
    connecting to the printer and later not. When that evidence does not come
    within FOLLOW_WINDOW seconds after the device call, the answer is
    `printer_not_reached` (with how the printer is connected and, for a network
    printer, its address) and the test job is cancelled. A test print the print
    server took without giving its number is answered ok `sent_not_followed`:
    no job is followed or cancelled, and whether it printed is seen on the
    printer.
    A printer test whose device call has not ended within TEST_TIMEOUT seconds,
    or whose follow-up is still running FOLLOW_MARGIN seconds after its window,
    answers `print_server_silent`; a scale reading that has not ended within
    TEST_TIMEOUT seconds answers `timeout`. Either way a test print not sent yet
    is not sent, a test job the box knows by its number is cancelled, and the
    device is freed. While a call to the print server left by such a test still
    has no answer, a printer test answers `print_server_silent` at once,
    without asking the print server.
    """
    device = _devices().get(identifier) if isinstance(identifier, str) else None
    if device is None:
        return _answer(FAILED, reason='not_connected')
    kind = kind_of(device)
    if device.device_type in ('scanner', 'keyboard'):
        return _answer(UNSUPPORTED, kind, reason='tested_by_page')
    if device.device_type == 'display':
        return _answer(UNSUPPORTED, kind, reason='shows_identification_page')
    if _how_tested(device) != 'box':
        return _answer(UNSUPPORTED, kind, reason='no_test')
    with _testing_lock:
        if _testing.get(identifier):
            return _answer(FAILED, kind, reason='test_in_progress')
        if _still_left_behind(identifier):
            return _answer(FAILED, kind, reason='print_server_silent')
        _testing[identifier] = True
    try:
        return _run_test(device, kind)
    finally:
        with _testing_lock:
            _testing.pop(identifier, None)


def _still_left_behind(identifier):
    """Whether a thread an earlier test of `identifier` left behind is still alive; the caller holds `_testing_lock`."""
    alive = [thread for thread in _left_behind.get(identifier, ()) if thread.is_alive()]
    if alive:
        _left_behind[identifier] = alive
    else:
        _left_behind.pop(identifier, None)
    return bool(alive)


def _leave_behind(identifier, threads):
    with _testing_lock:
        _left_behind[identifier] = [*_left_behind.get(identifier, ()), *(thread for thread in threads if thread)]
        _still_left_behind(identifier)


class _Trial:
    """The test job of a printer test, shared by the request and the thread that makes the device call.

    The job is only ever the number the print server gave for this test. Once
    the request has stopped waiting for that thread, the thread sends no test
    and keeps no job of its own: it cancels a test job it learns the number of
    later.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._job = None
        self._abandoned = False

    def may_send(self):
        """Whether the request still waits, so that the test may be sent."""
        with self._lock:
            return not self._abandoned

    def holds(self, number):
        """Keep `number` as the test job; False when the request has already stopped waiting."""
        with self._lock:
            if self._abandoned:
                return False
            self._job = number
            return True

    def abandon(self):
        """Stop waiting: the number of the test job the print server already holds, None when none is known."""
        with self._lock:
            self._abandoned = True
            return self._job


def _run_test(device, kind):
    outcome = {}
    connection = None
    trial = _Trial()
    if device.device_type != 'scale':
        try:
            connection = _own_connection()
        except Exception as error:
            _logger.warning('The print server cannot be reached: %s', type(error).__name__)
            return _answer(FAILED, kind, reason='queue_missing')

    def run():
        try:
            if connection is None:
                outcome['answer'] = _scale_test(device, kind)
            else:
                outcome['answer'] = _printer_test(device, kind, connection, trial)
        except Exception as error:
            _logger.warning(
                'The test of the device %s failed: %s', device.device_identifier, type(error).__name__, exc_info=True)
            outcome['answer'] = _answer(FAILED, kind, reason='device_error')

    worker = threading.Thread(target=run, name='ViinDeviceTest', daemon=True)
    worker.start()
    worker.join(TEST_TIMEOUT)
    if worker.is_alive():
        if connection is None:
            _logger.warning('The test of the device %s did not end within %s s', device.device_identifier, TEST_TIMEOUT)
            return _answer(FAILED, kind, reason='timeout')
        _logger.warning(
            'The print server did not answer the test of the device %s within %s s', device.device_identifier,
            TEST_TIMEOUT)
        job = trial.abandon()
        canceller = _cancel_on_new_connection(job) if job is not None else None
        _leave_behind(device.device_identifier, [worker, canceller])
        return _answer(FAILED, kind, reason='print_server_silent')
    answer = outcome['answer']
    if not isinstance(answer, _Sent):
        return answer
    followed = {}

    def follow():
        try:
            followed['answer'] = _follow_job(connection, kind, device.device_identifier, answer.job)
        except Exception as error:
            _logger.warning(
                'The test job of the device %s cannot be followed: %s', device.device_identifier, type(error).__name__,
                exc_info=True)
            followed['answer'] = _answer(FAILED, kind, reason='device_error')

    follower = threading.Thread(target=follow, name='ViinDeviceTestFollow', daemon=True)
    follower.start()
    follower.join(FOLLOW_WINDOW + FOLLOW_MARGIN)
    if follower.is_alive():
        _logger.warning(
            'The follow-up of the test job %s of the device %s did not end within %s s', answer.job,
            device.device_identifier, FOLLOW_WINDOW + FOLLOW_MARGIN)
        canceller = _cancel_on_new_connection(answer.job)
        _leave_behind(device.device_identifier, [follower, canceller])
        return _answer(FAILED, kind, reason='print_server_silent')
    return followed['answer']


def _cancel_on_new_connection(number):
    """Try to cancel the job `number` on a connection of its own, for at most CANCEL_WAIT seconds: the thread."""
    def cancel():
        try:
            _own_connection().cancelJob(number)
        except Exception as error:
            _logger.warning('The test job %s cannot be cancelled: %s', number, type(error).__name__)

    canceller = threading.Thread(target=cancel, name='ViinDeviceTestCancel', daemon=True)
    canceller.start()
    canceller.join(CANCEL_WAIT)
    return canceller


def _scale_test(device, kind):
    device.action({'action': SCALE_ACTION})
    data = device.data if isinstance(device.data, dict) else {}
    status = data.get('status') if isinstance(data.get('status'), dict) else {}
    status = {
        'status': _text(status.get('status'), 40),
        'message_title': _text(status.get('message_title')),
        'message_body': _text(status.get('message_body')),
    }
    value = data.get('value')
    # Core's scale drivers give no unit (the scale's own menu sets it): it is passed on only when the driver does.
    unit = _text(data.get('unit'), MAX_UNIT) if isinstance(data.get('unit'), str) else ''
    if status['status'] == 'error' or not isinstance(value, (int, float)) or isinstance(value, bool):
        return _answer(FAILED, kind, reason='no_reading', value=value if isinstance(value, (int, float)) else None,
                       status=status)
    if unit:
        return _answer(OK, kind, value=value, unit=unit, status=status)
    return _answer(OK, kind, value=value, status=status)


def _own_connection():
    """A connection to the print server that only the caller uses.

    The connection core shares is guarded by a lock core holds for as long as
    its scan of the printers lasts (a scan asks every print backend, which can
    take many seconds), so a test that waited for that lock would wait inside
    its own time limit. A connection of its own needs no lock: the print server
    serves each connection alone.
    """
    import cups
    return cups.Connection()


def _job_state(connection, number):
    attributes = connection.getJobAttributes(number, requested_attributes=['job-state'])
    return JOB_STATES.get(attributes.get('job-state'), 'unknown')


def _queue(connection, queue):
    attributes = connection.getPrinterAttributes(queue, requested_attributes=[
        'printer-state', 'printer-state-message', 'printer-is-accepting-jobs', 'printer-state-reasons',
        'device-uri'])
    reasons = attributes.get('printer-state-reasons') or []
    return {
        'state': QUEUE_STATES.get(attributes.get('printer-state'), 'unknown'),
        'accepting': bool(attributes.get('printer-is-accepting-jobs')),
        'message': _text(attributes.get('printer-state-message')),
        'device_uri': str(attributes.get('device-uri') or ''),
        'reasons': [_text(reason, 60) for reason in (reasons if isinstance(reasons, list) else [reasons])
                    if reason and reason != 'none'],
    }


def _label(device):
    """A small label in ZPL with two lines, in the characters every label printer has."""
    name = re.sub(r'[^ -~]|[\^~]', '', device.device_name or '')[:40]
    return (f'^XA^CF0,30^FO20,20^FD{LABEL_TEXT}^FS^CF0,24^FO20,60^FD{name}^FS^XZ\n').encode('ascii')


def _status_ticket(device):
    """The status ticket core's `print_status` composes for `device`, kept instead of printed.

    `print_status` hands its ticket to `print_raw`, which tells no one the
    number of the job: it runs here on a copy of the driver whose own
    `print_raw` keeps what it is given. The driver itself, which the point of
    sale prints through at the same time, is not changed.
    """
    composed = []
    twin = copy.copy(device)
    twin.print_raw = lambda data, *_args, **_options: composed.append(data)
    twin.print_status()
    return b''.join(composed)


def _lp(queue, data):
    """Send `data` to the print queue `queue` with the command core's `print_raw` runs, `lp -d <queue>`.

    (whether `lp` took it, the number of its job or None). `lp` names the job
    in its request id `<queue>-<number>`; TEST_JOB_NAME only names the job for
    a person reading the queue.
    """
    process = subprocess.run(
        ['lp', '-d', queue, '-t', TEST_JOB_NAME], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False)
    if process.returncode != 0:
        _logger.warning('lp did not take the test print for the queue %s: %s', queue,
                        _text(process.stderr.decode('utf-8', 'replace')))
        return False, None
    found = re.search(r'(?<!\S)' + re.escape(queue) + r'-(\d+)\b', process.stdout.decode('utf-8', 'replace'))
    return True, int(found.group(1)) if found else None


def _shown(queue, message=True):
    """What of a queue an answer carries: never its device address (it may hold credentials), its message if asked."""
    return {key: value for key, value in queue.items() if key != 'device_uri' and (message or key != 'message')}


def _connected_by(device_uri):
    """(how the printer is connected, its address) from the device address of a print queue.

    The way is `usb`, `network`, `serial`, or '' when the address says none of
    them. Only the schemes whose host part is the address of the printer itself
    (NETWORK_SCHEMES) make a network printer; its address is host[:port] as
    written in the device address, with no user name, password or path. No
    other way gives an address, nor does any other scheme (a vendor backend, a
    service name to be looked up, a class of queues).
    """
    uri = str(device_uri or '')
    scheme = uri.split(':', 1)[0].lower()
    if scheme == 'usb' or '/usb/' in uri.lower():
        return 'usb', ''
    if scheme == 'serial':
        return 'serial', ''
    if scheme not in NETWORK_SCHEMES:
        return '', ''
    try:
        parts = urlsplit(uri)
        host = parts.netloc.rpartition('@')[2]
    except ValueError:
        return '', ''
    if not host:
        return '', ''
    return 'network', _text(unquote(host), MAX_ADDRESS)


def _is_connecting(reasons):
    return any(reason == CONNECTING or reason.startswith(CONNECTING + '-') for reason in reasons)


def _printer_test(device, kind, connection, trial):
    """Hand a test job to the queue of `device`: a final answer, or `_Sent` once the print server holds the job.

    The test job is known only by the number the print server gives for it:
    the one `printTestPage` returns for an office printer, the one `lp`
    reports for the status ticket or the label. A test `lp` took without a
    number answers `sent_not_followed`, and no job is followed or cancelled:
    the queue may hold tickets of the point of sale sent in the same moment.
    Once `trial` is abandoned the test is not sent, and a test job whose
    number comes after that is cancelled here, on this same connection.
    """
    queue = device.device_identifier
    try:
        answered = _queue(connection, queue)
    except Exception as error:
        _logger.warning('The print queue of %s cannot be read: %s', queue, type(error).__name__)
        return _answer(FAILED, kind, reason='queue_missing')
    if not answered['accepting'] or answered['state'] == 'stopped':
        return _answer(FAILED, kind, reason='queue_not_accepting', queue=_shown(answered))
    data = None
    if kind == LABEL:
        data = _label(device)
    elif kind != OFFICE:
        data = _status_ticket(device)
    if not trial.may_send():
        _logger.warning('The test of the queue %s is not sent: the test ended before the print server answered', queue)
        return _answer(FAILED, kind, reason='print_server_silent')
    if data is None:
        sent, number = True, connection.printTestPage(queue) or None
    else:
        sent, number = _lp(queue, data)
    if number is None:
        if sent:
            _logger.warning('The test of the queue %s was sent with no job number: it is not followed', queue)
            return _answer(OK, kind, reason='sent_not_followed', queue=_shown(_queue(connection, queue)))
        return _answer(FAILED, kind, reason='not_queued', queue=_shown(_queue(connection, queue)))
    if not trial.holds(number):
        _logger.warning('The test job %s of the queue %s came after the test ended and is cancelled', number, queue)
        try:
            connection.cancelJob(number)
        except Exception as error:
            _logger.warning('The test job %s of the queue %s cannot be cancelled: %s', number, queue,
                            type(error).__name__)
        return _answer(FAILED, kind, reason='print_server_silent')
    return _Sent(number)


def _follow_job(connection, kind, queue, number):
    """Follow the test job `number` and its queue for FOLLOW_WINDOW seconds, and answer whether the printer took it.

    A completed job was taken. A job that is processing was taken once the
    queue, while this job was processing, has been seen connecting to the
    printer and no longer is (a job is already processing while its filters
    run, before the print server tries the printer). The queue connecting
    while this job is still pending is opening the printer for a job ahead of
    it, and says nothing about this one. A job that ends stopped, canceled or
    aborted is a failure.
    When the window ends with no such evidence, whatever the job state (pending
    or processing) and the reasons or message of the queue, the printer was not
    reached: this job only is cancelled and the answer is `printer_not_reached`.
    """
    deadline = time.monotonic() + FOLLOW_WINDOW
    seen_connecting = False
    while True:
        state = _job_state(connection, number)
        queue_now = _queue(connection, queue)
        connecting = _is_connecting(queue_now['reasons'])
        seen_connecting = seen_connecting or (connecting and state == 'processing')
        if state in JOB_FAILURES:
            return _answer(FAILED, kind, reason=f'job_{state}', job=number, job_state=state, queue=_shown(queue_now))
        if state == 'completed' or (state == 'processing' and seen_connecting and not connecting):
            return _answer(OK, kind, reason='taken', job=number, job_state=state, queue=_shown(queue_now))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(FOLLOW_STEP, remaining))
    _logger.warning(
        'The printer of the queue %s did not take the test job %s within %s s (job %s, queue %s: %s)',
        queue, number, FOLLOW_WINDOW, state, queue_now['state'],
        queue_now['message'] or ', '.join(queue_now['reasons']))
    try:
        connection.cancelJob(number)
    except Exception as error:
        _logger.warning('The test job %s of the queue %s cannot be cancelled: %s', number, queue, type(error).__name__)
    how, address = _connected_by(queue_now['device_uri'])
    detail = {'job': number, 'job_state': state, 'queue': _shown(queue_now, message=False), 'connection': how}
    if how == 'network':
        detail['address'] = address
    return _answer(FAILED, kind, reason='printer_not_reached', **detail)


def use_as(identifier, is_scanner):
    """Take a keyboard device for a scanner, or a scanner for a keyboard, with core's own driver action.

    {'status': 'switched', 'kind'}, or {'status': 'refused', 'reason':
    not_connected, not_switchable or invalid_choice}. The box keeps the choice
    across its restarts and has its server told at once.
    """
    if not isinstance(is_scanner, bool):
        return {'status': 'refused', 'reason': 'invalid_choice'}
    device = _devices().get(identifier) if isinstance(identifier, str) else None
    if device is None:
        return {'status': 'refused', 'reason': 'not_connected'}
    if SCANNER_ACTION not in device._actions:
        return {'status': 'refused', 'reason': 'not_switchable'}
    device.action({'action': SCANNER_ACTION, 'is_scanner': is_scanner})
    # Core reports a device when the list of devices changes, and the list holds the same device.
    reporting.request_report()
    _logger.info('The device %s is now used as a %s', identifier, device.device_type)
    return {'status': 'switched', 'kind': device.device_type}


def _label_queue_name(device, taken):
    base = re.sub(r'[^A-Za-z0-9_-]+', '_', device.device_name or device.device_identifier).strip('_') or 'printer'
    base = base[:MAX_QUEUE_NAME - 8]
    name, number = f'{base}_{LABEL_MARK}', 2
    while name in taken:
        name, number = f'{base}_{LABEL_MARK}{number}', number + 1
    return name


def _label_queue_of(device, queues):
    """The print queue, marked as a label printer's, that has the address of the queue of `device`, else ''."""
    uri = str(((queues or {}).get(device.device_identifier) or {}).get('device-uri') or '').lower()
    if not uri:
        return ''
    return next((name for name, attributes in sorted(queues.items())
                 if LABEL_MARK.lower() in name.lower() and str(attributes.get('device-uri') or '').lower() == uri), '')


def make_label_printer(identifier):
    """Have core take an office printer for the label printer it is, by a second print queue whose name says ZPL.

    The queue the device stands for is never touched: the device keeps
    printing through it, and core keeps reporting it, until the printer is
    unplugged. Core finds the new queue at its next look at the printers,
    within two minutes, takes it for a label printer by its name, and the
    server is told then. A printer that already has such a queue gets no
    other. {'status': 'added', 'queue'}, or {'status': 'refused', 'reason':
    not_connected, not_office_printer, queue_missing or already_added (+
    `queue`, the label queue the printer already has)}.
    """
    device = _devices().get(identifier) if isinstance(identifier, str) else None
    if device is None:
        return {'status': 'refused', 'reason': 'not_connected'}
    if device.device_type != 'printer' or getattr(device, 'device_subtype', '') != OFFICE:
        return {'status': 'refused', 'reason': 'not_office_printer'}
    connection = _own_connection()
    with _queues_lock:
        queues = connection.getPrinters()
        _remember_queues(queues)
        queue = queues.get(identifier)
        if not queue or not queue.get('device-uri'):
            return {'status': 'refused', 'reason': 'queue_missing'}
        existing = _label_queue_of(device, queues)
        if existing:
            return {'status': 'refused', 'reason': 'already_added', 'queue': existing}
        uri = queue['device-uri']
        name = _label_queue_name(device, queues)
        with core('tools.helpers').writable():
            connection.addPrinter(name=name, device=uri)
            connection.setPrinterInfo(name, _text(device.device_name, 120))
            connection.enablePrinter(name)
            connection.acceptJobs(name)
            connection.setPrinterUsersAllowed(name, ['all'])
            if 'usb' in uri:
                connection.addPrinterOptionDefault(name, 'usb-no-reattach', 'true')
                connection.addPrinterOptionDefault(name, 'usb-unidir', 'true')
        # The page names the new queue at once, even if the print server is slow to answer the next time.
        _remember_queues({**queues, name: {'device-uri': uri}}, fresh=False)
    _logger.info('The printer %s gets the print queue %s, which core takes for a label printer', identifier, name)
    return {'status': 'added', 'queue': name}
