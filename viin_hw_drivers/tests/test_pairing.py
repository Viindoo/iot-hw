"""Connecting the box to a server with a pairing code, and taking it away from that server."""
import concurrent.futures
import datetime
import socket
import threading
from pathlib import Path
from unittest import mock

from cryptography import x509

import common
import services_common as box

PAIR = '/viin_hw_homepage/pair'
CONNECT = '/hw_posbox_homepage/connect_to_server'
DISCONNECT = '/hw_posbox_homepage/server_clear'
WIFI_CLEAR = '/hw_posbox_homepage/wifi_clear'
CODE = 'K7Q2-M9XD-4TWP'
REFUSED_CODE = 'D3HV-8RNC-6YJA'
UNTRUSTED_CODE = 'W5TB-2GKE-9PZM'
CREDENTIAL = 'suite-credential-5T8WQ2ZN'
PAIRED = {'status': 'paired', 'token': CREDENTIAL, 'box_name': 'Packing line 2'}
# A credential and the settings code of the box that holds it, worked out apart from the box software: a box
# that has a server asks for the code.
VECTOR_CREDENTIAL = 'abc'
VECTOR_SETTINGS_CODE = 'R4HF8KC1'
# What core's own page sends to connect a box: a server address that carries the credential.
CORE_CONNECTION = 'http://other-server.example?token=other-credential&db_uuid=&enterprise_code='
# The reason by which the box refuses, as locked, a request that carries no valid unlock proof.
LOCKED = 'unlock_required'
# The files of the layer by which a box starts, trusts a server, pairs and locks its settings. A box built with
# them alone holds no part that confirms the readings it issues.
PAIRING_FILES = ('seams.startup', 'seams.trust', 'trust', 'pairing', 'lock')
# All a person needs to recognise a certificate, and what the pairing dialog decides on.
UNTRUSTED_KEYS = {'fingerprint', 'subject', 'expiry', 'names', 'problem', 'trustable'}
DAY = datetime.timedelta(days=1)
# What the box keeps for the box itself, and what the layer keeps for the server it left: the first stays when the
# box leaves its server, the second goes with it. The name typed for the box stays only with the certificate of the
# customer issued for it.
SETTINGS_OF_THE_BOX = {'viin_auto_update': '0', 'viin_box_address': 'box.shop.example'}
KEPT_FOR_THE_SERVER = {'viin_managed_name': 'k3x9.iot.example', 'viin_suite_setting': 'value'}
TYPED_NAME = SETTINGS_OF_THE_BOX['viin_box_address']


def as_time(text):
    """The moment an answer gives as text, to compare it with a moment in UTC; None when it gives none."""
    return text and datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))


def in_utc(moment):
    return moment.replace(tzinfo=datetime.timezone.utc)


def certificate_the_box_made(*names):
    """A certificate of the box issued for its host names and `names`."""
    host = socket.gethostname()
    return box.box_made_certificate([host, f'{host}.local', *names])


def names_of(certificate):
    """The host names a `Certificate` is issued for."""
    try:
        alternative = certificate.certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return []
    return alternative.value.get_values_for_type(x509.DNSName)


class ServerThatTakesItsTime(common.FakeServer):
    """Keeps a pairing request waiting until `answer_now` is set; `asked` tells that the request has come."""

    def __init__(self, **options):
        self.asked, self.answer_now = threading.Event(), threading.Event()
        super().__init__(**options)

    def answer_to(self, method, path, body):
        if path == '/iot/pair':
            self.asked.set()
            self.answer_now.wait(30)
        return super().answer_to(method, path, body)


class ServerThatChangesItsCertificate(common.FakeServer):
    """Shows its certificate to its first `callers` callers and `later`, another `Certificate`, to the others."""

    def __init__(self, later, callers, **options):
        self._later, self._callers = common.tls_with(later), callers
        super().__init__(**options)

    def tls_context(self):
        self._callers -= 1
        return super().tls_context() if self._callers >= 0 else self._later


def pair_a_box_that_does_not_confirm_readings(**said_by_the_caller):
    """Pair a box that loaded PAIRING_FILES and no other file of the layer. Meant for
    `common.in_new_process`: which files a box loaded holds for as long as its process.

    `said_by_the_caller` is what the caller of the box adds to its pairing request. Gives 'answer', what
    the box answered, and 'requests', the pairing requests its server received.
    """
    common.keep_all_but(*PAIRING_FILES)
    common.start()
    server = common.FakeServer(answer=PAIRED)
    answer = common.call_route(PAIR, link=server.link(CODE), **said_by_the_caller)
    return {'answer': answer, 'requests': server.pairing_requests}


def pair_a_box_that_confirms_readings():
    """Pair a box whose software said that it confirms the readings it issues. Meant for `common.in_new_process`:
    what the software of a box said of itself holds for as long as its process.

    Gives 'answer', what the box answered, and 'requests', the pairing requests its server received.
    """
    common.start()
    common.layer('pairing').declare_confirms_readings()
    server = common.FakeServer(answer=PAIRED)
    answer = common.call_route(PAIR, link=server.link(CODE))
    return {'answer': answer, 'requests': server.pairing_requests}


class PairingCase(common.BoxCase):

    def setUp(self):
        super().setUp()
        self.kept = common.kept()

    def assertNothingStored(self):
        self.assertEqual(common.kept(), self.kept)
        self.assertEqual(common.restarts, [])

    def assertAnswer(self, answer, status, reason):
        self.assertEqual((answer.get('status'), answer.get('reason')), (status, reason), answer)

    def assertUntrusted(self, answer, problem, trustable, shown):
        """The box refused the certificate of the server for `problem` and shows the `Certificate` `shown`."""
        self.assertAnswer(answer, 'failed', 'server_certificate_untrusted')
        self.assertLessEqual(UNTRUSTED_KEYS, set(answer))
        self.assertEqual((answer['problem'], answer['trustable']), (problem, trustable), answer)
        self.assertEqual(common.hex_digits(answer['fingerprint']), shown.fingerprint)
        self.assertEqual(answer['subject'], shown.certificate.subject.rfc4514_string())
        self.assertEqual(as_time(answer['expiry']), in_utc(shown.certificate.not_valid_after))
        self.assertEqual(answer['names'], names_of(shown))


