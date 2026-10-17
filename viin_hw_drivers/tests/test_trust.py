"""Trust: the certificate of the box, the certificate of a customer, and the servers a box may talk to."""
import contextlib
import datetime
import logging
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from unittest import mock

import certifi
import urllib3
from cryptography import x509
from cryptography.hazmat.primitives.serialization import (
    BestAvailableEncryption, Encoding, PrivateFormat, PublicFormat, load_pem_private_key)
from cryptography.x509.oid import NameOID

import common
import services_common as box

CERTIFICATE = '/viin_hw_homepage/certificate'
CUSTOMER_HOST = 'box.customer.example'
CREDENTIAL = 'suite-credential-5T8WQ2ZN'
DAY = datetime.timedelta(days=1)
# What a box that has started asks of its server: to take its report, to open its live channel, to give it
# driver files, to take its log.
REPORT = 'POST /iot/setup'
LIVE_CHANNEL = 'GET /websocket'
DRIVER_FILES = 'POST /iot/get_handlers'
BOX_LOG = 'POST /iot/log'
ROADS = (REPORT, LIVE_CHANNEL, DRIVER_FILES, BOX_LOG)
# A file in each of the two places where the box keeps what its web server reads: in memory, and on the card.
ROOT_FILES = ('/etc/ssl/private/box-suite-written.key', '/root_bypass_ramdisks/etc/ssl/private/box-suite-written.key')

NAME_PAIR = '/etc/ssl/private/viindoo-name'
WEB_SERVER_PID = Path('/run/nginx.pid')
# A file of configuration the web server reads beside the certificate of the box.
UNUSABLE_FILE = '/etc/nginx/conf.d/box-suite-unusable.conf'
# How long a test keeps the web server from reading after a first change of a pair, while a second change must
# write nothing: a change takes a fraction of it, and the box waits longer than that for the web server to read.
HELD = 2
# The address a certificate of the box was made at, and the addresses the box holds when it looks at it again.
EARLIER_ADDRESS = '10.11.12.1'
ADDRESS = '192.168.1.50'
SECOND_ADDRESS = '192.168.7.20'

_certificate_changes = []


def common_name(certificate):
    return certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value


def host_names(certificate):
    alternative = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return alternative.get_values_for_type(x509.DNSName)


def public_key(certificate):
    return certificate.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)


def name_of_this_box():
    return f'Viindoo IoT Box {common.board_serial()}'


def certificate_of_this_box_ending_in(days):
    """Install, as the certificate the box made for itself, one that ends in `days` days; return it."""
    host = socket.gethostname()
    certificate = common.make_certificate(
        name_of_this_box(), names=[host, f'{host}.local'], valid_until=common.utc_now() + days * DAY)
    common.install_web_certificate(certificate)
    return certificate


def certificate_changes():
    """The list the box started in this process adds to each time its web server gets another certificate."""
    if not _certificate_changes:
        changes = []
        common.layer('trust').on_certificate_change(lambda: changes.append(1))
        _certificate_changes.append(changes)
    del _certificate_changes[0][:]
    return _certificate_changes[0]


def accepted(authorities, server):
    """Whether a connection that verifies against the PEM file `authorities` accepts the certificate of `server`."""
    context = ssl.create_default_context(cafile=authorities)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        with socket.create_connection((server.host, server.port), timeout=10) as plain:
            with context.wrap_socket(plain, server_hostname=server.host):
                return True
    except ssl.SSLCertVerificationError:
        return False


def file_of_core_that_calls_a_server():
    """Stands for a driver file of core: a module that names urllib3 and calls a server through it, unverified."""
    file = types.ModuleType('box_suite_driver_file')
    file.urllib3 = urllib3
    file.send = lambda url: file.urllib3.PoolManager(cert_reqs='CERT_NONE').request('POST', url, retries=False)
    return file


def start_a_box_beside_its_server(certificate, key, port, public_authority=''):
    """Start the box of the configuration while its server listens on `port` of this machine.

    Meant for `common.in_new_process`. The server shows the certificate of the PEM texts
    `certificate` and `key` and gives a box that reports a live channel. `public_authority` is
    the PEM text of an authority that counts, on that box, among those every browser trusts.
    Gives 'calls', what the server received once the box had taken each of ROADS or 30 seconds
    had passed, and 'credentials', those of the reports among them.
    """
    if public_authority:
        common.make_public_from_the_start(public_authority)
    common.give_a_hardware_address()
    server = common.FakeServer(
        certificate=common.certificate_from(certificate, key), port=port, channel='box-suite-channel')
    common.start()
    with contextlib.suppress(AssertionError):
        common.wait_until(lambda: set(ROADS) <= set(server.calls), 'the box to call its server')
    return {'calls': server.calls, 'credentials': sorted(common.reported_credentials(server))}


def start_a_box_whose_memory_takes_no_new_file():
    """Start the box of the configuration on a machine whose memory-backed storage takes no new file.

    Meant for `common.in_new_process`. Gives 'answers', the status of the address that tells a
    box started, 'device_manager_runs' and 'device_handlers', how many kinds of devices core
    has loaded the handlers of.
    """
    storage = Path(tempfile.mkdtemp(prefix='box-suite-'))
    storage.chmod(0o500)
    tempfile.tempdir = str(storage)
    report = common.start_report(probes=[common.PING])
    main = common.core('main')
    return {
        'answers': report['answers'],
        'device_manager_runs': main.manager.is_alive(),
        'device_handlers': len(main.interfaces),
    }


def covered_names(certificate):
    """The host names and the addresses a certificate is issued for, as text."""
    alternative = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return {str(name) for name in alternative.get_values_for_type(x509.DNSName)
            } | {str(address) for address in alternative.get_values_for_type(x509.IPAddress)}


def web_certificate_covers(*names):
    """Whether the certificate installed for the web server is issued for all of `names`."""
    try:
        return set(names) <= covered_names(common.web_certificate())
    except (OSError, ValueError):
        return False


def certificate_made_for_the_box_at(address, **options):
    host = socket.gethostname()
    return box.box_made_certificate([host, f'{host}.local', address], **options)


def start_a_box_whose_certificate_was_made_at_another_address(earlier, address):
    """Start the box of the configuration, holding `address`, while the web server presents a certificate of the
    box that was made at the address `earlier`.

    Meant for `common.in_new_process`. Gives 'names', those the certificate has once it covers `address`.
    """
    common.install_web_certificate(certificate_made_for_the_box_at(earlier))
    with box.network({'eth0': address}):
        common.start()
        common.wait_until(lambda: web_certificate_covers(address), 'the box to make a certificate for its address')
    return {'names': sorted(covered_names(common.web_certificate()))}


