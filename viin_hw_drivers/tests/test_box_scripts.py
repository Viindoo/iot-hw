"""The scripts that build and install a box: what they keep out of a public log, what they leave in a configuration."""
import hashlib
import http.server
import json
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

import common
import services_common as box

BUILD_SCRIPT = box.REPOSITORY / 'box' / 'build' / 'build_image.sh'
INSTALL_SCRIPT = box.REPOSITORY / 'box' / 'install' / 'install_layer.sh'
INSTALL_LIBRARY = box.REPOSITORY / 'box' / 'install' / 'lib.sh'
RELEASE_CHECK = box.REPOSITORY / 'box' / 'build' / 'check_release.sh'
APPLY_SCRIPT = box.SYSTEM_FILES / 'apply.sh'
IMAGE_WORKFLOW = box.REPOSITORY / '.github' / 'workflows' / 'box-image.yml'
POLICY = 'etc/chromium/policies/managed/viindoo-local-network.json'
BUILD_APT = box.REPOSITORY / 'box' / 'build' / 'apt'
BUILD_APT_SETTINGS = next((BUILD_APT / 'etc' / 'apt' / 'apt.conf.d').iterdir())
BUILD_APT_SOURCES = next((BUILD_APT / 'etc' / 'apt' / 'sources.list.d').iterdir())
BUILD_APT_GET = BUILD_APT / 'usr' / 'local' / 'bin' / 'apt-get'
BUILD_APT_PINS = BUILD_APT / 'etc' / 'apt' / 'preferences.d' / 'viindoo-build.pref'
# The text core's image script writes to /etc/apt/apt.conf.d/99acquire-retries.
CORE_APT_RETRIES = 'Acquire::Retries 16;\n'
SECRET = 'S3cretFromCore'


# The firmware configuration of a box as core's image script leaves it: the stock sections, the last of them [all].
FIRMWARE_CONFIGURATION = (
    'dtparam=audio=on\n[pi4]\nmax_framebuffers=2\n[all]\ndtoverlay=vc4-fkms-v3d\ndisable_overscan=1\n')


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
    """The installer locks the password of the box user (``!``); core still has to be able to set a new one."""

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
        """Hash a password with the system's own crypt(3).

        :param password: the password to hash
        :param setting: the algorithm and the salt, in the form crypt(3) takes them
        :return: the hash
        """
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
        """Read the functions of the installer that write the options.

        :return: the text of the installer between two markers, as shipped
        """
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


