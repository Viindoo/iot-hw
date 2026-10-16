"""The scripts that build and install a box: what they keep out of a public log, what they leave in a configuration."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import common
import services_common as box

BUILD_SCRIPT = box.REPOSITORY / 'box' / 'build' / 'build_image.sh'
INSTALL_SCRIPT = box.REPOSITORY / 'box' / 'install' / 'install_layer.sh'
SECRET = 'S3cretFromCore'


def _bash(program, *arguments, stdin=None):
    return subprocess.run(['bash', '-c', program, '_', *arguments], input=stdin, capture_output=True, text=True,
                          check=False, timeout=60)


class TestPasswordCoreChoosesForTheBoxUser(unittest.TestCase):
    """The image script of core prints the password it sets, on the line after a line that reads "password"."""

    def test_the_password_core_prints_is_not_in_the_log_and_the_other_lines_are(self):
        done = _bash('source "$1"; withhold_core_password', str(BUILD_SCRIPT),
                     stdin=f'first\npassword\n{SECRET}\nlast\n')

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn(SECRET, done.stdout)
        self.assertEqual(done.stdout.splitlines()[0:2], ['first', 'password'])
        self.assertEqual(done.stdout.splitlines()[-1], 'last')
        self.assertEqual(len(done.stdout.splitlines()), 4)

    def test_the_password_is_withheld_when_the_log_has_windows_line_ends(self):
        done = _bash('source "$1"; withhold_core_password', str(BUILD_SCRIPT), stdin=f'password\r\n{SECRET}\r\n')

        self.assertNotIn(SECRET, done.stdout)

    def test_a_command_that_prints_it_and_fails_still_fails_the_build_and_prints_no_password(self):
        done = _bash(
            'source "$1"; run_withholding_password bash -c "printf \'password\\n%s\\n\' \\"$2\\"; exit 7"',
            str(BUILD_SCRIPT), SECRET)

        self.assertEqual(done.returncode, 7, done.stderr)
        self.assertNotIn(SECRET, done.stdout + done.stderr)

    def test_the_password_core_prints_on_standard_error_is_not_in_the_log_and_the_exit_status_stays_the_commands(self):
        done = _bash(
            'source "$1"; run_withholding_password bash -c "printf \'password\\n%s\\n\' \\"$2\\" >&2; exit 5"',
            str(BUILD_SCRIPT), SECRET)

        self.assertEqual(done.returncode, 5, done.stderr)
        self.assertNotIn(SECRET, done.stdout + done.stderr)

    def test_a_command_that_prints_it_and_succeeds_passes_the_other_lines_on(self):
        done = _bash(
            'source "$1"; run_withholding_password bash -c "printf \'before\\npassword\\n%s\\nafter\\n\' \\"$2\\""',
            str(BUILD_SCRIPT), SECRET)

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn(SECRET, done.stdout)
        self.assertEqual([line for line in done.stdout.splitlines() if line in ('before', 'after')],
                         ['before', 'after'])


class TestBoxUserWithALockedPassword(common.BoxStateCase):
    """The installer locks the password of the box user (`!`); core still has to be able to set a new one."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        common.start()

    @staticmethod
    def shadow_field():
        line = subprocess.run(['sudo', '-n', 'grep', '^pi:', '/etc/shadow'], capture_output=True, text=True,
                              check=True, timeout=30).stdout.strip()
        return line.split(':')[1]

    @staticmethod
    def hash_of(password, setting):
        """What the system's own crypt(3) makes of `password` under the algorithm and salt `setting` names."""
        return subprocess.run(['perl', '-e', 'print crypt($ENV{PASSWORD}, $ENV{SETTING})'],
                              env={'PASSWORD': password, 'SETTING': setting}, capture_output=True, text=True,
                              check=True, timeout=30).stdout

    def test_core_sets_a_working_password_on_a_box_user_whose_password_is_locked(self):
        kept = self.shadow_field()
        self.addCleanup(subprocess.run, ['sudo', '-n', 'usermod', '--password', kept, 'pi'], check=False,
                        capture_output=True, timeout=30)
        subprocess.run(['sudo', '-n', 'usermod', '--password', '!', 'pi'], check=True, capture_output=True,
                       timeout=30)
        self.assertEqual(self.shadow_field(), '!')

        password = common.core('tools.helpers').generate_password()

        field = self.shadow_field()
        self.assertNotEqual(field, '!')
        self.assertEqual(self.hash_of(password, field), field)


