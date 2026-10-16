"""What the test files of the box services share, beside `common`.

* `keep_files` - files of the box a test changes, put back when it ends;
* `outside_references` and `names_the_vendor` - what a page of the box loads from elsewhere, and whom it names;
* `LiveChannelServer` - a server with a live channel, as far as a box can tell;
* `FakeBoard` - the network interfaces of a board, as the system describes them;
* `Releases` - where a box takes its software from, made of local repositories, and the two trees it runs;
* `WebServer` - the web server of the box, run on ports of its own with the site the box installs;
* `print_jobs` - what the print server of the software box printed.

Child functions here are meant for `common.in_new_process`.
"""
import base64
import contextlib
import datetime
import hashlib
import html.parser
import http.client
import http.server
import json
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import common

HOME = Path('/home/pi')
STATE_FILE = HOME / 'viin-box-state.json'
CHANNEL_FILE = HOME / 'viin-box-channel'
OWNERS_FILE = Path('/run/odoo/viin-device-owners.json')
PRINTS = Path('/var/lib/softbox/prints')
SCALE_FILE = Path('/var/lib/softbox/scale-weight')
SCANNER = 'simulated_scanner'
SCALE = 'simulated_scale'
OFFICE_PRINTER = 'softbox_office'
LABEL_PRINTER = 'softbox_zpl'
RECEIPT_PRINTER = 'softbox_tm-m30'

REPOSITORY = common.LAYER_MODULE.parent
SYSTEM_FILES = REPOSITORY / 'box' / 'system'
GUARD = SYSTEM_FILES / 'usr' / 'local' / 'sbin' / 'viin-box-guard'
SITE = SYSTEM_FILES / 'etc' / 'nginx' / 'sites-enabled' / 'default'
RULES = SYSTEM_FILES / 'etc' / 'nginx' / 'viindoo' / 'box.conf'
NAME_TEMPLATE = SYSTEM_FILES / 'etc' / 'nginx' / 'viindoo' / 'name.conf.template'
BOX_DOWN_PAGE = SYSTEM_FILES / 'usr' / 'share' / 'viindoo-iot' / 'box-down.html'
POINTER_FILE = REPOSITORY / 'box-release.json'
# The two places the box software comes from.
LAYER_REPOSITORY = 'https://github.com/Viindoo/iot-hw.git'
CORE_REPOSITORY = 'https://github.com/Viindoo/odoo.git'
SERIES = '18.0'

STATUS = '/viin_hw/status'
DIAGNOSTIC = '/viin_hw/diagnostic'
CLOCK = '/viin_hw/clock'
DEVICE_TEST = '/viin_hw/device/test'
USE_AS = '/viin_hw/device/use_as'
LABEL_PRINTER_ROUTE = '/viin_hw/device/label_printer'
UPDATE = '/viin_hw/update'
PRINTER_ADD = '/viin_hw_homepage/printer_add'
PAGE_DATA = '/hw_posbox_homepage/data'
VERSION_INFO = '/hw_posbox_homepage/version_info'
UPDATE_GIT_TREE = '/hw_posbox_homepage/update_git_tree'
LOG_LEVELS_UPDATE = '/hw_posbox_homepage/log_levels_update'

# What a browser adds to a request that a page of another web site made it send.
FROM_ANOTHER_WEB_SITE = {'Sec-Fetch-Site': 'cross-site', 'Origin': common.ANOTHER_WEB_SITE}
# The reasons the box gives a request it refuses as locked, and one it refuses as sent for another web site.
LOCKED = 'unlock_required'
OTHER_SITE = 'other_site'
# A hardware address no board has, for a box the suite gives a network card.
SUITE_HARDWARE_ADDRESS = '02:00:5e:00:53:01'


# ---------------------------------------------------------------------------------------------- files of the box

def keep_files(case, *paths):
    """Put each file back as it was, or absent, when the test `case` ends."""
    for path in paths:
        case.addCleanup(_put_back, Path(path), Path(path).read_bytes() if Path(path).exists() else None)


def _put_back(path, content):
    if content is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(content)


def pointer_release():
    """The release the pointer file of the layer names: what the box this suite runs reports it runs."""
    return json.loads(POINTER_FILE.read_text())['release']


# ---------------------------------------------------------------------------------------------- pages