def follow_the_addresses_of_a_running_box(first, second):
    """Start the box of the configuration holding the address `first`, leave it with it for a while, then give it
    the address `second` as well.

    Meant for `common.in_new_process`. The wait of the box between two looks at its addresses is shortened
    to a tenth of a second. Gives 'unchanged', whether its certificate stayed the same through five looks
    that found no new address, 'names', those of the certificate once it covers `second`, and 'first_wait',
    the seconds the box waited before its first look at its addresses.
    """
    waits, addresses, sleep = [], {'eth0': first}, time.sleep

    def wait(seconds):
        if type(threading.current_thread()).__name__ == 'AddressWatcher':
            waits.append(seconds)
            return sleep(0.1)
        return sleep(seconds)

    time.sleep = wait
    with box.network(addresses):
        common.start()
        common.wait_until(lambda: web_certificate_covers(first), 'the box to make a certificate for its address')
        made = common.fingerprint(common.web_certificate())
        looked = len(waits)
        common.wait_until(lambda: len(waits) >= looked + 5, 'the box to look at its addresses five times')
        unchanged = common.fingerprint(common.web_certificate()) == made
        addresses['eth1'] = second
        common.wait_until(
            lambda: web_certificate_covers(first, second), 'the box to make a certificate for the new address')
    return {'unchanged': unchanged, 'names': sorted(covered_names(common.web_certificate())), 'first_wait': waits[0]}


def follow_the_looks_of_a_box_at_its_certificate():
    """Start a box whose start can run no command as root, then follow the looks it takes at its certificate.

    Meant for `common.in_new_process`. While the box waits for its second look, its certificate
    is replaced by one of its own that ends in 59 days. Gives 'after_the_first_look', the common
    name of the certificate installed when that wait began, 'waited', the seconds of that wait,
    and 'renewed', whether the certificate that neared its end was replaced once the second
    look was over.
    """
    waits, seen, looked_twice = [], {}, threading.Event()
    sleep = time.sleep

    def wait(seconds):
        trust = sys.modules.get(f'{common.LAYER}.trust')
        # A wait of an hour or more, on the thread that keeps the certificate, is the wait for its next look.
        if seconds < 3600 or not (trust and isinstance(threading.current_thread(), trust.CertificateKeeper)):
            return sleep(seconds)
        waits.append(seconds)
        if len(waits) == 1:
            seen['after_the_first_look'] = common_name(common.web_certificate())
            seen['nearing_its_end'] = certificate_of_this_box_ending_in(59).fingerprint
            return None
        looked_twice.set()
        return threading.Event().wait()

    time.sleep = wait
    with common.failing('sudo'):
        common.start()
    looked_twice.wait(60)
    installed = common.web_certificate()
    return {
        'after_the_first_look': seen.get('after_the_first_look'),
        'waited': waits[0] if waits else None,
        'renewed': (looked_twice.is_set() and common.fingerprint(installed) != seen.get('nearing_its_end')
                    and installed.not_valid_after - common.utc_now() > 819 * DAY),
    }


def first_writing_of_the_pair_beside_the_restart_of_the_web_server():
    """Start a box whose start can run no command as root, so that its keeper writes the pair the web server reads
    at its first look; give when the restart of the web server, core's first act, and that writing began.

    Meant for `common.in_new_process`. Gives 'restart', [began, ended] of core's restart of the web server, and
    'writing', the moment the keeper began writing the pair, as readings of the same clock.
    """
    moments, pair = {}, (common.WEB_CERTIFICATE.name, common.WEB_KEY.name)

    def timed(original):
        def run(command, *arguments, **options):
            words = [str(word) for word in command] if isinstance(command, (list, tuple)) else []
            began = time.monotonic()
            try:
                return original(command, *arguments, **options)
            finally:
                if words[-3:] == ['service', 'nginx', 'restart']:
                    moments.setdefault('restart', [began, time.monotonic()])
                elif (threading.current_thread().name == 'ViinCertificateKeeper' and words[2:3] in (['install'], ['mv'])
                      and any(Path(word).name.startswith(pair) for word in words)):
                    moments.setdefault('writing', began)

        return run

    subprocess.run, subprocess.check_call = timed(subprocess.run), timed(subprocess.check_call)
    with common.failing('sudo'):
        common.start()
    common.wait_until(lambda: {'restart', 'writing'} <= set(moments), 'the restart and the first writing', 90)
    return moments