class TestBoxUserLockedByTheInstaller(unittest.TestCase):
    """Locking the password of the box user leaves no copy of the field it replaced."""

    BACKUPS = ('/etc/shadow-', '/etc/gshadow-')

    def installer_function(self):
        text = INSTALL_SCRIPT.read_text().splitlines()
        start = next(number for number, line in enumerate(text) if line.startswith('lock_box_user_password() {'))
        end = next(number for number in range(start, len(text)) if text[number] == '}')
        return '\n'.join(text[start:end + 1])

    def test_no_copy_of_the_account_files_from_before_the_lock_is_left(self):
        kept = subprocess.run(['sudo', '-n', 'getent', 'shadow', 'pi'], capture_output=True, text=True, check=True,
                              timeout=30).stdout.strip().split(':')[1]
        self.addCleanup(subprocess.run, ['sudo', '-n', 'usermod', '--password', kept, 'pi'], check=False,
                        capture_output=True, timeout=30)
        subprocess.run(['sudo', '-n', 'usermod', '--password', 'x-not-locked', 'pi'], check=True,
                       capture_output=True, timeout=30)
        program = ('BOX_USER=pi; fail() { echo "$*" >&2; exit 1; }\n' + self.installer_function()
                   + '\nlock_box_user_password')

        done = subprocess.run(['sudo', '-n', 'bash', '-c', program], capture_output=True, text=True, check=False,
                              timeout=60)

        self.assertEqual(done.returncode, 0, done.stderr)
        left = subprocess.run(['sudo', '-n', 'ls', *self.BACKUPS], capture_output=True, text=True, check=False,
                              timeout=30)
        self.assertEqual(left.stdout.split(), [], left.stdout)


class TestConfigurationOfAnInstalledBox(unittest.TestCase):
    """The installer writes its options into a configuration core may have written its own section into."""

    PAIRED = (
        '[options]\nadmin_passwd = kept-secret\nhttp_port = 8069\n\n'
        '[iot.box]\nserver_url = https://example.invalid\nidentifier = box-1\n')

    def installer_functions(self):
        """The functions of the installer that write the options: its own text between two markers, as shipped."""
        text = INSTALL_SCRIPT.read_text().splitlines()
        start = next(number for number, line in enumerate(text) if line.startswith('set_options() {'))
        end = next(number for number, line in enumerate(text) if line.startswith('configure_box_service'))
        return '\n'.join(text[start:end])

    def run_installer_functions(self, configuration_text, body):
        directory = tempfile.mkdtemp(prefix='box-suite-installer-')
        self.addCleanup(subprocess.run, ['rm', '-rf', directory], check=False)
        configuration = Path(directory, 'odoo.conf')
        configuration.write_text(configuration_text)
        configuration.chmod(0o600)
        done = _bash(f'conf="$1"\n{self.installer_functions()}\n{body}', str(configuration))
        return done, configuration

    def test_options_are_written_into_the_options_section_of_a_configuration_whose_last_section_is_the_boxs_own(self):
        done, configuration = self.run_installer_functions(
            self.PAIRED, 'set_options addons_path /a,/b server_wide_modules m1,m2 list_db False')

        parsed = _parse(configuration)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(dict(parsed['options']),
                         {'admin_passwd': 'kept-secret', 'http_port': '8069', 'addons_path': '/a,/b',
                          'server_wide_modules': 'm1,m2', 'list_db': 'False'})
        self.assertEqual(dict(parsed['iot.box']), {'server_url': 'https://example.invalid', 'identifier': 'box-1'})
        self.assertEqual(configuration.stat().st_mode & 0o777, 0o600)

    def test_master_password_in_the_options_section_is_found_and_one_in_another_section_is_not(self):
        in_options, _ = self.run_installer_functions(self.PAIRED, 'has_option admin_passwd')
        elsewhere, _ = self.run_installer_functions(
            '[options]\nhttp_port = 8069\n\n[iot.box]\nadmin_passwd = not-the-masters\n', 'has_option admin_passwd')

        self.assertEqual(in_options.returncode, 0)
        self.assertEqual(elsewhere.returncode, 1)