class TestPairedAnswer(PairingCase):

    def test_paired_answer_stores_server_and_credential_and_asks_for_the_restart(self):
        server = self.server(answer=PAIRED)

        answer = self.pair(link=server.link(CODE))

        self.assertEqual(answer.get('status'), 'paired', answer)
        stored = common.box_configuration()
        self.assertEqual(stored.get('remote_server'), server.url)
        self.assertEqual(stored.get('token'), CREDENTIAL)
        self.assertEqual(len(common.restarts), 1)

    def test_paired_answer_names_the_server_and_the_box(self):
        server = self.server(answer=PAIRED)

        answer = self.pair(link=server.link(CODE))

        self.assertEqual((answer.get('server'), answer.get('box_name')), (server.url, 'Packing line 2'), answer)

    def test_name_of_the_box_is_cut_at_200_characters(self):
        server = self.server(answer={'status': 'paired', 'token': CREDENTIAL, 'box_name': 'Packing line ' * 100})

        answer = self.pair(link=server.link(CODE))

        self.assertEqual(answer.get('status'), 'paired', answer)
        self.assertEqual(len(answer.get('box_name')), 200)

    def test_server_address_and_code_entered_separately_pair_the_box(self):
        server = self.server(answer=PAIRED)

        answer = self.pair(server_url=server.url, code=CODE)

        self.assertEqual(answer.get('status'), 'paired', answer)
        self.assertEqual(common.box_configuration().get('token'), CREDENTIAL)

    def test_box_sends_the_code_as_typed_with_its_identity(self):
        server = self.server(answer=PAIRED)

        self.pair(link=server.link(CODE))

        self.assertEqual(len(server.pairing_requests), 1)
        request = server.pairing_requests[0]
        self.assertEqual(request['pairing_code'], CODE)
        self.assertEqual(request['protocol'], 1)
        self.assertEqual(set(request['iot_box']), {'name', 'identifier', 'ip', 'version', 'serial_number'})
        self.assertEqual(request['iot_box']['name'], socket.gethostname())
        self.assertEqual(request['iot_box']['serial_number'], common.board_serial())


class TestBoxThatDoesNotConfirmReadings(common.BoxStateCase):

    def test_box_whose_software_does_not_confirm_readings_sends_code_identity_and_protocol_only(self):
        paired = common.in_new_process(pair_a_box_that_does_not_confirm_readings)

        self.assertEqual(len(paired['requests']), 1)
        self.assertEqual(set(paired['requests'][0]), {'pairing_code', 'iot_box', 'protocol'})

    def test_caller_of_the_box_cannot_make_it_say_that_it_confirms_readings(self):
        paired = common.in_new_process(pair_a_box_that_does_not_confirm_readings, confirms_readings=True)

        self.assertEqual(len(paired['requests']), 1)
        self.assertNotIn('confirms_readings', paired['requests'][0])


class TestBoxThatConfirmsReadings(common.BoxStateCase):

    def test_box_whose_software_confirms_readings_says_so_in_its_pair_request(self):
        paired = common.in_new_process(pair_a_box_that_confirms_readings)

        self.assertEqual(paired['answer'].get('status'), 'paired', paired['answer'])
        self.assertEqual(len(paired['requests']), 1)
        self.assertIs(paired['requests'][0].get('confirms_readings'), True)


class TestRefusals(PairingCase):

    def test_code_the_server_does_not_know_is_refused_as_invalid_code(self):
        server = self.server(answer={'status': 'refused', 'reason': 'invalid_code'})

        answer = self.pair(link=server.link(CODE))

        self.assertAnswer(answer, 'refused', 'invalid_code')
        self.assertNothingStored()

    def test_code_older_than_its_life_is_refused_as_expired(self):
        server = self.server(answer={'status': 'refused', 'reason': 'expired'})

        answer = self.pair(link=server.link(CODE))

        self.assertAnswer(answer, 'refused', 'expired')
        self.assertNothingStored()

    def test_code_that_was_redeemed_is_refused_as_already_used(self):
        server = self.server(answer={'status': 'refused', 'reason': 'already_used'})

        answer = self.pair(link=server.link(CODE))

        self.assertAnswer(answer, 'refused', 'already_used')
        self.assertNothingStored()

    def test_hardware_the_server_already_knows_is_refused_with_the_name_of_that_box(self):
        server = self.server(
            answer={'status': 'refused', 'reason': 'identifier_exists', 'box_name': 'Packing line 2'})

        answer = self.pair(link=server.link(CODE))

        self.assertAnswer(answer, 'refused', 'identifier_exists')
        self.assertEqual(answer.get('box_name'), 'Packing line 2')
        self.assertNothingStored()

    def test_failure_inside_the_server_is_refused_as_server_error(self):
        server = self.server(answer={'status': 'refused', 'reason': 'server_error'})

        answer = self.pair(link=server.link(CODE))

        self.assertAnswer(answer, 'refused', 'server_error')
        self.assertNothingStored()

    def test_reason_the_box_does_not_know_is_a_refusal_that_carries_the_word_of_the_server(self):
        server = self.server(answer={'status': 'refused', 'reason': 'quota_exceeded'})

        answer = self.pair(link=server.link(CODE))

        self.assertEqual(answer.get('status'), 'refused', answer)
        self.assertEqual(answer.get('server_reason'), 'quota_exceeded')
        self.assertNothingStored()

    def test_unknown_refusal_stays_small_and_harmless_whatever_the_server_sent(self):
        sent = (
            '<script>alert(1)</script>',
            'x' * 5000,
            'two words',
            'line\nbreak',
            'Quota_Exceeded',
            '',
            None,
            12,
            ['invalid_code'],
            {'reason': 'invalid_code'},
        )
        for reason in sent:
            with self.subTest(reason=repr(reason)[:40]):
                server = self.server(answer={'status': 'refused', 'reason': reason, 'box_name': 'y' * 5000})

                answer = self.pair(link=server.link(CODE))

                self.assertEqual(answer.get('status'), 'refused', answer)
                self.assertRegex(str(answer.get('server_reason')), r'\A[a-z0-9_]{1,40}\Z')
                self.assertLess(len(str(answer)), 200)
                self.assertNothingStored()