class TestCertificateOfANewBox(common.BoxStateCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(common.restore_web_certificate, common.save_web_certificate())

    @classmethod
    def two_starts(cls):
        """A box that holds the certificate of the image starts, then starts again; once for the class.

        Gives that certificate ('shipped'), the one installed after the first start ('made') with the
        fingerprint of what the web server presented then ('served'), and the one installed after
        the second start ('kept').
        """
        if '_two_starts' not in vars(cls):
            shipped = common.make_certificate(common.SHIPPED_COMMON_NAME)
            common.install_web_certificate(shipped)
            common.in_new_process(common.start_report)
            made, served = common.web_certificate(), common.served_certificate()
            common.in_new_process(common.start_report)
            cls._two_starts = {
                'shipped': shipped.certificate, 'made': made, 'served': served, 'kept': common.web_certificate()}
        return cls._two_starts

    def test_box_serves_a_key_of_its_own_from_its_first_start(self):
        box = self.two_starts()

        self.assertNotEqual(public_key(box['made']), public_key(box['shipped']))
        self.assertEqual(box['served'], common.fingerprint(box['made']))

    def test_certificate_of_the_box_names_its_board(self):
        self.assertEqual(common_name(self.two_starts()['made']), name_of_this_box())

    def test_certificate_of_the_box_is_issued_for_its_host_names(self):
        host = socket.gethostname()

        self.assertLessEqual({host, f'{host}.local'}, set(host_names(self.two_starts()['made'])))

    def test_certificate_of_the_box_lasts_820_days(self):
        lifetime = self.two_starts()['made'].not_valid_after - common.utc_now()

        self.assertLess(819 * DAY, lifetime)
        self.assertLessEqual(lifetime, 820 * DAY)

    def test_box_keeps_its_certificate_when_it_starts_again(self):
        box = self.two_starts()

        self.assertEqual(common.fingerprint(box['kept']), common.fingerprint(box['made']))


class TestRenewal(common.BoxCase):

    def setUp(self):
        super().setUp()
        self.keep_web_certificate()

    def test_certificate_of_the_box_is_renewed_60_days_before_its_end(self):
        ending = certificate_of_this_box_ending_in(59)

        common.layer('trust').refresh_box_certificate()

        renewed = common.web_certificate()
        self.assertNotEqual(common.fingerprint(renewed), ending.fingerprint)
        self.assertLess(819 * DAY, renewed.not_valid_after - common.utc_now())
        common.wait_until(
            lambda: common.served_certificate() == common.fingerprint(renewed),
            'the web server to present the renewed certificate')

    def test_certificate_of_the_box_is_kept_until_60_days_before_its_end(self):
        lasting = certificate_of_this_box_ending_in(61)

        common.layer('trust').refresh_box_certificate()

        self.assertEqual(common.fingerprint(common.web_certificate()), lasting.fingerprint)


class TestCertificateOfTheBoxCoversItsAddresses(common.BoxCase):

    def setUp(self):
        super().setUp()
        self.keep_web_certificate()

    def refresh(self, *held):
        """Have the box look at its certificate while it holds the addresses `held`; True when it made another."""
        with box.network({f'eth{index}': address for index, address in enumerate(held)}):
            return common.layer('trust').refresh_box_certificate()

    def test_certificate_made_at_another_address_is_replaced_by_one_for_the_host_names_and_the_addresses_held(self):
        host = socket.gethostname()
        common.install_web_certificate(certificate_made_for_the_box_at(EARLIER_ADDRESS))

        changed = self.refresh(ADDRESS, SECOND_ADDRESS)

        self.assertIs(changed, True)
        self.assertLessEqual({host, f'{host}.local', ADDRESS, SECOND_ADDRESS}, covered_names(common.web_certificate()))
        common.wait_until(
            lambda: common.served_certificate() == common.fingerprint(common.web_certificate()),
            'the web server to present the new certificate')

    def test_certificate_that_covers_the_names_and_every_address_held_is_kept(self):
        host = socket.gethostname()
        covering = box.box_made_certificate([host, f'{host}.local', ADDRESS, SECOND_ADDRESS])
        common.install_web_certificate(covering)

        changed = self.refresh(ADDRESS, SECOND_ADDRESS)

        self.assertIs(changed, False)
        self.assertEqual(common.fingerprint(common.web_certificate()), covering.fingerprint)

    def test_certificate_of_the_customer_is_kept_whatever_address_the_box_holds(self):
        customer = common.make_certificate(CUSTOMER_HOST, valid_until=common.utc_now() + 400 * DAY)
        common.install_web_certificate(customer)

        changed = self.refresh(ADDRESS)

        self.assertIs(changed, False)
        self.assertEqual(common.fingerprint(common.web_certificate()), customer.fingerprint)

    def test_pair_of_the_viindoo_name_is_not_touched_when_the_box_makes_its_certificate(self):
        stand_in = f'{NAME_PAIR}/current/fullchain.pem'
        if common.as_root('sh', '-c', f'test -e {NAME_PAIR} && echo held || true').stdout.strip():
            self.skipTest('a pair of the Viindoo name is installed on this box')
        common.as_root('install', '-D', '-m', '600', '/dev/stdin', stand_in, data=b'stand-in for the name pair')
        self.addCleanup(common.as_root, 'rm', '-rf', NAME_PAIR)
        common.install_web_certificate(certificate_made_for_the_box_at(EARLIER_ADDRESS))

        self.assertIs(self.refresh(ADDRESS), True)

        kept = common.as_root('sh', '-c', f'cat {stand_in} 2>/dev/null || true').stdout
        self.assertEqual(kept, b'stand-in for the name pair')


class TestCertificateOfTheBoxAtItsStartAndWhileItRuns(common.BoxStateCase):

    def setUp(self):
        super().setUp()
        self.keep_web_certificate()

    def test_box_that_starts_holding_an_address_its_certificate_was_not_made_at_makes_one_for_it(self):
        host = socket.gethostname()

        made = common.in_new_process(
            start_a_box_whose_certificate_was_made_at_another_address, earlier=EARLIER_ADDRESS, address=ADDRESS)

        self.assertLessEqual({host, f'{host}.local', ADDRESS}, set(made['names']))

    def test_box_that_runs_makes_a_certificate_soon_after_a_new_address_appears_and_none_while_none_does(self):
        host = socket.gethostname()

        followed = common.in_new_process(follow_the_addresses_of_a_running_box, first=ADDRESS, second=SECOND_ADDRESS)

        self.assertIs(followed['unchanged'], True)
        self.assertLessEqual({host, f'{host}.local', ADDRESS, SECOND_ADDRESS}, set(followed['names']))
        self.assertLessEqual(followed['first_wait'], 60)


class TestLooksOfTheBoxAtItsCertificate(common.BoxStateCase):

    def looks(self):
        """What `follow_the_looks_of_a_box_at_its_certificate` gives of a box that holds the certificate of the
        image, once for the tests of the class."""
        cls = type(self)
        if '_looks' not in vars(cls):
            self.keep_web_certificate()
            common.install_web_certificate(common.make_certificate(common.SHIPPED_COMMON_NAME))
            cls._looks = common.in_new_process(follow_the_looks_of_a_box_at_its_certificate)
        return cls._looks

    def test_box_whose_start_could_not_make_its_certificate_makes_it_at_its_first_look(self):
        self.assertEqual(self.looks()['after_the_first_look'], name_of_this_box())

    def test_box_waits_24_hours_between_two_looks(self):
        self.assertEqual(self.looks()['waited'], 24 * 3600)

    def test_certificate_that_nears_its_end_while_the_box_runs_is_renewed_at_the_next_look(self):
        self.assertIs(self.looks()['renewed'], True)


class TestFirstLookOfTheBoxAtItsCertificate(common.BoxStateCase):

    def test_first_look_writes_nothing_the_web_server_reads_until_core_has_restarted_the_web_server(self):
        self.keep_web_certificate()
        common.install_web_certificate(common.make_certificate(common.SHIPPED_COMMON_NAME))
        # On a box the web server runs before the box service starts: one that runs already is not the restart.
        common.serve_web_certificate()

        moments = common.in_new_process(first_writing_of_the_pair_beside_the_restart_of_the_web_server)

        self.assertGreaterEqual(moments['writing'], moments['restart'][1], moments)


class TestCertificateTheWebServerRefusedToTake(common.BoxCase):

    def test_certificate_the_web_server_refused_to_take_is_served_at_the_next_look_though_no_file_changes(self):
        trust = common.layer('trust')
        self.keep_web_certificate()
        # A look that serves what the box holds leaves nothing waiting for the web server after the test.
        self.addCleanup(trust.refresh_box_certificate)
        certificate_of_this_box_ending_in(59)
        common.serve_web_certificate()
        with common.failing('nginx'), self.assertRaises((OSError, subprocess.SubprocessError)):
            trust.refresh_box_certificate()

        changed = trust.refresh_box_certificate()

        self.assertIs(changed, False)
        common.wait_until(
            lambda: common.served_certificate() == common.fingerprint(common.web_certificate()),
            'the web server to present the certificate the box made')


class TestWatcherOfTheAddressesAfterTheWebServerRefusedACertificate(common.BoxStateCase):

    def looks(self):
        """What `follow_a_certificate_the_web_server_refused_to_take` gives, once for the tests of the class."""
        cls = type(self)
        if '_looks' not in vars(cls):
            self.keep_web_certificate()
            cls._looks = common.in_new_process(box.follow_a_certificate_the_web_server_refused_to_take, address=ADDRESS)
        return cls._looks

    def test_certificate_the_web_server_refused_to_take_is_served_at_the_next_look_of_the_watcher(self):
        looks = self.looks()

        self.assertIs(looks['meanwhile']['served'], False)
        self.assertIs(looks['after']['served'], True)


class TestWebServerThatCannotReadItsFilesAgain(common.BoxCase):

    def setUp(self):
        super().setUp()
        self.trust = common.layer('trust')
        self.keep_web_certificate()
        # A look that serves what the box holds leaves nothing waiting for the web server after the test.
        self.addCleanup(self.trust.refresh_box_certificate)
        common.install_web_certificate(certificate_made_for_the_box_at(EARLIER_ADDRESS))
        common.serve_web_certificate()
        self.served = common.served_certificate()

    def make_files_unusable(self):
        """Give the web server a file it reads beside the certificate and cannot use: it refuses to read its files
        again while it runs, and cannot be started on them."""
        common.as_root('install', '-m', '644', '/dev/stdin', UNUSABLE_FILE, data=b'box_suite_unknown_directive on;\n')
        self.addCleanup(common.as_root, 'rm', '-f', UNUSABLE_FILE)

    def look_at_a_new_address(self):
        with box.network({'eth0': ADDRESS}):
            return self.trust.refresh_box_certificate()

    def address_line(self):
        """State, reason and action of the browser address line of a box that gives browsers ADDRESS."""
        chosen = {'address': ADDRESS, 'source': 'address', 'core': ADDRESS}
        with mock.patch.object(common.layer('seams.identity'), 'reported_address', return_value=chosen), \
                box.network({'eth0': ADDRESS}):
            lines = common.layer('selftest').run(wait=0, refresh=True)['lines']
        line = next(line for line in lines if line['id'] == 'browser_address')
        return [line['state'], line['reason'], line['action']]

    def test_running_web_server_that_cannot_read_the_new_certificate_keeps_serving_the_one_it_served(self):
        master = WEB_SERVER_PID.read_text()
        self.make_files_unusable()

        with common.web_server_restarted_as_on_a_box(), self.assertRaises((OSError, subprocess.SubprocessError)):
            self.look_at_a_new_address()

        self.assertEqual(common.served_certificate(), self.served)
        self.assertEqual(WEB_SERVER_PID.read_text(), master)
        self.assertIs(self.trust.certificate_unserved(), True)
        self.assertEqual(self.address_line(), ['warning', 'certificate_not_served', 'restart_box'])

    def test_web_server_that_does_not_run_is_started_on_the_new_certificate(self):
        common.as_root('service', 'nginx', 'stop')
        common.wait_until(lambda: common.served_certificate() is None, 'the web server to stop')

        with common.web_server_restarted_as_on_a_box():
            changed = self.look_at_a_new_address()

        self.assertIs(changed, True)
        common.wait_until(
            lambda: common.served_certificate() == common.fingerprint(common.web_certificate()),
            'the web server to present the new certificate', timeout=10)
        self.assertIs(self.trust.certificate_unserved(), False)


class TestCertificateOfTheCustomer(common.BoxCase):

    def setUp(self):
        super().setUp()
        self.keep_web_certificate()
        self.installed = common.fingerprint(common.web_certificate())

    def assertNothingInstalled(self):
        self.assertEqual(common.fingerprint(common.web_certificate()), self.installed)
        self.assertNotIn('viin_box_address', common.box_configuration())

    def assertRefused(self, answer, reason):
        self.assertEqual((answer.get('status'), answer.get('reason')), ('refused', reason), answer)
        self.assertNothingInstalled()

    def install(self, customer):
        return self.call(CERTIFICATE, certificate=customer.pem, key=customer.key_pem, host_name=CUSTOMER_HOST)

    def test_certificate_and_its_key_are_installed_for_the_host_name_of_the_customer(self):
        customer = common.make_certificate(CUSTOMER_HOST)

        answer = self.install(customer)

        self.assertEqual(answer.get('status'), 'installed', answer)
        self.assertEqual(common.box_configuration().get('viin_box_address'), CUSTOMER_HOST)
        common.wait_until(
            lambda: common.served_certificate() == customer.fingerprint,
            'the web server to present the certificate of the customer')

    def test_key_of_another_certificate_is_refused(self):
        customer = common.make_certificate(CUSTOMER_HOST)
        another = common.make_certificate(CUSTOMER_HOST)

        answer = self.call(CERTIFICATE, certificate=customer.pem, key=another.key_pem, host_name=CUSTOMER_HOST)

        self.assertRefused(answer, 'key_mismatch')

    def test_certificate_past_its_end_is_refused(self):
        now = common.utc_now()
        customer = common.make_certificate(CUSTOMER_HOST, valid_from=now - 60 * DAY, valid_until=now - DAY)

        answer = self.install(customer)

        self.assertRefused(answer, 'expired')

    def test_certificate_not_valid_yet_is_refused(self):
        now = common.utc_now()
        customer = common.make_certificate(CUSTOMER_HOST, valid_from=now + DAY, valid_until=now + 60 * DAY)

        answer = self.install(customer)

        self.assertRefused(answer, 'not_yet_valid')

    def test_certificate_is_refused_for_a_host_name_it_does_not_cover(self):
        customer = common.make_certificate(CUSTOMER_HOST)

        answer = self.call(
            CERTIFICATE, certificate=customer.pem, key=customer.key_pem, host_name='other.customer.example')

        self.assertRefused(answer, 'host_name_mismatch')

    def test_certificate_is_refused_for_a_host_name_that_is_none(self):
        customer = common.make_certificate(CUSTOMER_HOST)

        answer = self.call(
            CERTIFICATE, certificate=customer.pem, key=customer.key_pem, host_name='box customer example')

        self.assertRefused(answer, 'invalid_host_name')

    def test_encrypted_key_is_refused(self):
        customer = common.make_certificate(CUSTOMER_HOST)
        encrypted = customer.key.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, BestAvailableEncryption(b'box-suite-passphrase')).decode()

        answer = self.call(CERTIFICATE, certificate=customer.pem, key=encrypted, host_name=CUSTOMER_HOST)

        self.assertRefused(answer, 'key_encrypted')

    def test_text_that_is_no_key_is_refused(self):
        customer = common.make_certificate(CUSTOMER_HOST)

        answer = self.call(CERTIFICATE, certificate=customer.pem, key='not a key', host_name=CUSTOMER_HOST)

        self.assertRefused(answer, 'invalid_key')

    def test_certificate_without_a_common_name_is_refused(self):
        customer = common.make_certificate('', names=[CUSTOMER_HOST])

        answer = self.install(customer)

        self.assertRefused(answer, 'common_name_missing')

    def test_text_that_holds_no_certificate_is_refused(self):
        customer = common.make_certificate(CUSTOMER_HOST)

        answer = self.call(
            CERTIFICATE, certificate='not a certificate', key=customer.key_pem, host_name=CUSTOMER_HOST)

        self.assertRefused(answer, 'invalid_certificate')

    def test_installation_the_web_server_refuses_leaves_the_box_serving_what_it_served(self):
        with common.failing('nginx'):
            answer = self.install(common.make_certificate(CUSTOMER_HOST))

        self.assertRefused(answer, 'box_error')
        common.serve_web_certificate()
        self.assertEqual(common.served_certificate(), self.installed)

    def test_power_cut_during_an_installation_leaves_the_former_pair_or_the_new_one(self):
        customer = common.make_certificate(CUSTOMER_HOST)
        former = common.save_web_certificate()
        for commands in range(30):
            with self.subTest(commands_run_as_root_before_the_cut=commands):
                common.restore_web_certificate(former)
                with common.power_cut_after(commands) as power_was_cut, contextlib.suppress(common.PowerCut):
                    self.install(customer)
                if not power_was_cut():
                    break
                # The box looks at its certificate when it starts again.
                common.layer('trust').refresh_box_certificate()

                certificate = common.web_certificate()
                key = load_pem_private_key(common.as_root('cat', str(common.WEB_KEY)).stdout, password=None)
                self.assertEqual(public_key(certificate), public_key(key))
                self.assertIn(common.fingerprint(certificate), (self.installed, customer.fingerprint))
                common.serve_web_certificate()
        else:
            self.fail('The installation did not end within 30 commands run as root')

    def test_certificate_of_the_customer_is_kept_when_the_box_starts_again(self):
        customer = common.make_certificate(CUSTOMER_HOST)
        self.install(customer)

        common.in_new_process(common.start_report)

        self.assertEqual(common.fingerprint(common.web_certificate()), customer.fingerprint)

    def test_answer_reaches_a_caller_that_came_through_the_web_server(self):
        common.serve()
        customer = common.make_certificate(CUSTOMER_HOST)

        answer = common.through_web_server(
            CERTIFICATE, certificate=customer.pem, key=customer.key_pem, host_name=CUSTOMER_HOST)

        self.assertIsNotNone(answer, common.why_no_answer())
        self.assertEqual(answer.get('status'), 'installed', answer)
        common.wait_until(
            lambda: common.served_certificate() == customer.fingerprint,
            'the web server to present the certificate of the customer')

    def test_restore_puts_a_certificate_the_box_makes_back_in_place_of_the_one_of_the_customer(self):
        self.install(common.make_certificate(CUSTOMER_HOST))

        answer = self.call(CERTIFICATE, restore=True)

        self.assertEqual((answer.get('status'), answer.get('kind')), ('restored', 'box'), answer)
        restored = common.web_certificate()
        self.assertEqual(common_name(restored), name_of_this_box())
        common.wait_until(
            lambda: common.served_certificate() == common.fingerprint(restored),
            'the web server to present the certificate of the box')

    def test_restore_keeps_the_address_the_installer_entered_and_covers_it(self):
        self.install(common.make_certificate(CUSTOMER_HOST))

        self.call(CERTIFICATE, restore=True)

        self.assertEqual(common.box_configuration().get('viin_box_address'), CUSTOMER_HOST)
        self.assertIn(CUSTOMER_HOST, host_names(common.web_certificate()))

    def test_file_of_the_layer_that_asked_is_told_when_the_web_server_gets_another_certificate(self):
        changes = certificate_changes()

        self.install(common.make_certificate(CUSTOMER_HOST))

        self.assertEqual(len(changes), 1)

    def test_file_of_the_layer_that_asked_is_not_told_of_a_certificate_the_box_refused(self):
        changes = certificate_changes()
        now = common.utc_now()

        self.install(common.make_certificate(CUSTOMER_HOST, valid_from=now - 60 * DAY, valid_until=now - DAY))

        self.assertEqual(changes, [])