class TestLayerInstalledInTheHomeOfAnImage(unittest.TestCase):
    """The image script of core copies its files into the image as root. The installer runs on that image: its home,
    the core tree and the dotfiles in it can belong to root, and git refuses to work as the box user in a tree that
    belongs to another user. The installer still has to leave the layer, core and the home to the box user."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.scratch = Path(directory.name)
        releases = box.Releases(self.scratch / 'remotes')
        self.core_remote = releases.core_remote
        self.core_commit = releases.core_commits[0]
        self.other_core_commit = releases.core_commits[1]
        self.layer_source = self.scratch / 'layer-source'
        shutil.copytree(box.REPOSITORY / 'box', self.layer_source / 'box', ignore=shutil.ignore_patterns('__pycache__'))
        (self.layer_source / 'box-release.json').write_text(box.pointer('1.0', self.core_commit))
        releases.git(self.layer_source, 'init', '--quiet', '--initial-branch=start')
        releases.git(self.layer_source, 'add', '--all')
        releases.git(self.layer_source, 'commit', '--quiet', '-m', 'layer')
        self.layer_commit = releases.git(self.layer_source, 'rev-parse', 'HEAD')
        self.box_user = pwd.getpwnam('pi')
        self.home = self.scratch / 'image-home'
        self.addCleanup(self.sudo, 'rm', '-rf', str(self.home))
        # The home as the image holds it: owned by root, with the configuration core leaves, owned by the box user.
        self.sudo('install', '--directory', '--owner', 'root', '--group', 'root', '--mode', '0755', str(self.home))
        self.sudo('install', '--owner', 'pi', '--group', 'pi', '--mode', '0600', '/dev/null',
                  str(self.home / 'odoo.conf'))
        self.sudo('tee', str(self.home / 'odoo.conf'), stdin='[options]\nhttp_port = 8069\n')
        # The firmware configuration of the image, which the installer writes the screen setting into.
        self.boot_config = self.scratch / 'config.txt'
        self.boot_config.write_text(FIRMWARE_CONFIGURATION)
        # The installer locks the password of the box user; the suite needs it back as it was.
        kept = self.sudo('getent', 'shadow', 'pi').stdout.strip().split(':')[1]
        self.addCleanup(subprocess.run, ['sudo', '-n', 'usermod', '--password', kept, 'pi'], check=False,
                        capture_output=True, timeout=30)

    @staticmethod
    def sudo(*command, stdin=None):
        return subprocess.run(['sudo', '-n', *command], input=stdin, capture_output=True, text=True, check=True,
                              timeout=120)

    def copy_core_tree_as_the_image_script_does(self, whole_tree_belongs_to_root):
        """Lay out the core tree and the dotfiles as the image holds them when the installer starts.

        The image script clones core as root, copies it into the image, hands the tree to the box user inside the
        image, and then copies a few files over it as root again (``cp -a`` of a root-owned tree sets the owner of
        every directory on the way).

        :param whole_tree_belongs_to_root: when true the image script handed nothing over to the box user
        """
        tree = self.home / 'odoo'
        self.sudo('git', '-c', 'safe.directory=*', 'clone', '--quiet', str(self.core_remote), str(tree))
        self.sudo('git', '-c', 'safe.directory=*', '-C', str(tree), 'reset', '--hard', '--quiet',
                  self.other_core_commit)
        if not whole_tree_belongs_to_root:
            self.sudo('chown', '-R', 'pi:pi', str(tree))
        self.sudo('install', '--directory', '--owner', 'root', '--group', 'root', str(tree / 'addons'),
                  str(tree / 'addons' / 'point_of_sale'))
        self.sudo('install', '--owner', 'root', '--group', 'root', '/dev/null',
                  str(tree / 'addons' / 'point_of_sale' / '__manifest__.py'))
        self.sudo('chown', 'root:root', str(tree))
        for dotfile in ('.bashrc', '.inputrc', '.vimrc', '.xsession'):
            self.sudo('install', '--owner', 'root', '--group', 'root', '/dev/null', str(self.home / dotfile))

    def copy_existing_layer_tree(self):
        tree = self.home / 'iot-hw'
        self.sudo('git', '-c', 'safe.directory=*', 'clone', '--quiet', str(self.layer_source), str(tree))
        self.sudo('chown', 'root:root', str(tree))

    def install(self):
        return subprocess.run(
            ['sudo', '-n', 'bash', str(INSTALL_SCRIPT), '--layer-ref', 'start', '--layer-source',
             str(self.layer_source), '--core-source', str(self.core_remote), '--home', str(self.home),
             '--skip-apt', '--skip-system', '--boot-config', str(self.boot_config)],
            capture_output=True, text=True, check=False, timeout=300)

    def as_box_user(self, *command):
        return subprocess.run(['sudo', '-n', 'runuser', '-u', 'pi', '--', 'env', f'HOME={self.home}', *command],
                              capture_output=True, text=True, check=False, timeout=60)

    def head_as_box_user(self, tree):
        """Read a commit the way the box software reads it: by git, as the box user, with no exception.

        :param tree: a git tree
        :return: the commit its head names
        """
        done = self.as_box_user('git', '-C', str(tree), 'rev-parse', 'HEAD')
        self.assertEqual(done.returncode, 0, f'{tree}: {done.stderr}')
        return done.stdout.strip()

    def assert_installed_for_the_box_user(self):
        self.assertEqual(self.head_as_box_user(self.home / 'iot-hw'), self.layer_commit)
        self.assertEqual(self.head_as_box_user(self.home / 'odoo'), self.core_commit)
        for tree in ('iot-hw', 'odoo'):
            for directory, names, files in os.walk(self.home / tree):
                for name in [*names, *files]:
                    path = os.path.join(directory, name)
                    self.assertEqual(os.lstat(path).st_uid, self.box_user.pw_uid, path)
            self.assertEqual((self.home / tree).stat().st_uid, self.box_user.pw_uid, tree)
            status = self.as_box_user('git', '-C', str(self.home / tree), 'status', '--short')
            self.assertEqual(status.returncode, 0, status.stderr)

    def test_layer_and_core_are_installed_for_the_box_user_whoever_owns_its_home(self):
        done = self.install()

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_installed_for_the_box_user()

    def test_core_tree_the_image_script_copied_over_as_root_is_taken_over_by_the_box_user(self):
        self.copy_core_tree_as_the_image_script_does(whole_tree_belongs_to_root=False)

        done = self.install()

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_installed_for_the_box_user()

    def test_core_tree_that_belongs_to_root_through_and_through_is_taken_over_by_the_box_user(self):
        self.copy_core_tree_as_the_image_script_does(whole_tree_belongs_to_root=True)

        done = self.install()

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_installed_for_the_box_user()

    def test_layer_tree_that_exists_and_belongs_to_root_is_taken_over_by_the_box_user(self):
        self.copy_core_tree_as_the_image_script_does(whole_tree_belongs_to_root=False)
        self.copy_existing_layer_tree()

        done = self.install()

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_installed_for_the_box_user()

    def test_installer_sets_the_screen_setting_of_the_firmware_once(self):
        first = self.install()
        written = self.boot_config.read_text()
        second = self.install()

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(written, FIRMWARE_CONFIGURATION + 'hdmi_force_hotplug=1\n')
        self.assertEqual(self.boot_config.read_text(), written)

    def test_installer_fails_naming_the_firmware_configuration_when_there_is_none(self):
        self.boot_config.unlink()

        done = self.install()

        self.assertNotEqual(done.returncode, 0)
        self.assertIn(str(self.boot_config), done.stderr)

    def test_box_user_can_still_create_its_files_in_its_home_after_the_install(self):
        """The box software stages its state file in the home before it replaces the old one."""
        self.copy_core_tree_as_the_image_script_does(whole_tree_belongs_to_root=False)

        done = self.install()

        self.assertEqual(done.returncode, 0, done.stderr)
        staged = self.home / 'viin-box-state.json.new'
        written = self.as_box_user('touch', str(staged))
        self.assertEqual(written.returncode, 0, written.stderr)


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
        """Run the build's own reading of a tag, with the expectations the workflow would pass.

        :param tag: the tag to read
        :param expect_mode: ``candidate``, ``supported`` or empty
        :param expect_commit: the commit the workflow checked, or empty
        :param series_ref: the branch a supported release must be on, or empty
        :return: the exit status
        """
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
        self.assertEqual(self.vendor_words('Image 1.0, address 192.0.2.1, 118.04'), [])


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


class TestSystemFilesOfTheBox(unittest.TestCase):
    """``apply.sh`` puts every system file of the layer where a box reads it, with its mode, and only once."""

    @staticmethod
    def apply():
        return subprocess.run(['sudo', '-n', 'bash', str(APPLY_SCRIPT)], capture_output=True, text=True, check=False,
                              timeout=common.PROCESS_TIMEOUT)

    @staticmethod
    def listed_files():
        """Read the list of files ``apply.sh`` installs.

        :return: the mode of each file, by its path on the box
        """
        text = APPLY_SCRIPT.read_text()
        names = dict(re.findall(r'^readonly (\w+)=(\S+)$', text, re.MULTILINE))
        block = re.search(r'^readonly FILES="\n(.*?)"$', text, re.MULTILINE | re.DOTALL).group(1)
        listed = {}
        for line in block.splitlines():
            if line.strip():
                path, mode = re.sub(r'\$\{(\w+)\}', lambda name: names[name.group(1)], line).split()
                listed[path] = int(mode, 8)
        return listed

    def test_every_file_of_the_tree_is_listed_and_lands_with_its_mode_and_a_second_run_installs_nothing(self):
        installed = self.apply()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        listed = self.listed_files()
        files = {path: Path('/', path) for path in listed}
        tree = {path.relative_to(box.SYSTEM_FILES).as_posix() for path in box.SYSTEM_FILES.rglob('*')
                if path.is_file() and path.relative_to(box.SYSTEM_FILES).as_posix() not in ('apply.sh', 'README.md')}

        self.assertEqual(set(listed), tree)
        for path, mode in listed.items():
            with self.subTest(path=path):
                self.assertEqual(stat.S_IMODE(Path('/', path).stat().st_mode), mode)
                self.assertEqual(Path('/', path).read_bytes(), (box.SYSTEM_FILES / path).read_bytes())
        before = {path: (file.stat().st_ino, file.stat().st_mtime_ns) for path, file in files.items()}
        again = self.apply()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual({path: (file.stat().st_ino, file.stat().st_mtime_ns) for path, file in files.items()}, before)

    def test_roll_back_guard_of_the_release_can_run(self):
        done = subprocess.run(['python3', '-I', str(box.GUARD), '--check'], capture_output=True, text=True,
                              check=False, timeout=common.PROCESS_TIMEOUT)

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_kiosk_browser_policy_is_installed_on_a_box_that_has_none(self):
        policy = Path('/', POLICY)
        common.as_root('rm', '-f', str(policy))

        done = self.apply()

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(json.loads(policy.read_text()), {'LocalNetworkAccessAllowedForUrls': ['*']})
        self.assertEqual(stat.S_IMODE(policy.stat().st_mode), 0o644)
        self.assertEqual(policy.stat().st_uid, 0)


FIRMWARE_CASES = {
    'last section is all': (FIRMWARE_CONFIGURATION, FIRMWARE_CONFIGURATION + 'hdmi_force_hotplug=1\n'),
    'last section is another one': ('[all]\na=1\n[cm4]\nb=2\n', '[all]\na=1\n[cm4]\nb=2\n[all]\nhdmi_force_hotplug=1\n'),
    'no section and no end of line': ('a=1', 'a=1\n[all]\nhdmi_force_hotplug=1\n'),
    'another value in the last all': ('[all]\nhdmi_force_hotplug=0\na=1\n', '[all]\nhdmi_force_hotplug=1\na=1\n'),
    'set only in an earlier section': ('[pi4]\nhdmi_force_hotplug=1\n[all]\na=1\n',
                                       '[pi4]\nhdmi_force_hotplug=1\n[all]\na=1\nhdmi_force_hotplug=1\n'),
    'only a comment': ('[all]\n#hdmi_force_hotplug=1\n', '[all]\n#hdmi_force_hotplug=1\nhdmi_force_hotplug=1\n'),
}


class TestFirmwareScreenSetting(unittest.TestCase):
    """The firmware of a box reports a screen on its output whether or not one is plugged in, so X starts."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name, 'config.txt')

    def firmware(self, mode):
        return _bash('source "$1"; firmware_hotplug "$2" "$3"', str(INSTALL_LIBRARY), mode, str(self.config))

    def test_setting_goes_under_the_last_all_section_whatever_the_file_ends_with(self):
        for case, (before, after) in FIRMWARE_CASES.items():
            with self.subTest(case=case):
                self.config.write_text(before)

                done = self.firmware('set')

                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(self.config.read_text(), after)
                self.assertEqual(self.firmware('check').returncode, 0)

    def test_file_that_holds_the_setting_is_not_written_again(self):
        self.config.write_text(FIRMWARE_CONFIGURATION)
        self.firmware('set')
        written = self.config.stat().st_mtime_ns
        text = self.config.read_text()

        done = self.firmware('set')

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.config.read_text(), text)
        self.assertEqual(self.config.stat().st_mtime_ns, written)

    def test_file_without_the_setting_under_its_last_all_section_fails_the_check(self):
        for case, (before, _after) in FIRMWARE_CASES.items():
            with self.subTest(case=case):
                self.config.write_text(before)

                self.assertEqual(self.firmware('check').returncode, 1)