class _References(html.parser.HTMLParser):
    """Every address a page loads or links to: its attributes, and the addresses in its styles and scripts."""

    ATTRIBUTES = ('src', 'href', 'action', 'data', 'poster', 'srcset', 'formaction')
    IN_CODE = re.compile(
        r'''url\(\s*['"]?([^'")\s]+)|@import\s+['"]([^'"]+)|['"`]((?:[a-z][a-z0-9+.-]*:)?//[^'"`\s]+)''',
        re.IGNORECASE)

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found = []
        self._code = None

    def handle_starttag(self, tag, attributes):
        for name, value in attributes:
            if name == 'srcset' and value:
                self.found.extend(part.split()[0] for part in value.split(',') if part.strip())
            elif name in self.ATTRIBUTES and value:
                self.found.append(value)
            if name == 'style' and value:
                self._scan(value)
        if tag in ('style', 'script'):
            self._code = tag

    def handle_endtag(self, tag):
        if tag == self._code:
            self._code = None

    def handle_data(self, data):
        if self._code:
            self._scan(data)

    def _scan(self, code):
        for groups in self.IN_CODE.findall(code):
            self.found.extend(group for group in groups if group)


_LOCAL_SCHEMES = ('data:', 'javascript:', 'about:', 'blob:')


def outside_references(page):
    """The addresses in the HTML text `page` that lead to another host than the box: each names a host of its own."""
    references = _References()
    references.feed(page)
    outside = []
    for address in references.found:
        address = address.strip()
        if address.lower().startswith(_LOCAL_SCHEMES):
            continue
        if address.startswith('//') or re.match(r'[a-z][a-z0-9+.-]*:', address, re.IGNORECASE):
            outside.append(address)
    return outside


def page_text_check():
    """The module of the text check the repository runs before a release: the one rule for what a text may name."""
    import importlib.util

    specification = importlib.util.spec_from_file_location(
        'check_page_texts', REPOSITORY / 'box' / 'build' / 'check_page_texts.py')
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def names_the_vendor(text):
    """Whether `text` names the vendor of core, as the text check of the repository reads it."""
    return bool(page_text_check().vendor_words(text))


# ---------------------------------------------------------------------------------------------- devices

_device_classes = {}


def plug(identifier, device_type='keyboard'):
    """Plug a device into the box started in this process, as core holds its devices; return it.

    It takes the actions core's keyboard and scale devices are asked for, lists them in `asked`, and
    tells the pages that listen to it after each, as a device that read something does.
    """
    if 'device' not in _device_classes:
        class SuiteDevice(common.core('driver').Driver):

            def __init__(self, identifier, device_type):
                super().__init__(identifier, None)
                self.device_type = device_type
                self.device_name = f'Box suite {device_type}'
                self.asked = []
                self._actions.update(dict.fromkeys(('update_is_scanner', 'read_once', 'box_suite'), self._run))

            def _run(self, data):
                self.asked.append(data.get('action'))
                common.core('event_manager').event_manager.device_changed(self)

        _device_classes['device'] = SuiteDevice
    device = _device_classes['device'](identifier, device_type)
    common.core('main').iot_devices[identifier] = device
    return device


def wait_for_devices(*identifiers, timeout=60):
    """Wait until the box started in this process has the devices `identifiers`; return them."""
    main = common.core('main')
    common.wait_until(lambda: set(identifiers) <= set(main.iot_devices), f'the devices {identifiers}', timeout)
    return [main.iot_devices[identifier] for identifier in identifiers]


def print_jobs(queue):
    """{file name: content} of each job the print queue `queue` of the software box printed."""
    return {path.name: path.read_bytes() for path in PRINTS.glob(f'{queue}-job-*')}


def new_print_job(queue, before, timeout=30):
    """The content of the job `queue` printed since `before` (a `print_jobs` of that queue) - wait for it."""
    found = common.wait_until(lambda: set(print_jobs(queue)) - set(before), f'a print job on {queue}', timeout)
    return print_jobs(queue)[sorted(found)[-1]]


def kind_core_gives(queue):
    """The kind of printer core's own printer handlers make of the print queue `queue`, as they find it now.

    The printer interface of core looks at the print server every two minutes: this asks it now,
    and gives what core's printer driver then derives, without starting a driver.
    """
    main = common.core('main')
    interface = next(handler for handler in main.interfaces.values() if handler.connection_type == 'printer')
    found = interface().get_devices()
    identifier = interface.get_identifier(None, queue)
    if identifier not in found:
        return None
    driver = next(handler for handler in main.drivers if handler.connection_type == 'printer')
    return driver(identifier, found[identifier]).device_subtype


def print_queues():
    """What the print server says of its print queues: one line per queue, with the address of its printer."""
    return subprocess.run(['lpstat', '-v'], capture_output=True, text=True, check=False).stdout


def remove_print_queues(*queues):
    for queue in queues:
        subprocess.run(['lpadmin', '-x', queue], capture_output=True, check=False)


def add_print_queue(queue, device_uri):
    subprocess.run(['lpadmin', '-p', queue, '-E', '-v', device_uri], capture_output=True, check=True)


def settings_code(credential):
    """The settings code the administrators of the server read for the box that holds `credential`."""
    return common.layer('lock').settings_code(credential)