class TestFailuresOnTheWay(PairingCase):

    def test_text_that_is_no_pairing_link_fails_as_invalid_link(self):
        links = (
            'not a link',
            'https://server.example/',
            'https://server.example/#pair=',
            f'https://server.example/#code={CODE}',
            f'ftp://server.example/#pair={CODE}',
        )
        for link in links:
            with self.subTest(link=link):
                self.assertAnswer(self.pair(link=link), 'failed', 'invalid_link')
        self.assertNothingStored()

    def test_address_typed_apart_that_is_no_address_fails_as_invalid_address(self):
        for address in ('not an address', 'ftp://server.example', 'https://server.example/#pair=', ''):
            with self.subTest(address=address):
                self.assertAnswer(self.pair(server_url=address, code=CODE), 'failed', 'invalid_address')
        self.assertNothingStored()

    def test_address_typed_without_a_code_fails_as_code_missing(self):
        server = self.server(answer=PAIRED)

        for code in (None, '', '   '):
            with self.subTest(code=code):
                self.assertAnswer(self.pair(server_url=server.url, code=code), 'failed', 'code_missing')
        self.assertEqual(server.requests, [])
        self.assertNothingStored()

    def test_server_name_that_does_not_resolve_fails_as_server_not_found(self):
        answer = self.pair(link=f'https://box-suite-server.invalid/#pair={CODE}')

        self.assertAnswer(answer, 'failed', 'server_not_found')
        self.assertNothingStored()

    def test_server_that_does_not_answer_fails_as_server_unreachable(self):
        answer = self.pair(link=f'http://127.0.0.1:{common.closed_port()}/#pair={CODE}')

        self.assertAnswer(answer, 'failed', 'server_unreachable')
        self.assertNothingStored()

    def test_server_without_viindoo_pairing_fails_as_server_not_compatible(self):
        servers = (
            {'reply': (404, b'<html><body>Not found</body></html>')},
            {'reply': (200, b'<html><body>Welcome</body></html>')},
            {'reply': (200, b'{"jsonrpc": "2.0", "id": null, "error": {"code": 404, "message": "Not found"}}')},
            {'answer': {'status': 'welcome'}},
            {'answer': {'status': 'paired', 'box_name': 'Packing line 2'}},
        )
        for options in servers:
            with self.subTest(server=options):
                server = self.server(**options)

                answer = self.pair(link=server.link(CODE))

                self.assertAnswer(answer, 'failed', 'server_not_compatible')
                self.assertNothingStored()

    def test_certificate_outside_its_dates_by_the_clock_of_the_box_only_fails_as_box_clock_wrong(self):
        now = common.utc_now().replace(microsecond=0)
        server_clock = now + datetime.timedelta(days=3)
        certificate = common.make_certificate(
            'localhost', valid_from=now + datetime.timedelta(days=2), valid_until=now + datetime.timedelta(days=30))
        server = self.server(answer=PAIRED, certificate=certificate, clock=server_clock)

        answer = self.pair(link=server.link(CODE), trust_fingerprint=certificate.fingerprint)

        self.assertAnswer(answer, 'failed', 'box_clock_wrong')
        self.assertEqual(as_time(answer.get('server_time')), in_utc(server_clock))
        self.assertLess(abs(as_time(answer.get('box_time')) - in_utc(now)), datetime.timedelta(minutes=1))
        self.assertEqual(server.pairing_requests, [])
        self.assertNothingStored()

    def test_certificate_no_public_authority_issued_fails_with_what_the_installer_compares(self):
        certificate = common.make_certificate('localhost')
        server = self.server(answer=PAIRED, certificate=certificate)

        answer = self.pair(link=server.link(CODE))

        self.assertUntrusted(answer, 'unknown_authority', True, shown=certificate)
        self.assertRegex(answer['fingerprint'], r'\A([0-9A-F]{2}:){31}[0-9A-F]{2}\Z')
        self.assertNothingStored()