class TestImageHoldsNoSshHostKey(unittest.TestCase):
    """A host key in the image would be the key of every box written from it: each box makes its own at its first
    start, through the unit of the base system."""

    UNIT = 'regenerate_ssh_host_keys.service'
    KEYS = ('ssh_host_ed25519_key', 'ssh_host_ed25519_key.pub', 'ssh_host_rsa_key', 'ssh_host_ecdsa_key.pub')

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name, 'root')
        (self.root / 'etc' / 'ssh').mkdir(parents=True)
        (self.root / 'etc' / 'ssh' / 'sshd_config').write_text('Port 22\n')
        wants = self.root / 'etc' / 'systemd' / 'system' / 'multi-user.target.wants'
        wants.mkdir(parents=True)
        (wants / self.UNIT).symlink_to(f'/lib/systemd/system/{self.UNIT}')
        (self.root / 'usr' / 'lib' / 'systemd' / 'system').mkdir(parents=True)
        (self.root / 'usr' / 'lib' / 'systemd' / 'system' / self.UNIT).write_text('[Service]\n')

    def plant_keys(self, *names):
        for name in names or self.KEYS:
            (self.root / 'etc' / 'ssh' / name).write_text('key\n')

    def run_function(self, function):
        return _bash(f'source "$1"; root="$2"; {function}', str(BUILD_SCRIPT), str(self.root))

    def keys_left(self):
        return sorted(path.name for path in (self.root / 'etc' / 'ssh').glob('ssh_host_*'))

    def test_image_with_a_host_key_is_refused_and_the_path_is_named(self):
        self.plant_keys('ssh_host_ed25519_key.pub')

        done = self.run_function('refuse_ssh_host_keys')

        self.assertNotEqual(done.returncode, 0)
        self.assertIn('/etc/ssh/ssh_host_ed25519_key.pub', done.stderr)

    def test_image_with_no_host_key_is_accepted(self):
        self.assertEqual(self.run_function('refuse_ssh_host_keys').returncode, 0)

    def test_host_keys_the_build_made_are_removed_and_the_rest_of_the_directory_stays(self):
        self.plant_keys()

        done = self.run_function('remove_ssh_host_keys && refuse_ssh_host_keys')

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.keys_left(), [])
        self.assertTrue((self.root / 'etc' / 'ssh' / 'sshd_config').exists())

    def test_build_fails_naming_the_unit_when_the_image_does_not_enable_it(self):
        (self.root / 'etc' / 'systemd' / 'system' / 'multi-user.target.wants' / self.UNIT).unlink()

        done = self.run_function('remove_ssh_host_keys')

        self.assertNotEqual(done.returncode, 0)
        self.assertIn(self.UNIT, done.stderr)

    def test_build_fails_when_the_unit_is_enabled_but_the_image_does_not_hold_it(self):
        (self.root / 'usr' / 'lib' / 'systemd' / 'system' / self.UNIT).unlink()

        done = self.run_function('remove_ssh_host_keys')

        self.assertNotEqual(done.returncode, 0)
        self.assertIn(self.UNIT, done.stderr)


class TestImageVerificationOfTheScreenSetting(unittest.TestCase):
    """The build reads the image, not its own intentions: an image with no screen setting, or with a host key, is
    refused whatever else it holds."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name, 'root')
        (self.root / 'boot').mkdir(parents=True)

    def verify(self):
        return _bash('source "$1"; root="$2"; verify_and_describe_image', str(BUILD_SCRIPT), str(self.root))

    def test_image_without_the_setting_is_refused_and_the_setting_is_named(self):
        (self.root / 'boot' / 'config.txt').write_text(FIRMWARE_CONFIGURATION)

        done = self.verify()

        self.assertNotEqual(done.returncode, 0)
        self.assertIn('hdmi_force_hotplug=1', done.stderr)

    def test_image_whose_firmware_partition_holds_no_configuration_is_refused(self):
        done = self.verify()

        self.assertNotEqual(done.returncode, 0)
        self.assertIn('/boot/config.txt', done.stderr)

    def test_image_with_the_setting_is_not_refused_for_it(self):
        (self.root / 'boot' / 'config.txt').write_text(FIRMWARE_CONFIGURATION + 'hdmi_force_hotplug=1\n')

        done = self.verify()

        self.assertNotIn('hdmi_force_hotplug', done.stderr)
        self.assertNotIn('/boot/config.txt', done.stderr)

    def test_image_with_a_host_key_is_refused_by_the_verification(self):
        (self.root / 'etc' / 'ssh').mkdir(parents=True)
        (self.root / 'etc' / 'ssh' / 'ssh_host_rsa_key').write_text('key\n')
        (self.root / 'boot' / 'config.txt').write_text(FIRMWARE_CONFIGURATION + 'hdmi_force_hotplug=1\n')

        done = self.verify()

        self.assertNotEqual(done.returncode, 0)
        self.assertIn('/etc/ssh/ssh_host_rsa_key', done.stderr)


class TestCandidateStep(unittest.TestCase):
    """What happens to the branch candidate boxes follow when a candidate tag is built, from two commits."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repository = str(Path(directory.name, 'layer'))
        subprocess.run(['git', 'init', '--quiet', '--initial-branch=main', self.repository], check=True, timeout=60)
        self.older = self.commit('older')
        self.newer = self.commit('newer')
        self.git('checkout', '--quiet', '-b', 'other', self.older)
        self.other = self.commit('other line of history')

    def git(self, *arguments):
        return subprocess.run(['git', '-c', 'user.name=Box Test', '-c', 'user.email=box@example.invalid', '-C',
                               self.repository, *arguments], capture_output=True, text=True, check=True,
                              timeout=60).stdout.strip()

    def commit(self, text):
        self.git('commit', '--quiet', '--allow-empty', '--message', text)
        return self.git('rev-parse', 'HEAD')

    def step(self, tagged, *branch_head):
        return subprocess.run(['bash', str(RELEASE_CHECK), 'candidate-step', tagged, *branch_head, '--repository',
                               self.repository], capture_output=True, text=True, check=False, timeout=60)

    def test_no_branch_is_created(self):
        done = self.step(self.newer)

        self.assertEqual((done.returncode, done.stdout.strip()), (0, 'create'), done.stderr)

    def test_tagged_commit_that_contains_the_branch_head_moves_the_branch_forward(self):
        done = self.step(self.newer, self.older)

        self.assertEqual((done.returncode, done.stdout.strip()), (0, 'advance'), done.stderr)

    def test_tagged_commit_that_is_the_branch_head_moves_the_branch_to_where_it_is(self):
        done = self.step(self.newer, self.newer)

        self.assertEqual((done.returncode, done.stdout.strip()), (0, 'advance'), done.stderr)

    def test_older_candidate_keeps_the_branch_where_it_is_and_does_not_fail(self):
        done = self.step(self.older, self.newer)

        self.assertEqual((done.returncode, done.stdout.strip()), (0, 'keep'), done.stderr)

    def test_candidate_on_another_line_of_history_keeps_the_branch_where_it_is_and_does_not_fail(self):
        done = self.step(self.other, self.newer)

        self.assertEqual((done.returncode, done.stdout.strip()), (0, 'keep'), done.stderr)

    def test_branch_head_the_repository_does_not_hold_is_an_error_not_a_keep(self):
        done = self.step(self.newer, 'f' * 40)

        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.stdout.strip(), '')

    def test_commit_that_is_not_a_full_commit_id_is_an_error(self):
        self.assertNotEqual(self.step('main').returncode, 0)
        self.assertNotEqual(self.step(self.newer, 'main').returncode, 0)


