"""What the test files of the box suite share.

The suite runs the box software of core with the Viindoo layer on top of it, both unchanged, in
the file layout of a box: the software box (box/dev/softbox). A test file imports this module by
its plain name and finds here:

* `BoxCase` - a box started once in the process of the tests and driven through its routes;
* `in_new_process` with `start_report`, and `seam_check` - a box started, or checked, in a process
  of its own, for what only a start can show: the load order, a core that moved, the reset file;
* `FakeServer` - a server, as far as a box can tell;
* `make_certificate` - key material, made when a test asks for it and never stored.

The suite changes the machine it runs on - its shell password, its server, its certificate - so
it runs in the software box and nowhere else: `require_software_box` refuses every other machine,
a real box included.

In a box started by `start`, three effects of core are replaced and nothing else: a restart of the
box service is listed in `restarts` instead of ending the process, the remounts of the card are
left out (the software box has no read-only card), and the error stream, which core takes for its
log, goes back to the runner. The log of the box goes to its log file, as on a box. Of the layer,
the client of the Viindoo name is stopped once the box has started: a test of the name drives it.
"""
import collections
import configparser
import contextlib
import datetime
import http.client
import http.server
import importlib
import ipaddress
import json
import logging
import os
import pwd
import re
import runpy
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
import unittest
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, load_pem_private_key)
from cryptography.x509.oid import NameOID
from werkzeug.serving import make_server
from werkzeug.test import Client

CONFIGURATION = Path('/home/pi/odoo.conf')
LOG_FILE = Path('/var/log/odoo/odoo-server.log')
WEB_CERTIFICATE = Path('/etc/ssl/certs/nginx-cert.crt')
WEB_KEY = Path('/etc/ssl/private/nginx-cert.key')
BOARD_SERIAL = Path('/sys/firmware/devicetree/base/serial-number')
# The file the software box leaves in its container once it has laid out the files of a box: no box has it.
SOFTWARE_BOX = Path('/var/lib/softbox/laid-out')
BOOT_PARTITION = Path('/boot')
BOX_USER = 'pi'
TESTS = Path(__file__).resolve().parent
LAYER_MODULE = TESTS.parent
BOOT_FILE = LAYER_MODULE / 'boot.py'

CORE = 'odoo.addons.hw_drivers'
LAYER = 'odoo.addons.viin_hw_drivers'
CORE_MODULES = ('hw_drivers', 'hw_escpos', 'hw_posbox_homepage')
# The module of core whose pages the page of the box is built on; a copy of it links to its files but the Python ones.
WEB_MODULE = 'web'
LAYER_MODULES = ('viin_hw_drivers', 'viin_hw_homepage')
SECTION = 'iot.box'
TRUSTED_CERTIFICATES_KEY = 'viin_server_pins'
# The common name of the certificate every image of core ships, whose private key is public.
SHIPPED_COMMON_NAME = 'OdooTempIoTBoxCertificate'
PROCESS_TIMEOUT = 180
# How long the suite lets the web server of the box take to read its files after a reload before it fails a test.
WEB_SERVER_READ_TIMEOUT = 30
# A route of core that changes nothing and that the layer leaves to core: it answers on any started box.
PING = '/hw_posbox_homepage/ping'
# Where Viindoo publishes the box software: the one host, beside its own server, that the description of the
# module lets the box software contact.
SOFTWARE_PUBLISHER = 'github.com'
# The web site of someone else, as a browser names it to the box when a page of that site calls the box.
ANOTHER_WEB_SITE = 'https://another-site.example'

# The delay of each restart of the box service that was asked for in this process.
restarts = []
_started = []
_serving = []
# Why the last call of `through_web_server` came back without an answer.
_no_answer = {'reason': ''}
# The name under which a file of the layer is loaded on its own in a process that started no box.
_LAYER_ALONE = 'box_suite_layer'


class NotTheSoftwareBox(RuntimeError):
    """The machine is not the software box: nothing of the suite may run on it."""


def require_software_box():
    """Raise NotTheSoftwareBox unless this machine is the software box.

    A real box has the configuration, the board and the user of a box too: only the file the
    software box leaves in its own container tells the two apart.
    """
    laid_out_as_a_box = (
        CONFIGURATION.is_file() and BOARD_SERIAL.is_file() and pwd.getpwuid(os.getuid()).pw_name == BOX_USER)
    if not (laid_out_as_a_box and SOFTWARE_BOX.is_file()):
        raise NotTheSoftwareBox('The box suite runs in the software box only: see box/dev/softbox/README.md')


def core(name):
    """A module of the box software of core, once a box is started in this process."""
    return sys.modules[f'{CORE}.{name}']


def layer(name):
    """A file of the layer, once a box is started in this process."""
    return sys.modules[f'{LAYER}.{name}']


def utc_now():
    """Now, as the naive UTC time certificates carry their dates in."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def wait_until(condition, what, timeout=30):
    """Return what `condition()` gives as soon as it is true; fail the test after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f'Waited {timeout} seconds for {what}')
        time.sleep(0.05)


def board_serial():
    return BOARD_SERIAL.read_text().strip('\x00\n')


def as_root(*command, data=None):
    """Run a command as root, the way the box software does for what the box user may not touch."""
    return subprocess.run(['sudo', '-n', *command], input=data, capture_output=True, check=True, timeout=60)


def keep_from_loading(*files):
    """Make files of the layer fail to load in the box this process starts; call it before `start`.

    A file is named by its place in the package of the layer, a dot for each directory and no `.py`.
    The box then runs without what the file provides, as a box built without it does; naming a file
    the layer does not have changes nothing.
    """
    for file in files:
        sys.modules[f'{LAYER}.{file}'] = None


def keep_all_but(*files):
    """Make every file the layer loads at its start, but `files`, fail to load in the box this process starts.

    Call it before `start`; the files are named as `keep_from_loading` names them. The box then
    runs as one built with `files` alone, whatever other files the layer holds. The file that
    starts the layer is always loaded.
    """
    found = [*LAYER_MODULE.glob('*.py'), *LAYER_MODULE.glob('seams/*.py'), *LAYER_MODULE.glob('controllers/*.py')]
    names = {'.'.join(path.relative_to(LAYER_MODULE).with_suffix('').parts) for path in found
             if path.name != '__init__.py'}
    keep_from_loading(*sorted(names - set(files) - {'boot'}))