class TestImageBuildRefusesAReleaseTheWorkflowDidNotCheck(unittest.TestCase):
    """The build reads the tag itself and agrees with what the release workflow checked before it needs any tool."""

    CANDIDATE_TAG = 'box-18.0-rc.1'
    SUPPORTED_TAG = 'box-18.0-1.0'

    def git(self, *arguments):
        done = subprocess.run(['git', '-c', 'user.name=Box Test', '-c', 'user.email=box@example.invalid',
                               '-C', self.repository, *arguments], capture_output=True, text=True, check=True,
                              timeout=60)
        return done.stdout.strip()

    def commit_pointer(self, release, candidate):
        pointer = {'release': release, 'series': '18.0', 'odoo_commit': 'a' * 40, 'min_image': '1.0',
                   'candidate': candidate}
        (Path(self.repository) / 'box-release.json').write_text(json.dumps(pointer))
        self.git('add', 'box-release.json')
        self.git('commit', '--message', f'release {release}')
        return self.git('rev-parse', 'HEAD')

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repository = str(Path(directory.name) / 'layer')
        self.scratch = directory.name
        subprocess.run(['git', 'init', '--quiet', '--initial-branch=main', self.repository], check=True, timeout=60)
        self.tagged = self.commit_pointer('rc.1', True)
        self.git('tag', self.CANDIDATE_TAG)
        self.later = self.commit_pointer('rc.2', True)
        self.git('checkout', '--quiet', self.tagged)

    def read_release(self, tag, expect_mode='', expect_commit='', series_ref=''):
        """The exit status of the build's own reading of `tag`, with the expectations the workflow would pass."""
        program = ('source "$1"; tag="$2"; repository="$3"; expect_mode="$4"; expect_commit="$5"; series_ref="$6"; '
                   'read_release')
        return _bash(program, str(BUILD_SCRIPT), tag, self.repository, expect_mode, expect_commit, series_ref
                     ).returncode

    def test_tag_and_checkout_at_the_checked_commit_are_accepted(self):
        self.assertEqual(self.read_release(self.CANDIDATE_TAG, expect_commit=self.tagged), 0)

    def test_tag_of_the_expected_kind_is_accepted(self):
        self.assertEqual(self.read_release(self.CANDIDATE_TAG, expect_mode='candidate'), 0)

    def test_tag_naming_another_commit_than_the_checked_one_is_refused(self):
        self.git('checkout', '--quiet', self.later)

        self.assertNotEqual(self.read_release(self.CANDIDATE_TAG, expect_commit=self.later), 0)

    def test_checkout_away_from_the_checked_commit_is_refused(self):
        self.git('checkout', '--quiet', self.later)

        self.assertNotEqual(self.read_release(self.CANDIDATE_TAG, expect_commit=self.tagged), 0)

    def test_candidate_tag_is_refused_when_a_supported_release_was_expected(self):
        self.assertNotEqual(self.read_release(self.CANDIDATE_TAG, expect_mode='supported'), 0)

    def test_supported_tag_is_refused_when_a_candidate_release_was_expected(self):
        self.git('checkout', '--quiet', 'main')
        self.commit_pointer('1.0', False)
        self.git('tag', self.SUPPORTED_TAG)

        self.assertNotEqual(self.read_release(self.SUPPORTED_TAG, expect_mode='candidate', series_ref='main'), 0)


class TestPublishedChecksums(unittest.TestCase):
    """What the build writes beside the image: a reader who downloaded the image and the checksum file checks them."""

    def test_checksum_file_lists_only_the_image_so_a_reader_who_downloaded_nothing_else_gets_a_pass(self):
        with tempfile.TemporaryDirectory(prefix='box-suite-') as directory:
            work, posbox, output, reader = (Path(directory, name) for name in ('work', 'posbox', 'out', 'reader'))
            for folder in (work, posbox, output, reader):
                folder.mkdir()
            (posbox / 'iotbox.img').write_bytes(b'bytes of an image')
            (work / 'manifest.txt').write_text('image: viindoo-iot-box\n')
            # The compressor is not the matter here: it only has to write the file that is published.
            program = ('xz() { cat "${posbox_dir}/iotbox.img"; }\n'
                       'source "$1"; work=$2; posbox_dir=$3; output=$4; image_name=iotbox.img.xz; publish_files')

            published = _bash(program, str(BUILD_SCRIPT), str(work), str(posbox), str(output))
            for name in ('iotbox.img.xz', 'SHA256SUMS'):
                (reader / name).write_bytes((output / name).read_bytes())
            check = subprocess.run(['sha256sum', '--check', 'SHA256SUMS'], cwd=reader, capture_output=True,
                                   text=True, check=False, timeout=60)

        self.assertEqual(published.returncode, 0, published.stderr)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)