class TestFilesTheBoxWritesAsRoot(common.BoxCase):

    def setUp(self):
        super().setUp()
        # A writing the power cut short may leave a file of its own beside the one it wrote.
        self.addCleanup(common.as_root, 'sh', '-c', f'rm -f {ROOT_FILES[0]}*')

    def written(self):
        """What the file holds; None when it does not exist."""
        read = subprocess.run(['sudo', '-n', 'cat', ROOT_FILES[0]], capture_output=True, check=False)
        return None if read.returncode else read.stdout

    def test_content_is_written_for_root_with_the_permissions_asked(self):
        common.layer('trust').write_system_files(ROOT_FILES, b'box suite content', '600')

        self.assertEqual(self.written(), b'box suite content')
        self.assertEqual(common.as_root('stat', '-c', '%a %U', ROOT_FILES[0]).stdout.split(), [b'600', b'root'])

    def test_file_that_exists_gets_the_new_content(self):
        trust = common.layer('trust')
        trust.write_system_files(ROOT_FILES, b'former content', '600')

        trust.write_system_files(ROOT_FILES, b'new content', '600')

        self.assertEqual(self.written(), b'new content')

    def test_power_cut_during_a_writing_leaves_the_former_content_or_the_new_one(self):
        trust = common.layer('trust')
        for commands in range(30):
            with self.subTest(commands_run_as_root_before_the_cut=commands):
                trust.write_system_files(ROOT_FILES, b'former content', '600')
                with common.power_cut_after(commands) as power_was_cut, contextlib.suppress(common.PowerCut):
                    trust.write_system_files(ROOT_FILES, b'new content', '600')
                if not power_was_cut():
                    break

                self.assertIn(self.written(), (b'former content', b'new content'))
        else:
            self.fail('The writing did not end within 30 commands run as root')

    def test_files_are_removed(self):
        trust = common.layer('trust')
        trust.write_system_files(ROOT_FILES, b'box suite content', '600')

        trust.remove_system_files(ROOT_FILES)

        self.assertIsNone(self.written())

    def test_removing_a_file_that_does_not_exist_is_no_error(self):
        common.layer('trust').remove_system_files(ROOT_FILES)

        self.assertIsNone(self.written())

    def test_web_server_presents_the_certificate_put_in_its_files_once_it_is_told(self):
        self.keep_web_certificate()
        changes = certificate_changes()
        certificate = common.make_certificate(CUSTOMER_HOST)
        common.install_web_certificate(certificate)

        common.layer('trust').serve_new_certificate()

        common.wait_until(
            lambda: common.served_certificate() == certificate.fingerprint,
            'the web server to present the certificate put in its files')
        self.assertEqual(len(changes), 1)