def start(configuration=CONFIGURATION):
    """Start the box in this process as its service does; return once core has finished its own start.

    A process holds one box: only the first call does anything.
    """
    require_software_box()
    if _started:
        return
    import odoo

    error_stream = sys.stderr
    odoo.tools.config.parse_config(
        ['--config', str(configuration), '--logfile', str(LOG_FILE)], setup_logging=True)
    odoo.service.server.load_server_wide_modules()
    sys.stderr = error_stream
    _replace_effects(core('tools.helpers'))
    _wait_for_core()
    _hold_the_name_client()
    _started.append(str(configuration))


def _hold_the_name_client():
    """Stop the client of the Viindoo name, which asks the server of the box on a clock of its own.

    Two minutes after a start it would question whatever server a test set up by then: a test
    that counts what its server received would count a question it did not cause. A test of the
    name drives the client itself.
    """
    public_name = sys.modules.get(f'{LAYER}.public_name')
    if public_name:
        public_name.KEEPER.stop()
        if public_name.KEEPER.is_alive():
            public_name.KEEPER.join(10)


def _replace_effects(helpers):
    def odoo_restart(delay=0):
        restarts.append(delay)

    @contextlib.contextmanager
    def writable():
        with helpers.lock:
            yield

    helpers.odoo_restart = odoo_restart
    helpers.writable = writable


def _wait_for_core():
    """Core starts on a thread of its own: web server, device handlers, then the nightly actions it schedules.

    Only the actions of core tell that it got there: a file of the layer may schedule one of its own earlier.
    """
    import schedule

    wait_until(
        lambda: [job for job in schedule.jobs
                 if (getattr(job.job_func, '__module__', None) or '').startswith(f'{CORE}.')],
        'core to finish its start')


