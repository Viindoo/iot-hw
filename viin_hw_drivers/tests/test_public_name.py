"""The Viindoo name of the box: a name of its own, asked of its server, served beside its own certificate.

The box runs with the web server site of the layer installed, as a box has it, and every file is
written for real, as root. Its server, the name service of its site and its clock are scripted.
"""
import contextlib
import datetime
import json
import logging
import re
import socket
import ssl
import subprocess
import tempfile
import threading
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

import common
import services_common as box

CREDENTIAL = 'suite-credential-public-name'
ROUTE = '/iot/certificate'
ZONE = 'box-names.example'
FQDN = f'k3x9q7m2p5w8r4t6.{ZONE}'
OTHER_FQDN = f'a2b3c4d5e6f7g2h3.{ZONE}'
OWN_NAME = 'box.shop.example'
# Where the web server of the software box answers: the address of the box on its site.
BOX_ADDRESS = '127.0.0.1'
OTHER_ADDRESS = '192.168.1.77'
MINUTE = datetime.timedelta(minutes=1)
HOUR = datetime.timedelta(hours=1)
DAY = datetime.timedelta(days=1)
SECOND = datetime.timedelta(seconds=1)
FIRST_QUESTION = 2 * MINUTE
# The server relays one question of a box a minute; the box asks again five seconds after that, never at the minute.
FLOOR = MINUTE
FOLLOW_UP = FLOOR + 5 * SECOND
# The longest a daily question may come after its day: a day, and at most an hour drawn at random.
DAY_AT_THE_LATEST = DAY + HOUR + SECOND
WEEK = 7 * DAY
# The longest the client may take over what is due: it wakes once a minute.
LOOK_AT_THE_LATEST = 60
CERTIFICATE_LIFE = 89 * DAY

OWN_CERTIFICATE = '/etc/ssl/certs/nginx-cert.crt'
OWN_KEY = '/etc/ssl/private/nginx-cert.key'
PENDING_FILES = '/etc/ssl/private/nginx-viin-name.pending.*'
# The pair of the name: one directory per place, a generation directory for each pair, `current` naming the one in use.
NAME_PAIR = '/etc/ssl/private/viindoo-name'
NAME_PAIR_ON_THE_CARD = '/root_bypass_ramdisks/etc/ssl/private/viindoo-name'
NAME_BLOCKS = '/etc/nginx/viindoo/names'
NAME_TEMPLATE = '/etc/nginx/viindoo/name.conf.template'
PENDING_KEY = '/etc/ssl/private/nginx-viin-name.pending.key'
PENDING_REQUEST = '/etc/ssl/private/nginx-viin-name.pending.csr'
NAME_KEY = f'{NAME_PAIR}/current/privkey.pem'
NAME_CERTIFICATE = f'{NAME_PAIR}/current/fullchain.pem'
WEB_SERVER_PID = '/run/nginx.pid'
MANAGED_NAME = 'viin_managed_name'
MANAGED_NAME_SERVER = 'viin_managed_name_server'
# What the system files of the layer change on a machine, put back after the tests of a class.
SYSTEM_PLACES = ('etc/nginx', 'etc/avahi', 'etc/systemd', 'usr/local/sbin', 'usr/share/viindoo-iot')
TIME = '%Y-%m-%dT%H:%M:%SZ'


def spki(public_key):
    return public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def pem(certificate):
    return certificate.public_bytes(serialization.Encoding.PEM).decode()


def authority(name, issuer=None):
    """A `common.Certificate` of an authority, valid for longer than any name the tests give."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    signer = issuer.key if issuer else key
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer.certificate.subject if issuer else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(common.utc_now() - 2 * DAY)
        .not_valid_after(common.utc_now() + 400 * DAY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
            key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False),
            critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), critical=False)
        .sign(signer, hashes.SHA256())
    )
    return common.Certificate(certificate, key)


def issue(public_key, names, valid_from, valid_until, issuer):
    """A certificate of a web site for `public_key`, as a public authority issues it."""
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(issuer.certificate.subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(valid_from)
        .not_valid_after(valid_until)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=True, data_encipherment=False,
            key_agreement=False, key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False),
            critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name) for name in names]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer.key.public_key()), critical=False)
        .sign(issuer.key, hashes.SHA256())
    )


@contextlib.contextmanager
def public_authorities(root):
    """Count the authority `root` among those every browser trusts, for the box started in this process."""
    trust = common.layer('trust')
    with tempfile.TemporaryDirectory(prefix='box-suite-') as directory:
        authorities = Path(directory, 'authorities.pem')
        authorities.write_bytes(Path(trust.PUBLIC_AUTHORITIES).read_bytes() + b'\n' + root.pem.encode())
        with mock.patch.object(trust, 'PUBLIC_AUTHORITIES', str(authorities)):
            yield


@contextlib.contextmanager
def site_resolver(names):
    """Be the name service of the site for the names of ZONE during the block.

    `names` maps a name to the addresses it gives, and may change meanwhile; a name it lacks is
    refused, as a router that keeps public names from leading to private addresses refuses it.
    """
    resolve = socket.getaddrinfo

    def getaddrinfo(host, port, *arguments, **options):
        name = host.decode() if isinstance(host, bytes) else host
        if isinstance(name, str) and name.endswith(f'.{ZONE}'):
            if not names.get(name):
                raise socket.gaierror(socket.EAI_NONAME, 'Name or service not known')
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (address, port))
                    for address in names[name]]
        return resolve(host, port, *arguments, **options)

    with mock.patch.object(socket, 'getaddrinfo', getaddrinfo):
        yield names


class BoxClock:
    """The clock of the box, which a test sets."""

    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class NameServer(common.FakeServer):
    """A server that offers its boxes a name, as far as a box can tell, answering as viin_iot does.

    A question without a request is answered with the name `fqdn`; a request for it gets a
    certificate `issuing` signs for its key - the one given before when the key is the same - valid
    from the day before until CERTIFICATE_LIFE after `today()`. `answers` are given first, one per
    question, in their order: a result, a function of the parameters that gives one, or an HTTP
    status alone.
    """

    def __init__(self, issuing, today, fqdn=FQDN, **options):
        self.issuing = issuing
        self.today = today
        self.fqdn = fqdn
        self.answers = []
        self.issued = []
        self._given = {}
        super().__init__(**options)

    @property
    def questions(self):
        return [request['body']['params'] for request in self.requests if request['path'] == ROUTE]

    def requested_keys(self):
        """The public key of the request each question carried, None for a question without one."""
        return [spki(x509.load_pem_x509_csr(question['csr'].encode()).public_key()) if question.get('csr') else None
                for question in self.questions]

    def answer_to(self, method, path, body):
        if method != 'POST' or path != ROUTE:
            return super().answer_to(method, path, body)
        answer = self.answers.pop(0) if self.answers else self.offered
        if callable(answer):
            answer = answer(body['params'])
        if isinstance(answer, int):
            return answer, b''
        return 200, common.rpc_result(answer)

    def offered(self, params):
        if not params.get('csr'):
            return {'status': 'name', 'fqdn': self.fqdn}
        public_key = x509.load_pem_x509_csr(params['csr'].encode()).public_key()
        if spki(public_key) not in self._given:
            self._given[spki(public_key)] = self.certificate_for(public_key)
        return self._given[spki(public_key)]

    def certificate_for(self, public_key, names=None, valid_from=None, valid_until=None, issuing=None):
        """The `issued` answer that carries a certificate for `public_key`."""
        issuing = issuing or self.issuing
        leaf = issue(public_key, names or [self.fqdn], valid_from or common.utc_now() - DAY,
                     valid_until or self.today() + CERTIFICATE_LIFE, issuing)
        self.issued.append(leaf)
        return {'status': 'issued', 'fqdn': self.fqdn, 'certificate': pem(leaf) + issuing.pem,
                'not_after': leaf.not_valid_after.strftime(TIME)}

    def for_another_key(self, _params):
        return self.certificate_for(rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key())

    def requested_certificate(self, **changes):
        """An answer giving a certificate for the key of the request, made with `changes` (see certificate_for)."""
        return lambda params: self.certificate_for(
            x509.load_pem_x509_csr(params['csr'].encode()).public_key(), **changes)


def read_as_root(path):
    """The bytes of a file the box user may not read; None when it does not exist."""
    found = subprocess.run(['sudo', '-n', 'cat', '--', path], capture_output=True, check=False)
    return found.stdout if found.returncode == 0 else None


def presented(name=None):
    """Fingerprint of the certificate the web server of the box presents to a browser that asks by `name`,
    or by address when None."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((BOX_ADDRESS, 443), timeout=10) as plain:
        with context.wrap_socket(plain, server_hostname=name) as secured:
            return common.fingerprint(x509.load_der_x509_certificate(secured.getpeercert(binary_form=True)))