@contextlib.contextmanager
def web_server_held():
    """Keep the web server of the box from reading its files during the block: the reloads it is asked for
    meanwhile are read once the block ends."""
    master = WEB_SERVER_PID.read_text().strip()
    common.as_root('kill', '-STOP', master)
    try:
        yield
    finally:
        common.as_root('kill', '-CONT', master)


class Changing(threading.Thread):
    """A change of a pair the web server reads, made on a thread of its own: `result` is what it gave, `error` what
    ended it otherwise."""

    def __init__(self, change, *arguments):
        super().__init__(daemon=True)
        self.change, self.arguments, self.result, self.error = change, arguments, None, None

    def run(self):
        try:
            self.result = self.change(*self.arguments)
        except Exception as error:  # noqa: BLE001 - the test reads it
            self.error = error


class TestChangesOfThePairsWhileTheWebServerReadsThem(common.BoxCase):

    def setUp(self):
        super().setUp()
        self.keep_web_certificate()
        self.trust = common.layer('trust')

    def install(self, customer):
        return Changing(self.trust.install_certificate, customer.pem, customer.key_pem, CUSTOMER_HOST)

    def test_second_installation_writes_nothing_until_the_web_server_has_read_the_first(self):
        first, second = common.make_certificate(CUSTOMER_HOST), common.make_certificate(CUSTOMER_HOST)
        installing_first, installing_second = self.install(first), self.install(second)
        with web_server_held():
            installing_first.start()
            common.wait_until(
                lambda: common.fingerprint(common.web_certificate()) == first.fingerprint, 'the first pair written')
            installing_second.start()
            time.sleep(HELD)
            meanwhile = common.fingerprint(common.web_certificate())
        installing_first.join(60)
        installing_second.join(60)

        self.assertEqual(meanwhile, first.fingerprint)
        self.assertEqual([installing_first.error, installing_second.error], [None, None])
        self.assertEqual([installing_first.result['status'], installing_second.result['status']],
                         ['installed', 'installed'])
        self.assertEqual(common.fingerprint(common.web_certificate()), second.fingerprint)
        common.wait_until(lambda: common.served_certificate() == second.fingerprint,
                          'the web server to present the second certificate')

    def test_pair_of_the_viindoo_name_is_not_switched_until_the_web_server_has_read_an_installation(self):
        if common.as_root('sh', '-c', f'test -e {NAME_PAIR} && echo held || true').stdout.strip():
            self.skipTest('a pair of the Viindoo name is installed on this box')
        self.addCleanup(common.as_root, 'rm', '-rf', *self.trust.NAME_PAIR_DIRECTORIES)
        customer, name = common.make_certificate(CUSTOMER_HOST), common.make_certificate('k3x9.iot.viindoo.example')
        installing = self.install(customer)
        switching = Changing(self.trust.replace_pair, self.trust.NAME_PAIR_DIRECTORIES, name.pem, name.key_pem)
        with web_server_held():
            installing.start()
            common.wait_until(
                lambda: common.fingerprint(common.web_certificate()) == customer.fingerprint, 'the pair written')
            switching.start()
            time.sleep(HELD)
            meanwhile = self.name_pair_in_use()
        installing.join(60)
        switching.join(60)

        self.assertEqual(meanwhile, b'')
        self.assertEqual([installing.error, switching.error], [None, None])
        self.assertEqual(self.name_pair_in_use(), name.pem.encode())

    def name_pair_in_use(self):
        """The certificate `current` of the pair of the Viindoo name leads to; empty when there is none."""
        return common.as_root('sh', '-c', f'cat {NAME_PAIR}/current/fullchain.pem 2>/dev/null || true').stdout