# ---------------------------------------------------------------------------------------------- live channel

_WEBSOCKET_KEY = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


class LiveChannelServer:
    """A server with a live channel, as far as a box can tell.

    It takes the reports of a box and answers them with the name of its channel, takes print
    completions and the box log, and lets a box connect to the channel and subscribe.
    `happened` lists, in the order they came: ('report', what the box sent) for each report,
    ('subscribe', what the box sent) for each subscription, ('completion', parameters) for each
    print completion, ('drop', None) when it dropped the connections. `send(messages)` sends
    messages to every box connected, `drop()` closes every connection as a server that restarts
    does. `channel` is what it answers a report with.
    """

    def __init__(self, port=0, channel='box-suite-channel'):
        self.channel = channel
        self.happened = []
        self._lock = threading.Lock()
        self._connections = []
        self._server = self._listener()(('127.0.0.1', port), self._handler())
        self.port = self._server.server_address[1]
        self.url = f'http://127.0.0.1:{self.port}'
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        self._thread.start()

    def kinds(self):
        return [kind for kind, _what in list(self.happened)]

    def reports(self):
        return [what for kind, what in list(self.happened) if kind == 'report']

    def subscriptions(self):
        return [what for kind, what in list(self.happened) if kind == 'subscribe']

    def completions(self):
        return [what for kind, what in list(self.happened) if kind == 'completion']

    def send(self, messages):
        payload = json.dumps(messages).encode()
        length = bytes([len(payload)]) if len(payload) < 126 else struct.pack('>BH', 126, len(payload))
        header = bytes([0x81]) + length
        with self._lock:
            connections = list(self._connections)
        for connection in connections:
            with contextlib.suppress(OSError):
                connection.sendall(header + payload)

    def drop(self):
        with self._lock:
            connections, self._connections[:] = list(self._connections), []
            self.happened.append(('drop', None))
        for connection in connections:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)

    def close(self):
        self.drop()
        if self._thread.is_alive():
            self._server.shutdown()
            self._server.server_close()

    def _note(self, kind, what):
        with self._lock:
            self.happened.append((kind, what))

    @staticmethod
    def _listener():
        class Listener(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                """A box that leaves is what several tests are about: say nothing."""

        return Listener

    def _handler(self):
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):

            def log_message(self, *arguments):
                pass

            def do_POST(self):  # noqa: N802 - the name the standard library calls
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
                params = body.get('params') or {}
                if self.path == '/iot/setup':
                    server._note('report', params)
                    self._answer(server.channel)
                elif self.path == '/iot/printer/status':
                    server._note('completion', params)
                    self._answer(True)
                elif self.path == '/iot/log':
                    self._answer(True)
                else:
                    self.send_response_only(404)
                    self.send_header('Content-Length', '0')
                    self.end_headers()

            def _answer(self, result):
                data = common.rpc_result(result)
                self.send_response_only(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                key = self.headers.get('Sec-WebSocket-Key')
                if self.path != '/websocket' or not key:
                    self.send_response_only(404)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                accept = base64.b64encode(hashlib.sha1(key.encode() + _WEBSOCKET_KEY).digest()).decode()
                self.send_response_only(101)
                self.send_header('Upgrade', 'websocket')
                self.send_header('Connection', 'Upgrade')
                self.send_header('Sec-WebSocket-Accept', accept)
                self.end_headers()
                self.wfile.flush()
                with server._lock:
                    server._connections.append(self.connection)
                self.close_connection = True
                while True:
                    frame = self._frame()
                    if frame is None or frame[0] == 8:
                        return
                    if frame[0] == 1:
                        message = json.loads(frame[1])
                        if message.get('event_name') == 'subscribe':
                            server._note('subscribe', message.get('data') or {})

            def _frame(self):
                try:
                    head = self.rfile.read(2)
                    if len(head) < 2:
                        return None
                    length = head[1] & 0x7f
                    if length == 126:
                        length = struct.unpack('>H', self.rfile.read(2))[0]
                    elif length == 127:
                        length = struct.unpack('>Q', self.rfile.read(8))[0]
                    mask = self.rfile.read(4) if head[1] & 0x80 else b'\0\0\0\0'
                    data = self.rfile.read(length)
                except OSError:
                    return None
                return head[0] & 0x0f, bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))

        return Handler


def print_command(mac, device, print_id, document):
    """The message by which a server has the box print `document` (bytes) on `device`, as viin_iot sends it."""
    return [{'message': {'type': 'iot_action', 'payload': {
        'iotDevice': {'iotIdentifiers': [mac], 'identifiers': [{'identifier': device}]},
        'action': '', 'print_id': print_id, 'iot_idempotent_id': f'{print_id}-once',
        'document': base64.b64encode(document).decode(),
    }}}]