GH_STUB = """#!/usr/bin/env python3
import os
import subprocess
import sys

arguments = sys.argv[1:]
with open(os.environ['GH_LOG'], 'a') as log:
    log.write(' '.join(arguments) + '\\n')
fields = {}
for flag, value in zip(arguments, arguments[1:]):
    if flag in ('-f', '-F'):
        key, _, text = value.partition('=')
        fields[key] = text
git = ['git', '--git-dir', os.environ['GH_REMOTE']]
path = next(argument for argument in arguments if argument.startswith('repos/'))
if '--method' in arguments:
    ref = 'refs/' + path.split('/git/refs/', 1)[1]
    if fields.get('force') != 'false':
        sys.exit('the update is forced')
    current = subprocess.run([*git, 'rev-parse', ref], capture_output=True, text=True, check=True).stdout.strip()
    if subprocess.run([*git, 'merge-base', '--is-ancestor', current, fields['sha']]).returncode:
        sys.exit('Update is not a fast forward')
else:
    ref = fields['ref']
    if subprocess.run([*git, 'rev-parse', '--verify', '--quiet', ref], capture_output=True).returncode == 0:
        sys.exit('Reference already exists')
subprocess.run([*git, 'update-ref', ref, fields['sha']], check=True)
"""


class TestCandidateBranchStepOfTheWorkflow(unittest.TestCase):
    """The step of the release workflow that moves the branch candidate boxes follow, run on local repositories with
    a stand-in for the GitHub client."""

    STEP = 'The branch the candidate boxes follow'
    CHANNEL = 'refs/heads/box-candidate/18.0'
    REPOSITORY = 'example/iot-hw'

    def workflow_lines(self):
        return IMAGE_WORKFLOW.read_text().splitlines()

    def step_bounds(self):
        lines = self.workflow_lines()
        start = lines.index(f'      - name: {self.STEP}')
        end = next((number for number in range(start + 1, len(lines)) if lines[number].startswith('      - ')
                    or lines[number].startswith('  ') and not lines[number].startswith('   ')), len(lines))
        return start, end

    def step_script(self):
        lines = self.workflow_lines()
        start, end = self.step_bounds()
        run_at = next(number for number in range(start, end) if lines[number].strip() == 'run: |')
        indent = len(lines[run_at]) - len(lines[run_at].lstrip()) + 2
        return textwrap.dedent('\n'.join(line[indent:] if line.strip() else '' for line in lines[run_at + 1:end]))

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.scratch = Path(directory.name)
        self.remote = self.scratch / 'remote.git'
        subprocess.run(['git', 'init', '--quiet', '--bare', '--initial-branch=main', str(self.remote)], check=True,
                       timeout=60)
        self.work = self.scratch / 'work'
        subprocess.run(['git', 'clone', '--quiet', str(self.remote), str(self.work)], check=True, capture_output=True,
                       timeout=60)
        self.older = self.commit('older')
        self.newer = self.commit('newer')
        self.git('push', '--quiet', 'origin', 'main')
        self.git('checkout', '--quiet', '-b', 'other', self.older)
        self.other = self.commit('other line of history')
        self.git('push', '--quiet', 'origin', 'other')
        (self.work / 'box' / 'build').mkdir(parents=True)
        shutil.copy(RELEASE_CHECK, self.work / 'box' / 'build' / 'check_release.sh')
        self.bin = self.scratch / 'bin'
        self.bin.mkdir()
        (self.bin / 'gh').write_text(GH_STUB)
        (self.bin / 'gh').chmod(0o755)
        self.log = self.scratch / 'gh.log'

    def git(self, *arguments, directory=None):
        return subprocess.run(['git', '-c', 'user.name=Box Test', '-c', 'user.email=box@example.invalid', '-C',
                               str(directory or self.work), *arguments], capture_output=True, text=True, check=True,
                              timeout=60).stdout.strip()

    def commit(self, text):
        self.git('commit', '--quiet', '--allow-empty', '--message', text)
        return self.git('rev-parse', 'HEAD')

    def run_step(self, tagged):
        environment = {
            **os.environ, 'PATH': f'{self.bin}:{os.environ["PATH"]}', 'GH_TOKEN': 'unused', 'GH_REPO': self.REPOSITORY,
            'CHANNEL': self.CHANNEL, 'LAYER_COMMIT': tagged, 'GH_LOG': str(self.log), 'GH_REMOTE': str(self.remote),
            'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': f'url.{self.remote}.insteadOf',
            'GIT_CONFIG_VALUE_0': f'https://github.com/{self.REPOSITORY}.git'}
        return subprocess.run(['bash', '-c', self.step_script()], cwd=self.work, env=environment,
                              capture_output=True, text=True, check=False, timeout=120)

    def branch(self):
        done = subprocess.run(['git', '--git-dir', str(self.remote), 'rev-parse', '--verify', '--quiet', self.CHANNEL],
                              capture_output=True, text=True, check=False, timeout=60)
        return done.stdout.strip() or None

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def set_branch(self, commit):
        self.git('update-ref', self.CHANNEL, commit, directory=self.remote)

    def test_candidate_branch_is_created_at_the_tagged_commit_when_there_is_none(self):
        done = self.run_step(self.newer)

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.branch(), self.newer)

    def test_candidate_branch_moves_forward_without_force_to_a_tagged_commit_that_contains_it(self):
        self.set_branch(self.older)

        done = self.run_step(self.newer)

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.branch(), self.newer)
        self.assertEqual(len(self.calls()), 1)
        self.assertIn('force=false', self.calls()[0])

    def test_candidate_branch_stays_and_the_step_does_not_fail_for_an_older_candidate(self):
        self.set_branch(self.newer)

        done = self.run_step(self.older)

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.branch(), self.newer)
        self.assertEqual(self.calls(), [])

    def test_candidate_branch_stays_and_the_step_does_not_fail_for_another_line_of_history(self):
        self.set_branch(self.newer)

        done = self.run_step(self.other)

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.branch(), self.newer)
        self.assertEqual(self.calls(), [])

    def test_a_move_the_repository_refuses_fails_the_step(self):
        self.set_branch(self.older)
        # The client refuses every move: the step must not report success.
        (self.bin / 'gh').write_text('#!/bin/sh\nexit 1\n')

        done = self.run_step(self.newer)

        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.branch(), self.older)

    def test_only_a_candidate_reaches_the_branch(self):
        lines = self.workflow_lines()
        start, end = self.step_bounds()
        checkout = max(number for number in range(start) if lines[number].startswith('      - '))
        condition = "if: needs.gate.outputs.mode == 'candidate'"

        self.assertIn(condition, [line.strip() for line in lines[start:end]])
        self.assertIn(condition, [line.strip() for line in lines[checkout:start]])
        outside = [line for number, line in enumerate(lines) if not start <= number < end and 'git/refs' in line]
        self.assertEqual(outside, [])