class StopLooking(BaseException):
    """Ends the looks a test lets a thread of the box take: no look catches it."""


class TestLogOfLooksThatKeepFailing(common.BoxCase):

    def logged(self, looking, outcomes, between_looks=lambda: None):
        """Run `looking`, the work of the keeper or of the watcher of the addresses, on this thread for one look at
        the certificate of the box per entry of `outcomes`: the error that ends that look, or None for a look that
        succeeds; `between_looks` runs in each wait for the next look. Give each record the box logged about them:
        the failure when it carries its traceback, else the record itself."""
        trust, this, remaining = common.layer('trust'), threading.current_thread(), list(outcomes)
        done, sleep = threading.Event(), time.sleep

        def look():
            if threading.current_thread() is not this:
                # The threads of the box started in this process take no look while the test runs.
                done.wait()
                return False
            outcome = remaining.pop(0)
            if outcome:
                raise outcome
            return False

        def wait(seconds):
            if threading.current_thread() is not this:
                return sleep(seconds)
            between_looks()
            if not remaining:
                raise StopLooking
            return None

        self.addCleanup(done.set)
        with mock.patch.object(trust, 'refresh_box_certificate', look), mock.patch.object(time, 'sleep', wait), \
                self.assertLogs(trust.__name__, logging.WARNING) as log, self.assertRaises(StopLooking):
            looking()
        return [record.exc_info[1] if record.exc_info else record for record in log.records
                if record.thread == this.ident]

    def assertOneLine(self, record):
        self.assertIsInstance(record, logging.LogRecord)
        self.assertNotIn('\n', record.getMessage())

    def failure(self, text):
        return OSError(f'{self.id()}: {text}')

    def test_keeper_logs_a_failure_in_full_once_and_its_repeats_in_one_line_each(self):
        trust = common.layer('trust')
        first, again = self.failure('the card is read-only'), self.failure('the card is read-only')
        after_a_success = self.failure('the card is read-only')
        other = subprocess.CalledProcessError(1, ['sudo', '-n', 'mv'])
        keeper = trust.CertificateKeeper()

        with mock.patch.object(trust, 'AddressWatcher'):
            logged = self.logged(keeper.run, [first, again, again, None, after_a_success, other])

        self.assertEqual(len(logged), 5, logged)
        self.assertIs(logged[0], first)
        self.assertOneLine(logged[1])
        self.assertOneLine(logged[2])
        self.assertEqual(logged[3:], [after_a_success, other])

    def test_watcher_of_the_addresses_logs_a_failure_in_full_once_and_its_repeats_in_one_line_each(self):
        trust = common.layer('trust')
        first, again = self.failure('the card is read-only'), self.failure('the card is read-only')
        watcher, addresses = trust.AddressWatcher(), {}

        # An address appears after the watcher began, and it looks again at once after a look that failed.
        with box.network(addresses), mock.patch.object(trust, 'FAILED_LOOK_WAIT', 0):
            logged = self.logged(watcher.run, [first, again, again], lambda: addresses.update(eth0=ADDRESS))

        self.assertEqual(len(logged), 3, logged)
        self.assertIs(logged[0], first)
        self.assertOneLine(logged[1])
        self.assertOneLine(logged[2])

    def test_repeat_of_a_failure_names_its_type_and_the_beginning_of_its_text_in_a_short_line(self):
        trust = common.layer('trust')
        text = 'the card is read-only\n' + '\n'.join(['while the box wrote its certificate'] * 300)
        first, again = self.failure(text), self.failure(text)
        keeper = trust.CertificateKeeper()

        with mock.patch.object(trust, 'AddressWatcher'):
            logged = self.logged(keeper.run, [first, again])

        self.assertIs(logged[0], first)
        self.assertOneLine(logged[1])
        repeat = logged[1].getMessage()
        self.assertIn('OSError', repeat)
        self.assertIn(' '.join(str(again).split())[:200], repeat)
        self.assertLess(len(repeat), 1000)


class TrustCase(common.BoxCase):
    """A box, and servers that show it a certificate no public authority issued."""

    def server_nobody_trusted(self, certificate=None):
        """An https server the box was given without a pairing: its certificate was shown to no installer."""
        server = self.server(certificate=certificate or common.make_certificate('localhost'))
        common.store_box_configuration(remote_server=server.url, token=CREDENTIAL)
        return server

    def server_the_installer_trusted(self, kind=None):
        """An https server the box is paired with. Its certificate may issue others, as many self-signed ones may."""
        self.trusted = common.make_certificate('localhost', authority=True)
        return self.box_with_server(CREDENTIAL, certificate=self.trusted, kind=kind)

    def other_host_the_trusted_certificate_vouches_for(self):
        """A server under another name of this machine, whose certificate the trusted one issued."""
        other_host = common.other_host()
        return self.server(certificate=common.make_certificate(other_host, issuer=self.trusted), host=other_host)