def serve():
    """Serve the box started in this process on the port its web server forwards to; once per process."""
    if _serving:
        return
    import odoo

    server = make_server('127.0.0.1', odoo.tools.config['http_port'], odoo.http.root, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _serving.append(server)


def through_web_server(path, proof=None, sent=None, **parameters):
    """Call a JSON route through the web server of the box: its result, None when no answer came back.

    `sent` is what the browser of the caller adds to the request of its own, as header names and values.
    `why_no_answer` then tells why the answer is None.
    """
    headers = {'Content-Type': 'application/json', **(sent or {})}
    if proof:
        headers[layer('lock').PROOF_HEADER] = proof
    _no_answer['reason'] = ''
    connection = http.client.HTTPSConnection('127.0.0.1', 443, timeout=60, context=_unverified_context())
    try:
        connection.request('POST', path, body=json.dumps(_rpc_call(parameters)), headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            _no_answer['reason'] = f'the web server answered {response.status} {response.reason}'
            return None
        answer = json.loads(response.read())
        if 'result' not in answer:
            _no_answer['reason'] = f'the box answered an error: {_error_of(answer.get("error"))}'
        return answer.get('result')
    except (OSError, http.client.HTTPException, ValueError) as error:
        _no_answer['reason'] = f'no answer came back: {type(error).__name__}: {error}'
        return None
    finally:
        connection.close()


def why_no_answer():
    """Why the last call of `through_web_server` gave None: the error of the call, or what the box answered instead
    of a result; empty when it gave a result."""
    return _no_answer['reason']


def _error_of(error):
    error = error if isinstance(error, dict) else {}
    data = error.get('data') if isinstance(error.get('data'), dict) else {}
    return ' - '.join(str(part) for part in (error.get('message'), data.get('name'), data.get('message')) if part)


def _rpc_call(parameters):
    return {'jsonrpc': '2.0', 'method': 'call', 'params': parameters}


def call_route(path, **parameters):
    """The result of a JSON route of the box started in this process, for a caller that is no `BoxCase`."""
    import odoo

    return Client(odoo.http.root).post(path, json=_rpc_call(parameters)).get_json()['result']


def rpc_result(result):
    """The body by which a JSON route of a server answers `result`."""
    return json.dumps({'jsonrpc': '2.0', 'id': None, 'result': result}).encode()


@contextlib.contextmanager
def box_log(level=logging.DEBUG):
    """Give a function that returns what the box wrote to its log during the block.

    Every logger the box configured writes at `level` meanwhile, as on a box set to that level.
    """
    loggers = [logging.getLogger(), *(
        logger for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger) and logger.level)]
    levels = [logger.level for logger in loggers]
    beginning = LOG_FILE.stat().st_size
    for logger in loggers:
        logger.setLevel(level)
    try:
        yield lambda: LOG_FILE.read_bytes()[beginning:].decode(errors='replace')
    finally:
        for logger, former in zip(loggers, levels):
            logger.setLevel(former)


@contextlib.contextmanager
def _answering_commands(answer):
    """Ask `answer` before each command the box runs with `subprocess.run` or `subprocess.check_call`.

    `answer(command)` gets the command as a list of texts. None lets the command run; a number
    is the exit code it ends with instead, without running.
    """
    originals = {name: getattr(subprocess, name) for name in ('run', 'check_call')}

    def replacement(name, original):
        def run(command, *arguments, **options):
            code = answer([str(part) for part in command]) if isinstance(command, (list, tuple)) else None
            if code is None:
                return original(command, *arguments, **options)
            if code and (name == 'check_call' or options.get('check')):
                raise subprocess.CalledProcessError(code, command)
            return subprocess.CompletedProcess(command, code, b'', b'') if name == 'run' else code

        return run

    for name, original in originals.items():
        setattr(subprocess, name, replacement(name, original))
    try:
        yield
    finally:
        for name, original in originals.items():
            setattr(subprocess, name, original)


@contextlib.contextmanager
def without_running(*scripts):
    """List the calls core makes to the system scripts and programs named, instead of running them.

    Each call is the list of its arguments; every other command runs.
    """
    calls = []

    def answer(command):
        if Path(command[0]).name not in scripts:
            return None
        calls.append(command[1:])
        return 0

    with _answering_commands(answer):
        yield calls


@contextlib.contextmanager
def without_starting(*programs):
    """List the starts of the programs named, which the box starts and leaves running, instead of starting them.

    Each start is the list of its arguments; every other program starts.
    """
    starts = []
    start = subprocess.Popen

    def listed(command, *arguments, **options):
        if isinstance(command, (list, tuple)) and command and Path(str(command[0])).name in programs:
            starts.append([str(part) for part in command[1:]])
            return None
        return start(command, *arguments, **options)

    with mock.patch.object(subprocess, 'Popen', listed):
        yield starts


@contextlib.contextmanager
def failing(program):
    """Make every command that names the system program `program` fail, when the thread of the block runs it.

    The commands of the other threads of the box run.
    """
    thread = threading.current_thread()

    def answer(command):
        named = program in [Path(part).name for part in command]
        return 1 if named and threading.current_thread() is thread else None

    with _answering_commands(answer):
        yield


@contextlib.contextmanager
def web_server_restarted_as_on_a_box():
    """Have each restart of the web server asked for during the block stop it, then start it, as the service manager
    of a box does: only the start reads its files, and a web server that cannot use them stays stopped.

    The software box restarts it with the script of the web server instead, which reads the files first and leaves
    a web server it could not start again running.
    """
    def answer(command):
        if command[-3:] != ['service', 'nginx', 'restart']:
            return None
        subprocess.run([*command[:-1], 'stop'], capture_output=True, check=False)
        return subprocess.run([*command[:-1], 'start'], capture_output=True, check=False).returncode

    with _answering_commands(answer):
        yield


class PowerCut(BaseException):
    """Stands for the power of the box going off: nothing of what the box was doing runs after it."""


@contextlib.contextmanager
def root_commands():
    """List, for the block, every command any thread of the box runs as root; each is the list of its words."""
    ran = []

    def answer(command):
        if command and command[0] == 'sudo':
            ran.append(command)
        return None

    with _answering_commands(answer):
        yield ran


@contextlib.contextmanager
def power_cut_after(commands):
    """Cut the power once the thread of the block has run `commands` commands as root.

    The command that would come next is not run and ends in PowerCut; neither is any command
    as root after it, whoever caught the cut. Gives a function that tells whether the power
    was cut.
    """
    thread = threading.current_thread()
    ran = []

    def answer(command):
        if command[0] != 'sudo' or threading.current_thread() is not thread:
            return None
        ran.append(command)
        if len(ran) > commands:
            raise PowerCut
        return None

    with _answering_commands(answer):
        yield lambda: len(ran) > commands


def _read_configuration():
    # The values are read as the file holds them: one that holds a percent sign is a value like any other here.
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(CONFIGURATION)
    return parser


def box_configuration():
    """What the box keeps about itself and its server: the section [iot.box] of its configuration file."""
    parser = _read_configuration()
    return dict(parser[SECTION]) if parser.has_section(SECTION) else {}


def store_box_configuration(**values):
    """Arrange what a box stored in an earlier life; an empty value removes its key."""
    parser = _read_configuration()
    if not parser.has_section(SECTION):
        parser.add_section(SECTION)
    for key, value in values.items():
        if value:
            parser.set(SECTION, key, value)
        else:
            parser.remove_option(SECTION, key)
    with CONFIGURATION.open('w', encoding='utf-8') as file:
        parser.write(file)
    forget_cached_configuration()


def forget_cached_configuration():
    """Stand for the restart after which core reads its configuration again: it reads it once per process."""
    helpers = sys.modules.get(f'{CORE}.tools.helpers')
    if helpers:
        helpers.get_conf.cache_clear()
        helpers.get_odoo_server_url.cache_clear()


def _pem_certificates(data):
    blocks = re.findall(rb'-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----', data, re.DOTALL)
    return [x509.load_pem_x509_certificate(block) for block in blocks]


def trusted_certificates():
    """Fingerprints of the certificates the installer trusted for the server of the box, as the box stores them."""
    path = box_configuration().get(TRUSTED_CERTIFICATES_KEY)
    if not path or not Path(path).is_file():
        return []
    return [fingerprint(certificate) for certificate in _pem_certificates(Path(path).read_bytes())]


def kept():
    """All a box keeps about its server; equal before and after an attempt means the attempt stored nothing."""
    return {'configuration': box_configuration(), 'trusted_certificates': trusted_certificates()}


def save_state():
    """What `restore_state` needs to give the box back what it keeps about its server."""
    trusted = box_configuration().get(TRUSTED_CERTIFICATES_KEY)
    return {
        'configuration': CONFIGURATION.read_bytes(),
        'trusted': (trusted, Path(trusted).read_bytes()) if trusted and Path(trusted).is_file() else None,
    }


def restore_state(state):
    trusted = box_configuration().get(TRUSTED_CERTIFICATES_KEY)
    if trusted:
        Path(trusted).unlink(missing_ok=True)
    CONFIGURATION.write_bytes(state['configuration'])
    if state['trusted']:
        Path(state['trusted'][0]).write_bytes(state['trusted'][1])
    forget_cached_configuration()


def fingerprint(certificate):
    """SHA-256 of a certificate: lower-case hexadecimal digits, no separator."""
    return certificate.fingerprint(hashes.SHA256()).hex()


def hex_digits(text):
    """The hexadecimal digits of a fingerprint in any spelling, to compare it with `fingerprint`."""
    return re.sub(r'[^0-9a-f]', '', str(text or '').lower())


class Certificate:
    """A certificate with its private key: the objects, both PEM texts, and `fingerprint`."""

    def __init__(self, certificate, key):
        self.certificate = certificate
        self.key = key
        self.pem = certificate.public_bytes(Encoding.PEM).decode()
        self.key_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
        self.fingerprint = fingerprint(certificate)


def certificate_from(pem, key_pem):
    """The `Certificate` of the PEM texts of a certificate and of its private key."""
    return Certificate(
        x509.load_pem_x509_certificate(pem.encode()), load_pem_private_key(key_pem.encode(), password=None))


def make_certificate(common_name, names=None, valid_from=None, valid_until=None, issuer=None, authority=False,
                     key=None):
    """Make a certificate and its key, valid from yesterday for 30 days unless dates (naive UTC) say otherwise.

    `names` are the host names and addresses it is issued for (the common name when left out). It
    is self-signed unless `issuer`, a `Certificate` made with `authority`, signs it. `key` is the
    private key to make it for, a new one when left out. Without a `common_name` its subject
    names an organisation only.
    """
    key = key or ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name) if common_name
        else x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'Box suite')])
    alternative = []
    for name in [common_name] if names is None else names:
        try:
            alternative.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            alternative.append(x509.DNSName(name))
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer.certificate.subject if issuer else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(valid_from or utc_now() - datetime.timedelta(days=1))
        .not_valid_after(valid_until or utc_now() + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=authority, path_length=None), critical=True)
    )
    if alternative:
        builder = builder.add_extension(x509.SubjectAlternativeName(alternative), critical=False)
    return Certificate(builder.sign(issuer.key if issuer else key, hashes.SHA256()), key)