class TestServerCertificateNoConfirmationMakesGood(PairingCase):

    def not_good(self):
        """{problem: a certificate no public authority issued and that has it}, each for the name `localhost`."""
        now = common.utc_now()
        return {
            'name_mismatch': common.make_certificate('another-name.example'),
            'expired': common.make_certificate('localhost', valid_from=now - 60 * DAY, valid_until=now - DAY),
            'not_yet_valid': common.make_certificate('localhost', valid_from=now + DAY, valid_until=now + 60 * DAY),
        }

    def test_certificate_issued_for_another_name_fails_as_name_mismatch(self):
        certificate = self.not_good()['name_mismatch']
        server = self.server(answer=PAIRED, certificate=certificate)

        answer = self.pair(link=server.link(CODE))

        self.assertUntrusted(answer, 'name_mismatch', False, shown=certificate)
        self.assertNothingStored()

    def test_certificate_past_its_end_fails_as_expired(self):
        certificate = self.not_good()['expired']
        server = self.server(answer=PAIRED, certificate=certificate)

        answer = self.pair(link=server.link(CODE))

        self.assertUntrusted(answer, 'expired', False, shown=certificate)
        self.assertNothingStored()

    def test_certificate_before_its_start_fails_as_not_yet_valid(self):
        certificate = self.not_good()['not_yet_valid']
        server = self.server(answer=PAIRED, certificate=certificate)

        answer = self.pair(link=server.link(CODE))

        self.assertUntrusted(answer, 'not_yet_valid', False, shown=certificate)
        self.assertNothingStored()

    def test_fingerprint_sent_back_does_not_pair_with_a_server_whose_certificate_is_not_good(self):
        for problem, certificate in self.not_good().items():
            with self.subTest(problem=problem):
                server = self.server(answer=PAIRED, certificate=certificate)

                answer = self.pair(link=server.link(CODE), trust_fingerprint=certificate.fingerprint)

                self.assertUntrusted(answer, problem, False, shown=certificate)
                self.assertEqual(server.pairing_requests, [])
                self.assertNothingStored()

    def test_server_that_changes_its_certificate_once_the_installer_confirmed_it_gets_no_code(self):
        confirmed = common.make_certificate('localhost')
        later = common.make_certificate('localhost')
        # The box looks three times at the certificate of a server before it sends the code.
        server = self.server(
            ServerThatChangesItsCertificate, later=later, callers=3, answer=PAIRED, certificate=confirmed)

        answer = self.pair(link=server.link(CODE), trust_fingerprint=confirmed.fingerprint)

        self.assertUntrusted(answer, 'invalid', False, shown=later)
        self.assertEqual(server.pairing_requests, [])
        self.assertNothingStored()


class TestServerCertificateNobodyVouchesFor(PairingCase):

    def setUp(self):
        super().setUp()
        self.certificate = common.make_certificate('localhost')
        self.server_met = self.server(answer=PAIRED, certificate=self.certificate)

    def test_code_is_not_sent_to_a_server_whose_certificate_the_installer_did_not_confirm(self):
        self.pair(link=self.server_met.link(CODE))

        self.assertEqual(self.server_met.pairing_requests, [])
        self.assertNothingStored()

    def test_fingerprint_of_another_certificate_does_not_confirm_the_server(self):
        another = common.make_certificate('localhost')

        answer = self.pair(link=self.server_met.link(CODE), trust_fingerprint=another.fingerprint)

        self.assertAnswer(answer, 'failed', 'server_certificate_untrusted')
        self.assertEqual(self.server_met.pairing_requests, [])
        self.assertNothingStored()

    def test_installer_who_sends_back_the_fingerprint_the_box_showed_pairs_with_that_server(self):
        shown = self.pair(link=self.server_met.link(CODE)).get('fingerprint')

        answer = self.pair(link=self.server_met.link(CODE), trust_fingerprint=shown)

        self.assertEqual(answer.get('status'), 'paired', answer)
        self.assertEqual(common.box_configuration().get('token'), CREDENTIAL)
        self.assertEqual(common.trusted_certificates(), [self.certificate.fingerprint])


class TestServerCertificateOfAPublicAuthority(PairingCase):

    def test_server_a_public_authority_vouches_for_pairs_without_any_fingerprint(self):
        with common.public_authority() as authority:
            server = self.server(answer=PAIRED, certificate=common.make_certificate('localhost', issuer=authority))

            answer = self.pair(link=server.link(CODE))

        self.assertEqual(answer.get('status'), 'paired', answer)
        self.assertEqual(common.box_configuration().get('token'), CREDENTIAL)
        self.assertEqual(common.trusted_certificates(), [])


class TestServerCertifiedByAnAuthorityOfTheCustomer(PairingCase):

    def setUp(self):
        super().setUp()
        self.root = common.make_certificate('Customer root authority', names=(), authority=True)
        self.certificate = common.make_certificate('localhost', issuer=self.root)
        self.server_met = self.server(answer=PAIRED, certificate=self.certificate)
        self.link = self.server_met.link(CODE)

    def assertNotPaired(self):
        self.assertEqual(self.server_met.pairing_requests, [])
        self.assertNothingStored()

    def test_certificate_alone_cannot_be_trusted(self):
        answer = self.pair(link=self.link)

        self.assertUntrusted(answer, 'private_authority', False, shown=self.certificate)
        self.assertNotPaired()

    def test_fingerprint_of_the_certificate_does_not_pair_the_box(self):
        answer = self.pair(link=self.link, trust_fingerprint=self.certificate.fingerprint)

        self.assertUntrusted(answer, 'private_authority', False, shown=self.certificate)
        self.assertNotPaired()

    def test_root_authority_the_installer_gives_is_shown_for_him_to_compare(self):
        answer = self.pair(link=self.link, authority=self.root.pem)

        self.assertUntrusted(answer, 'private_authority', True, shown=self.root)
        self.assertNotPaired()

    def test_installer_who_confirms_the_root_authority_pairs_with_that_server(self):
        answer = self.pair(link=self.link, authority=self.root.pem, trust_fingerprint=self.root.fingerprint)

        self.assertEqual(answer.get('status'), 'paired', answer)
        self.assertEqual(common.box_configuration().get('token'), CREDENTIAL)
        self.assertEqual(common.trusted_certificates(), [self.root.fingerprint])

    def test_box_paired_on_a_root_authority_reports_to_its_server(self):
        self.pair(link=self.link, authority=self.root.pem, trust_fingerprint=self.root.fingerprint)
        common.forget_cached_configuration()

        self.report_to(self.server_met)

        self.assertEqual(common.reported_credentials(self.server_met), {CREDENTIAL})

    def test_root_authority_vouches_for_no_other_host_than_the_server_it_was_given_for(self):
        other_host = common.other_host()
        other_server = self.server(
            certificate=common.make_certificate(other_host, issuer=self.root), host=other_host)
        self.pair(link=self.link, authority=self.root.pem, trust_fingerprint=self.root.fingerprint)
        common.forget_cached_configuration()

        self.report_to(other_server)

        self.assertEqual(other_server.requests, [])

    def test_fingerprint_of_another_certificate_does_not_confirm_the_root_authority(self):
        answer = self.pair(link=self.link, authority=self.root.pem, trust_fingerprint=self.certificate.fingerprint)

        self.assertUntrusted(answer, 'private_authority', True, shown=self.root)
        self.assertNotPaired()

    def test_authority_that_did_not_issue_the_certificate_does_not_pair_the_box(self):
        another = common.make_certificate('Another root authority', names=(), authority=True)

        answer = self.pair(link=self.link, authority=another.pem, trust_fingerprint=another.fingerprint)

        self.assertAnswer(answer, 'failed', 'server_certificate_untrusted')
        self.assertNotPaired()

    def test_authority_that_is_no_root_fails_as_authority_not_root(self):
        issuing = common.make_certificate('Customer issuing authority', names=(), authority=True, issuer=self.root)

        answer = self.pair(link=self.link, authority=issuing.pem, trust_fingerprint=issuing.fingerprint)

        self.assertAnswer(answer, 'failed', 'authority_not_root')
        self.assertNotPaired()

    def test_text_that_holds_no_certificate_fails_as_invalid_authority(self):
        answer = self.pair(link=self.link, authority='not a certificate')

        self.assertAnswer(answer, 'failed', 'invalid_authority')
        self.assertNotPaired()

    def test_box_set_not_to_accept_authorities_answers_that_the_certificate_cannot_be_trusted(self):
        with mock.patch.object(common.layer('pairing'), 'ACCEPTS_AUTHORITY', False):
            answer = self.pair(link=self.link, authority=self.root.pem, trust_fingerprint=self.root.fingerprint)

        self.assertUntrusted(answer, 'private_authority', False, shown=self.certificate)
        self.assertNotPaired()