class ServerThatSendsItsCallersElsewhere(common.FakeServer):
    """Answers a report by sending the box to `elsewhere`, the address of another server."""
    elsewhere = ''

    def answer_to(self, method, path, body):
        if path == '/iot/setup':
            return 307, b'', [('Location', f'{self.elsewhere}{path}')]
        return super().answer_to(method, path, body)


class TestCredentialGoesToVerifiedServersOnly(TrustCase):

    def send_print_result(self):
        common.core('websocket_client').send_to_controller('printer', {'print_id': 'suite-print', 'status': 'success'})

    def test_report_with_the_credential_is_not_sent_to_a_server_nobody_trusted(self):
        server = self.server_nobody_trusted()

        self.report_to(server)

        self.assertEqual(server.requests, [])

    def test_report_with_the_credential_reaches_a_server_the_installer_trusted(self):
        server = self.server_the_installer_trusted()

        self.report_to(server)

        self.assertEqual(common.reported_credentials(server), {CREDENTIAL})

    def test_report_with_the_credential_reaches_a_server_a_public_authority_vouches_for(self):
        with common.public_authority() as authority:
            server = self.server(certificate=common.make_certificate('localhost', issuer=authority))
            common.store_box_configuration(remote_server=server.url, token=CREDENTIAL)

            self.report_to(server)

        self.assertEqual(common.reported_credentials(server), {CREDENTIAL})

    def test_report_reaches_the_server_whose_trusted_certificate_gave_way_to_one_of_a_public_authority(self):
        server = self.server_the_installer_trusted()
        server.close()
        with common.public_authority() as authority:
            renewed = self.server(certificate=common.make_certificate('localhost', issuer=authority), port=server.port)

            self.report_to(renewed)

        self.assertEqual(common.reported_credentials(renewed), {CREDENTIAL})

    def test_report_is_not_sent_to_another_host_the_trusted_certificate_vouches_for(self):
        self.server_the_installer_trusted()
        other_server = self.other_host_the_trusted_certificate_vouches_for()

        self.report_to(other_server)

        self.assertEqual(other_server.requests, [])

    def test_report_the_server_sends_elsewhere_does_not_follow_on_the_trusted_certificate(self):
        server = self.server_the_installer_trusted(ServerThatSendsItsCallersElsewhere)
        other_server = self.other_host_the_trusted_certificate_vouches_for()
        server.elsewhere = other_server.url

        self.report_to(server)

        self.assertIn(REPORT, server.calls)
        self.assertEqual(other_server.requests, [])

    def test_print_result_is_not_sent_to_a_server_nobody_trusted(self):
        server = self.server_nobody_trusted()

        self.send_print_result()

        self.assertEqual(server.requests, [])

    def test_print_result_reaches_a_server_the_installer_trusted(self):
        server = self.server_the_installer_trusted()

        self.send_print_result()

        self.assertIn('/iot/printer/status', [request['path'] for request in server.requests])


class TestAuthoritiesOfTheRestOfTheBox(TrustCase):

    def test_trusted_certificate_is_among_the_authorities_for_its_server(self):
        server = self.server_the_installer_trusted()

        authorities = common.layer('trust').authorities_for(f'{server.url}/iot/setup')

        self.assertTrue(accepted(authorities, server))

    def test_trusted_certificate_is_not_among_the_authorities_for_another_host(self):
        self.server_the_installer_trusted()
        other_server = self.other_host_the_trusted_certificate_vouches_for()

        authorities = common.layer('trust').authorities_for(f'{other_server.url}/iot/setup')

        self.assertFalse(accepted(authorities, other_server))

    def test_trusted_certificate_is_not_among_the_authorities_the_other_libraries_use(self):
        server = self.server_the_installer_trusted()
        other_server = self.other_host_the_trusted_certificate_vouches_for()

        for authorities in (certifi.where(), common.layer('trust').PUBLIC_AUTHORITIES):
            with self.subTest(authorities=authorities):
                self.assertFalse(accepted(authorities, server))
                self.assertFalse(accepted(authorities, other_server))


class TestCallsALaterFileMakesToTheServer(TrustCase):

    def send(self, server, **asked_by_the_caller):
        manager = common.layer('trust').pool_manager(**asked_by_the_caller)
        return manager.request('POST', f'{server.url}/iot/reading', body=b'{}', retries=False)

    def test_call_reaches_a_server_the_installer_trusted(self):
        server = self.server_the_installer_trusted()

        self.send(server)

        self.assertIn('POST /iot/reading', server.calls)

    def test_call_is_not_sent_to_a_server_nobody_trusted_whatever_the_caller_asked(self):
        server = self.server_nobody_trusted()

        with self.assertRaises(urllib3.exceptions.HTTPError):
            self.send(server, cert_reqs='CERT_NONE')

        self.assertEqual(server.requests, [])

    def test_call_is_not_sent_to_another_host_the_trusted_certificate_vouches_for(self):
        self.server_the_installer_trusted()
        other_server = self.other_host_the_trusted_certificate_vouches_for()

        with self.assertRaises(urllib3.exceptions.HTTPError):
            self.send(other_server)

        self.assertEqual(other_server.requests, [])


class ServerThatAnswersLate(common.FakeServer):
    """Answers every request after `delay` seconds."""
    delay = 3

    def answer_to(self, method, path, body):
        time.sleep(self.delay)
        return super().answer_to(method, path, body)


class TestCallsToAServerThatDoesNotAnswer(common.BoxCase):

    def late_server(self):
        return self.server(kind=ServerThatAnswersLate)

    def call(self, server, **asked_by_the_caller):
        manager = common.layer('trust').pool_manager(**asked_by_the_caller)
        return manager.request('POST', f'{server.url}/iot/reading', body=b'{}', retries=False)

    def test_caller_that_names_no_timeout_does_not_wait_for_ever(self):
        server = self.late_server()

        with mock.patch.object(common.layer('trust'), 'READ_TIMEOUT', 0.5):
            with self.assertRaises(urllib3.exceptions.ReadTimeoutError):
                self.call(server)

    def test_caller_that_names_a_timeout_for_its_manager_keeps_it(self):
        server = self.late_server()

        with mock.patch.object(common.layer('trust'), 'READ_TIMEOUT', 30):
            with self.assertRaises(urllib3.exceptions.ReadTimeoutError):
                self.call(server, timeout=urllib3.Timeout(connect=5, read=0.5))

    def test_server_that_answers_within_the_timeout_is_answered(self):
        server = self.server()

        response = self.call(server)

        self.assertEqual(response.status, 404)
        self.assertIn('POST /iot/reading', server.calls)