class TestVendorWordsOfThePageTexts(unittest.TestCase):
    """The rule that keeps the vendor of core and its series number out of every text a customer reads."""

    def vendor_words(self, text):
        return box.page_text_check().vendor_words(text)

    def test_the_vendor_naming_a_kind_of_server_is_found(self):
        for text in ('Connect to your Odoo Community server', 'your own Odoo Community server with the Viindoo apps',
                     'm\u00e1y ch\u1ee7 Odoo Community c\u1ee7a ri\u00eang b\u1ea1n', 'Odoo Community.', 'Odoo Community'):
            with self.subTest(text=text):
                self.assertTrue(self.vendor_words(text))

    def test_the_vendor_naming_a_product_is_found(self):
        for text in ('Odoo Community Edition', 'Odoo Community IoT Box', "Odoo Community's IoT Box",
                     'Odoo Community\u2019s IoT Box', 'Odoo Community-Edition', 'Odoo Community (Edition)'):
            with self.subTest(text=text):
                self.assertTrue(self.vendor_words(text))

    def test_the_vendor_named_in_any_other_way_is_found(self):
        for text in ("Odoo's IoT Box", 'an ENTERPRISE plan', 'see odoo.com', 'an enterprise network',
                     'ODOO Communityx server', 'Odoo community', 'odoo'):
            with self.subTest(text=text):
                self.assertTrue(self.vendor_words(text))

    def test_the_viindoo_names_of_servers_and_of_the_box_are_not_the_vendor(self):
        for text in ('The server can be Viindoo Cloud, or your own Viindoo server.',
                     'M\u00e1y ch\u1ee7 c\u00f3 th\u1ec3 l\u00e0 Viindoo Cloud, ho\u1eb7c m\u00e1y ch\u1ee7 Viindoo c\u1ee7a ri\u00eang b\u1ea1n.',
                     'Viindoo IoT Box', 'viindoo.com'):
            with self.subTest(text=text):
                self.assertEqual(self.vendor_words(text), [])

    def test_a_series_number_is_found_and_a_version_of_another_shape_is_not(self):
        self.assertTrue(self.vendor_words('Works with 18.0'))
        self.assertEqual(self.vendor_words('Image 1.0, address 192.168.10.1, 118.04'), [])


class TestKeysOfThePageTexts(unittest.TestCase):
    """The rule that a text of the box page exists in every language the page is shown in."""

    def missing(self, **languages):
        return box.page_text_check().missing_keys(languages)

    def test_a_key_missing_in_vietnamese_is_reported_with_its_file(self):
        found = self.missing(**{'en.json': {'save': 'Save', 'cancel': 'Cancel'}, 'vi.json': {'save': 'L\u01b0u'}})
        self.assertEqual(len(found), 1)
        self.assertIn('vi.json', found[0])
        self.assertIn('"cancel"', found[0])

    def test_a_key_missing_in_english_is_reported_with_its_file(self):
        found = self.missing(**{'en.json': {'save': 'Save'}, 'vi.json': {'save': 'L\u01b0u', 'cancel': 'H\u1ee7y'}})
        self.assertEqual(len(found), 1)
        self.assertIn('en.json', found[0])
        self.assertIn('"cancel"', found[0])

    def test_a_key_missing_in_a_third_language_is_reported(self):
        found = self.missing(**{'en.json': {'save': 'Save'}, 'vi.json': {'save': 'L\u01b0u'}, 'fr.json': {}})
        self.assertEqual(len(found), 1)
        self.assertIn('fr.json', found[0])

    def test_languages_with_the_same_keys_are_clean(self):
        self.assertEqual(self.missing(**{'en.json': {'save': 'Save'}, 'vi.json': {'save': 'L\u01b0u'}}), [])

    def test_the_texts_of_the_box_page_hold_the_same_keys_in_every_language(self):
        directory = box.REPOSITORY / 'viin_hw_homepage' / 'static' / 'src' / 'app' / 'i18n'
        texts = {file.name: json.loads(file.read_text(encoding='utf-8')) for file in directory.glob('*.json')}
        self.assertEqual(self.missing(**texts), [])


def _parse(path):
    import configparser

    parser = configparser.RawConfigParser()
    parser.optionxform = str
    parser.read(path)
    return parser
