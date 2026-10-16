"""Who uses the box page, and when: what `viin-box-wifi watch` reads before it takes the radio from a person who may
be setting the box up through its access point.

A browser reaches the box service through the web server of the box, or straight on the port of the box service,
which core's status ticket prints. The routes that the box page and its log page read regularly note each request
in RECORD, whichever way it came: the address of the device, and the time `time.monotonic` gives, a clock every
process of the box reads alike and that no change of the time of day moves. The web server hands each request on
from WEB_SERVER, with the address of its own caller in CLIENT_HEADER, which it sets whatever the caller sent. The
header is believed from there only: a device that reaches the box service straight is noted by its own address,
whatever it claims.
"""
import json
import logging
import os
import threading
import time
from pathlib import Path

_logger = logging.getLogger(__name__)

# {address: time of its last use}. `viin-box-wifi` reads it at this place. The directory is the one the box service is
# given under /run before each start: in memory, kept while the box is on, so a restart of the box service keeps it.
RECORD = Path('/run/odoo/viin-page-use.json')
# The address from which the web server of the box hands requests on, and the header in which it names its caller.
WEB_SERVER = '127.0.0.1'
CLIENT_HEADER = 'X-Real-IP'
# Seconds. A use older than this is dropped as the record is written: the watch reads far less of the past.
KEPT = 3600

_writing = threading.Lock()
_reported = []


def begin():
    """Make RECORD, empty, unless it is there: the watch tells a box page nobody used from a record nobody keeps,
    where it counts every device connected to the access point as a person."""
    try:
        descriptor = os.open(RECORD, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    except OSError as error:
        _report(error)
        return
    with os.fdopen(descriptor, 'w') as record:
        json.dump({}, record)


def note(request):
    """Note a use of the box page by `request`, the HTTP request of werkzeug being answered. Never raises."""
    address = _caller(request)
    if not address:
        return
    now = time.monotonic()
    with _writing:
        try:
            used = json.loads(RECORD.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            used = {}
        used = {
            other: when for other, when in (used.items() if isinstance(used, dict) else ())
            if isinstance(when, (int, float)) and now - when < KEPT}
        used[address] = now
        staged = RECORD.with_name(f'.{RECORD.name}.new')
        try:
            with os.fdopen(os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as record:
                json.dump(used, record)
            os.replace(staged, RECORD)
        except OSError as error:
            _report(error)


def _caller(request):
    peer = request.environ.get('REMOTE_ADDR')
    if peer == WEB_SERVER:
        return request.headers.get(CLIENT_HEADER) or peer
    return peer


def _report(error):
    if not _reported:
        _reported.append(True)
        _logger.warning(
            'The use of the box page cannot be noted in %s (%s): the Wi-Fi watch of the box may misjudge who sets '
            'the box up through its access point', RECORD, type(error).__name__)