# ---------------------------------------------------------------------------------------------- board

class FakeBoard:
    """The network interfaces of a board, as the system describes them under /sys, in a directory of its own.

    `patched()` makes the box started in this process read them there for the block.
    """

    def __init__(self, directory, device_tree=True):
        self.root = Path(directory)
        self.net = self.root / 'class' / 'net'
        self.tree = self.root / 'devicetree'
        self.net.mkdir(parents=True)
        for bus in ('usb', 'platform'):
            (self.root / 'bus' / bus).mkdir(parents=True)
        if device_tree:
            self.tree.mkdir()
        self._usb_ports = 0

    def interface(self, name, address, wireless=False, on='board'):
        """An Ethernet-type interface: `on` the board itself, on a USB port (`usb`), or on the internal USB hub of
        the board (`board_usb`), the way some boards attach their wired interface."""
        if on == 'board':
            device = self.root / 'devices' / 'platform' / name
            device.mkdir(parents=True)
            (device / 'of_node').mkdir()
            (device / 'subsystem').symlink_to(self.root / 'bus' / 'platform')
        else:
            self._usb_ports += 1
            port = self.root / 'devices' / 'usb1' / f'1-{self._usb_ports}'
            device = port / f'1-{self._usb_ports}:1.0'
            device.mkdir(parents=True)
            (device / 'subsystem').symlink_to(self.root / 'bus' / 'usb')
            if on == 'board_usb':
                (port / 'of_node').mkdir()
        interface = self.net / name
        interface.mkdir()
        (interface / 'type').write_text('1\n')
        (interface / 'device').symlink_to(device)
        (interface / 'address').write_text(f'{address}\n')
        if wireless:
            (interface / 'wireless').mkdir()

    def patched(self):
        return mock.patch.multiple(common.layer('seams.identity'), NETWORK_INTERFACES=self.net, DEVICE_TREE=self.tree)


@contextlib.contextmanager
def network(addresses, gateway=None):
    """Give the box started in this process the IPv4 addresses `addresses`, {interface: address}, for the block.

    Core and the layer read the interfaces of the machine through netifaces; the software box has none. The
    dictionary is read at each question, so an address added to it during the block appears on the box.
    """
    import netifaces

    read = netifaces.ifaddresses

    def ifaddresses(interface):
        if interface in addresses:
            return {netifaces.AF_INET: [{'addr': addresses[interface]}],
                    netifaces.AF_LINK: [{'addr': SUITE_HARDWARE_ADDRESS}]}
        return read(interface)

    gateways = {'default': {netifaces.AF_INET: (gateway, next(iter(addresses), 'lo'))}} if gateway else {'default': {}}
    with mock.patch.object(netifaces, 'interfaces', side_effect=lambda: ['lo', *addresses]), \
            mock.patch.object(netifaces, 'ifaddresses', ifaddresses), \
            mock.patch.object(netifaces, 'gateways', return_value=gateways):
        yield


def box_made_certificate(names, made_days_ago=0, lasting_days=400):
    """The certificate the box makes for itself, as a `Certificate`, made `made_days_ago` days ago for `names`."""
    day = datetime.timedelta(days=1)
    return common.make_certificate(
        f'Viindoo IoT Box {common.board_serial()}', names=names,
        valid_from=common.utc_now() - (made_days_ago + 1) * day, valid_until=common.utc_now() + lasting_days * day)


def follow_a_certificate_the_web_server_refused_to_take(address):
    """Start a box that holds `address`, whose watcher of the addresses looks only when it is let; have the box
    replace its own certificate that nears its end at a look whose web server refuses to take the new one, then let
    the watcher look once, at the same addresses.

    Meant for `common.in_new_process`. Gives 'meanwhile', what holds once the refusal is over, and 'after', once the
    watcher looked: 'served', whether the web server presents the certificate installed, and 'line', the state, the
    reason and the action of the browser address line of the self-test.
    """
    let_look, watcher_waits, keeper_waits, sleep = threading.Semaphore(0), [], [], time.sleep

    def wait(seconds):
        trust = sys.modules.get(f'{common.LAYER}.trust')
        thread = threading.current_thread()
        # A command the watcher runs during a look waits on `time.sleep` too: only its wait between two looks is held.
        if trust and isinstance(thread, trust.AddressWatcher) and seconds >= trust.ADDRESS_LOOK_PERIOD:
            watcher_waits.append(seconds)
            return let_look.acquire()
        if trust and isinstance(thread, trust.CertificateKeeper) and seconds >= 3600:
            keeper_waits.append(seconds)
            return threading.Event().wait()
        return sleep(seconds)

    time.sleep = wait
    host = socket.gethostname()
    names = [host, f'{host}.local', address]
    common.install_web_certificate(box_made_certificate(names))
    with network({'eth0': address}):
        common.start()
        common.wait_until(lambda: watcher_waits and keeper_waits, 'the box to end the looks of its start')
        common.install_web_certificate(box_made_certificate(names, lasting_days=59))
        common.serve_web_certificate()
        with common.failing('nginx'), contextlib.suppress(OSError, subprocess.SubprocessError):
            common.layer('trust').refresh_box_certificate()
        meanwhile = _what_browsers_meet(address)
        looks = len(watcher_waits)
        let_look.release()
        common.wait_until(lambda: len(watcher_waits) > looks, 'the watcher of the addresses to look')
        with contextlib.suppress(AssertionError):
            common.wait_until(_installed_certificate_served, 'the web server to present the certificate', 10)
        return {'meanwhile': meanwhile, 'after': _what_browsers_meet(address)}