class TestAuthorityTextThatHoldsSeveralRoots(PairingCase):

    def setUp(self):
        super().setUp()
        self.root = common.make_certificate('Customer root authority', names=(), authority=True)
        self.other_root = common.make_certificate('Another root authority', names=(), authority=True)
        self.certificate = common.make_certificate('localhost', issuer=self.root)
        self.server_met = self.server(answer=PAIRED, certificate=self.certificate)
        self.link = self.server_met.link(CODE)
        self.other_root_first = self.other_root.pem + self.root.pem

    def assertNotPaired(self):
        self.assertEqual(self.server_met.pairing_requests, [])
        self.assertNothingStored()

    def test_root_that_signs_the_chain_of_the_server_is_shown_when_it_comes_first_in_the_text(self):
        answer = self.pair(link=self.link, authority=self.root.pem + self.other_root.pem)

        self.assertUntrusted(answer, 'private_authority', True, shown=self.root)
        self.assertNotPaired()

    def test_root_that_signs_the_chain_of_the_server_is_shown_when_another_root_comes_before_it(self):
        answer = self.pair(link=self.link, authority=self.other_root_first)

        self.assertUntrusted(answer, 'private_authority', True, shown=self.root)
        self.assertNotPaired()

    def test_installer_who_confirms_the_root_that_signs_the_chain_pairs_and_the_box_keeps_that_root_alone(self):
        answer = self.pair(link=self.link, authority=self.other_root_first, trust_fingerprint=self.root.fingerprint)

        self.assertEqual(answer.get('status'), 'paired', answer)
        self.assertEqual(common.box_configuration().get('token'), CREDENTIAL)
        self.assertEqual(common.trusted_certificates(), [self.root.fingerprint])

    def test_box_paired_on_one_root_of_the_text_reports_to_its_server(self):
        self.pair(link=self.link, authority=self.other_root_first, trust_fingerprint=self.root.fingerprint)
        common.forget_cached_configuration()

        self.report_to(self.server_met)

        self.assertEqual(common.reported_credentials(self.server_met), {CREDENTIAL})

    def test_root_kept_from_the_text_vouches_for_no_other_host_than_the_server(self):
        other_host = common.other_host()
        other_server = self.server(
            certificate=common.make_certificate(other_host, issuer=self.root), host=other_host)
        self.pair(link=self.link, authority=self.other_root_first, trust_fingerprint=self.root.fingerprint)
        common.forget_cached_configuration()

        self.report_to(other_server)

        self.assertEqual(other_server.requests, [])

    def test_other_root_of_the_text_vouches_for_nothing_once_the_box_is_paired(self):
        self.pair(link=self.link, authority=self.other_root_first, trust_fingerprint=self.root.fingerprint)
        common.forget_cached_configuration()
        self.server_met.close()
        in_its_place = self.server(
            certificate=common.make_certificate('localhost', issuer=self.other_root),
            host=self.server_met.host, port=self.server_met.port)

        self.report_to(in_its_place)

        self.assertEqual(in_its_place.requests, [])

    def test_text_of_which_no_root_signs_the_chain_is_answered_as_no_authority_at_all(self):
        third_root = common.make_certificate('A third root authority', names=(), authority=True)
        without_authority = self.pair(link=self.link)

        answer = self.pair(link=self.link, authority=self.other_root.pem + third_root.pem)

        self.assertEqual(answer, without_authority)
        self.assertUntrusted(answer, 'private_authority', False, shown=self.certificate)
        self.assertNotPaired()

    def test_fingerprint_of_a_root_that_does_not_sign_the_chain_does_not_pair_the_box(self):
        answer = self.pair(
            link=self.link, authority=self.other_root_first, trust_fingerprint=self.other_root.fingerprint)

        self.assertUntrusted(answer, 'private_authority', True, shown=self.root)
        self.assertNotPaired()


