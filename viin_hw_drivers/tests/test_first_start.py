"""The first start of a box: each card written from the same image gives its box a master password of its own."""
import configparser
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import services_common as box

SCRIPT = box.SYSTEM_FILES / 'usr' / 'local' / 'sbin' / 'viin-box-first-start'
SERIAL = b'10000000c0ffee01\0'
OTHER_OPTION = 'http_port = 8069'
IMAGE_NAME = 'raspberrypi'
PAIRED_BOX_CONFIGURATION = (
    '[options]\nadmin_passwd = \n' + OTHER_OPTION + '\n\n[iot.box]\nserver_url = https://example.invalid\n')


class FirstStartCase(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='box-suite-first-start-')
        self.addCleanup(shutil.rmtree, self.directory, True)

    def fresh_copy(self, name, serial=SERIAL, configuration_text=None):
        """The root of a box as the image leaves it before its first start: an empty master password."""
        root = Path(self.directory, name)
        (root / 'home' / 'pi').mkdir(parents=True)
        (root / 'etc').mkdir()
        (root / 'etc' / 'hostname').write_text(f'{IMAGE_NAME}\n')
        (root / 'etc' / 'hosts').write_text(f'127.0.0.1\tlocalhost\n127.0.1.1\t{IMAGE_NAME}\n::1\tlocalhost\n')
        configuration = root / 'home' / 'pi' / 'odoo.conf'
        configuration.write_text(configuration_text or f'[options]\nadmin_passwd = \n{OTHER_OPTION}\n')
        configuration.chmod(0o600)
        if serial is not None:
            device_tree = root / 'sys' / 'firmware' / 'devicetree' / 'base'
            device_tree.mkdir(parents=True)
            (device_tree / 'serial-number').write_bytes(serial)
        return root

    @staticmethod
    def start(root):
        return subprocess.run(['bash', str(SCRIPT)], env={**os.environ, 'VIIN_BOX_ROOT': str(root)},
                              capture_output=True, text=True, check=False, timeout=60)

    @staticmethod
    def master_passwords(root):
        text = (root / 'home' / 'pi' / 'odoo.conf').read_text()
        return re.findall(r'^admin_passwd[ \t]*=[ \t]*(.*)$', text, re.MULTILINE)

    def master_password(self, root):
        """The one master password the configuration of `root` holds."""
        found = self.master_passwords(root)
        self.assertEqual(len(found), 1, found)
        return found[0]


class TestMasterPasswordOfABox(FirstStartCase):

    def test_two_boxes_written_from_the_same_image_get_different_master_passwords(self):
        first, second = self.fresh_copy('first'), self.fresh_copy('second')

        self.assertEqual(self.start(first).returncode, 0)
        self.assertEqual(self.start(second).returncode, 0)

        one, other = self.master_password(first), self.master_password(second)
        self.assertTrue(one.strip() and other.strip())
        self.assertNotEqual(one, other)

    def test_master_password_is_shown_nowhere_by_the_first_start(self):
        root = self.fresh_copy('box')

        done = self.start(root)

        password = self.master_password(root)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(password.strip())
        self.assertNotIn(password, done.stdout + done.stderr)

    def test_configuration_stays_readable_by_its_owner_alone_with_one_master_password_and_its_other_options(self):
        root = self.fresh_copy('box')

        self.assertEqual(self.start(root).returncode, 0)

        configuration = root / 'home' / 'pi' / 'odoo.conf'
        self.assertEqual(configuration.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.master_passwords(root)), 1)
        self.assertIn(OTHER_OPTION, configuration.read_text().splitlines())
        self.assertEqual(configuration.read_text().splitlines()[0], '[options]')
        self.assertEqual(sorted(path.name for path in configuration.parent.iterdir()),
                         ['odoo.conf', 'viin-box-first-start-done'])

    def test_second_start_keeps_the_master_password_of_the_first(self):
        root = self.fresh_copy('box')
        self.assertEqual(self.start(root).returncode, 0)
        first = self.master_password(root)

        self.assertEqual(self.start(root).returncode, 0)

        self.assertEqual(self.master_passwords(root), [first])

    def test_board_that_gives_no_serial_number_still_gets_a_master_password_once(self):
        root = self.fresh_copy('box', serial=None)

        done = self.start(root)
        first = self.master_password(root)
        self.start(root)

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(first.strip())
        self.assertEqual(self.master_passwords(root), [first])

    def test_master_password_goes_to_the_options_section_of_a_configuration_whose_last_section_is_the_boxs_own(self):
        root = self.fresh_copy('box', configuration_text=PAIRED_BOX_CONFIGURATION)

        done = self.start(root)

        configuration = configparser.RawConfigParser()
        configuration.read(root / 'home' / 'pi' / 'odoo.conf')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertRegex(configuration.get('options', 'admin_passwd'), r'^[0-9a-f]{48}$')
        self.assertFalse(configuration.has_option('iot.box', 'admin_passwd'))
        self.assertEqual(configuration.get('iot.box', 'server_url'), 'https://example.invalid')
        self.assertEqual((root / 'home' / 'pi' / 'odoo.conf').stat().st_mode & 0o777, 0o600)


class TestHostNameOfABox(FirstStartCase):

    def test_box_with_a_serial_number_is_named_after_the_last_four_digits_of_it_in_one_line_of_the_hosts_file(self):
        root = self.fresh_copy('box')

        done = self.start(root)

        hosts = (root / 'etc' / 'hosts').read_text().splitlines()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((root / 'etc' / 'hostname').read_text().strip(), 'viindoo-iot-ee01')
        self.assertEqual([line for line in hosts if line.startswith('127.0.1.1')], ['127.0.1.1\tviindoo-iot-ee01'])
        self.assertIn('127.0.0.1\tlocalhost', hosts)

    def test_board_that_gives_no_serial_number_keeps_the_name_of_the_image(self):
        root = self.fresh_copy('box', serial=None)

        self.start(root)

        self.assertEqual((root / 'etc' / 'hostname').read_text().strip(), IMAGE_NAME)
        self.assertEqual([line for line in (root / 'etc' / 'hosts').read_text().splitlines()
                          if line.startswith('127.0.1.1')], [f'127.0.1.1\t{IMAGE_NAME}'])