def _unverified_context():
    """Accepts any certificate: to read which one the web server of the box presents, and to call the box
    under test on this machine, whose certificate is its own. Nothing secret goes over it."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def web_certificate():
    """The certificate installed for the web server of the box."""
    return x509.load_pem_x509_certificate(WEB_CERTIFICATE.read_bytes())


def install_web_certificate(certificate):
    """Put a `Certificate` where the web server reads it, as an image or an earlier start left it there."""
    _write_web_certificate(certificate.pem.encode(), certificate.key_pem.encode())


def _write_web_certificate(certificate_pem, key_pem):
    wait_for_the_web_server_to_read_its_files()
    as_root('install', '-m', '600', '/dev/stdin', str(WEB_KEY), data=key_pem)
    as_root('install', '-m', '644', '/dev/stdin', str(WEB_CERTIFICATE), data=certificate_pem)


def served_certificate():
    """Fingerprint of the certificate the web server of the box presents; None when it does not answer."""
    try:
        with socket.create_connection(('127.0.0.1', 443), timeout=5) as plain:
            with _unverified_context().wrap_socket(plain) as secured:
                return fingerprint(x509.load_der_x509_certificate(secured.getpeercert(binary_form=True)))
    except OSError:
        return None


def serve_web_certificate():
    """Have the web server present the installed certificate, whether it runs or not; return once it does."""
    installed = fingerprint(web_certificate())
    # A restart the box asked for may be under way: the web server then refuses both commands for a moment.
    for _attempt in range(10):
        if subprocess.run(['sudo', '-n', 'nginx', '-s', 'reload'], capture_output=True, check=False).returncode:
            subprocess.run(['sudo', '-n', 'service', 'nginx', 'restart'], capture_output=True, check=False)
        with contextlib.suppress(AssertionError):
            wait_until(lambda: served_certificate() == installed, 'the web server', timeout=3)
            # A worker may present the installed certificate before the web server has read all its files again; the
            # box itself changes them later without waiting for this reload.
            wait_for_the_web_server_to_read_its_files()
            return
    raise AssertionError('The web server does not present the certificate installed for it')


def wait_for_the_web_server_to_read_its_files():
    """Return once the web server of the box has read its files for every reload it was asked for.

    A reload command returns before the web server reads the files: one written in between may be read beside the
    other file of an earlier pair, and the web server then fails every connection. Call it before writing a file the
    web server reads. Fails the test when the web server takes more than WEB_SERVER_READ_TIMEOUT seconds.
    """
    if not _layer_file('trust').wait_until_web_server_reloaded(timeout=WEB_SERVER_READ_TIMEOUT):
        raise AssertionError(
            f'Waited {WEB_SERVER_READ_TIMEOUT} seconds for the web server of the box to read its files after a reload')


def _layer_file(name):
    """A file of the layer: the one the box started in this process loaded, or else the file loaded on its own,
    without the start of the layer, which only a box may run."""
    started = sys.modules.get(f'{LAYER}.{name}')
    if started:
        return started
    if _LAYER_ALONE not in sys.modules:
        package = types.ModuleType(_LAYER_ALONE)
        package.__path__ = [str(LAYER_MODULE)]
        sys.modules[_LAYER_ALONE] = package
    return importlib.import_module(f'{_LAYER_ALONE}.{name}')


@contextlib.contextmanager
def public_authority():
    """Give a `Certificate` of an authority that, during the block, counts among those every browser trusts.

    A certificate made with it as `issuer` is then one "issued by a public authority" for the
    box started in this process.
    """
    authority = make_certificate('Box suite authority', names=(), authority=True)
    trust = layer('trust')
    with tempfile.TemporaryDirectory(prefix='box-suite-') as directory:
        authorities = Path(directory, 'authorities.pem')
        authorities.write_bytes(Path(trust.PUBLIC_AUTHORITIES).read_bytes() + b'\n' + authority.pem.encode())
        with mock.patch.object(trust, 'PUBLIC_AUTHORITIES', str(authorities)):
            yield authority


def make_public_from_the_start(authority_pem):
    """Count an authority among those every browser trusts on the box this process starts; call it before `start`.

    `authority_pem` is the PEM text of its certificate.
    """
    import certifi

    authorities = Path(tempfile.mkdtemp(prefix='box-suite-'), 'authorities.pem')
    authorities.write_bytes(Path(certifi.where()).read_bytes() + b'\n' + authority_pem.encode())
    certifi.where = lambda: str(authorities)


def give_a_hardware_address():
    """Give the machine a network card, as far as the box this process starts can tell; call it before `start`.

    The software box has none, and core asks its server for driver files only once it knows the
    hardware address of the box.
    """
    import netifaces

    read = netifaces.ifaddresses

    def ifaddresses(interface):
        found = dict(read(interface))
        if netifaces.AF_INET in found:
            found[netifaces.AF_LINK] = [{'addr': '02:00:5e:00:53:01'}]
        return found

    netifaces.ifaddresses = ifaddresses


@contextlib.contextmanager
def named(name):
    """Give this machine the host name `name` during the block, as far as the box started in this process can tell."""
    forget = core('tools.helpers').get_hostname.cache_clear
    with mock.patch.object(socket, 'gethostname', return_value=name):
        forget()
        try:
            yield
        finally:
            forget()


def other_host():
    """A name of this machine that no `FakeServer` goes by unless it is told to: to a box, another host."""
    return socket.gethostname()


def save_web_certificate():
    """What `restore_web_certificate` needs to put the certificate and the key of the web server back."""
    return WEB_CERTIFICATE.read_bytes(), as_root('cat', str(WEB_KEY)).stdout


def restore_web_certificate(saved):
    _write_web_certificate(*saved)
    serve_web_certificate()


def tls_with(certificate):
    """What a server needs to speak https with `certificate`, a `Certificate`."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with tempfile.TemporaryDirectory(prefix='box-suite-') as directory:
        pair = Path(directory, 'pair.pem')
        pair.write_text(certificate.key_pem + certificate.pem)
        context.load_cert_chain(pair)
    return context