class TestFileOfCoreLoadedAfterTheStart(TrustCase):

    def test_file_asked_to_verify_does_not_call_a_server_nobody_trusted(self):
        server = self.server_nobody_trusted()
        file = file_of_core_that_calls_a_server()

        verifies = common.layer('seams.trust').verify_server_calls(file)

        self.assertIs(verifies, True)
        with self.assertRaises(urllib3.exceptions.HTTPError):
            file.send(f'{server.url}/iot/keyboard_layouts')
        self.assertEqual(server.requests, [])

    def test_file_asked_twice_to_verify_reaches_a_server_the_installer_trusted(self):
        server = self.server_the_installer_trusted()
        file = file_of_core_that_calls_a_server()
        verify = common.layer('seams.trust').verify_server_calls

        verified = [verify(file), verify(file)]
        file.send(f'{server.url}/iot/keyboard_layouts')

        self.assertEqual(verified, [True, True])
        self.assertIn('POST /iot/keyboard_layouts', server.calls)

    def test_file_that_does_not_call_through_urllib3_is_told_apart(self):
        file = types.ModuleType('box_suite_other_file')

        self.assertIs(common.layer('seams.trust').verify_server_calls(file), False)


class TestCertificatesAFileOfTheLayerTrusts(TrustCase):

    def test_certificate_trusted_for_the_server_lets_the_report_reach_it(self):
        certificate = common.make_certificate('localhost')
        server = self.server_nobody_trusted(certificate=certificate)

        fingerprint = common.layer('trust').trust_certificate(certificate.pem.encode())
        self.report_to(server)

        self.assertEqual(common.hex_digits(fingerprint), certificate.fingerprint)
        self.assertEqual(common.reported_credentials(server), {CREDENTIAL})

    def test_certificate_given_in_its_binary_form_is_trusted_as_well(self):
        certificate = common.make_certificate('localhost')
        server = self.server_nobody_trusted(certificate=certificate)

        common.layer('trust').trust_certificate(certificate.certificate.public_bytes(Encoding.DER))
        self.report_to(server)

        self.assertEqual(common.reported_credentials(server), {CREDENTIAL})

    def test_certificate_trusted_for_the_server_vouches_for_no_other_host(self):
        self.trusted = common.make_certificate('localhost', authority=True)
        self.server_nobody_trusted(certificate=self.trusted)
        other_server = self.other_host_the_trusted_certificate_vouches_for()

        common.layer('trust').trust_certificate(self.trusted.certificate)
        self.report_to(other_server)

        self.assertEqual(other_server.requests, [])

    def test_no_certificate_can_be_trusted_on_a_box_without_a_server(self):
        certificate = common.make_certificate('localhost')

        with self.assertRaises(ValueError):
            common.layer('trust').trust_certificate(certificate.certificate)

        self.assertEqual(common.trusted_certificates(), [])

    def test_box_lists_the_certificate_trusted_for_its_server(self):
        self.server_the_installer_trusted()

        listed = common.layer('trust').trusted_certificates()

        self.assertEqual([common.hex_digits(certificate['fingerprint']) for certificate in listed],
                         [self.trusted.fingerprint])
        self.assertEqual(listed[0]['subject'], self.trusted.certificate.subject.rfc4514_string())
        self.assertEqual(listed[0]['names'], ['localhost'])

    def test_box_given_another_server_lists_no_certificate_trusted_for_the_one_it_left(self):
        self.server_the_installer_trusted()
        common.store_box_configuration(remote_server='https://other-server.example')

        self.assertEqual(common.layer('trust').trusted_certificates(), [])


class RoadsToTheServer:
    """The roads a box takes to its server when it starts, for a test class that says what the server shows."""

    def certificate_of_the_server(self):
        """(`Certificate` the server shows once the box starts, PEM text of the public authority it has or '')."""
        raise NotImplementedError

    def box(self):
        """What `start_a_box_beside_its_server` gives, once for the tests of the class.

        The box was paired with a server whose certificate the installer trusted; the server of
        `certificate_of_the_server` listens at the same address when the box starts.
        """
        cls = type(self)
        if '_box' not in vars(cls):
            self.trusted = common.make_certificate('localhost')
            paired_with = self.box_with_server(CREDENTIAL, certificate=self.trusted)
            paired_with.close()
            certificate, public_authority = self.certificate_of_the_server()
            cls._box = common.in_new_process(
                start_a_box_beside_its_server, certificate=certificate.pem, key=certificate.key_pem,
                port=paired_with.port, public_authority=public_authority)
        return cls._box

    def test_report_with_the_credential_reaches_the_server(self):
        self.assertEqual(self.box()['credentials'], [CREDENTIAL])

    def test_live_channel_reaches_the_server(self):
        self.assertIn(LIVE_CHANNEL, self.box()['calls'])

    def test_driver_files_are_asked_of_the_server(self):
        self.assertIn(DRIVER_FILES, self.box()['calls'])

    def test_log_of_the_box_reaches_the_server(self):
        self.assertIn(BOX_LOG, self.box()['calls'])


class TestStartBesideAServerTheInstallerTrusted(RoadsToTheServer, common.BoxCase):

    def certificate_of_the_server(self):
        return self.trusted, ''


class TestStartBesideAServerWhoseTrustedCertificateGaveWayToAPublicOne(RoadsToTheServer, common.BoxCase):

    def certificate_of_the_server(self):
        authority = common.make_certificate('Box suite authority', names=(), authority=True)
        return common.make_certificate('localhost', issuer=authority), authority.pem


class TestStartOfABoxWhoseMemoryTakesNoNewFile(common.BoxCase):

    def box(self):
        """A box paired with a server whose certificate the installer trusted starts where its memory-backed
        storage takes no new file; once for the tests of the class. Also gives 'calls', what its server
        received from the pairing on."""
        cls = type(self)
        if '_box' not in vars(cls):
            server = self.box_with_server(CREDENTIAL, certificate=common.make_certificate('localhost'))
            box = common.in_new_process(start_a_box_whose_memory_takes_no_new_file)
            cls._box = dict(box, calls=server.calls)
        return cls._box

    def test_box_still_starts(self):
        self.assertEqual(self.box()['answers'], {common.PING: 200})

    def test_device_manager_of_core_keeps_running(self):
        box = self.box()

        self.assertIs(box['device_manager_runs'], True)
        self.assertGreater(box['device_handlers'], 0)

    def test_nothing_is_sent_to_the_server_the_box_can_no_longer_verify(self):
        self.assertEqual(self.box()['calls'], ['POST /iot/pair'])


class TestDriverDownloadThatTakesItsAuthoritiesElsewhere(common.MovedSeamCase):
    number = 10
    core_changes = {'ca_certs=certifi.where()': 'ca_certs=requests.certs.where()'}
    other_seams = (1, 2, 3, 12, 13)


class TestFunctionThatSendsPrintResultsRenamed(common.MovedSeamCase):
    number = 12
    core_changes = {'send_to_controller': 'report_to_controller'}
    other_seams = (1, 2, 3, 10, 13)

    def test_report_with_the_credential_still_goes_to_verified_servers_only(self):
        server = self.server(certificate=common.make_certificate('localhost'))
        common.store_box_configuration(remote_server=server.url, token=CREDENTIAL)

        common.in_new_process(common.start_report, **self.moved)

        self.assertEqual(server.requests, [])