def _installed_certificate_served():
    return common.served_certificate() == common.fingerprint(common.web_certificate())


def _what_browsers_meet(address):
    chosen = {'address': address, 'source': 'address', 'core': address}
    with mock.patch.object(common.layer('seams.identity'), 'reported_address', return_value=chosen):
        lines = common.layer('selftest').run(wait=0, refresh=True)['lines']
    line = next(line for line in lines if line['id'] == 'browser_address')
    return {'served': _installed_certificate_served(), 'line': [line['state'], line['reason'], line['action']]}


# ---------------------------------------------------------------------------------------------- releases

def release_channel(series):
    """The branch a published release of `series` moves, and nothing else does."""
    return f'box-release/{series}'


def pointer(release, core_commit, series=SERIES, min_image='24.11', candidate=None):
    """The text of a pointer file; a release named `rc.<n>` is a candidate unless `candidate` says otherwise."""
    if candidate is None:
        candidate = bool(re.fullmatch(r'(?:.*-)?rc\.[0-9]+', release))
    return json.dumps({'release': release, 'series': series, 'odoo_commit': core_commit, 'min_image': min_image,
                       'candidate': candidate})


class Releases:
    """Where a box takes its software from, made of local repositories, and the two trees of a box that runs it.

    `layer_remote` and `core_remote` stand for the two places the box software comes from; `layer_tree`
    and `core_tree` are the trees of the box, clones of them. The core repository starts with
    the commits `core_commits[0]` and `[1]`; the layer repository with the commit `first`, on the
    branch `start`, whose pointer names the release `1.0` and `core_commits[0]`, as the trees are.
    Each release holds a `box/system/apply.sh` that only says it ran, in `system_files_of`.
    """

    def __init__(self, directory):
        self.root = Path(directory).resolve()
        self.layer_remote = self._bare('layer.git')
        self.core_remote = self._bare('core.git')
        self._layer_work = self._work('layer-work', self.layer_remote)
        self._core_work = self._work('core-work', self.core_remote)
        self.core_commits = [self.core_commit('core C0'), self.core_commit('core C1')]
        self.first = self.publish('start', pointer('1.0', self.core_commits[0]), release='1.0')
        self.layer_tree = self._clone(self.layer_remote, 'layer', 'start')
        self.core_tree = self._clone(self.core_remote, 'core', SERIES)
        self.git(self.core_tree, 'reset', '--hard', '--quiet', self.core_commits[0])
        self.reachable = self._configuration('gitconfig', self.layer_remote, self.core_remote)
        self.unreachable = self._configuration('gitconfig-unreachable', self.root / 'none.git', self.root / 'none.git')

    @staticmethod
    def git(directory, *arguments):
        return subprocess.run(
            ['git', '-C', str(directory), '-c', 'user.name=Box suite', '-c', 'user.email=box-suite@localhost',
             '-c', 'init.defaultBranch=start', *arguments],
            capture_output=True, check=True, text=True, timeout=120).stdout.strip()

    def _bare(self, name):
        path = self.root / name
        subprocess.run(['git', 'init', '--quiet', '--bare', str(path)], check=True, capture_output=True)
        return path

    def _work(self, name, remote):
        path = self.root / name
        subprocess.run(['git', 'init', '--quiet', str(path)], check=True, capture_output=True)
        self.git(path, 'remote', 'add', 'origin', str(remote))
        return path

    def _clone(self, remote, name, branch):
        path = self.root / name
        subprocess.run(['git', 'clone', '--quiet', '--branch', branch, str(remote), str(path)],
                       check=True, capture_output=True)
        return path

    def _configuration(self, name, layer, core):
        path = self.root / name
        path.write_text(
            f'[url "{layer}"]\n\tinsteadOf = {LAYER_REPOSITORY}\n'
            f'[url "{core}"]\n\tinsteadOf = {CORE_REPOSITORY}\n')
        return str(path)

    def core_commit(self, text, branch=SERIES):
        """A new commit of core on `branch` of the core repository; its name."""
        work = self._core_work
        self.git(work, 'checkout', '--quiet', '-B', branch)
        (work / 'core.txt').write_text(f'{text}\n')
        self.git(work, 'add', '--all')
        self.git(work, 'commit', '--quiet', '-m', text)
        self.git(work, 'push', '--quiet', '--force', 'origin', f'{branch}:{branch}')
        return self.git(work, 'rev-parse', 'HEAD')

    def publish(self, branch, pointer_text, release='', apply_fails=False):
        """A new commit at the head of `branch` of the layer repository; its name.

        `pointer_text` is the whole pointer file, None for a commit without one. `release` names
        the marker its apply script leaves; `apply_fails` makes that script fail instead.
        """
        work = self._layer_work
        self.git(work, 'checkout', '--quiet', '--orphan', f'box-suite-{time.monotonic_ns()}')
        self.git(work, 'rm', '-r', '--quiet', '--cached', '--ignore-unmatch', '.')
        for entry in work.iterdir():
            if entry.name != '.git':
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        if pointer_text is not None:
            (work / 'box-release.json').write_text(pointer_text)
        script = work / 'box' / 'system' / 'apply.sh'
        script.parent.mkdir(parents=True)
        marker = f'system-files-of-{release or "a-release"}'
        script.write_text('#!/usr/bin/env bash\n' + (
            'exit 1\n' if apply_fails else f'touch "$(dirname "$0")/../../{marker}"\n'))
        (work / 'release.txt').write_text(f'{release} {time.monotonic_ns()}\n')
        self.git(work, 'add', '--all')
        self.git(work, 'commit', '--quiet', '-m', f'release {release or "?"}')
        self.git(work, 'push', '--quiet', '--force', 'origin', f'HEAD:refs/heads/{branch}')
        return self.git(work, 'rev-parse', 'HEAD')

    def delete_branch(self, branch):
        self.git(self.layer_remote, 'branch', '-D', branch)

    def name_release(self, series, commit, tag=None, annotated=False):
        """Make `commit` the head of the release channel of `series`, tagged as release `tag` when there is one."""
        self.git(self.layer_remote, 'update-ref', f'refs/heads/{release_channel(series)}', commit)
        if tag:
            self.tag(series, commit, tag, annotated)

    def tag(self, series, commit, release, annotated=False):
        """Tag `commit` as the published release `release` of `series`, moving a tag of that name that exists."""
        arguments = ['-a', '-m', f'release {release}'] if annotated else []
        self.git(self.layer_remote, 'tag', '--force', *arguments, f'box-{series}-{release}', commit)

    def release(self, series, pointer_text, release='', tag=None, annotated=False, **options):
        """A new commit at the head of the release channel of `series`, published as the release `tag`.

        `tag` defaults to `release`, False leaves the commit without a tag; the rest is `publish`.
        """
        commit = self.publish(release_channel(series), pointer_text, release=release, **options)
        if tag is not False:
            self.tag(series, commit, tag or release, annotated)
        return commit

    def heads(self):
        """(commit of the layer tree, commit of the core tree): what the box runs."""
        return self.git(self.layer_tree, 'rev-parse', 'HEAD'), self.git(self.core_tree, 'rev-parse', 'HEAD')

    def commit_of(self, branch):
        return self.git(self.layer_remote, 'rev-parse', branch)

    def system_files_of(self, release):
        """Whether the apply script of `release` ran in the layer tree."""
        return (self.layer_tree / f'system-files-of-{release}').exists()