class TestBuildOnlyPackageConfiguration(unittest.TestCase):
    """The package settings and sources only the image build uses are in the image while it installs packages and
    are not in the image a customer writes."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.scratch = Path(directory.name)

    def image_root(self):
        """An image root that holds the files of the operating system the build leaves alone."""
        root = self.scratch / 'root'
        (root / 'etc' / 'apt' / 'apt.conf.d').mkdir(parents=True)
        (root / 'var' / 'lib' / 'apt' / 'lists').mkdir(parents=True)
        (root / 'etc' / 'apt' / 'sources.list').write_text('deb http://stock.example.invalid/raspbian/ bookworm main\n')
        (root / 'etc' / 'apt' / 'apt.conf.d' / '99acquire-retries').write_text(CORE_APT_RETRIES)
        lists = root / 'var' / 'lib' / 'apt' / 'lists'
        (lists / 'stock.example.invalid_raspbian_dists_bookworm_InRelease').write_text('x')
        return root

    def hold_build_only_files(self, root):
        for path in BUILD_APT.rglob('*'):
            if path.is_file():
                target = root / path.relative_to(BUILD_APT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(path, target)
        lists = root / 'var' / 'lib' / 'apt' / 'lists'
        (lists / 'partial').mkdir(exist_ok=True)
        for _address, name in apt_index_files(self.scratch, BUILD_APT_SOURCES):
            (lists / f'{name}.lz4').write_text('x')
            (lists / 'partial' / name).write_text('x')
        (root / 'var' / 'cache' / 'apt').mkdir(parents=True, exist_ok=True)
        for name in ('pkgcache.bin', 'srcpkgcache.bin'):
            (root / 'var' / 'cache' / 'apt' / name).write_text('x')

    def files_of(self, root):
        return sorted(path.relative_to(root).as_posix() for path in root.rglob('*') if path.is_file())

    def run_function(self, root, function):
        return _bash(f'source "$1"; root="$2"; {function}', str(BUILD_SCRIPT), str(root))

    def test_files_of_the_build_only_configuration_are_put_where_the_image_script_of_core_copies_them_from(self):
        posbox = self.scratch / 'posbox'
        (posbox / 'overwrite_before_init').mkdir(parents=True)

        done = _bash('source "$1"; posbox_dir="$2"; place_build_only_apt_configuration', str(BUILD_SCRIPT), str(posbox))

        self.assertEqual(done.returncode, 0, done.stderr)
        kept = {path.relative_to(BUILD_APT) for path in BUILD_APT.rglob('*') if path.is_file()}
        self.assertIn(BUILD_APT_SETTINGS.relative_to(BUILD_APT), kept)
        self.assertIn(BUILD_APT_SOURCES.relative_to(BUILD_APT), kept)
        for relative in kept:
            placed = posbox / 'overwrite_before_init' / relative
            self.assertEqual(placed.read_bytes(), (BUILD_APT / relative).read_bytes(), relative)
            self.assertEqual(stat.S_IMODE(placed.stat().st_mode), stat.S_IMODE((BUILD_APT / relative).stat().st_mode))
        self.assertTrue(os.access(posbox / 'overwrite_before_init' / BUILD_APT_GET.relative_to(BUILD_APT), os.X_OK))

    def test_build_only_configuration_and_the_index_files_of_its_sources_leave_the_image_and_nothing_else_does(self):
        root = self.image_root()
        stock = self.files_of(root)
        self.hold_build_only_files(root)
        self.assertNotEqual(self.files_of(root), stock)

        done = self.run_function(root, 'remove_build_only_apt_configuration')

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.files_of(root), stock)

    def test_image_that_still_holds_a_file_of_the_build_only_configuration_is_refused_and_the_path_is_named(self):
        root = self.image_root()
        self.hold_build_only_files(root)

        done = self.run_function(root, 'refuse_build_only_apt_configuration')

        self.assertNotEqual(done.returncode, 0)
        self.assertIn(f'/{BUILD_APT_SETTINGS.relative_to(BUILD_APT).as_posix()}', done.stderr)
        self.assertIn(f'/{BUILD_APT_SOURCES.relative_to(BUILD_APT).as_posix()}', done.stderr)

    def test_image_that_still_holds_an_index_file_of_a_source_of_the_build_is_refused_and_the_path_is_named(self):
        root = self.image_root()
        _address, name = apt_index_files(self.scratch, BUILD_APT_SOURCES)[0]
        left = root / 'var' / 'lib' / 'apt' / 'lists' / f'{name}.lz4'
        left.write_text('x')

        done = self.run_function(root, 'refuse_build_only_apt_configuration')

        self.assertNotEqual(done.returncode, 0)
        self.assertIn(f'/var/lib/apt/lists/{left.name}', done.stderr)

    def test_image_that_still_holds_what_apt_kept_of_the_sources_of_the_build_is_refused_and_the_paths_are_named(self):
        root = self.image_root()
        self.hold_build_only_files(root)
        for path in BUILD_APT.rglob('*'):
            if path.is_file():
                (root / path.relative_to(BUILD_APT)).unlink()

        done = self.run_function(root, 'refuse_build_only_apt_configuration')

        self.assertNotEqual(done.returncode, 0)
        _address, name = apt_index_files(self.scratch, BUILD_APT_SOURCES)[0]
        self.assertIn(f'/var/lib/apt/lists/partial/{name}', done.stderr)
        self.assertIn('/var/cache/apt/pkgcache.bin', done.stderr)
        self.assertIn('/var/cache/apt/srcpkgcache.bin', done.stderr)

    def test_image_script_of_core_finds_the_apt_get_of_the_build_first_and_keeps_the_tools_of_the_host(self):
        posbox = self.scratch / 'posbox'
        posbox.mkdir()
        (posbox / 'posbox_create_image.sh').write_text('echo "$PATH"\n')

        done = _bash('PATH=/opt/host-tools:/usr/bin:/bin; source "$1"; posbox_dir="$2"; series=18.0; core_script',
                     str(BUILD_SCRIPT), str(posbox))

        directories = done.stdout.strip().split(':')
        self.assertLess(directories.index(f'/{BUILD_APT_GET.parent.relative_to(BUILD_APT).as_posix()}'),
                        directories.index('/usr/bin'))
        self.assertIn('/opt/host-tools', directories)

    def test_command_run_in_the_image_is_given_a_path_that_finds_the_apt_get_of_the_build_first(self):
        done = _bash('chroot() { printf "%s\\n" "$@"; }; source "$1"; root=/image; in_image echo done',
                     str(BUILD_SCRIPT))

        lines = done.stdout.splitlines()
        path = next(line for line in lines if line.startswith('PATH=')).removeprefix('PATH=').split(':')
        wrapper_directory = f'/{BUILD_APT_GET.parent.relative_to(BUILD_APT).as_posix()}'
        self.assertLess(path.index(wrapper_directory), path.index('/usr/bin'))
        self.assertEqual(lines[0], '/image')
        self.assertEqual(lines[-2:], ['echo', 'done'])

    def test_image_with_the_package_files_of_the_operating_system_only_is_not_refused(self):
        done = self.run_function(self.image_root(), 'refuse_build_only_apt_configuration')

        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_verification_of_the_image_refuses_an_image_that_holds_the_build_only_configuration(self):
        root = self.image_root()
        self.hold_build_only_files(root)

        done = self.run_function(root, 'verify_and_describe_image')

        self.assertNotEqual(done.returncode, 0)
        self.assertIn(f'/{BUILD_APT_SETTINGS.relative_to(BUILD_APT).as_posix()}', done.stderr)


def apt_index_files(scratch, sources_file):
    """Ask apt which index files the sources in `sources_file` make, without any network.

    :param scratch: a directory to work in
    :param sources_file: a file of source lines
    :return: the address and the name, in the directory apt keeps index files in, of each index file
    """
    parts = Path(scratch, 'sources-parts')
    parts.mkdir(exist_ok=True)
    shutil.copy(sources_file, parts / 'only.list')
    state = Path(scratch, 'sources-state', 'lists', 'partial')
    state.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        ['apt-get', 'update', '--print-uris', '-o', 'Dir::Etc::sourcelist=/dev/null',
         '-o', f'Dir::Etc::sourceparts={parts}', '-o', f'Dir::State={state.parent.parent}', '-o', 'Debug::NoLocking=1'],
        capture_output=True, text=True, check=True, timeout=60)
    return [tuple(line.split()[:2]) for line in done.stdout.splitlines() if line.startswith("'")]


class PackageMirrors:
    """A repository of one package behind a primary address that answers like a mirror director: it serves the
    indexes itself and redirects each package to another address."""

    PACKAGE = 'hello-exp'

    def __init__(self, scratch, cleanup):
        self.scratch = Path(scratch)
        self.cleanup = cleanup
        self.repository = self.scratch / 'repository'
        self.digest = self.build_repository(self.repository, '1.0')

    def build_repository(self, repository, version):
        """Make a repository at `repository` that holds the package at `version`; the digest of the package."""
        (repository / 'dists' / 'bookworm' / 'main' / 'binary-armhf').mkdir(parents=True)
        (repository / 'pool').mkdir()
        source = repository.parent / f'package-{version}' / 'DEBIAN'
        source.mkdir(parents=True)
        (source / 'control').write_text(f'Package: {self.PACKAGE}\nVersion: {version}\nArchitecture: armhf\n'
                                        'Maintainer: Box Test <box@example.invalid>\nDescription: test package\n')
        archive = repository / 'pool' / f'{self.PACKAGE}_{version}_armhf.deb'
        subprocess.run(['dpkg-deb', '--build', str(source.parent), str(archive)], check=True, capture_output=True,
                       timeout=60)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        packages = (f'Package: {self.PACKAGE}\nVersion: {version}\nArchitecture: armhf\n'
                    f'Maintainer: Box Test <box@example.invalid>\nFilename: pool/{archive.name}\n'
                    f'Size: {archive.stat().st_size}\nSHA256: {digest}\nDescription: test package\n').encode()
        (repository / 'dists' / 'bookworm' / 'main' / 'binary-armhf' / 'Packages').write_bytes(packages)
        (repository / 'dists' / 'bookworm' / 'Release').write_text(
            'Suite: bookworm\nCodename: bookworm\nArchitectures: armhf\nComponents: main contrib non-free rpi\n'
            'Date: Mon, 01 Jan 2024 00:00:00 UTC\nSHA256:\n'
            f' {hashlib.sha256(packages).hexdigest()} {len(packages)} main/binary-armhf/Packages\n')
        return digest

    def serve(self, address, handler):
        server = http.server.ThreadingHTTPServer((address, 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.cleanup(server.server_close)
        self.cleanup(server.shutdown)
        return f'http://{address}:{server.server_address[1]}'

    def holding_everything(self, address, repository=None):
        """A mirror at `address` that holds the whole repository, or the one at `repository`; its address."""
        directory = str(repository or self.repository)

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *arguments, **keywords):
                super().__init__(*arguments, directory=directory, **keywords)

            def log_message(self, *arguments):
                pass

        return self.serve(address, Handler)

    def answering_with_a_redirect(self, address, mirror):
        """A primary address at `address`: indexes from itself, every package redirected to `mirror`."""
        repository = self.repository

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.removeprefix('/raspbian')
                if '/dists/' in path:
                    target = repository / path.lstrip('/')
                    if target.is_file():
                        body = target.read_bytes()
                        self.send_response(200)
                        self.send_header('Content-Length', str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    else:
                        self.send_response(404)
                        self.end_headers()
                    return
                self.send_response(302)
                self.send_header('Location', f'{mirror}{path}')
                self.send_header('Content-Length', '0')
                self.end_headers()

            def log_message(self, *arguments):
                pass

        return self.serve(address, Handler)

    def one_version_ahead(self, address):
        """A mirror at `address` that holds the package at a version the other mirrors do not list yet."""
        repository = self.scratch / 'ahead'
        self.build_repository(repository, '2.0')
        return self.holding_everything(address, repository)

    def stalled(self, address):
        """A mirror at `address` that accepts a connection and sends nothing."""
        stop = threading.Event()
        self.cleanup(stop.set)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                stop.wait(600)

            def log_message(self, *arguments):
                pass

        return self.serve(address, Handler)

    def gone(self, address):
        """A mirror at `address` that answers every request with "not found"."""
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(404)
                self.end_headers()

            def log_message(self, *arguments):
                pass

        return self.serve(address, Handler)

    def out_of_sync(self, address):
        """A mirror at `address` whose package index is not the one its release file describes."""
        directory = str(self.repository)

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *arguments, **keywords):
                super().__init__(*arguments, directory=directory, **keywords)

            def do_GET(self):
                if not self.path.endswith('/Packages'):
                    return super().do_GET()
                body = b'Package: another\nVersion: 2\nArchitecture: armhf\nDescription: not the indexed state\n'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *arguments):
                pass

        return self.serve(address, Handler)

    @staticmethod
    def unreachable(address):
        """An address and a port nothing listens on."""
        with socket.socket() as probe:
            probe.bind((address, 0))
            return f'http://{address}:{probe.getsockname()[1]}'


def build_only_source_line(address):
    """The first source line of the build, with the address of a mirror of the test in place of its own."""
    line = next(line for line in BUILD_APT_SOURCES.read_text().splitlines() if line.startswith('deb '))
    found = re.fullmatch(r'deb \[ (?P<options>[^\]]*) \] \S+ (?P<rest>.*)', line)
    return f'deb [ {found.group("options")} trusted=yes ] {address}/ {found.group("rest")}'


class AptCase(unittest.TestCase):
    """A scratch apt that reads sources and settings of the test, and servers that stand for mirrors."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.scratch = Path(directory.name)
        self.mirrors = PackageMirrors(self.scratch, self.addCleanup)

    def apt_environment(self, sources, settings, source_files=None, preferences=None):
        """The environment of an apt that reads the source lines `sources`, the files `source_files` (name: text)
        of sources.list.d, the files `settings` (name: text) of apt.conf.d and the files `preferences` (name: text)
        of preferences.d."""
        root = self.scratch / 'apt'
        for name in ('state/lists/partial', 'cache/archives/partial', 'etc/apt/apt.conf.d', 'etc/apt/sources.list.d',
                     'etc/apt/preferences.d'):
            (root / name).mkdir(parents=True, exist_ok=True)
        for name, text in (preferences or {}).items():
            (root / 'etc' / 'apt' / 'preferences.d' / name).write_text(text)
        (root / 'etc' / 'apt' / 'sources.list').write_text(sources)
        for name, text in (source_files or {}).items():
            (root / 'etc' / 'apt' / 'sources.list.d' / name).write_text(text)
        for name, text in settings.items():
            (root / 'etc' / 'apt' / 'apt.conf.d' / name).write_text(text)
        (root / 'apt.conf').write_text(
            f'Dir::Etc "{root}/etc/apt"; Dir::State "{root}/state"; Dir::Cache "{root}/cache"; '
            'Dir::State::status "/var/lib/dpkg/status"; Dir::Etc::trusted "/dev/null"; '
            'Dir::Etc::trustedparts "/dev/null"; Debug::NoLocking "true"; '
            'APT::Architecture "armhf"; APT::Architectures "armhf";')
        self.lists = root / 'state' / 'lists'
        return {**os.environ, 'APT_CONFIG': str(root / 'apt.conf')}

    @staticmethod
    def build_only_pins(host):
        """The preferences file of the build, with `host` in place of the first host it names."""
        hosts = iter([host])
        return re.sub(r'(?m)^(Pin: origin )\S+$', lambda found: found.group(1) + next(hosts, 'unused.invalid'),
                      BUILD_APT_PINS.read_text())

    @staticmethod
    def stock_line(primary, path='/raspbian/'):
        return f'deb [ arch=armhf trusted=yes ] {primary}{path} bookworm main contrib non-free rpi\n'