class TrustedServerCase(common.BoxCase):
    """A box whose server shows a certificate the installer trusted, and the proof that unlocks its settings.

    That certificate is issued for the name of the server and for another name of this machine,
    by which a second server can show it.
    """

    def setUp(self):
        super().setUp()
        self.first_certificate = common.make_certificate('localhost', names=['localhost', common.other_host()])
        self.first_server = self.box_with_server(VECTOR_CREDENTIAL, certificate=self.first_certificate)
        _answer, self.proof = self.unlock(VECTOR_SETTINGS_CODE)

    def credentials_sent_to(self, server):
        """The credentials `server` gets when core reports to it, on the box as it is connected now."""
        common.forget_cached_configuration()
        self.report_to(server)
        return common.reported_credentials(server)

    def taking_the_place_of(self, server):
        """Close `server`; return one that listens at its address and shows the certificate trusted first."""
        server.close()
        return self.server(certificate=self.first_certificate, host=server.host, port=server.port)


class TestTrustAfterPairing(TrustedServerCase):

    def test_box_paired_again_sends_its_credential_to_the_server_it_paired_with_last(self):
        second_certificate = common.make_certificate('localhost')
        second_server = self.server(answer=PAIRED, certificate=second_certificate)

        answer = self.pair(
            proof=self.proof, link=second_server.link(CODE), trust_fingerprint=second_certificate.fingerprint)

        self.assertEqual(answer.get('status'), 'paired', answer)
        self.assertEqual(self.credentials_sent_to(second_server), {CREDENTIAL})
        self.assertEqual(common.trusted_certificates(), [second_certificate.fingerprint])

    def test_box_paired_again_sends_no_credential_to_a_server_that_shows_the_certificate_trusted_before(self):
        second_certificate = common.make_certificate('localhost')
        second_server = self.server(answer=PAIRED, certificate=second_certificate)
        self.pair(proof=self.proof, link=second_server.link(CODE), trust_fingerprint=second_certificate.fingerprint)

        impostor = self.taking_the_place_of(second_server)

        self.assertEqual(self.credentials_sent_to(impostor), set())
        self.assertEqual(impostor.requests, [])

    def test_certificate_the_box_trusts_already_does_not_vouch_in_a_new_pairing(self):
        kept = common.kept()

        answer = self.pair(proof=self.proof, link=self.first_server.link(CODE))

        self.assertEqual((answer.get('status'), answer.get('reason')), ('failed', 'server_certificate_untrusted'))
        self.assertEqual(common.kept(), kept)

    def test_pairing_with_a_server_that_needs_no_trust_leaves_no_trusted_certificate(self):
        plain_server = self.server(answer=PAIRED)

        answer = self.pair(proof=self.proof, link=plain_server.link(CODE))

        self.assertEqual(answer.get('status'), 'paired', answer)
        self.assertEqual(self.credentials_sent_to(self.first_server), set())
        self.assertEqual(common.trusted_certificates(), [])


class TestTrustAfterTheConnectRouteOfCore(TrustedServerCase):

    def test_box_connected_to_another_server_sends_it_no_credential_on_the_certificate_trusted_before(self):
        other_server = self.server(certificate=self.first_certificate, host=common.other_host())

        self.call(
            CONNECT, proof=self.proof, token=f'{other_server.url}?token=other-credential&db_uuid=&enterprise_code=')

        self.assertEqual(common.box_configuration().get('remote_server'), other_server.url)
        self.assertEqual(self.credentials_sent_to(other_server), set())
        self.assertEqual(other_server.requests, [])
        self.assertEqual(common.trusted_certificates(), [])

    def test_connect_call_that_names_no_other_server_keeps_the_server_and_the_certificate_the_box_trusts(self):
        with common.without_running('rename_iot.sh') as renames:
            self.call(CONNECT, proof=self.proof, iotname='renamed-box')

        self.assertEqual(renames, [])
        self.assertEqual(common.box_configuration().get('remote_server'), self.first_server.url)
        self.assertEqual(self.credentials_sent_to(self.first_server), {VECTOR_CREDENTIAL})
        self.assertEqual(common.trusted_certificates(), [self.first_certificate.fingerprint])

    def test_call_the_lock_refuses_leaves_server_and_trusted_certificate_as_they_are(self):
        kept = common.kept()

        answer = self.call(CONNECT, token=CORE_CONNECTION)

        self.assertEqual(answer.get('reason'), LOCKED, answer)
        self.assertEqual(common.kept(), kept)
        self.assertEqual(self.credentials_sent_to(self.first_server), {VECTOR_CREDENTIAL})


class TestTrustWhenTheBoxIsGivenAnotherServerWithoutAnyRoute(TrustedServerCase):

    def test_box_sends_no_credential_to_another_server_that_shows_the_certificate_it_trusts(self):
        other_server = self.server(certificate=self.first_certificate, host=common.other_host())
        common.store_box_configuration(remote_server=other_server.url)

        self.assertEqual(self.credentials_sent_to(other_server), set())
        self.assertEqual(other_server.requests, [])


class TestWhatPairingLetsOut(common.BoxCase):

    def test_pairing_contacts_no_host_but_the_server_and_the_publisher_of_the_box_software(self):
        certificate = common.make_certificate('localhost')
        server = self.server(answer=PAIRED, certificate=certificate)

        with common.AskedHosts() as asked:
            answer = self.pair(link=server.link(CODE), trust_fingerprint=certificate.fingerprint)

        self.assertEqual(answer.get('status'), 'paired', answer)
        self.assertIn(server.host, asked.hosts)
        # The name of the server, and the addresses of this machine that name stands for.
        self.assertLessEqual(set(asked.hosts), {'localhost', '127.0.0.1', '::1', common.SOFTWARE_PUBLISHER})

    def test_pairing_code_and_credential_never_reach_the_box_log(self):
        refusing_server = self.server(answer={'status': 'refused', 'reason': 'invalid_code'})
        certificate = common.make_certificate('localhost')
        server = self.server(answer=PAIRED, certificate=certificate)

        with common.box_log() as written:
            self.pair(link=refusing_server.link(REFUSED_CODE))
            self.pair(link=server.link(UNTRUSTED_CODE))
            answer = self.pair(link=server.link(CODE), trust_fingerprint=certificate.fingerprint)
            log = written()

        self.assertEqual(answer.get('status'), 'paired', answer)
        self.assertTrue(log)
        for secret in (REFUSED_CODE, UNTRUSTED_CODE, CODE, CREDENTIAL):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, log)