@contextlib.contextmanager
def web_server_loads():
    """List, for the block, each time the box has the web server load its configuration - a reload or a
    start - with whether the web server accepted the configuration on the disk at that moment."""
    loads = []
    originals = {name: getattr(subprocess, name) for name in ('run', 'call', 'check_call')}

    def accepted():
        return originals['run'](['sudo', '-n', 'nginx', '-t', '-q'], capture_output=True, check=False).returncode == 0

    def watching(original):
        def call(command, *arguments, **options):
            if is_web_server_load(command):
                loads.append((' '.join(str(part) for part in command), accepted()))
            return original(command, *arguments, **options)

        return call

    with contextlib.ExitStack() as stack:
        for name, original in originals.items():
            stack.enter_context(mock.patch.object(subprocess, name, watching(original)))
        yield loads


def is_web_server_load(command):
    words = [str(part) for part in command] if isinstance(command, (list, tuple)) else []
    return 'nginx' in words and bool({'reload', 'restart', 'start'} & set(words))


@contextlib.contextmanager
def web_server_ignoring_loads():
    """A web server that goes on with the configuration it runs during the block: asked to load it again, it
    says it did and changes nothing."""
    run = subprocess.run

    def call(command, *arguments, **options):
        if is_web_server_load(command):
            return subprocess.CompletedProcess(command, 0, b'', b'')
        return run(command, *arguments, **options)

    with mock.patch.object(subprocess, 'run', call):
        yield


@contextlib.contextmanager
def web_server_load_failing_once():
    """The first command that would have the web server load its configuration again cannot be run."""
    run = subprocess.run
    failed = []

    def call(command, *arguments, **options):
        if is_web_server_load(command) and not failed:
            failed.append(command)
            raise OSError('the system refused the command')
        return run(command, *arguments, **options)

    with mock.patch.object(subprocess, 'run', call):
        yield failed


@contextlib.contextmanager
def refused_name_template():
    """A template of the name block that names a directive this web server does not know, as a release could
    ship one, during the block."""
    template = read_as_root(NAME_TEMPLATE)
    refused = template.replace(b'server_name @NAME@;', b'server_name @NAME@;\n    box_suite_unknown_directive on;')
    common.as_root('install', '-m', '644', '/dev/stdin', NAME_TEMPLATE, data=refused)
    try:
        yield
    finally:
        common.as_root('install', '-m', '644', '/dev/stdin', NAME_TEMPLATE, data=template)


@contextlib.contextmanager
def missing_name_template():
    """No template of the name block on the box during the block, as a release that ships none leaves it."""
    template = read_as_root(NAME_TEMPLATE)
    common.as_root('rm', '-f', NAME_TEMPLATE)
    try:
        yield
    finally:
        common.as_root('install', '-m', '644', '/dev/stdin', NAME_TEMPLATE, data=template)


def files_holding_a_private_key():
    found = common.as_root(
        'sh', '-c', 'grep -r -l -s -- "PRIVATE KEY" /etc/ssl/certs /etc/ssl/private || true').stdout.decode()
    return set(found.split())


def pair_state(directory=NAME_PAIR):
    """(the generation `current` names or None, the generations the directory holds, the staged link or None)."""
    current = common.as_root('sh', '-c', f'readlink -- {directory}/current || true').stdout.decode().strip()
    listing = common.as_root('sh', '-c', f'ls -A -1 -- {directory} 2>/dev/null || true').stdout.decode().split()
    return (current or None, sorted(name for name in listing if name.startswith('g-')),
            'current.viin-new' in listing)


def key_matches_certificate(certificate_pem, key_pem):
    """Whether the private key of `key_pem` is the key of the first certificate of `certificate_pem`."""
    leaf = x509.load_pem_x509_certificate(certificate_pem)
    key = serialization.load_pem_private_key(key_pem, password=None)
    return spki(leaf.public_key()) == spki(key.public_key())


def name_blocks():
    return common.as_root('find', NAME_BLOCKS, '-name', '*.conf').stdout.decode().split()


def files_of_the_name():
    """{path: bytes} of the certificate, the key and the web server block of the name."""
    return {path: read_as_root(path) for path in (NAME_CERTIFICATE, NAME_KEY, *name_blocks())}


def files_under_the_certificates_of_the_system_belonging_to(leaf):
    """The files of /etc/ssl/certs that hold the certificate `leaf`, or are named for the Viindoo name."""
    body = pem(leaf).splitlines()[1]
    found = common.as_root('sh', '-c', f'grep -r -l -s -F -- "{body}" /etc/ssl/certs || true; '
                                       'find /etc/ssl/certs -name "*viin*" || true').stdout.decode()
    return found.split()


def key_lines(data):
    """The lines of base64 inside the private key blocks of `data`."""
    blocks = re.findall(rb'-----BEGIN [A-Z ]*PRIVATE KEY-----(.+?)-----END', data or b'', re.DOTALL)
    return [line.decode() for block in blocks for line in block.split() if len(line) > 40]


def request_lines(text):
    found = re.search(r'-----BEGIN CERTIFICATE REQUEST-----(.+?)-----END', text or '', re.DOTALL)
    return [line for line in found.group(1).split() if len(line) > 40] if found else []


def own_certificate_files():
    return read_as_root(OWN_CERTIFICATE), read_as_root(OWN_KEY)


def lay_out_system_files(directory):
    """Install the system files of the layer, as a box has them; give what puts the machine back as it was."""
    saved = str(Path(directory, 'system-before.tar'))
    present = [place for place in SYSTEM_PLACES if Path('/', place).exists()]
    common.as_root('tar', '-C', '/', '-cpf', saved, *present)

    def put_back():
        common.wait_for_the_web_server_to_read_its_files()
        common.as_root('rm', '-rf', *(f'/{place}' for place in SYSTEM_PLACES))
        common.as_root('tar', '-C', '/', '-xpf', saved)
        common.as_root('rm', '-rf', directory)
        common.serve_web_certificate()

    return put_back