class TestPackageDownloadsOfTheBuild(AptCase):
    """What apt does with the files of the build while a mirror does not answer, on a repository of its own that
    holds one package: apt itself runs, as the build runs it inside the image."""

    def download(self, environment):
        """Update the indexes, then download the package; the downloaded file, or None. `self.seconds` is the time
        the download took."""
        subprocess.run(['apt-get', 'update'], env=environment, capture_output=True, check=False, timeout=120)
        started = time.monotonic()
        subprocess.run(['apt-get', 'download', PackageMirrors.PACKAGE], env=environment, cwd=self.scratch,
                       capture_output=True, check=False, timeout=240)
        self.seconds = time.monotonic() - started
        found = list(self.scratch.glob('*.deb'))
        return found[0] if found else None

    @staticmethod
    def settings_in_force(environment):
        dump = subprocess.run(['apt-config', 'dump'], env=environment, capture_output=True, text=True, check=True,
                              timeout=60).stdout
        return dict(re.findall(r'^(\S+) "([^"]*)";$', dump, re.MULTILINE))

    def build_environment(self, primary, second=None, ranked=True):
        extra = f'{build_only_source_line(second)}\n' if second else ''
        pins = {BUILD_APT_PINS.name: self.build_only_pins(re.match(r'http://([^:]+):', second).group(1))} \
            if second and ranked else {}
        return self.apt_environment(self.stock_line(primary) + extra,
                                    {BUILD_APT_SETTINGS.name: BUILD_APT_SETTINGS.read_text()}, preferences=pins)

    @staticmethod
    def candidate(environment):
        """Update the indexes and give the version apt would install of the package."""
        subprocess.run(['apt-get', 'update'], env=environment, capture_output=True, check=False, timeout=120)
        done = subprocess.run(['apt-cache', 'policy', PackageMirrors.PACKAGE], env=environment, capture_output=True,
                              text=True, check=True, timeout=60)
        return re.search(r'Candidate: (\S+)', done.stdout).group(1)

    def test_settings_of_the_build_win_over_the_retries_core_writes_and_bound_the_wait_on_one_source(self):
        environment = self.apt_environment('', {'99acquire-retries': CORE_APT_RETRIES,
                                                BUILD_APT_SETTINGS.name: BUILD_APT_SETTINGS.read_text()})

        settings = self.settings_in_force(environment)

        # An attempt on a mirror that accepts a connection and stays silent takes up to twice the timeout.
        waits = (int(settings['Acquire::Retries']) + 1) * 2 * int(settings['Acquire::http::Timeout'])
        self.assertLessEqual(waits, 60)

    def test_extra_sources_of_the_build_give_the_same_suite_and_architecture_from_other_hosts_than_the_stock_one(self):
        indexes = apt_index_files(self.scratch, BUILD_APT_SOURCES)

        self.assertTrue(indexes)
        hosts = {re.match(r'\'https?://([^/]+)/', address).group(1) for address, _name in indexes}
        self.assertNotIn('raspbian.raspberrypi.com', hosts)
        self.assertTrue(all('/dists/bookworm/' in address for address, _name in indexes))
        self.assertTrue(any('/binary-armhf/' in address for address, _name in indexes))
        self.assertFalse(any(re.search(r'/binary-(?!armhf|all)', address) for address, _name in indexes))

    def test_package_whose_redirect_names_a_mirror_that_does_not_answer_is_taken_from_the_second_source(self):
        primary = self.mirrors.answering_with_a_redirect('127.0.0.11', PackageMirrors.unreachable('127.0.0.12'))
        environment = self.build_environment(primary, self.mirrors.holding_everything('127.0.0.13'))

        downloaded = self.download(environment)

        self.assertIsNotNone(downloaded)
        self.assertEqual(hashlib.sha256(downloaded.read_bytes()).hexdigest(), self.mirrors.digest)

    def test_package_from_a_mirror_that_accepts_and_stays_silent_comes_from_the_second_source_in_time(self):
        """Proves, with the settings of the build as they are: one download from a mirror that accepts the connection
        and sends nothing waits no longer than the settings allow for it, and the package then comes from the second
        source. Does not prove the total for many packages: apt asks a stalled host for one package after the other,
        so each adds its wait, and only the time limit of the workflow step bounds the sum."""
        primary = self.mirrors.answering_with_a_redirect('127.0.0.11', self.mirrors.stalled('127.0.0.12'))
        environment = self.build_environment(primary, self.mirrors.holding_everything('127.0.0.13'))
        settings = self.settings_in_force(environment)

        downloaded = self.download(environment)

        self.assertIsNotNone(downloaded)
        self.assertEqual(hashlib.sha256(downloaded.read_bytes()).hexdigest(), self.mirrors.digest)
        allowed = (int(settings['Acquire::Retries']) + 1) * 2 * int(settings['Acquire::http::Timeout'])
        self.assertLessEqual(self.seconds, allowed + 15)

    def test_every_extra_source_of_the_build_is_ranked_below_the_stock_source(self):
        indexes = apt_index_files(self.scratch, BUILD_APT_SOURCES)
        sources = {re.match(r'\'https?://([^/]+)/', address).group(1) for address, _name in indexes}
        text = BUILD_APT_PINS.read_text()

        pinned = re.findall(r'(?m)^Pin: origin (\S+)$', text)
        priorities = re.findall(r'(?m)^Pin-Priority: (\d+)$', text)

        self.assertEqual(set(pinned), sources)
        self.assertTrue(all(int(priority) < 500 for priority in priorities))
        self.assertEqual(len(priorities), len(pinned))

    def test_mirror_that_lists_one_version_more_than_the_stock_source_does_not_change_the_version_installed(self):
        primary = self.mirrors.holding_everything('127.0.0.11')
        ahead = self.mirrors.one_version_ahead('127.0.0.13')
        environment = self.apt_environment(
            self.stock_line(primary, '/') + f'{build_only_source_line(ahead)}\n',
            {BUILD_APT_SETTINGS.name: BUILD_APT_SETTINGS.read_text()},
            preferences={BUILD_APT_PINS.name: self.build_only_pins('127.0.0.13')})

        self.assertEqual(self.candidate(environment), '1.0')

    def test_without_the_ranking_a_mirror_that_lists_one_version_more_decides_the_version_installed(self):
        primary = self.mirrors.holding_everything('127.0.0.11')
        ahead = self.mirrors.one_version_ahead('127.0.0.13')
        environment = self.apt_environment(self.stock_line(primary, '/') + f'{build_only_source_line(ahead)}\n',
                                           {BUILD_APT_SETTINGS.name: BUILD_APT_SETTINGS.read_text()})

        self.assertEqual(self.candidate(environment), '2.0')

    def test_without_a_second_source_the_package_cannot_be_downloaded(self):
        primary = self.mirrors.answering_with_a_redirect('127.0.0.11', PackageMirrors.unreachable('127.0.0.12'))

        self.assertIsNone(self.download(self.build_environment(primary)))