class TestDisconnect(common.BoxCase):

    def setUp(self):
        super().setUp()
        self.server_left = self.box_with_server(VECTOR_CREDENTIAL, certificate=common.make_certificate('localhost'))
        common.store_box_configuration(**SETTINGS_OF_THE_BOX, **KEPT_FOR_THE_SERVER)
        _answer, self.proof = self.unlock(VECTOR_SETTINGS_CODE)

    def serving(self, certificate):
        """Have the web server of the box present `certificate`, a `Certificate`, or none when it is None."""
        self.keep_web_certificate()
        if certificate:
            common.install_web_certificate(certificate)
        else:
            common.as_root('rm', '-f', str(common.WEB_CERTIFICATE))

    def assertNameForgottenAndChoiceKept(self):
        stored = common.box_configuration()
        self.assertNotIn('viin_box_address', stored)
        self.assertEqual(stored.get('viin_auto_update'), SETTINGS_OF_THE_BOX['viin_auto_update'])

    def test_box_service_restarts_after_a_disconnect(self):
        self.request(DISCONNECT, proof=self.proof)

        self.assertEqual(len(common.restarts), 1)

    def test_disconnected_box_given_its_server_again_sends_no_credential_on_the_certificate_trusted_before(self):
        self.request(DISCONNECT, proof=self.proof)
        # Given back without a pairing: this time no installer confirmed the certificate of the server.
        common.store_box_configuration(remote_server=self.server_left.url, token=VECTOR_CREDENTIAL)

        self.report_to(self.server_left)

        self.assertEqual(common.reported_credentials(self.server_left), set())

    def test_disconnect_clears_server_credential_and_what_the_layer_kept_about_them(self):
        self.serving(certificate_the_box_made())

        response = self.request(DISCONNECT, proof=self.proof)

        self.assertEqual(response.status_code, 200)
        stored = common.box_configuration()
        self.assertNotIn('remote_server', stored)
        self.assertNotIn('token', stored)
        self.assertEqual({key for key in stored if key.startswith('viin_')}, {'viin_auto_update'})
        self.assertEqual(common.trusted_certificates(), [])

    def test_disconnect_keeps_the_choice_to_update_by_itself_and_the_name_of_the_certificate_of_the_customer(self):
        self.serving(common.make_certificate(TYPED_NAME, valid_until=common.utc_now() + 400 * DAY))

        self.request(DISCONNECT, proof=self.proof)

        stored = common.box_configuration()
        self.assertEqual({key: stored.get(key) for key in SETTINGS_OF_THE_BOX}, SETTINGS_OF_THE_BOX)

    def test_disconnect_forgets_the_typed_name_while_the_box_serves_a_certificate_it_made_for_that_name(self):
        self.serving(certificate_the_box_made(TYPED_NAME))

        self.request(DISCONNECT, proof=self.proof)

        self.assertNameForgottenAndChoiceKept()

    def test_disconnect_forgets_the_typed_name_while_the_box_serves_the_certificate_of_the_image(self):
        self.serving(common.make_certificate(common.SHIPPED_COMMON_NAME, names=[TYPED_NAME]))

        self.request(DISCONNECT, proof=self.proof)

        self.assertNameForgottenAndChoiceKept()

    def test_disconnect_forgets_the_typed_name_while_the_box_serves_no_certificate(self):
        self.serving(None)

        self.request(DISCONNECT, proof=self.proof)

        self.assertNameForgottenAndChoiceKept()

    def test_disconnect_forgets_the_typed_name_while_the_certificate_of_the_customer_is_for_another_name(self):
        self.serving(common.make_certificate('till.shop.example', valid_until=common.utc_now() + 400 * DAY))

        self.request(DISCONNECT, proof=self.proof)

        self.assertNameForgottenAndChoiceKept()

    def test_disconnected_box_pairs_with_another_server(self):
        self.request(DISCONNECT, proof=self.proof)
        other_server = self.server(answer=PAIRED)

        answer = self.pair(link=other_server.link(CODE))

        self.assertEqual(answer.get('status'), 'paired', answer)
        stored = common.box_configuration()
        self.assertEqual(stored.get('remote_server'), other_server.url)
        self.assertEqual(stored.get('token'), CREDENTIAL)


class TestWhetherTheBoxIsPaired(common.BoxCase):

    def test_box_without_a_server_is_not_paired(self):
        self.assertIs(common.layer('pairing').is_paired(), False)

    def test_box_that_holds_the_credential_of_a_server_is_paired(self):
        self.box_with_server(CREDENTIAL)

        self.assertIs(common.layer('pairing').is_paired(), True)