def run_guard(*arguments, env=None):
    """Run the roll-back guard the system files install, as the system runs it around a start of the box service."""
    return subprocess.run(['python3', '-I', str(GUARD), *arguments], capture_output=True, text=True, timeout=300,
                          env=dict(os.environ, **(env or {})), check=False)


def trial_after_update(releases, previous, target):
    """The record an update leaves for the guard: the box moved its trees from `previous` to `target`.

    Both are (release, layer commit, core commit). It is the contract between the box software
    and the guard.
    """
    def pair(release, layer, core):
        return {'release': release, 'layer_commit': layer, 'odoo_commit': core}

    return {
        'phase': 'applied', 'starts': 0, 'since': '2026-10-15T00:00:00Z', 'promote': False,
        'trees': {'layer': str(releases.layer_tree), 'core': str(releases.core_tree)},
        'previous': pair(*previous),
        'target': dict(pair(*target), candidate=False, channel=f'refs/heads/{release_channel(SERIES)}', series=SERIES),
    }


# ---------------------------------------------------------------------------------------------- web server

class WebServer:
    """The web server of the box with the site the box installs, on ports of its own, run by the box user.

    The site is read from the system files of the layer as they are; only where it listens, where
    it finds its files and certificates and - for `box_service_port` - where the box service
    answers are set for the software box. `names` adds the block of one more name per entry,
    {name: `common.Certificate`}, written from the template the box writes them from.
    """

    def __init__(self, directory, box_certificate, names=None, box_service_port=None):
        self.root = Path(directory)
        self.tls_port, self.plain_port = (common.closed_port(), common.closed_port())
        while self.plain_port == self.tls_port:
            self.plain_port = common.closed_port()
        viindoo = self.root / 'viindoo'
        (viindoo / 'names').mkdir(parents=True)
        for directory_name in ('body', 'proxy', 'fastcgi', 'uwsgi', 'scgi'):
            (self.root / 'temporary' / directory_name).mkdir(parents=True)
        self._certificate('box', box_certificate)
        files = {
            '/etc/nginx/viindoo/': f'{viindoo}/',
            '/etc/ssl/certs/nginx-cert.crt': str(self.root / 'box.crt'),
            '/etc/ssl/private/nginx-cert.key': str(self.root / 'box.key'),
        }
        (self.root / 'site.conf').write_text(self._listening_here(_changed(SITE.read_text(), files)))
        rules = {'/usr/share/viindoo-iot/box-down.html': str(BOX_DOWN_PAGE)}
        if box_service_port:
            rules['proxy_pass http://127.0.0.1:8069;'] = f'proxy_pass http://127.0.0.1:{box_service_port};'
        (viindoo / 'box.conf').write_text(_changed(RULES.read_text(), rules))
        for name, certificate in (names or {}).items():
            self._certificate(name, certificate)
            block = self._listening_here(_changed(NAME_TEMPLATE.read_text(), {
                '@NAME@': name,
                '/etc/ssl/private/viindoo-name/current/fullchain.pem': str(self.root / f'{name}.crt'),
                '/etc/ssl/private/viindoo-name/current/privkey.pem': str(self.root / f'{name}.key'),
                '/etc/nginx/viindoo/': f'{viindoo}/',
            }))
            (viindoo / 'names' / f'{name}.conf').write_text(block)
        temporary = self.root / 'temporary'
        (self.root / 'nginx.conf').write_text(
            f'pid {self.root}/nginx.pid;\nerror_log {self.root}/error.log;\nevents {{}}\nhttp {{\n'
            f'    access_log off;\n    client_body_temp_path {temporary}/body;\n'
            f'    proxy_temp_path {temporary}/proxy;\n    fastcgi_temp_path {temporary}/fastcgi;\n'
            f'    uwsgi_temp_path {temporary}/uwsgi;\n    scgi_temp_path {temporary}/scgi;\n'
            f'    include {self.root}/site.conf;\n}}\n')
        self._process = subprocess.Popen(
            ['nginx', '-p', str(self.root), '-c', str(self.root / 'nginx.conf'), '-e', str(self.root / 'error.log'),
             '-g', 'daemon off;'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            common.wait_until(lambda: self._listening() or self._process.poll() is not None, 'the web server', 10)
        except AssertionError:
            self.close()
            raise
        if self._process.poll() is not None:
            raise RuntimeError(f'The web server refuses the site of the box: {self._process.stderr.read().decode()}')

    def _listening_here(self, text):
        """`text` listening on the two ports of this web server, on this machine only, each block as it says."""
        ports = {'443': self.tls_port, '80': self.plain_port}
        listening = re.compile(r'^(\s*)listen (\[::\]:)?(443|80)\b([^;]*);', re.MULTILINE)
        if not listening.search(text):
            raise RuntimeError('The site of the box no longer listens on 443 or 80: the suite reads it to run it')
        return listening.sub(
            lambda found: '' if found[2] else f'{found[1]}listen 127.0.0.1:{ports[found[3]]}{found[4]};', text)

    def _certificate(self, name, certificate):
        (self.root / f'{name}.crt').write_text(certificate.pem)
        (self.root / f'{name}.key').write_text(certificate.key_pem)

    def _listening(self):
        try:
            with socket.create_connection(('127.0.0.1', self.tls_port), timeout=1):
                return True
        except OSError:
            return False

    def close(self):
        if self._process.poll() is None:
            self._process.terminate()
            self._process.wait(10)

    def presented(self, name=None):
        """Fingerprint of the certificate presented to a browser that asks by `name`, or by address when None."""
        with self._secured(name) as secured:
            return common.fingerprint(common.x509.load_der_x509_certificate(secured.getpeercert(binary_form=True)))

    @contextlib.contextmanager
    def _secured(self, name):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with socket.create_connection(('127.0.0.1', self.tls_port), timeout=10) as plain:
            with context.wrap_socket(plain, server_hostname=name) as secured:
                yield secured

    def ask(self, method, path, headers=None, name=None, tls=True):
        """(status, {header name in lower case: value}, body) of a request asked by `name`, or by address."""
        host = name or '127.0.0.1'
        lines = [f'{method} {path} HTTP/1.1', f'Host: {host}', 'Connection: close', 'Content-Length: 0',
                 *(f'{key}: {value}' for key, value in (headers or {}).items())]
        request = ('\r\n'.join(lines) + '\r\n\r\n').encode()
        if tls:
            with self._secured(name) as secured:
                secured.sendall(request)
                answer = _read_all(secured)
        else:
            with socket.create_connection(('127.0.0.1', self.plain_port), timeout=10) as plain:
                plain.sendall(request)
                answer = _read_all(plain)
        head, _separator, body = answer.partition(b'\r\n\r\n')
        status_line, *header_lines = head.decode('latin-1').split('\r\n')
        received = {}
        for line in header_lines:
            key, _colon, value = line.partition(':')
            received[key.strip().lower()] = value.strip()
        return int(status_line.split()[1]), received, body


def _read_all(connection):
    chunks = []
    with contextlib.suppress(OSError):
        while chunk := connection.recv(65536):
            chunks.append(chunk)
    return b''.join(chunks)


def _changed(text, changes):
    """`text` with each key of `changes` replaced by its value; a key the text does not hold is an error."""
    for former, later in changes.items():
        if former not in text:
            raise RuntimeError(f'The site of the box no longer holds {former!r}: the suite reads it to run it')
        text = text.replace(former, later)
    return text


# ---------------------------------------------------------------------------------------------- children

def with_layer_files(files):
    """Write a configuration of the box whose layer also holds `files`, {path in the layer: content}."""
    return common.configuration_file(tempfile.mkdtemp(prefix='box-suite-'), layer_files=files)


class SilentServer:
    """A server that takes a connection and the request on it, and never answers; it lets go after `holds` seconds."""

    def __init__(self, holds=20):
        self._listener = socket.socket()
        self._listener.bind(('127.0.0.1', 0))
        self._listener.listen()
        self.port = self._listener.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}'
        self._holds = holds
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
            with contextlib.suppress(OSError):
                deadline = time.monotonic() + self._holds
                while time.monotonic() < deadline and connection.recv(65536):
                    pass

    def close(self):
        self._listener.close()


class ServerOfASeries(common.FakeServer):
    """A server that says which series it runs, as the version route of a server does."""

    def __init__(self, series, **options):
        self.series = series
        super().__init__(**options)

    def answer_to(self, method, path, body):
        if method == 'POST' and path == '/web/webclient/version_info':
            return 200, common.rpc_result({'server_serie': self.series, 'server_version': self.series})
        return super().answer_to(method, path, body)


@contextlib.contextmanager
def clock_commands():
    """List, for the block, the times (seconds since 1970) the box asks the system to set its clock to.

    The software box cannot set its clock: the command is not run and answers that it succeeded.
    """
    asked = []
    run = subprocess.run

    def system(command, *arguments, **options):
        if isinstance(command, (list, tuple)) and 'date' in [str(part) for part in command]:
            asked.append(int(str(command[-1]).lstrip('@')))
            return subprocess.CompletedProcess(command, 0, b'', b'')
        return run(command, *arguments, **options)

    with mock.patch.object(subprocess, 'run', system):
        yield asked


def timestamp(moment):
    """Seconds since 1970 of a naive UTC time."""
    return moment.replace(tzinfo=datetime.timezone.utc).timestamp()


class _GitCalls(list):

    def __init__(self):
        super().__init__()
        self.environments = []


@contextlib.contextmanager
def git_commands():
    """List, for the block, the arguments of each git command the box runs, by whichever function it runs it.

    None runs: each answers as a git that found nothing, so that a look for a release reaches no
    repository. `environments` holds the environment each was given, None when it was given none.
    """
    commands = _GitCalls()

    def replacing(name, original):
        def call(command, *arguments, **options):
            if not (isinstance(command, (list, tuple)) and command and Path(str(command[0])).name == 'git'):
                return original(command, *arguments, **options)
            commands.append([str(part) for part in command[1:]])
            commands.environments.append(options.get('env'))
            empty = '' if options.get('text') or options.get('universal_newlines') else b''
            if name == 'run':
                return subprocess.CompletedProcess(command, 0, empty, empty)
            return empty if name == 'check_output' else 0

        return call

    with contextlib.ExitStack() as stack:
        for name in ('run', 'check_call', 'check_output'):
            stack.enter_context(mock.patch.object(subprocess, name, replacing(name, getattr(subprocess, name))))
        yield commands