class TestExtraSourcesNeverFailAnUpdate(AptCase):
    """A mirror of the extra sources that answers but is gone or out of sync fails the update of apt itself; the
    apt-get of the build repeats such an update without the extra sources, as the sources of the operating system
    alone would have run it. Real apt runs on repositories of the test."""

    def update(self, environment, program):
        done = subprocess.run([program, 'update'], env=environment, capture_output=True, text=True, check=False,
                              timeout=240)
        return done.returncode, done.stderr

    def environment_with_extra(self, primary, extra):
        return self.apt_environment(
            '', {BUILD_APT_SETTINGS.name: BUILD_APT_SETTINGS.read_text()},
            {'stock.list': self.stock_line(primary, '/'), BUILD_APT_SOURCES.name: f'{build_only_source_line(extra)}\n'})

    def bad_extra_mirrors(self):
        return {'a mirror that is gone': self.mirrors.gone('127.0.0.12'),
                'a mirror that is out of sync': self.mirrors.out_of_sync('127.0.0.13')}

    def test_extra_mirror_that_is_gone_or_out_of_sync_fails_the_update_of_apt_itself(self):
        primary = self.mirrors.holding_everything('127.0.0.11')
        for name, extra in self.bad_extra_mirrors().items():
            with self.subTest(name):
                environment = self.environment_with_extra(primary, extra)

                self.assertNotEqual(self.update(environment, 'apt-get')[0], 0)

    def test_extra_mirror_that_is_gone_or_out_of_sync_fails_no_update_through_the_apt_get_of_the_build(self):
        primary = self.mirrors.holding_everything('127.0.0.11')
        for name, extra in self.bad_extra_mirrors().items():
            with self.subTest(name):
                environment = self.environment_with_extra(primary, extra)

                code, _stderr = self.update(environment, str(BUILD_APT_GET))

                self.assertEqual(code, 0)
                self.assertTrue(any(self.lists.glob('127.0.0.11*_dists_bookworm_*')))

    def test_healthy_extra_mirror_is_listed_in_the_update_of_the_build_and_the_update_is_not_repeated_without_it(self):
        primary = self.mirrors.holding_everything('127.0.0.11')
        extra = self.mirrors.holding_everything('127.0.0.13')
        environment = self.environment_with_extra(primary, extra)

        code, stderr = self.update(environment, str(BUILD_APT_GET))

        self.assertEqual(code, 0, stderr)
        self.assertTrue(any(self.lists.glob('127.0.0.13*_dists_bookworm_*')))
        self.assertNotIn('operating system alone', stderr)

    def test_update_that_the_sources_of_the_operating_system_alone_would_fail_ends_with_the_status_of_apt_get(self):
        primary = self.mirrors.gone('127.0.0.11')
        environment = self.environment_with_extra(primary, self.mirrors.holding_everything('127.0.0.13'))

        code, _stderr = self.update(environment, str(BUILD_APT_GET))

        self.assertNotEqual(code, 0)
        self.assertEqual(code, self.update(environment, 'apt-get')[0])

    def test_options_of_an_update_that_is_repeated_reach_apt_get_unchanged(self):
        """An update that succeeds removes the index files of no source; `APT::Get::List-Cleanup=0` keeps them."""
        primary = self.mirrors.holding_everything('127.0.0.11')
        environment = self.environment_with_extra(primary, self.mirrors.gone('127.0.0.12'))
        stray = self.lists / 'stray.example.invalid_dists_bookworm_InRelease'
        stray.write_text('x')

        without_the_option = self.update(environment, str(BUILD_APT_GET))
        removed = not stray.exists()
        stray.write_text('x')
        with_the_option = subprocess.run([str(BUILD_APT_GET), 'update', '-o', 'APT::Get::List-Cleanup=0'],
                                         env=environment, capture_output=True, text=True, check=False, timeout=240)

        self.assertEqual((without_the_option[0], with_the_option.returncode), (0, 0))
        self.assertTrue(removed)
        self.assertTrue(stray.exists())

    def test_command_that_succeeds_gives_the_same_output_as_apt_get(self):
        primary = self.mirrors.holding_everything('127.0.0.11')
        environment = self.environment_with_extra(primary, self.mirrors.holding_everything('127.0.0.13'))
        self.update(environment, 'apt-get')
        arguments = ['--print-uris', '-qq', 'download', PackageMirrors.PACKAGE]

        through_the_build = subprocess.run([str(BUILD_APT_GET), *arguments], env=environment, capture_output=True,
                                           text=True, check=False, timeout=120, cwd=self.scratch)
        directly = subprocess.run(['apt-get', *arguments], env=environment, capture_output=True, text=True,
                                  check=False, timeout=120, cwd=self.scratch)

        self.assertEqual(directly.returncode, 0, directly.stderr)
        self.assertEqual((through_the_build.returncode, through_the_build.stdout), (0, directly.stdout))

    def test_command_other_than_an_update_is_not_repeated_without_the_extra_sources_and_ends_as_apt_get_ends(self):
        primary = self.mirrors.holding_everything('127.0.0.11')
        environment = self.environment_with_extra(primary, self.mirrors.holding_everything('127.0.0.13'))

        done = subprocess.run([str(BUILD_APT_GET), 'download', 'no-such-package-in-any-source'], env=environment,
                              capture_output=True, text=True, check=False, timeout=120, cwd=self.scratch)

        directly = subprocess.run(['apt-get', 'download', 'no-such-package-in-any-source'], env=environment,
                                  capture_output=True, text=True, check=False, timeout=120, cwd=self.scratch)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.returncode, directly.returncode)
        self.assertNotIn('operating system alone', done.stderr)


def _parse(path):
    import configparser

    parser = configparser.RawConfigParser()
    parser.optionxform = str
    parser.read(path)
    return parser