class TestChangesOfTheServerAtOnce(PairingCase):

    def setUp(self):
        super().setUp()
        self.callers = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.addCleanup(self.callers.shutdown)
        self.first_server = self.server(
            ServerThatTakesItsTime, answer={'status': 'paired', 'token': CREDENTIAL, 'box_name': 'Packing line 2'})
        self.addCleanup(self.first_server.answer_now.set)

    def pairing_under_way(self):
        """Start a pairing with the first server and return it once that server holds its request."""
        pairing = self.callers.submit(common.call_route, PAIR, link=self.first_server.link(CODE))
        self.assertTrue(self.first_server.asked.wait(30))
        return pairing

    def test_second_of_two_pairing_requests_at_once_stores_no_server(self):
        second_server = self.server(answer={'status': 'paired', 'token': 'suite-credential-second', 'box_name': ''})
        first = self.pairing_under_way()

        second_answer = self.pair(link=second_server.link(CODE))
        self.first_server.answer_now.set()
        first_answer = first.result(60)

        self.assertEqual(first_answer.get('status'), 'paired', first_answer)
        self.assertEqual(second_answer.get('status'), 'failed', second_answer)
        self.assertEqual(second_server.requests, [])
        stored = common.box_configuration()
        self.assertEqual((stored.get('remote_server'), stored.get('token')), (self.first_server.url, CREDENTIAL))
        self.assertEqual(len(common.restarts), 1)

    def test_connect_route_of_core_called_during_a_pairing_meets_the_lock_of_the_paired_box(self):
        first = self.pairing_under_way()

        connecting = self.callers.submit(common.call_route, CONNECT, token=CORE_CONNECTION)
        # A box that did not make the call wait would have connected by now.
        concurrent.futures.wait([connecting], timeout=2)
        self.first_server.answer_now.set()

        self.assertEqual(first.result(60).get('status'), 'paired')
        self.assertEqual(connecting.result(60).get('reason'), LOCKED)
        stored = common.box_configuration()
        self.assertEqual((stored.get('remote_server'), stored.get('token')), (self.first_server.url, CREDENTIAL))


class TestPairingThatAsksFirst(PairingCase):

    def test_pairing_the_caller_does_not_permit_attempts_nothing(self):
        server = self.server(answer=PAIRED)

        answer = common.layer('pairing').pair(link=server.link(CODE), permitted=lambda: False)

        self.assertIsNone(answer)
        self.assertEqual(server.requests, [])
        self.assertNothingStored()

    def test_pairing_the_caller_permits_pairs_the_box(self):
        server = self.server(answer=PAIRED)

        answer = common.layer('pairing').pair(link=server.link(CODE), permitted=lambda: True)

        self.assertEqual(answer.get('status'), 'paired', answer)
        self.assertEqual(common.box_configuration().get('remote_server'), server.url)

    def test_caller_is_asked_once_no_other_pairing_can_come_in_between(self):
        pairing = common.layer('pairing')
        server, other_server = self.server(answer=PAIRED), self.server(answer=PAIRED)
        met_meanwhile = []

        def permitted():
            met_meanwhile.append(pairing.pair(link=other_server.link(CODE)))
            return True

        answer = pairing.pair(link=server.link(CODE), permitted=permitted)

        self.assertEqual(answer.get('status'), 'paired', answer)
        self.assertEqual([attempt.get('status') for attempt in met_meanwhile], ['failed'])
        self.assertEqual(other_server.requests, [])
        self.assertEqual(common.box_configuration().get('remote_server'), server.url)


class ResetFileCase(common.BoxCase):
    """A paired box started with the reset file on its card, while its web server serves `served_at_the_reset`."""
    reset_file = common.BOOT_PARTITION / 'viindoo-reset'

    def served_at_the_reset(self):
        return certificate_the_box_made()

    def setUp(self):
        super().setUp()
        self.box_with_server(VECTOR_CREDENTIAL, certificate=common.make_certificate('localhost'))
        common.store_box_configuration(**SETTINGS_OF_THE_BOX, **KEPT_FOR_THE_SERVER)
        # The box that starts may make itself another certificate.
        self.keep_web_certificate()
        common.install_web_certificate(self.served_at_the_reset())
        self.trusted_file = Path(common.box_configuration()[common.TRUSTED_CERTIFICATES_KEY])
        common.as_root('touch', str(self.reset_file))
        self.addCleanup(common.as_root, 'rm', '-f', str(self.reset_file))
        # A setting only an unlocked request may change tells, on the box that starts, whether it is still locked.
        self.box = common.in_new_process(common.start_report, probes=[common.PING, WIFI_CLEAR])


class TestResetFile(ResetFileCase):

    def test_reset_file_found_at_start_clears_the_server_and_everything_the_layer_kept_for_it(self):
        stored = common.box_configuration()
        self.assertNotIn('remote_server', stored)
        self.assertNotIn('token', stored)
        self.assertEqual({key for key in stored if key.startswith('viin_')}, {'viin_auto_update'})
        self.assertFalse(self.trusted_file.exists())
        self.assertEqual(self.box['answers'], {common.PING: 200, WIFI_CLEAR: 200})

    def test_reset_file_keeps_the_choice_to_update_by_itself_and_forgets_the_name_typed_for_the_box(self):
        stored = common.box_configuration()

        self.assertEqual(stored.get('viin_auto_update'), SETTINGS_OF_THE_BOX['viin_auto_update'])
        self.assertNotIn('viin_box_address', stored)

    def test_reset_file_is_deleted_once_the_box_honoured_it(self):
        self.assertFalse(self.reset_file.exists())


class TestResetFileNamedAsAComputerNamesATextFile(TestResetFile):
    reset_file = common.BOOT_PARTITION / 'viindoo-reset.txt'


class TestResetFileWhileTheCertificateOfTheCustomerIsServed(ResetFileCase):

    def served_at_the_reset(self):
        return common.make_certificate(TYPED_NAME, valid_until=common.utc_now() + 400 * DAY)

    def test_reset_file_keeps_the_name_typed_for_the_box_the_certificate_of_the_customer_is_issued_for(self):
        stored = common.box_configuration()

        self.assertEqual({key: stored.get(key) for key in SETTINGS_OF_THE_BOX}, SETTINGS_OF_THE_BOX)