class ViindooNameCase(common.BoxCase):
    """The box started in this process, paired with a NameServer, with the web server site of the layer."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        common.serve()
        directory = tempfile.mkdtemp(prefix='box-suite-')
        cls.addClassCleanup(lay_out_system_files(directory))
        common.wait_for_the_web_server_to_read_its_files()
        common.as_root('bash', str(box.SYSTEM_FILES / 'apply.sh'))
        common.serve_web_certificate()
        cls.public_name = common.layer('public_name')
        cls.root = authority('Box suite root authority')
        cls.issuing = authority('Box suite issuing authority', issuer=cls.root)

    def setUp(self):
        super().setUp()
        box.keep_files(self, box.STATE_FILE)
        self.addCleanup(self.forget_the_name)
        self.enterContext(public_authorities(self.root))
        self.clock = BoxClock(common.utc_now())
        self.enterContext(mock.patch.object(common.layer('trust'), 'utc_now', self.clock))
        self.enterContext(mock.patch.object(common.core('tools.helpers'), 'get_ip', return_value=BOX_ADDRESS))
        self.site = self.enterContext(site_resolver({FQDN: [BOX_ADDRESS], OTHER_FQDN: [BOX_ADDRESS]}))
        self.name_server = self.server(NameServer, issuing=self.issuing, today=self.clock)
        common.store_box_configuration(remote_server=self.name_server.url, token=CREDENTIAL)
        self.own_files = own_certificate_files()
        self.keys_before = files_holding_a_private_key()
        self.own = presented()
        self.started = None

    def forget_the_name(self):
        """Leave the box with no name, as one that never had any."""
        common.wait_for_the_web_server_to_read_its_files()
        common.as_root('sh', '-c', f'rm -rf {NAME_PAIR} {NAME_PAIR_ON_THE_CARD} {PENDING_FILES} && '
                                   f'rm -f {NAME_BLOCKS}/*.conf')
        common.serve_web_certificate()
        common.store_box_configuration(**{MANAGED_NAME: '', MANAGED_NAME_SERVER: ''})
        common.layer('update').change_state(lambda state: state.pop('public_name', None))

    def service_starts(self):
        """What the start of the box service does for the name: reconcile, then plan the first question."""
        self.public_name.reconcile()
        self.public_name.begin()
        self.started = self.clock.now

    def at(self, after_the_start):
        """Let the time of the box come to `after_the_start` and let the client do what is due then."""
        self.clock.now = self.started + after_the_start
        return self.public_name.tick()

    def named(self):
        """Start the box service and let it get its name and the certificate of it; give the moment of the
        question that brought the certificate."""
        self.service_starts()
        self.at(FIRST_QUESTION)
        self.at(FIRST_QUESTION + FOLLOW_UP)
        return FIRST_QUESTION + FOLLOW_UP

    def done_in_time_while_the_web_server_ignores_loads(self, after_the_start):
        """`at`, with a web server that keeps the configuration it runs; give whether the client was done
        within LOOK_AT_THE_LATEST seconds."""
        worker = threading.Thread(target=self.at, args=(after_the_start,), daemon=True)
        with web_server_ignoring_loads():
            worker.start()
            worker.join(LOOK_AT_THE_LATEST)
        done = not worker.is_alive()
        if not done:
            # The web server then presents what the client waits for, so it lets the box go for the next tests.
            common.as_root('nginx', '-s', 'reload')
            worker.join(LOOK_AT_THE_LATEST)
        return done

    def name_record(self):
        return common.layer('update').read_state()['public_name']

    def new_key_files(self):
        """The files holding a private key that the box did not hold before the test."""
        return sorted(files_holding_a_private_key() - self.keys_before)

    @contextlib.contextmanager
    def server_that_gives_no_time(self):
        """The server answers as before but sends no Date header."""
        name = common.layer('public_name')
        post = name._post

        def without_date(*arguments):
            status, data, _date = post(*arguments)
            return status, data, None

        with mock.patch.object(name, '_post', without_date):
            yield

    def name_line(self):
        lines = common.layer('selftest').run(wait=0, refresh=True)['lines']
        return next(line for line in lines if line['id'] == 'viindoo_name')

    def managed_name(self):
        configuration = common.box_configuration()
        return configuration.get(MANAGED_NAME, ''), configuration.get(MANAGED_NAME_SERVER, '')

    def presented_once_settled(self, name, expected):
        """What the web server presents for `name`, given time to finish a reload that brings `expected`."""
        with contextlib.suppress(AssertionError):
            common.wait_until(lambda: presented(name) == expected, f'the web server to present {expected}', 10)
        return presented(name)

    def assertServesTheName(self, certificate):
        self.assertEqual(self.presented_once_settled(FQDN, common.fingerprint(certificate)),
                         common.fingerprint(certificate))

    def assertServesNoName(self):
        self.assertEqual(name_blocks(), [])
        self.assertEqual(self.presented_once_settled(FQDN, self.own), self.own)

    def assertOwnCertificateUntouched(self):
        self.assertEqual(own_certificate_files(), self.own_files)
        self.assertEqual(self.presented_once_settled(None, self.own), self.own)


class TestSchedule(ViindooNameCase):

    def test_paired_box_asks_for_its_name_two_minutes_after_its_service_started(self):
        self.service_starts()

        self.at(FIRST_QUESTION - SECOND)
        before = len(self.name_server.questions)
        self.at(FIRST_QUESTION)

        self.assertEqual((before, len(self.name_server.questions)), (0, 1))

    def test_box_holding_its_name_asks_again_the_next_day(self):
        named_at = self.named()
        asked = len(self.name_server.questions)

        self.at(named_at + DAY - SECOND)
        before_the_day = len(self.name_server.questions)
        self.at(named_at + DAY_AT_THE_LATEST)

        self.assertEqual((before_the_day, len(self.name_server.questions)), (asked, asked + 1))

    def test_certificate_in_the_making_is_asked_for_again_after_the_wait_given_kept_within_a_minute_and_a_day(self):
        self.name_server.answers = [{'status': 'pending', 'fqdn': FQDN, 'retry_after': 5},
                                    {'status': 'pending', 'fqdn': FQDN, 'retry_after': 10 ** 7}]
        self.service_starts()
        self.at(FIRST_QUESTION)

        self.at(FIRST_QUESTION + MINUTE - SECOND)
        before_a_minute = len(self.name_server.questions)
        self.at(FIRST_QUESTION + MINUTE)
        after_a_minute = len(self.name_server.questions)
        self.at(FIRST_QUESTION + MINUTE + DAY - SECOND)
        before_a_day = len(self.name_server.questions)
        self.at(FIRST_QUESTION + MINUTE + DAY)

        self.assertEqual((before_a_minute, after_a_minute, before_a_day, len(self.name_server.questions)),
                         (1, 2, 2, 3))

    def test_box_whose_question_got_no_answer_asks_again_within_the_hour(self):
        no_answer = {
            'server down': None,
            'server without an answer from its provider': {'status': 'unavailable', 'reason': 'provider_unreachable'},
            'answer that is no answer': 502,
        }
        for case, answer in no_answer.items():
            with self.subTest(case):
                self.forget_the_name()
                server = self.server(NameServer, issuing=self.issuing, today=self.clock)
                common.store_box_configuration(remote_server=server.url)
                self.service_starts()
                if answer is None:
                    server.close()
                else:
                    server.answers = [answer]
                self.at(FIRST_QUESTION)
                if answer is None:
                    server = self.server(NameServer, issuing=self.issuing, today=self.clock, port=server.port)

                self.at(FIRST_QUESTION + HOUR - SECOND)
                before_the_hour = len(server.questions)
                self.at(FIRST_QUESTION + HOUR)

                self.assertEqual((before_the_hour, len(server.questions)), (0, 1) if answer is None else (1, 2))

    def test_server_that_offers_no_name_is_not_asked_again_for_a_week_and_causes_no_warning(self):
        logger = self.public_name._logger
        for case, answer in {'no provider': {'status': 'unavailable', 'reason': 'no_provider'},
                             'server without the question': 404}.items():
            with self.subTest(case):
                self.forget_the_name()
                server = self.server(NameServer, issuing=self.issuing, today=self.clock)
                common.store_box_configuration(remote_server=server.url)
                server.answers = [answer]
                self.service_starts()

                with self.assertNoLogs(logger, logging.WARNING):
                    self.at(FIRST_QUESTION)
                    self.at(FIRST_QUESTION + WEEK - SECOND)
                within_the_week = len(server.questions)
                self.at(FIRST_QUESTION + WEEK)

                self.assertEqual((within_the_week, len(server.questions)), (1, 2))
                self.assertEqual(self.name_line()['state'], 'neutral')

    def test_request_follows_the_name_after_65_seconds_not_after_the_minute(self):
        self.service_starts()
        self.at(FIRST_QUESTION)

        self.at(FIRST_QUESTION + FOLLOW_UP - SECOND)
        before = len(self.name_server.questions)
        self.at(FIRST_QUESTION + FOLLOW_UP)

        self.assertEqual((before, len(self.name_server.questions)), (1, 2))
        self.assertIsNotNone(self.name_server.requested_keys()[1])

    def test_floor_of_the_server_is_waited_out_by_the_wait_it_gives_plus_5_seconds_with_the_same_request(self):
        floors = {'one second left': (1, 6), 'half a minute left': (30, 35), 'a whole minute left': (60, 65),
                  'more than a minute left': (600, 65), 'no wait given': (None, 65)}
        for case, (retry_after, wait) in floors.items():
            with self.subTest(case):
                self.forget_the_name()
                server = self.server(NameServer, issuing=self.issuing, today=self.clock)
                common.store_box_configuration(remote_server=server.url)
                floor = {'status': 'refused', 'reason': 'too_soon'}
                if retry_after is not None:
                    floor['retry_after'] = retry_after
                server.answers = [server.offered, floor]
                self.service_starts()
                self.at(FIRST_QUESTION)
                shown = self.name_line()
                record = {key: value for key, value in self.name_record().items()
                          if key not in ('asked_at', 'next_question_at')}

                refused_at = FIRST_QUESTION + FOLLOW_UP
                with self.assertNoLogs(self.public_name._logger, logging.WARNING):
                    self.at(refused_at)
                    after_the_floor = {key: value for key, value in self.name_record().items()
                                       if key not in ('asked_at', 'next_question_at')}
                    pending = (read_as_root(PENDING_KEY), read_as_root(PENDING_REQUEST))
                    self.at(refused_at + wait * SECOND - SECOND)
                    before_the_wait = len(server.questions)
                    self.at(refused_at + wait * SECOND)

                self.assertEqual(after_the_floor, record)
                self.assertEqual(shown['reason'], 'certificate_on_its_way')
                self.assertEqual(before_the_wait, 2)
                self.assertEqual(len(server.questions), 3)
                self.assertEqual(server.questions[2]['csr'], server.questions[1]['csr'])
                self.assertEqual(pending[1].decode(), server.questions[1]['csr'])
                self.assertEqual(server.requested_keys()[2], server.requested_keys()[1])

    def test_floor_of_the_server_leaves_the_line_of_the_name_and_the_pending_request_as_they_were(self):
        self.name_server.answers = [self.name_server.offered,
                                    {'status': 'refused', 'reason': 'too_soon', 'retry_after': 40}]
        self.service_starts()
        self.at(FIRST_QUESTION)
        self.at(FIRST_QUESTION + FOLLOW_UP)
        pending = (read_as_root(PENDING_KEY), read_as_root(PENDING_REQUEST))

        line = self.name_line()

        self.assertEqual((line['state'], line['reason']), ('neutral', 'certificate_on_its_way'))
        self.assertEqual(self.name_record()['last_answer'], 'name')
        self.assertEqual((read_as_root(PENDING_KEY), read_as_root(PENDING_REQUEST)), pending)
        self.assertIsNotNone(pending[0])

    def test_limit_of_the_week_keeps_its_own_line_and_its_own_wait_beside_the_floor(self):
        self.name_server.answers = [self.name_server.offered,
                                    {'status': 'refused', 'reason': 'try_later', 'retry_after': 3600}]
        named_at = self.named()

        line = self.name_line()
        self.at(named_at + HOUR - SECOND)
        before_the_wait = len(self.name_server.questions)
        self.at(named_at + HOUR)

        self.assertEqual((line['state'], line['reason']), ('neutral', 'try_later'))
        self.assertEqual((before_the_wait, len(self.name_server.questions)), (2, 3))

    def test_box_with_no_address_asks_without_one_and_asks_again_65_seconds_after_one_appears(self):
        address = self.enterContext(mock.patch.object(common.core('tools.helpers'), 'get_ip', return_value=None))
        self.name_server.answers = [{'status': 'unavailable', 'reason': 'no_provider'}]
        self.service_starts()
        self.at(FIRST_QUESTION)

        address.return_value = BOX_ADDRESS
        self.at(FIRST_QUESTION + 10 * SECOND)
        self.at(FIRST_QUESTION + FOLLOW_UP - SECOND)
        before_the_floor = len(self.name_server.questions)
        self.at(FIRST_QUESTION + FOLLOW_UP)

        asked = self.name_server.questions
        self.assertNotIn('ip', asked[0])
        self.assertEqual((before_the_floor, len(asked)), (1, 2))
        self.assertEqual(asked[1]['ip'], BOX_ADDRESS)

    def test_box_holding_its_name_that_loses_its_address_keeps_its_request_when_the_server_refuses_the_question(self):
        address = self.enterContext(mock.patch.object(common.core('tools.helpers'), 'get_ip', return_value=BOX_ADDRESS))
        named_at = self.named()
        installed = self.name_server.requested_keys()[1]
        issued = len(self.name_server.issued)
        address.return_value = None
        self.name_server.answers = [{'status': 'refused', 'reason': 'invalid_request'}]

        refused_at = named_at + DAY_AT_THE_LATEST
        self.at(refused_at)
        address.return_value = BOX_ADDRESS
        self.at(refused_at + FOLLOW_UP - SECOND)
        before_the_follow_up = len(self.name_server.questions)
        self.at(refused_at + FOLLOW_UP)

        asked = self.name_server.questions
        self.assertNotIn('ip', asked[2])
        self.assertEqual((before_the_follow_up, len(asked)), (3, 4))
        self.assertEqual(asked[3]['ip'], BOX_ADDRESS)
        self.assertEqual(asked[3]['csr'], asked[1]['csr'])
        self.assertEqual(self.name_server.requested_keys()[3], installed)
        self.assertEqual(len(self.name_server.issued), issued)

    def test_refusal_of_a_question_without_address_leaves_the_request_for_the_daily_question_too(self):
        address = self.enterContext(mock.patch.object(common.core('tools.helpers'), 'get_ip', return_value=BOX_ADDRESS))
        named_at = self.named()
        issued = len(self.name_server.issued)
        address.return_value = None
        self.name_server.answers = [{'status': 'refused', 'reason': 'invalid_request'}]

        refused_at = named_at + DAY_AT_THE_LATEST
        self.at(refused_at)
        self.at(refused_at + DAY_AT_THE_LATEST)

        asked = self.name_server.questions
        self.assertEqual(len(asked), 4)
        self.assertEqual(asked[3]['csr'], asked[1]['csr'])
        self.assertEqual(len(self.name_server.issued), issued)

    def test_box_whose_question_with_its_address_is_refused_as_invalid_asks_the_next_day_with_a_new_request(self):
        named_at = self.named()
        installed = self.name_server.requested_keys()[1]
        self.name_server.answers = [{'status': 'refused', 'reason': 'invalid_request'}]

        refused_at = named_at + DAY_AT_THE_LATEST
        self.at(refused_at)
        self.at(refused_at + DAY_AT_THE_LATEST)

        asked = self.name_server.questions
        self.assertIn('ip', asked[2])
        self.assertEqual(len(asked), 4)
        self.assertNotIn(self.name_server.requested_keys()[3], (None, installed))

    def test_server_that_has_no_name_to_offer_yet_is_asked_again_the_next_day_and_causes_no_warning(self):
        self.name_server.answers = [{'status': 'unavailable', 'reason': 'not_offered'}]
        self.service_starts()

        with self.assertNoLogs(self.public_name._logger, logging.WARNING):
            self.at(FIRST_QUESTION)
            line = self.name_line()
            self.at(FIRST_QUESTION + DAY - SECOND)
            before_the_day = len(self.name_server.questions)
            self.at(FIRST_QUESTION + DAY_AT_THE_LATEST)

        self.assertEqual((before_the_day, len(self.name_server.questions)), (1, 2))
        self.assertEqual((line['state'], line['reason']), ('neutral', 'not_offered'))

    def test_box_with_the_certificate_of_its_installer_never_asks(self):
        common.store_box_configuration(viin_box_address=OWN_NAME)
        self.service_starts()

        for moment in (FIRST_QUESTION, DAY, WEEK + DAY):
            self.at(moment)

        self.assertEqual(self.name_server.questions, [])

    def test_box_that_left_its_server_while_serving_its_own_certificate_asks_for_a_name_once_paired_again(self):
        host = socket.gethostname()
        self.keep_web_certificate()
        common.install_web_certificate(box.box_made_certificate([host, f'{host}.local', OWN_NAME]))
        common.store_box_configuration(viin_box_address=OWN_NAME)
        common.layer('pairing').clear_viindoo_configuration()
        common.store_box_configuration(remote_server=self.name_server.url, token=CREDENTIAL)
        self.service_starts()

        self.at(FIRST_QUESTION)

        self.assertEqual(len(self.name_server.questions), 1)

    def test_question_that_fails_on_the_box_after_an_address_change_is_not_tried_again_at_every_wake(self):
        address = self.enterContext(mock.patch.object(common.core('tools.helpers'), 'get_ip', return_value=BOX_ADDRESS))
        self.service_starts()
        self.at(FIRST_QUESTION)
        address.return_value = OTHER_ADDRESS
        card_full = self.enterContext(mock.patch.object(common.layer('trust'), 'write_system_files',
                                                        side_effect=OSError('the card is not writable')))

        for minute in range(1, 8):
            self.at(FIRST_QUESTION + minute * MINUTE)

        self.assertEqual(card_full.call_count, 1)
        self.assertEqual(len(self.name_server.questions), 1)
        self.assertEqual(self.new_key_files(), [])


class TestRequest(ViindooNameCase):

    def test_request_is_for_the_given_name_alone_on_a_new_2048_bit_rsa_key_signed_with_sha256(self):
        self.named()

        request = x509.load_pem_x509_csr(self.name_server.questions[1]['csr'].encode())

        key = request.public_key()
        self.assertTrue(request.is_signature_valid)
        self.assertIsInstance(key, rsa.RSAPublicKey)
        self.assertEqual((key.key_size, key.public_numbers().e), (2048, 65537))
        self.assertIsInstance(request.signature_hash_algorithm, hashes.SHA256)
        self.assertEqual([(attribute.oid, attribute.value) for attribute in request.subject],
                         [(NameOID.COMMON_NAME, FQDN)])
        self.assertEqual([extension.value for extension in request.extensions],
                         [x509.SubjectAlternativeName([x509.DNSName(FQDN)])])

    def test_key_and_request_are_on_the_card_before_the_request_is_sent(self):
        on_the_card = []

        def answer(params):
            on_the_card.append((read_as_root(PENDING_KEY), read_as_root(PENDING_REQUEST)))
            return {'status': 'pending', 'fqdn': FQDN, 'retry_after': 120}

        self.name_server.answers = [self.name_server.offered, answer]
        self.service_starts()
        self.at(FIRST_QUESTION)
        self.at(FIRST_QUESTION + FOLLOW_UP)

        sent = self.name_server.questions[1]['csr']
        key, request = on_the_card[0]
        self.assertIsNotNone(key)
        self.assertEqual(spki(serialization.load_pem_private_key(key, None).public_key()),
                         spki(x509.load_pem_x509_csr(sent.encode()).public_key()))
        self.assertEqual(request.decode(), sent)

    def test_key_of_the_name_is_in_nothing_the_box_sends(self):
        named_at = self.named()
        self.at(named_at + DAY_AT_THE_LATEST)

        key = key_lines(read_as_root(NAME_KEY))
        sent = json.dumps(self.name_server.requests)
        self.assertTrue(key)
        self.assertNotIn('PRIVATE KEY', sent)
        self.assertEqual([line for line in key if line in sent], [])

    def test_key_request_and_credential_are_in_no_line_of_the_box_log(self):
        with common.box_log() as logged:
            named_at = self.named()
            self.at(named_at + DAY_AT_THE_LATEST)

        text = logged()
        key = key_lines(read_as_root(NAME_KEY))
        request = request_lines(self.name_server.questions[1]['csr'])
        self.assertTrue(key and request)
        self.assertNotIn('PRIVATE KEY', text)
        self.assertEqual([line for line in key + request if line in text], [])
        self.assertNotIn(CREDENTIAL, text)

    def test_key_of_the_name_is_in_neither_the_state_file_nor_the_diagnostic_file(self):
        self.named()

        key = key_lines(read_as_root(NAME_KEY))
        response = self.request(box.DIAGNOSTIC)
        diagnostic = response.get_data(as_text=True)
        state = box.STATE_FILE.read_text()
        self.assertEqual(response.status_code, 200)
        self.assertIn(FQDN, state)
        self.assertTrue(key)
        self.assertEqual([line for line in key if line in diagnostic + state], [])
        self.assertNotIn('PRIVATE KEY', diagnostic + state)

    def test_key_waiting_for_its_certificate_is_readable_by_root_alone(self):
        self.name_server.answers = [self.name_server.offered, {'status': 'pending', 'fqdn': FQDN, 'retry_after': 120}]
        self.service_starts()
        self.at(FIRST_QUESTION)
        self.at(FIRST_QUESTION + FOLLOW_UP)

        holding = self.new_key_files()
        own_owner = common.as_root('stat', '-c', '%U', OWN_KEY).stdout.split()
        self.assertIn(PENDING_KEY, holding)
        self.assertEqual({path: common.as_root('stat', '-c', '%a %U', path).stdout.split() for path in holding},
                         {path: [b'600', *own_owner] for path in holding})

    def test_pair_of_the_name_keeps_certificates_and_key_in_files_of_their_own_and_the_key_for_root_alone(self):
        self.named()

        leaf = self.name_server.issued[-1]
        generation = pair_state()[0]
        chain, key = read_as_root(NAME_CERTIFICATE), read_as_root(NAME_KEY)
        certificates = common.layer('trust').load_certificates(chain)
        self.assertNotIn(b'PRIVATE KEY', chain)
        self.assertEqual(common.fingerprint(certificates[0]), common.fingerprint(leaf))
        self.assertEqual(key.count(b'-----BEGIN'), 1)
        self.assertIn(b'-----BEGIN PRIVATE KEY-----', key)
        self.assertNotIn(b'CERTIFICATE', key)
        self.assertTrue(key_matches_certificate(chain, key))
        self.assertEqual(self.new_key_files(), [f'{NAME_PAIR}/{generation}/privkey.pem'])
        self.assertEqual(files_under_the_certificates_of_the_system_belonging_to(leaf), [])
        for directory in (NAME_PAIR, NAME_PAIR_ON_THE_CARD):
            modes = {path: common.as_root('stat', '-c', '%a %U', path).stdout.split() for path in (
                directory, f'{directory}/{generation}', f'{directory}/{generation}/privkey.pem',
                f'{directory}/{generation}/fullchain.pem')}
            self.assertEqual(modes, {
                directory: [b'700', b'root'], f'{directory}/{generation}': [b'700', b'root'],
                f'{directory}/{generation}/privkey.pem': [b'600', b'root'],
                f'{directory}/{generation}/fullchain.pem': [b'644', b'root']})


class TestInstall(ViindooNameCase):

    def test_certificate_issued_is_served_for_the_name_and_the_own_one_for_the_address(self):
        self.named()

        self.assertOwnCertificateUntouched()
        self.assertServesTheName(self.name_server.issued[-1])

    def test_name_reachable_on_the_site_is_reported_at_the_very_question_that_installs_it(self):
        self.service_starts()
        self.at(FIRST_QUESTION)

        with self.assertNoLogs(self.public_name._logger, logging.WARNING):
            self.at(FIRST_QUESTION + FOLLOW_UP)

        line = self.name_line()
        self.assertEqual(self.managed_name(), (FQDN, self.name_server.url))
        self.assertEqual((line['state'], line['reason']), ('ok', 'in_use'))
        self.assertEqual(self.name_record()['failing_since'], '')

    def test_web_server_that_never_presents_the_certificate_of_a_new_name_fails_the_check_within_a_minute(self):
        self.service_starts()
        self.at(FIRST_QUESTION)

        done = self.done_in_time_while_the_web_server_ignores_loads(FIRST_QUESTION + FOLLOW_UP)

        line = self.name_line()
        self.assertTrue(done)
        self.assertEqual((line['state'], line['reason'], line['details'].get('check')),
                         ('warning', 'name_check_failed', 'wrong_certificate'))
        self.assertEqual(self.managed_name(), ('', ''))

    def test_network_that_refuses_the_name_keeps_the_box_reported_by_its_address_and_names_the_zone(self):
        for case, addresses in {'no answer for the name': [], 'another address': [OTHER_ADDRESS]}.items():
            with self.subTest(case):
                self.forget_the_name()
                self.site[FQDN] = addresses
                self.named()

                line = self.name_line()
                self.assertEqual(self.managed_name(), ('', ''))
                self.assertEqual((line['state'], line['reason'], line['details'].get('zone')),
                                 ('warning', 'network_refuses_name', ZONE))
                self.assertOwnCertificateUntouched()

    def test_name_the_resolver_of_the_site_gives_ten_minutes_after_the_install_is_reported_within_fifteen(self):
        self.site[FQDN] = []
        installed_at = self.named()
        questions = len(self.name_server.questions)

        for minutes in range(1, 16):
            if minutes == 10:
                self.site[FQDN] = [BOX_ADDRESS]
            self.at(installed_at + minutes * MINUTE)
            if minutes == 9:
                reported_before_the_resolver_gave_it = self.managed_name()

        self.assertEqual(reported_before_the_resolver_gave_it, ('', ''))
        self.assertEqual(self.managed_name(), (FQDN, self.name_server.url))
        self.assertEqual(self.name_line()['state'], 'ok')
        self.assertEqual(len(self.name_server.questions), questions)

    def test_certificate_that_does_not_match_the_request_or_is_not_valid_now_is_not_installed(self):
        mismatches = {
            'another key': (self.name_server.for_another_key, 'key_mismatch'),
            'another name': (self.name_server.requested_certificate(names=[OTHER_FQDN]), 'name_mismatch'),
            'ended': (self.name_server.requested_certificate(
                valid_from=common.utc_now() - 100 * DAY, valid_until=common.utc_now() - 10 * DAY), 'expired'),
            'not yet valid': (self.name_server.requested_certificate(
                valid_from=common.utc_now() + 10 * DAY, valid_until=common.utc_now() + 100 * DAY), 'not_yet_valid'),
        }
        for case, (answer, problem) in mismatches.items():
            with self.subTest(case):
                self.forget_the_name()
                self.name_server.answers = [self.name_server.offered, answer]
                self.named()

                line = self.name_line()
                self.assertEqual((line['state'], line['reason'], line['details'].get('problem')),
                                 ('warning', 'not_installed', problem))
                self.assertIsNone(read_as_root(NAME_CERTIFICATE))
                self.assertServesNoName()
                self.assertEqual(self.managed_name(), ('', ''))
                self.assertOwnCertificateUntouched()

    def test_certificate_that_does_not_lead_to_the_authorities_of_the_box_is_not_installed_and_says_they_are_out_of_date(
            self):
        other_root = authority('Authority the box does not carry')
        self.name_server.answers = [self.name_server.offered, self.name_server.requested_certificate(
            issuing=authority('Issuing authority the box does not carry', issuer=other_root))]
        self.named()

        line = self.name_line()
        self.assertEqual((line['state'], line['reason'], line['action']),
                         ('warning', 'authorities_out_of_date', 'write_new_image'))
        self.assertIsNone(read_as_root(NAME_CERTIFICATE))
        self.assertServesNoName()
        self.assertEqual(self.managed_name(), ('', ''))
        self.assertOwnCertificateUntouched()

    def test_certificate_outside_its_dates_when_the_server_gave_no_time_says_the_box_cannot_tell_its_clock_from_it(
            self):
        self.name_server.answers = [self.name_server.offered, self.name_server.requested_certificate(
            valid_from=common.utc_now() - 100 * DAY, valid_until=common.utc_now() - 10 * DAY)]

        with self.server_that_gives_no_time():
            self.named()

        line = self.name_line()
        self.assertEqual((line['state'], line['reason'], line['action']),
                         ('warning', 'validity_unconfirmed', 'set_clock_from_server'))
        self.assertIsNone(read_as_root(NAME_CERTIFICATE))
        self.assertServesNoName()
        self.assertEqual(self.managed_name(), ('', ''))

    def test_certificate_valid_by_the_server_clock_only_says_the_box_clock_is_wrong(self):
        self.clock.now = common.utc_now() - 30 * DAY
        self.named()

        line = self.name_line()
        self.assertEqual((line['state'], line['reason']), ('warning', 'box_clock_wrong'))
        self.assertServesNoName()
        self.assertIsNone(read_as_root(NAME_CERTIFICATE))
        self.assertEqual(pair_state(), (None, [], False))
        self.assertEqual(self.managed_name(), ('', ''))

    def test_question_after_a_certificate_not_taken_comes_the_next_day_with_a_new_key(self):
        self.name_server.answers = [self.name_server.offered,
                                    self.name_server.requested_certificate(names=[OTHER_FQDN])]
        named_at = self.named()

        self.at(named_at + DAY - SECOND)
        before_the_day = len(self.name_server.questions)
        self.at(named_at + DAY_AT_THE_LATEST)

        keys = self.name_server.requested_keys()
        self.assertEqual((before_the_day, len(keys)), (2, 3))
        self.assertIsNotNone(keys[2])
        self.assertNotEqual(keys[2], keys[1])

    def test_name_block_the_web_server_refuses_is_removed_and_the_web_server_keeps_running(self):
        web_server = read_as_root(WEB_SERVER_PID)

        with refused_name_template(), web_server_loads() as loads:
            self.named()

        line = self.name_line()
        self.assertEqual([load for load in loads if not load[1]], [])
        self.assertEqual(files_of_the_name(), {NAME_CERTIFICATE: None, NAME_KEY: None})
        self.assertEqual(subprocess.run(['sudo', '-n', 'nginx', '-t', '-q'], capture_output=True).returncode, 0)
        self.assertEqual(read_as_root(WEB_SERVER_PID), web_server)
        self.assertEqual((line['reason'], line['details'].get('problem')), ('not_installed', 'web_server_refused'))
        self.assertEqual(self.managed_name(), ('', ''))
        self.assertOwnCertificateUntouched()

    def test_first_name_the_web_server_fails_to_load_leaves_nothing_of_the_name(self):
        with web_server_load_failing_once() as failed:
            self.named()

        line = self.name_line()
        self.assertTrue(failed)
        self.assertEqual(files_of_the_name(), {NAME_CERTIFICATE: None, NAME_KEY: None})
        self.assertServesNoName()
        self.assertEqual((line['reason'], line['details'].get('problem')), ('not_installed', 'web_server_refused'))
        self.assertEqual(self.managed_name(), ('', ''))
        self.assertOwnCertificateUntouched()


class TestCertificateNotTaken(ViindooNameCase):
    """What the next question carries after a certificate could not be installed: a fault of the box keeps the
    request, so the server answers it with the same certificate and orders nothing; a fault of the certificate
    needs a new request."""

    def box_faults(self):
        """{case: (context of the install question that fails, problem the line of the name gives)}."""
        trust = common.layer('trust')
        root_read = trust.read_as_root

        def key_unreadable(path):
            if path == PENDING_KEY:
                raise trust.Unreadable('the key could not be read')
            return root_read(path)

        return {
            'web server refuses the block': (refused_name_template, 'web_server_refused'),
            'no template for the block': (missing_name_template, 'no_site_template'),
            'pair cannot be written': (lambda: mock.patch.object(trust, 'replace_pair', side_effect=OSError('full')),
                                       'not_written'),
            'key cannot be read': (lambda: mock.patch.object(trust, 'read_as_root', side_effect=key_unreadable),
                                   'not_readable'),
        }

    def test_certificate_the_box_could_not_install_for_its_own_fault_is_asked_for_again_with_the_same_request(self):
        for case, (fault, problem) in self.box_faults().items():
            with self.subTest(case):
                self.forget_the_name()
                self.name_server.requests.clear()
                del self.name_server.issued[:]
                self.name_server._given.clear()
                self.service_starts()
                self.at(FIRST_QUESTION)
                with fault():
                    self.at(FIRST_QUESTION + FOLLOW_UP)
                self.assertEqual(self.name_line()['details'].get('problem'), problem)
                orders = len(self.name_server.issued)

                self.at(FIRST_QUESTION + FOLLOW_UP + DAY_AT_THE_LATEST)

                questions = self.name_server.questions
                self.assertEqual(orders, 1)
                self.assertEqual(len(self.name_server.issued), orders)
                self.assertEqual(questions[-1]['csr'], questions[1]['csr'])
                self.assertServesTheName(self.name_server.issued[0])

    def test_certificate_a_new_request_cannot_cure_is_asked_for_again_with_the_same_request(self):
        other_root = authority('Authority the box does not carry')
        unknown_authority = self.name_server.requested_certificate(
            issuing=authority('Issuing authority the box does not carry', issuer=other_root))
        ended = self.name_server.requested_certificate(
            valid_from=common.utc_now() - 100 * DAY, valid_until=common.utc_now() - 10 * DAY)
        cases = {
            'authority the box does not carry': (unknown_authority, contextlib.nullcontext),
            'dates wrong and no time from the server': (ended, self.server_that_gives_no_time),
        }
        for case, (answer, circumstance) in cases.items():
            with self.subTest(case):
                self.forget_the_name()
                self.name_server.requests.clear()
                del self.name_server.issued[:]
                self.name_server._given.clear()
                self.name_server.answers = [self.name_server.offered, answer]
                with circumstance():
                    named_at = self.named()

                    self.at(named_at + DAY_AT_THE_LATEST)

                questions = self.name_server.questions
                self.assertEqual(questions[-1]['csr'], questions[1]['csr'])
                self.assertIsNotNone(questions[1]['csr'])

    def test_box_whose_clock_was_wrong_installs_the_same_certificate_once_the_clock_is_right(self):
        self.clock.now = common.utc_now() - 30 * DAY
        named_at = self.named()
        orders = len(self.name_server.issued)
        self.assertEqual(self.name_line()['reason'], 'box_clock_wrong')

        self.started += 30 * DAY
        self.at(named_at + DAY_AT_THE_LATEST)

        questions = self.name_server.questions
        self.assertEqual(len(self.name_server.issued), orders)
        self.assertEqual(questions[-1]['csr'], questions[1]['csr'])
        self.assertServesTheName(self.name_server.issued[0])

    def test_certificate_that_is_not_the_one_asked_for_is_followed_by_a_new_request_and_a_new_order(self):
        faults = {
            'another key': self.name_server.for_another_key,
            'another name': self.name_server.requested_certificate(names=[OTHER_FQDN]),
            'ended': self.name_server.requested_certificate(
                valid_from=common.utc_now() - 100 * DAY, valid_until=common.utc_now() - 10 * DAY),
            'not yet valid': self.name_server.requested_certificate(
                valid_from=common.utc_now() + 10 * DAY, valid_until=common.utc_now() + 100 * DAY),
        }
        for case, answer in faults.items():
            with self.subTest(case):
                self.forget_the_name()
                self.name_server.requests.clear()
                del self.name_server.issued[:]
                self.name_server._given.clear()
                self.name_server.answers = [self.name_server.offered, answer]
                named_at = self.named()
                orders = len(self.name_server.issued)

                self.at(named_at + DAY_AT_THE_LATEST)

                keys = self.name_server.requested_keys()
                self.assertNotEqual(keys[-1], keys[1])
                self.assertEqual(len(self.name_server.issued), orders + 1)


class TestPairOfTheName(ViindooNameCase):

    def test_power_cut_while_a_new_pair_takes_its_place_leaves_in_every_directory_the_old_pair_or_the_new_one(self):
        trust = common.layer('trust')
        self.named()
        old_generation = pair_state()[0]
        old = common.fingerprint(self.name_server.issued[-1])
        new = common.make_certificate(FQDN)
        for commands in range(60):
            with self.subTest(commands_run_as_root_before_the_cut=commands):
                with common.power_cut_after(commands) as power_was_cut, contextlib.suppress(common.PowerCut):
                    trust.replace_pair((NAME_PAIR, NAME_PAIR_ON_THE_CARD), new.pem, new.key_pem)
                if not power_was_cut():
                    self.assertEqual(common.fingerprint(x509.load_pem_x509_certificate(
                        read_as_root(NAME_CERTIFICATE))), new.fingerprint)
                    break

                for directory in (NAME_PAIR, NAME_PAIR_ON_THE_CARD):
                    chain, key = read_as_root(f'{directory}/current/fullchain.pem'), read_as_root(
                        f'{directory}/current/privkey.pem')
                    self.assertTrue(key_matches_certificate(chain, key))
                    self.assertIn(common.fingerprint(x509.load_pem_x509_certificate(chain)), (old, new.fingerprint))
                self.assertEqual(subprocess.run(['sudo', '-n', 'nginx', '-t', '-q'], capture_output=True).returncode, 0)
                trust.restore_pair((NAME_PAIR, NAME_PAIR_ON_THE_CARD), old_generation)
                trust.prune_pairs((NAME_PAIR, NAME_PAIR_ON_THE_CARD))
        else:
            self.fail('The change did not end within 60 commands run as root')

    def test_start_after_a_change_that_was_interrupted_removes_what_it_left_and_serves_the_pair_current_names(self):
        self.named()
        served = self.name_server.issued[-1]
        generation = pair_state()[0]
        leftover = f'{NAME_PAIR}/g-0123456789abcdef'
        common.as_root('install', '-d', '-m', '700', leftover)
        common.as_root('sh', '-c', f'cp -- {NAME_PAIR}/{generation}/* {leftover}/ && '
                                   f'ln -sfn g-0123456789abcdef {NAME_PAIR}/current.viin-new')

        self.service_starts()

        self.assertEqual(pair_state(), (generation, [generation], False))
        self.assertEqual(self.presented_once_settled(FQDN, common.fingerprint(served)), common.fingerprint(served))
        self.assertEqual(self.managed_name(), (FQDN, self.name_server.url))

    def test_pair_that_is_refused_keeps_a_key_out_of_the_certificate_file_and_writes_nothing(self):
        trust = common.layer('trust')
        certificate = common.make_certificate(FQDN)
        refused = {
            'a key among the certificates': (certificate.pem + certificate.key_pem, certificate.key_pem),
            'a certificate where the key goes': (certificate.pem, certificate.pem),
            'two keys': (certificate.pem, certificate.key_pem + certificate.key_pem),
        }
        for case, (chain, key) in refused.items():
            with self.subTest(case):
                with self.assertRaises(ValueError):
                    trust.replace_pair((NAME_PAIR, NAME_PAIR_ON_THE_CARD), chain, key)

                self.assertEqual(pair_state(), (None, [], False))

    def web_server_accepts_its_configuration(self):
        return subprocess.run(['sudo', '-n', 'nginx', '-t', '-q'], capture_output=True).returncode == 0

    def test_link_of_the_pair_in_use_that_root_refuses_to_read_leaves_every_generation_in_place(self):
        self.named()
        generation = pair_state()[0]
        leftover = f'{NAME_PAIR}/g-0123456789abcdef'
        common.as_root('install', '-d', '-m', '700', leftover)
        before = pair_state()

        with common.failing('readlink'):
            common.layer('trust').prune_pairs((NAME_PAIR, NAME_PAIR_ON_THE_CARD))

        self.assertEqual(pair_state(), before)
        self.assertIn(generation, pair_state()[1])
        self.assertTrue(self.web_server_accepts_its_configuration())

    def test_link_of_the_pair_in_use_that_names_no_generation_leaves_every_generation_in_place(self):
        self.named()
        leftover = f'{NAME_PAIR}/g-0123456789abcdef'
        common.as_root('install', '-d', '-m', '700', leftover)
        for target in ('somewhere-else', 'g-short', f'{NAME_PAIR}/g-0123456789abcdef'):
            with self.subTest(target):
                common.as_root('ln', '-sfn', target, f'{NAME_PAIR}/current')
                before = pair_state()

                common.layer('trust').prune_pairs((NAME_PAIR, NAME_PAIR_ON_THE_CARD))

                self.assertEqual(pair_state(), before)
                self.assertIn('g-0123456789abcdef', pair_state()[1])

    def test_start_while_root_refuses_to_read_the_link_of_the_pair_keeps_the_pair_and_the_name_served(self):
        self.named()
        served = self.name_server.issued[-1]
        before = pair_state()

        with common.failing('readlink'):
            self.service_starts()

        self.assertEqual(pair_state(), before)
        self.assertTrue(self.web_server_accepts_its_configuration())
        self.assertEqual(self.presented_once_settled(FQDN, common.fingerprint(served)), common.fingerprint(served))

    def test_certificate_that_root_refuses_to_read_does_not_empty_the_pair_the_box_holds_nor_cost_a_new_order(self):
        named_at = self.named()
        before = (pair_state(), files_of_the_name())
        orders, questions = len(self.name_server.issued), len(self.name_server.questions)

        with common.failing('cat'):
            self.service_starts()
            self.at(named_at + DAY_AT_THE_LATEST)

        self.assertEqual((pair_state(), files_of_the_name()), before)
        self.assertEqual(len(self.name_server.issued), orders)
        self.assertIsNone(read_as_root(PENDING_KEY))
        self.assertEqual(self.managed_name(), (FQDN, self.name_server.url))
        self.assertEqual(len(self.name_server.questions), questions)

    def test_start_of_a_box_whose_pair_is_gone_removes_the_block_of_the_name_and_the_web_server_accepts_its_files(self):
        self.named()
        common.as_root('rm', '-rf', NAME_PAIR, NAME_PAIR_ON_THE_CARD)
        self.assertNotEqual(name_blocks(), [])
        self.assertFalse(self.web_server_accepts_its_configuration())

        self.service_starts()

        self.assertEqual(name_blocks(), [])
        self.assertTrue(self.web_server_accepts_its_configuration())
        self.assertEqual(self.managed_name(), ('', ''))


class TestFailures(ViindooNameCase):

    def test_answers_without_a_certificate_leave_the_box_on_its_own_certificate_by_address(self):
        no_certificate = {
            'provider unreachable': {'status': 'unavailable', 'reason': 'provider_unreachable'},
            'provider error': {'status': 'unavailable', 'reason': 'provider_error'},
            'box unknown': {'status': 'unavailable', 'reason': 'box_unknown'},
            'pending': {'status': 'pending', 'fqdn': FQDN, 'retry_after': 120},
            'address not private': {'status': 'refused', 'reason': 'address_not_private'},
            'limit reached': {'status': 'refused', 'reason': 'limit_reached'},
            'try later': {'status': 'refused', 'reason': 'try_later', 'retry_after': 86400},
            'server error': {'status': 'refused', 'reason': 'server_error'},
            'answer that is no answer': 502,
            'server down': None,
        }
        for case, answer in no_certificate.items():
            with self.subTest(case):
                self.forget_the_name()
                if answer is None:
                    self.name_server.close()
                self.name_server.answers = [answer, answer]
                self.service_starts()
                self.at(FIRST_QUESTION)
                self.at(FIRST_QUESTION + DAY_AT_THE_LATEST)

                self.assertServesNoName()
                self.assertEqual(self.managed_name(), ('', ''))
                self.assertOwnCertificateUntouched()

    def test_later_success_switches_to_the_name_without_any_restart(self):
        self.name_server.answers = [{'status': 'unavailable', 'reason': 'provider_unreachable'},
                                    {'status': 'pending', 'fqdn': FQDN, 'retry_after': 120}]
        web_server = read_as_root(WEB_SERVER_PID)
        self.service_starts()
        self.at(FIRST_QUESTION)
        self.at(FIRST_QUESTION + HOUR)
        self.assertServesNoName()

        self.at(FIRST_QUESTION + HOUR + 2 * MINUTE)

        self.assertServesTheName(self.name_server.issued[-1])
        self.assertEqual(self.managed_name(), (FQDN, self.name_server.url))
        self.assertEqual((common.restarts, read_as_root(WEB_SERVER_PID)), ([], web_server))

    def test_box_the_server_does_not_know_is_told_to_reset_it_then_pair_it_on_the_line_of_the_name(self):
        self.name_server.answers = [{'status': 'unavailable', 'reason': 'box_unknown'}]
        self.service_starts()

        self.at(FIRST_QUESTION)

        line = self.name_line()
        self.assertEqual((line['state'], line['reason'], line['action']), ('warning', 'box_unknown', 'reset_then_pair'))

    def test_question_refused_as_invalid_while_the_box_has_no_address_waits_and_with_an_address_is_a_refusal(self):
        address = self.enterContext(mock.patch.object(common.core('tools.helpers'), 'get_ip', return_value=None))
        self.name_server.answers = [{'status': 'refused', 'reason': 'invalid_request'}] * 2
        self.service_starts()
        self.at(FIRST_QUESTION)

        without_address = self.name_line()
        address.return_value = BOX_ADDRESS
        with_address = self.name_line()

        self.assertEqual((without_address['state'], without_address['reason']), ('neutral', 'waiting_for_address'))
        self.assertEqual((with_address['state'], with_address['reason']), ('warning', 'refused'))


class TestRenewal(ViindooNameCase):

    def test_daily_question_carries_the_key_in_use_until_a_third_of_the_validity_is_left_then_a_new_one(self):
        named_at = self.named()
        installed = self.name_server.requested_keys()[1]

        self.at(named_at + 10 * DAY)
        self.at(named_at + 61 * DAY)

        keys = self.name_server.requested_keys()
        self.assertEqual(keys[2], installed)
        self.assertNotIn(keys[3], (None, installed))
        self.assertServesTheName(self.name_server.issued[-1])

    def test_renewal_that_fails_keeps_the_name_while_its_certificate_is_valid(self):
        named_at = self.named()
        in_use = self.name_server.issued[-1]
        self.name_server.answers = [{'status': 'refused', 'reason': 'limit_reached'}] * 3

        self.at(named_at + 61 * DAY)
        self.at(named_at + 75 * DAY)

        self.assertEqual(self.name_line()['reason'], 'renewal_retrying')
        self.assertServesTheName(in_use)
        self.assertEqual(self.managed_name(), (FQDN, self.name_server.url))

    def test_renewal_that_meets_a_server_with_no_name_to_offer_is_no_failed_renewal(self):
        named_at = self.named()
        in_use = self.name_server.issued[-1]
        self.name_server.answers = [{'status': 'unavailable', 'reason': 'not_offered'}]

        self.at(named_at + 61 * DAY)

        line = self.name_line()
        self.assertEqual((line['state'], line['reason']), ('ok', 'in_use'))
        self.assertServesTheName(in_use)

    def test_renewal_the_web_server_never_presents_fails_the_check_though_the_old_one_is_trusted(self):
        named_at = self.named()

        done = self.done_in_time_while_the_web_server_ignores_loads(named_at + 61 * DAY)

        line = self.name_line()
        self.assertTrue(done)
        self.assertEqual((line['state'], line['reason'], line['details'].get('check')),
                         ('warning', 'name_check_failed', 'wrong_certificate'))
        self.assertEqual(self.managed_name(), ('', ''))

    def test_renewal_the_web_server_does_not_take_leaves_the_name_served_as_it_was(self):
        failures = {'block refused': refused_name_template, 'load that fails': web_server_load_failing_once}
        for case, failure in failures.items():
            with self.subTest(case):
                self.forget_the_name()
                named_at = self.named()
                in_use = self.name_server.issued[-1]
                before = files_of_the_name()
                generation_before = pair_state()

                with failure(), web_server_loads() as loads:
                    self.at(named_at + 61 * DAY)

                line = self.name_line()
                self.assertNotEqual(common.fingerprint(self.name_server.issued[-1]), common.fingerprint(in_use))
                self.assertEqual([load for load in loads if not load[1]], [])
                self.assertEqual(files_of_the_name(), before)
                self.assertEqual(pair_state(), generation_before)
                self.assertEqual(presented(FQDN), common.fingerprint(in_use))
                self.assertEqual(self.managed_name(), (FQDN, self.name_server.url))
                self.assertEqual(line['details'].get('problem'), 'web_server_refused')

    def test_name_whose_certificate_ended_without_renewal_is_dropped_and_the_box_says_so(self):
        named_at = self.named()
        ends = self.name_server.issued[-1].not_valid_after
        self.name_server.answers = [{'status': 'refused', 'reason': 'limit_reached'}] * 4
        self.at(named_at + 61 * DAY)

        self.at(ends - self.started + SECOND)

        line = self.name_line()
        self.assertEqual((line['state'], line['reason']), ('warning', 'expired'))
        self.assertServesNoName()
        self.assertEqual(self.managed_name(), ('', ''))
        self.assertEqual(common.restarts, [])


class TestLosingTheName(ViindooNameCase):

    def test_name_switched_off_in_viindoo_is_dropped(self):
        named_at = self.named()
        self.name_server.answers = [{'status': 'unavailable', 'reason': 'disabled'}]

        self.at(named_at + DAY_AT_THE_LATEST)

        self.assertServesNoName()
        self.assertEqual(self.new_key_files(), [])
        self.assertEqual(self.managed_name(), ('', ''))
        self.assertEqual((self.name_line()['state'], self.name_line()['reason']), ('neutral', 'disabled'))
        self.assertEqual(common.restarts, [])

    def test_name_of_a_box_the_server_no_longer_knows_is_dropped_like_one_switched_off(self):
        named_at = self.named()
        self.name_server.answers = [{'status': 'unavailable', 'reason': 'box_unknown'}]

        self.at(named_at + DAY_AT_THE_LATEST)

        self.assertServesNoName()
        self.assertEqual(self.new_key_files(), [])
        self.assertEqual(self.managed_name(), ('', ''))
        self.assertEqual(common.restarts, [])

    def test_box_the_server_no_longer_knows_asks_again_the_next_day(self):
        named_at = self.named()
        self.name_server.answers = [{'status': 'unavailable', 'reason': 'box_unknown'}]
        self.at(named_at + DAY_AT_THE_LATEST)
        asked = len(self.name_server.questions)

        self.at(named_at + DAY_AT_THE_LATEST + DAY - SECOND)
        before_the_day = len(self.name_server.questions)
        self.at(named_at + 2 * DAY_AT_THE_LATEST + HOUR)

        self.assertEqual((before_the_day, len(self.name_server.questions)), (asked, asked + 1))

    def test_request_waiting_for_its_certificate_is_removed_when_the_server_no_longer_knows_the_box(self):
        self.name_server.answers = [{'status': 'name', 'fqdn': FQDN},
                                    {'status': 'pending', 'fqdn': FQDN, 'retry_after': 120},
                                    {'status': 'unavailable', 'reason': 'box_unknown'}]
        self.service_starts()
        self.at(FIRST_QUESTION)
        self.at(FIRST_QUESTION + FOLLOW_UP)
        kept_while_waiting = self.new_key_files()

        self.at(FIRST_QUESTION + FOLLOW_UP + 121 * SECOND)

        self.assertNotEqual(kept_while_waiting, [])
        self.assertEqual(self.new_key_files(), [])
        self.assertServesNoName()
        self.assertEqual(self.managed_name(), ('', ''))
        self.assertEqual(common.restarts, [])

    def test_name_whose_check_failed_three_days_running_is_no_longer_served(self):
        named_at = self.named()
        served = self.name_server.issued[-1]
        pair = (read_as_root(NAME_CERTIFICATE), read_as_root(NAME_KEY), pair_state())
        orders = len(self.name_server.issued)
        self.site[FQDN] = []

        self.at(named_at + DAY_AT_THE_LATEST)
        self.at(named_at + 2 * DAY_AT_THE_LATEST)
        reported_after_two_days = self.managed_name()
        served_after_two_days = presented(FQDN)
        self.at(named_at + 3 * DAY_AT_THE_LATEST)

        self.assertEqual(reported_after_two_days, ('', ''))
        self.assertEqual(served_after_two_days, common.fingerprint(served))
        self.assertServesNoName()
        self.assertEqual(common.restarts, [])
        self.assertEqual((read_as_root(NAME_CERTIFICATE), read_as_root(NAME_KEY), pair_state()), pair)

        self.site[FQDN] = [BOX_ADDRESS]
        self.at(named_at + 4 * DAY_AT_THE_LATEST)

        self.assertServesTheName(served)
        self.assertEqual(self.managed_name(), (FQDN, self.name_server.url))
        self.assertEqual(len(self.name_server.issued), orders)
        self.assertEqual(self.name_server.requested_keys()[-1], self.name_server.requested_keys()[1])
        self.assertEqual((read_as_root(NAME_CERTIFICATE), pair_state()), (pair[0], pair[2]))

    def test_start_of_a_box_that_may_no_longer_hold_its_name_drops_it(self):
        other_server = self.server(NameServer, issuing=self.issuing, today=self.clock)
        changes = {
            'disconnected': {'token': ''},
            'paired with another server': {'remote_server': other_server.url},
            'given the certificate of its installer': {'viin_box_address': OWN_NAME},
        }
        for case, change in changes.items():
            with self.subTest(case):
                self.forget_the_name()
                common.store_box_configuration(remote_server=self.name_server.url, token=CREDENTIAL,
                                               viin_box_address='')
                self.named()
                common.store_box_configuration(**change)

                self.service_starts()

                self.assertServesNoName()
                self.assertEqual(self.new_key_files(), [])
                self.assertEqual(self.managed_name(), ('', ''))
                self.assertEqual(common.restarts, [])

    def leave_only_an_unreadable_pair(self):
        """Leave of the name on the box a pair whose certificate reads as none, with no record, setting or block."""
        common.as_root('sh', '-c', f'echo none > {NAME_PAIR}/$(readlink {NAME_PAIR}/current)/fullchain.pem; '
                                   f'rm -f {NAME_BLOCKS}/*.conf')
        common.store_box_configuration(**{MANAGED_NAME: '', MANAGED_NAME_SERVER: ''})
        common.layer('update').change_state(lambda state: state.pop('public_name', None))

    def test_box_not_allowed_a_name_runs_no_root_command_while_it_waits_and_removes_the_pair_it_holds_at_start(self):
        left = {
            'a pair': lambda: None,
            'a pair nothing else of the name tells of': self.leave_only_an_unreadable_pair,
        }
        changes = {'disconnected': {'token': ''},
                   'given the certificate of its installer': {'viin_box_address': OWN_NAME}}
        for case, spoil in left.items():
            for change_case, change in changes.items():
                with self.subTest(f'{case}, {change_case}'):
                    self.forget_the_name()
                    common.store_box_configuration(remote_server=self.name_server.url, token=CREDENTIAL,
                                                   viin_box_address='')
                    self.named()
                    spoil()
                    common.store_box_configuration(**change)

                    self.service_starts()
                    with common.root_commands() as commands:
                        for moment in (MINUTE, 5 * MINUTE, HOUR, DAY, 2 * DAY):
                            self.at(moment)

                    self.assertEqual(common.as_root('sh', '-c', f'ls -A {NAME_PAIR} 2>/dev/null || true').stdout, b'')
                    self.assertEqual(self.new_key_files(), [])
                    self.assertEqual(commands, [])


class TestBoxLog(ViindooNameCase):

    def test_box_log_gets_one_line_per_change_of_the_name_and_none_per_daily_question(self):
        logger = self.public_name._logger
        named_at = self.named()

        with self.assertNoLogs(logger, logging.INFO):
            for day in (1, 2, 3):
                self.at(named_at + day * DAY_AT_THE_LATEST)
        self.name_server.answers = [{'status': 'unavailable', 'reason': 'disabled'}]
        with self.assertLogs(logger, logging.INFO) as logged:
            self.at(named_at + 4 * DAY_AT_THE_LATEST)

        self.assertEqual(len(logged.records), 1)