class FakeServer:
    """A server, as far as a box can tell.

    It answers a pairing request (`POST /iot/pair`) with `answer` as its result - or with `reply`,
    a (status, body) pair, as it stands - accepts the reports and the log of a box, has no driver
    file to give it, and answers "not found" to the rest; a subclass answers more in `answer_to`.
    `requests` lists what it received: method, path and body, the body read as JSON when it is.
    With a `certificate` it speaks https, under the name `localhost` unless `host` gives another
    name of this machine. `port` is the port it listens on, a free one when left out. `channel`
    is the name of the live channel it gives a box that reports, none when left out. `clock` is
    the time it states in its answers when its clock is not the one of the box.
    """

    def __init__(self, answer=None, reply=None, certificate=None, clock=None, host=None, port=0, channel=''):
        self.answer = answer
        self.reply = reply
        self.clock = clock
        self.channel = channel
        self.requests = []
        self._secured = tls_with(certificate) if certificate else None
        scheme = 'https' if certificate else 'http'
        self.host = host or ('localhost' if certificate else '127.0.0.1')
        self._server = self._listener()((socket.gethostbyname(self.host), port), self._handler())
        self.port = self._server.server_address[1]
        self.url = f'{scheme}://{self.host}:{self.port}'
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        self._thread.start()

    @property
    def pairing_requests(self):
        """The parameters of each pairing request received."""
        return [request['body']['params'] for request in self.requests if request['path'] == '/iot/pair']

    @property
    def calls(self):
        """What it received, each as 'METHOD path'."""
        return [f"{request['method']} {request['path']}" for request in self.requests]

    def link(self, code):
        """The pairing link this server shows for `code`."""
        return f'{self.url}/#pair={code}'

    def close(self):
        """Stop listening; closing a closed server changes nothing."""
        if self._thread.is_alive():
            self._server.shutdown()
            self._server.server_close()
            self._thread.join()

    def tls_context(self):
        """What secures the connection that is being opened; None for a server that speaks http."""
        return self._secured

    def answer_to(self, method, path, body):
        """What this server answers to a request: (status, body), or (status, body, headers) with headers
        as (name, value) pairs. `body` is what `requests` keeps of the request."""
        if method != 'POST':
            return 404, b''
        if path == '/iot/pair':
            return self.reply or (200, rpc_result(self.answer))
        if path == '/iot/setup':
            # What a box gets for a report: the name of its live channel.
            return 200, rpc_result(self.channel)
        if path in ('/iot/log', '/iot/printer/status'):
            return 200, rpc_result(True)
        if path == '/iot/get_handlers':
            return 200, b''
        return 404, b''

    def _receive(self, method, path, body):
        try:
            body = json.loads(body)
        except ValueError:
            body = body.decode(errors='replace')
        self.requests.append({'method': method, 'path': path, 'body': body})
        return self.answer_to(method, path, body)

    @staticmethod
    def _listener():
        class Listener(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                """A caller that turns the server down, or leaves, is what several tests are about: say nothing."""

        return Listener

    def _handler(self):
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            # A caller that connects and says nothing is dropped; it never holds the server back.
            timeout = 10

            def setup(self):
                secured = server.tls_context()
                if secured:
                    self.request.settimeout(self.timeout)
                    self.request = secured.wrap_socket(self.request, server_side=True)
                super().setup()

            def log_message(self, *arguments):
                pass

            def do_HEAD(self):  # noqa: N802 - the name the standard library calls
                self._answer(200, b'')

            def do_GET(self):  # noqa: N802
                self._answer(*server._receive('GET', self.path, b''))

            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                self._answer(*server._receive('POST', self.path, body))

            def _answer(self, status, body, headers=()):
                stated = (server.clock or utc_now()).replace(tzinfo=datetime.timezone.utc)
                self.send_response_only(status)
                self.send_header('Date', format_datetime(stated, usegmt=True))
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(body)

        return Handler


def reported_credentials(server):
    """The credentials in the reports of a box that `server` received: none when it received no report."""
    return {request['body']['params']['iot_box']['token']
            for request in server.requests if request['path'] == '/iot/setup'}


def closed_port():
    """A port of this machine on which nothing listens."""
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


class ProcessFailed(AssertionError):
    """A process started for a test ended without its result: a box that stops fails the test."""


def in_new_process(function, **arguments):
    """Run `function(**arguments)` in a Python process of its own and return what it returns.

    `function` is a module-level function of this file or of a test file; its arguments and its
    result travel as JSON. The process ends as soon as the function returns, whatever it started.
    """
    require_software_box()
    # A command the box ran - the compiler behind a library lookup of core, every few seconds - may outlive
    # it by a moment and still write its own temporary files here: what it leaves is no failure of a test.
    with tempfile.TemporaryDirectory(prefix='box-suite-', ignore_cleanup_errors=True) as directory:
        result = Path(directory, 'result.json')
        command = [
            sys.executable, '-c', 'import common; common._run_for_parent()',
            function.__module__, function.__qualname__, json.dumps(arguments), str(result),
        ]
        try:
            # The temporary files of the process go where they are removed with it.
            finished = subprocess.run(
                command, cwd=TESTS, env=dict(os.environ, TMPDIR=directory), capture_output=True, text=True,
                timeout=PROCESS_TIMEOUT, check=False)
        except subprocess.TimeoutExpired as error:
            raise ProcessFailed(f'{function.__qualname__} did not end:\n{error.stderr}') from None
        finally:
            forget_cached_configuration()
        if finished.returncode or not result.is_file():
            raise ProcessFailed(
                f'{function.__qualname__} ended with code {finished.returncode}:\n{finished.stderr[-4000:]}')
        return json.loads(result.read_text())


def _run_for_parent():
    module, name, arguments, result = sys.argv[1:5]
    code = 1
    try:
        value = getattr(importlib.import_module(module), name)(**json.loads(arguments))
        Path(result).write_text(json.dumps(value))
        code = 0
    except BaseException:  # noqa: BLE001 - whatever ended the function, the parent must learn it
        traceback.print_exc()
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        # The threads of a started box never end, and core's exit handlers may wait for them.
        os._exit(code)


def start_report(server_wide_modules=None, core_changes=None, changed_signatures=(), probes=(), web_changes=None,
                 layer_files=None, without=(), script_changes=None):
    """Start a box in this process and say what its start did. Meant for `in_new_process`.

    The box starts as its configuration says, unless `server_wide_modules` gives another list of
    modules, `core_changes`, `web_changes` or `script_changes` a core that moved, `layer_files` a layer that holds
    more files (see `configuration_file` for the four), `changed_signatures` library functions
    that an update gave another signature (see `change_signature`) or `without` files of the
    layer that cannot be loaded (see `keep_from_loading`). The report:

    * 'integrity' - the state the layer gives of itself, None when the layer is not loaded;
    * 'threads' - one entry per thread, in the order they really started: 'thread' (its class,
      or its function for a plain thread) and 'seams_applied' (the numbers of the seams applied
      at that moment, None when the layer was not loaded yet);
    * 'asked' - every host the process asked meanwhile (see `AskedHosts`);
    * 'answers' - the status of a GET on each address of `probes`, once the box is started.
    """
    configuration = CONFIGURATION
    if server_wide_modules or core_changes or web_changes or layer_files or script_changes:
        configuration = configuration_file(
            tempfile.mkdtemp(prefix='box-suite-'), server_wide_modules, core_changes, web_changes, layer_files,
            script_changes)
    keep_from_loading(*without)
    with ThreadStarts() as threads, AskedHosts() as asked:
        for name in changed_signatures:
            change_signature(name)
        start(configuration)
    import odoo

    client = Client(odoo.http.root)
    seams = sys.modules.get(f'{LAYER}.seams')
    return {
        'integrity': seams.integrity() if seams else None,
        'threads': threads.started,
        'asked': asked.hosts,
        'answers': {path: client.get(path).status_code for path in probes},
    }


# What core's update check needs to find to change the code of a box: a server on a series other than the
# branch the box has checked out. The series is one for which core has no upgrade script to run.
OTHER_SERIES = '17.0'
CHECKED_OUT_BRANCH = '18.0'
CORE_UPDATE_SCRIPT = 'posbox_update.sh'
CORE_CHECKOUT = '--git-dir=/home/pi/odoo/.git'


@contextlib.contextmanager
def core_update_possible():
    """Arrange the checkout of core so that its update check, were it to run, would update the box.

    The box then has CHECKED_OUT_BRANCH checked out and its remote offers every branch: a server that
    says it runs OTHER_SERIES (a `ServerOfASeries` of services_common) makes core rename the branch and run
    its update script. Nothing is run or changed. Gives the list of what core did to update, each as
    [program, *arguments]: empty while core's check does not run. The commands of the layer, which
    names its repositories with `-C`, are left alone.
    """
    updated = []
    check_output = subprocess.check_output

    def core_command(command):
        return isinstance(command, (list, tuple)) and any(
            Path(str(part)).name == CORE_UPDATE_SCRIPT or str(part) == CORE_CHECKOUT for part in command)

    def answer(command):
        # What changes the code of the box: its update script, and the git commands that set the branch it follows.
        if core_command(command) and (CORE_UPDATE_SCRIPT in command[0] or 'branch' in command):
            updated.append([Path(command[0]).name, *command[1:]])
            return 0
        return None

    def answering_git(command, *arguments, **options):
        if core_command(command) and 'ls-remote' in command:
            return b'0123456789abcdef\trefs/heads/' + OTHER_SERIES.encode()
        if core_command(command) and 'symbolic-ref' in command:
            return CHECKED_OUT_BRANCH.encode()
        return check_output(command, *arguments, **options)

    with _answering_commands(answer), mock.patch.object(subprocess, 'check_output', answering_git):
        yield updated


def start_report_of_a_box_whose_core_could_update(**arguments):
    """`start_report`, plus 'core_updated': what core did to update the box (see `core_update_possible`)."""
    with core_update_possible() as updated:
        report = start_report(**arguments)
    return dict(report, core_updated=list(updated))


def configuration_file(directory, server_wide_modules=None, core_changes=None, web_changes=None, layer_files=None,
                       script_changes=None):
    """Write in `directory` a configuration of the box with other server-wide modules, or with other code.

    `core_changes` maps a text of core to what an update of core made of it; it is applied to
    every Python file of a copy of the box modules of core. `web_changes` does the same to a copy
    of the module of core the page of the box is built on. `layer_files` maps the path of a file
    in the layer to its content: the files are added to a copy of the modules of the layer, as a
    later version of the layer may hold them. `script_changes` maps the path of a script of core
    on the addons path - one the box runs as root, outside its modules - to the changes, as
    `core_changes` gives them, an update of core made to it. The configuration puts each copy
    first on its addons path.
    """
    parser = _read_configuration()
    if server_wide_modules:
        parser.set('options', 'server_wide_modules', server_wide_modules)
    addons_path = parser.get('options', 'addons_path')
    copies = []
    if layer_files:
        copies.append(_layer_with_files(directory, layer_files))
    if core_changes:
        copies.append(_changed_core(Path(directory, 'addons'), addons_path, CORE_MODULES, core_changes, shutil.copy2))
    if web_changes:
        copies.append(_changed_core(Path(directory, 'web'), addons_path, (WEB_MODULE,), web_changes, _copy_code))
    if script_changes:
        copies.append(_changed_scripts(Path(directory, 'scripts'), addons_path, script_changes))
    if copies:
        parser.set('options', 'addons_path', ','.join([*map(str, copies), addons_path]))
    path = Path(directory, 'odoo.conf')
    with path.open('w', encoding='utf-8') as file:
        parser.write(file)
    return path


def _copy_code(source, target):
    """Copy a Python file; make of any other file a link to the original."""
    if source.endswith('.py'):
        shutil.copy2(source, target)
    else:
        os.symlink(source, target)


def _changed_core(addons, addons_path, modules, changes, copy):
    source = next(Path(entry) for entry in addons_path.split(',') if Path(entry, modules[0]).is_dir())
    for module in modules:
        shutil.copytree(
            source / module, addons / module, ignore=shutil.ignore_patterns('__pycache__'), copy_function=copy)
    not_found = set(changes)
    for file in addons.rglob('*.py'):
        text = changed = file.read_text(encoding='utf-8')
        for former, later in changes.items():
            if former in changed:
                not_found.discard(former)
                changed = changed.replace(former, later)
        if changed != text:
            file.write_text(changed, encoding='utf-8')
    if not_found:
        raise RuntimeError(f'Core has no such text any more, the change cannot be made: {sorted(not_found)}')
    return addons


def _changed_scripts(scripts, addons_path, changes):
    for script, replacements in changes.items():
        source = next(Path(entry, script) for entry in addons_path.split(',') if Path(entry, script).is_file())
        text = source.read_text(encoding='utf-8')
        not_found = sorted(former for former in replacements if former not in text)
        if not_found:
            raise RuntimeError(f'{script} has no such text any more, the change cannot be made: {not_found}')
        for former, later in replacements.items():
            text = text.replace(former, later)
        target = scripts / script
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        target.chmod(source.stat().st_mode)
    return scripts


def _layer_with_files(directory, files):
    layer = Path(directory, 'layer')
    for module in LAYER_MODULES:
        shutil.copytree(
            LAYER_MODULE.parent / module, layer / module, ignore=shutil.ignore_patterns('__pycache__', 'tests'))
    for path, content in files.items():
        Path(layer, path).write_text(content, encoding='utf-8')
    return layer


def change_signature(name):
    """Give a library function - 'certifi.where', 'threading.Thread.start' - one more parameter."""
    module, *path, attribute = name.split('.')
    owner = importlib.import_module(module)
    for part in path:
        owner = getattr(owner, part)
    original = getattr(owner, attribute)

    def changed(*arguments, added_by_an_update=None, **options):
        return original(*arguments, **options)

    setattr(owner, attribute, changed)


def seam_states(integrity):
    """{seam number: state} of the state the layer gives of itself."""
    return {seam['number']: seam['state'] for seam in integrity['seams']}


SeamCheck = collections.namedtuple('SeamCheck', 'exit_code holds deferred failed_parts output')
# The state of a seam that holds and that another seam applies once core has loaded its device handlers.
DEFERRED = 'deferred'


def seam_check(server_wide_modules=None, core_changes=None, changed_signatures=(), web_changes=None,
               layer_files=None, script_changes=None):
    """Run the standalone seam check of the layer, as a person or a build does.

    The arguments are those of `start_report`. Returns the exit code of the check, {seam number:
    whether it holds}, the numbers of the seams it reports as deferred, the parts of the layer it
    reports as not working, and all it printed.
    """
    require_software_box()
    with tempfile.TemporaryDirectory(prefix='box-suite-') as directory:
        configuration, boot_file = CONFIGURATION, BOOT_FILE
        if server_wide_modules or core_changes or web_changes or layer_files or script_changes:
            configuration = configuration_file(
                directory, server_wide_modules, core_changes, web_changes, layer_files, script_changes)
        if layer_files:
            boot_file = Path(directory, 'layer', BOOT_FILE.relative_to(LAYER_MODULE.parent))
        arguments = ['--config', str(configuration)]
        if changed_signatures:
            command = [sys.executable, '-c', 'import common; common._seam_check_after_changes()',
                       json.dumps(list(changed_signatures)), str(boot_file), *arguments]
        else:
            command = [sys.executable, str(boot_file), *arguments]
        finished = subprocess.run(
            command, cwd=TESTS, env=dict(os.environ, TMPDIR=directory), capture_output=True, text=True,
            timeout=PROCESS_TIMEOUT, check=False)
    # A command core runs meanwhile may leave its own words in front of a line of the check.
    verdicts = re.findall(r'seam +(\d+) +(ok|FAILED)\b(.*)', finished.stdout)
    holds = {int(number): word == 'ok' for number, word, _rest in verdicts}
    deferred = {int(number) for number, word, rest in verdicts if word == 'ok' and f' {DEFERRED}:' in rest}
    failed_parts = re.findall(r'^FAILED\s+(.+?):', finished.stdout, re.MULTILINE)
    return SeamCheck(finished.returncode, holds, deferred, failed_parts, finished.stdout + finished.stderr)


def _seam_check_after_changes():
    for name in json.loads(sys.argv.pop(1)):
        change_signature(name)
    boot_file = sys.argv.pop(1)
    sys.argv[0] = boot_file
    runpy.run_path(boot_file, run_name='__main__')


class ThreadStarts:
    """Lists, while its block runs, each thread at the moment it really starts.

    `started` holds, per thread, 'thread' - its class, or the function it runs when it is a plain
    thread - and 'seams_applied': the numbers of the seams the layer had applied by then, None
    when the layer was not loaded yet.
    """

    def __enter__(self):
        self.started = []
        self._start = original = threading.Thread.start
        started, label, seams_applied = self.started, self._label, self._seams_applied

        # The gate of the layer takes the place of this method only if it still has the signature it knows.
        def start(self):
            started.append({'thread': label(self), 'seams_applied': seams_applied()})
            original(self)

        threading.Thread.start = start
        return self

    def __exit__(self, *error):
        threading.Thread.start = self._start

    @staticmethod
    def _label(thread):
        described = type(thread)
        if described is threading.Thread and getattr(thread, '_target', None):
            described = thread._target
        return f'{described.__module__}.{described.__qualname__}'

    @staticmethod
    def _seams_applied():
        seams = sys.modules.get(f'{LAYER}.seams')
        if not seams:
            return None
        return [seam['number'] for seam in seams.integrity()['seams'] if seam['state'] == 'applied']


class AskedHosts:
    """Lists, while its block runs, the hosts this process asks, in `hosts`.

    A host is asked when its name is resolved or a stream connection to its address is opened. A
    datagram socket that is only given a destination sends nothing - core finds its own address
    that way - and is left out, like the sockets of the machine itself.
    """

    def __enter__(self):
        self.hosts = []
        self._resolve = resolve = socket.getaddrinfo
        hosts = self.hosts

        def getaddrinfo(host, *arguments, **options):
            if host is not None:
                hosts.append(host.decode() if isinstance(host, bytes) else str(host))
            return resolve(host, *arguments, **options)

        def note(sock, address):
            if sock.type == socket.SOCK_STREAM and sock.family in (socket.AF_INET, socket.AF_INET6):
                hosts.append(address[0])

        def connect(sock, address):
            note(sock, address)
            return super(socket.socket, sock).connect(address)

        def connect_ex(sock, address):
            note(sock, address)
            return super(socket.socket, sock).connect_ex(address)

        socket.getaddrinfo = getaddrinfo
        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex
        return self

    def __exit__(self, *error):
        socket.getaddrinfo = self._resolve
        del socket.socket.connect
        del socket.socket.connect_ex


class BoxStateCase(unittest.TestCase):
    """A test after which the box keeps what it kept before about its server."""

    # How `started_box` starts the box of the class: the arguments of `start_report`.
    start_options = {}

    @classmethod
    def setUpClass(cls):
        require_software_box()
        super().setUpClass()

    def setUp(self):
        require_software_box()
        super().setUp()
        self.addCleanup(restore_state, save_state())

    @classmethod
    def started_box(cls):
        """What `start_report` says of a box started with `start_options`, once for all the tests of the class."""
        if '_started_box' not in vars(cls):
            cls._started_box = in_new_process(start_report, **cls.start_options)
        return cls._started_box

    def server(self, kind=None, **options):
        """A `FakeServer`, or a server of the subclass `kind`, that is closed when the test ends."""
        server = (kind or FakeServer)(**options)
        self.addCleanup(server.close)
        return server

    def keep_web_certificate(self):
        """Put the certificate and the key of the web server back when the test ends."""
        self.addCleanup(restore_web_certificate, save_web_certificate())


class BoxCase(BoxStateCase):
    """A test on the box started in this process, driven through its routes the way a browser drives it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        start()

    def setUp(self):
        super().setUp()
        import odoo

        del restarts[:]
        self.client = Client(odoo.http.root)

    def request(self, path, parameters=None, proof=None, sent=None, box=None):
        """The response to a GET, or to a JSON call when `parameters` is given; `proof` is an unlock proof.

        `sent` is what the browser of the caller adds to the request of its own, as header names and
        values. `box` is the address by which the caller reaches the box, `http://localhost` when left out.
        """
        headers = dict(sent or {})
        if proof:
            headers[layer('lock').PROOF_HEADER] = proof
        if parameters is None:
            return self.client.get(path, headers=headers, base_url=box)
        return self.client.post(path, json=_rpc_call(parameters), headers=headers, base_url=box)

    def call(self, path, proof=None, **parameters):
        """The result of a JSON route."""
        answer = self.request(path, parameters, proof).get_json()
        if 'result' not in answer:
            raise AssertionError(f'{path} answered an error instead of a result: {answer}')
        return answer['result']

    def pair(self, proof=None, **parameters):
        return self.call('/viin_hw_homepage/pair', proof=proof, **parameters)

    def unlock(self, code):
        """(answer, unlock proof) of the unlock route; the proof is None when the box gave none."""
        response = self.request('/viin_hw_homepage/unlock', {'code': code})
        return response.get_json()['result'], response.headers.get(layer('lock').PROOF_HEADER)

    def report_to(self, server):
        """Have core send `server` its report, which carries the credential, as it does once the box has started.

        Core reads the address of its server when it starts: `server` stands for the one it read.
        """
        manager = core('main').manager
        self.addCleanup(setattr, manager, 'server_url', manager.server_url)
        manager.server_url = server.url
        manager.send_alldevices()

    def box_with_server(self, credential, certificate=None, kind=None, **options):
        """Pair the box with a new `FakeServer`, which gives it `credential`, and return that server.

        `kind` and `options` are those of `server`. With a `certificate` the server speaks https
        and the installer confirmed that certificate. The box is left as its restart leaves it: it
        has read its server, and no restart is pending.
        """
        server = self.server(
            kind, answer={'status': 'paired', 'token': credential, 'box_name': 'Suite box'}, certificate=certificate,
            **options)
        answer = self.pair(
            link=server.link('K7Q2-M9XD-4TWP'), trust_fingerprint=certificate and certificate.fingerprint)
        self.assertEqual(answer.get('status'), 'paired', answer)
        forget_cached_configuration()
        del restarts[:]
        return server


class MovedSeamCase(BoxStateCase):
    """What the box does about ONE seam whose symbol moved in core, or in a library.

    A test class inherits from this one and says which seam (`number`), what moved (`core_changes`,
    `web_changes`, `script_changes`, `layer_files` or `changed_signatures`, see `start_report`) and which
    `other_seams` must be left as they are. `probes` are addresses of the started box, beside
    the one that tells it started, whose answers a test of the class reads.
    """
    number = 0
    core_changes = None
    web_changes = None
    script_changes = None
    layer_files = None
    changed_signatures = ()
    other_seams = ()
    probes = ()

    def __init_subclass__(cls, **options):
        super().__init_subclass__(**options)
        cls.moved = {
            'core_changes': cls.core_changes, 'web_changes': cls.web_changes, 'script_changes': cls.script_changes,
            'layer_files': cls.layer_files,
            'changed_signatures': list(cls.changed_signatures)}
        cls.start_options = dict(cls.moved, probes=[PING, *cls.probes])

    def test_integrity_state_names_the_moved_seam(self):
        integrity = self.started_box()['integrity']

        self.assertEqual(seam_states(integrity).get(self.number), 'failed')
        self.assertIs(integrity['ok'], False)

    def test_other_seams_stay_applied(self):
        states = seam_states(self.started_box()['integrity'])

        self.assertEqual(
            {number: states.get(number) for number in self.other_seams}, dict.fromkeys(self.other_seams, 'applied'))

    def test_box_still_starts(self):
        self.assertEqual(self.started_box()['answers'][PING], 200)

    def test_standalone_check_fails_and_names_the_moved_seam(self):
        check = seam_check(**self.moved)

        self.assertEqual(check.exit_code, 1, check.output)
        self.assertIs(check.holds.get(self.number), False, check.output)
        self.assertEqual(
            {number: check.holds.get(number) for number in self.other_seams}, dict.fromkeys(self.other_seams, True),
            check.output)
