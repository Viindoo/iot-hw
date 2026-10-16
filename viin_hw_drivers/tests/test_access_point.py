"""The name the access point of a box announces: Viindoo-IoT- and the hardware address of its Wi-Fi interface, on a
box written from the image and on a box updated in place."""
import configparser
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import common
import services_common as box

PROGRAM = box.SYSTEM_FILES / 'usr' / 'local' / 'sbin' / 'viin-box-access-point-name'
DROP_IN = box.SYSTEM_FILES / 'etc' / 'systemd' / 'system' / 'hostapd.service.d' / 'viindoo.conf'
INSTALLER = box.SYSTEM_FILES / 'apply.sh'
# The configuration of the access point as core's image ships it, and the path hostapd is given it by: its unit sets
# DAEMON_CONF to that path, and core's /etc/default/hostapd sets it to the same.
CORE_CONFIGURATION = 'interface=wlan0\nssid=IoTBox\nchannel=1\n'
CONFIGURATION = 'etc/hostapd/hostapd.conf'
# How core's wireless_ap.sh and its status ticket read the name of the access point: grep -oP '(?<=ssid=).*'.
CORE_READS_THE_NAME = re.compile(r'(?<=ssid=).*')
HARDWARE_ADDRESS = 'b8:27:eb:1a:2b:3c'
OTHER_HARDWARE_ADDRESS = 'dc:a6:32:00:9f:e1'
# The longest name a Wi-Fi network has, in bytes.
LONGEST_NAME = 32


class AccessPointCase(unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.directory = tempfile.mkdtemp(prefix='box-suite-access-point-')
        self.addCleanup(shutil.rmtree, self.directory, True)

    def box_copy(self, name='box', configuration=CORE_CONFIGURATION, addresses=None):
        """The root of a box: its access point configured as `configuration`, its interfaces with `addresses`."""
        root = Path(self.directory, name)
        path = root / CONFIGURATION
        path.parent.mkdir(parents=True)
        path.write_text(configuration)
        path.chmod(0o644)
        for interface, address in (addresses if addresses is not None else {'wlan0': HARDWARE_ADDRESS}).items():
            (root / 'sys' / 'class' / 'net' / interface).mkdir(parents=True)
            (root / 'sys' / 'class' / 'net' / interface / 'address').write_text(f'{address}\n')
        return root

    @staticmethod
    def start_access_point(root, program=PROGRAM, *arguments):
        """Run what hostapd runs before it starts, on the box `root`."""
        return subprocess.run(['python3', '-I', str(program), *arguments], capture_output=True, text=True,
                              env={**os.environ, 'VIIN_BOX_ROOT': str(root)}, check=False, timeout=60)

    @staticmethod
    def configuration(root):
        return (root / CONFIGURATION).read_text()

    def announced(self, root):
        """The name the access point of `root` announces, as core reads it from the configuration."""
        found = CORE_READS_THE_NAME.findall(self.configuration(root))
        self.assertEqual(len(found), 1, found)
        return found[0]


class TestNameOfTheAccessPoint(AccessPointCase):

    def test_box_written_from_the_image_announces_viindoo_iot_and_the_hardware_address_of_its_wifi(self):
        root = self.box_copy()

        done = self.start_access_point(root)

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.announced(root), 'Viindoo-IoT-b827eb1a2b3c')
        self.assertLessEqual(len(self.announced(root).encode()), LONGEST_NAME)

    def test_box_whose_access_point_core_named_already_announces_the_viindoo_name(self):
        root = self.box_copy(configuration='interface=wlan0\nssid=IoTBox-b827eb1a2b3c\nchannel=1\n')

        self.start_access_point(root)

        self.assertEqual(self.announced(root), 'Viindoo-IoT-b827eb1a2b3c')

    def test_two_boxes_announce_names_of_their_own(self):
        first = self.box_copy('first')
        second = self.box_copy('second', addresses={'wlan0': OTHER_HARDWARE_ADDRESS})

        self.start_access_point(first)
        self.start_access_point(second)

        self.assertEqual((self.announced(first), self.announced(second)),
                         ('Viindoo-IoT-b827eb1a2b3c', 'Viindoo-IoT-dca632009fe1'))

    def test_access_point_of_a_box_whose_wifi_gives_no_hardware_address_announces_viindoo_iot(self):
        for case, addresses in {'no address': {}, 'address that is none': {'wlan0': '00:00'}}.items():
            with self.subTest(case):
                root = self.box_copy(case.replace(' ', '-'), addresses=addresses)

                done = self.start_access_point(root)

                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertEqual(self.announced(root), 'Viindoo-IoT')

    def test_name_comes_from_the_interface_the_access_point_runs_on(self):
        root = self.box_copy(configuration='interface=wlan1\nssid=IoTBox\nchannel=6\n',
                             addresses={'wlan0': HARDWARE_ADDRESS, 'wlan1': OTHER_HARDWARE_ADDRESS})

        self.start_access_point(root)

        self.assertEqual(self.announced(root), 'Viindoo-IoT-dca632009fe1')

    def test_settings_of_the_access_point_other_than_its_name_are_kept_and_the_box_service_still_reads_it(self):
        root = self.box_copy(configuration='interface=wlan0\nssid=IoTBox\nssid=Second\nchannel=1\nhw_mode=g\n')

        self.start_access_point(root)

        self.assertEqual(self.configuration(root),
                         'interface=wlan0\nssid=Viindoo-IoT-b827eb1a2b3c\nchannel=1\nhw_mode=g\n')
        self.assertEqual((root / CONFIGURATION).stat().st_mode & 0o777, 0o644)

    def test_access_point_started_again_keeps_its_name_and_its_configuration_is_not_written_again(self):
        root = self.box_copy()
        self.start_access_point(root)
        written = (root / CONFIGURATION).stat()

        done = self.start_access_point(root)

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.announced(root), 'Viindoo-IoT-b827eb1a2b3c')
        self.assertEqual(((root / CONFIGURATION).stat().st_ino, (root / CONFIGURATION).stat().st_mtime_ns),
                         (written.st_ino, written.st_mtime_ns))


class TestNameOfTheAccessPointIsInstalled(AccessPointCase, common.BoxStateCase):
    """The installer of the system files - run on the image and after each update - has hostapd name the access
    point before each start."""

    PLACES = ('etc/systemd', 'usr/local/sbin')
    INSTALLED_PROGRAM = Path('/usr/local/sbin/viin-box-access-point-name')
    INSTALLED_DROP_IN = Path('/etc/systemd/system/hostapd.service.d/viindoo.conf')
    # What the unit of hostapd sets DAEMON_CONF to, before /etc/default/hostapd.
    DAEMON_CONF = '/etc/hostapd/hostapd.conf'

    def install_on_a_box_of_the_release_before(self):
        """Run the installer on a box that has neither the program nor the drop-in, as a box of an earlier release."""
        saved = str(Path(self.directory, 'before.tar'))
        common.as_root('tar', '-C', '/', '-cpf', saved, *self.PLACES)

        def put_back():
            common.as_root('rm', '-rf', *(f'/{place}' for place in self.PLACES))
            common.as_root('tar', '-C', '/', '-xpf', saved)

        self.addCleanup(put_back)
        common.as_root('rm', '-rf', str(self.INSTALLED_PROGRAM), str(self.INSTALLED_DROP_IN.parent))
        return subprocess.run(['sudo', '-n', 'bash', str(INSTALLER)], capture_output=True, text=True, check=False,
                              timeout=common.PROCESS_TIMEOUT)

    def test_box_updated_in_place_names_its_access_point_from_the_next_start_of_the_access_point(self):
        done = self.install_on_a_box_of_the_release_before()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.INSTALLED_DROP_IN.read_bytes() if self.INSTALLED_DROP_IN.exists() else None,
                         DROP_IN.read_bytes())
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.read(self.INSTALLED_DROP_IN)
        command, *arguments = shlex.split(parser['Service']['ExecStartPre'])
        root = self.box_copy()

        given = [argument.replace('${DAEMON_CONF}', self.DAEMON_CONF) for argument in arguments]
        started = self.start_access_point(root, command.lstrip('-'), *given)

        self.assertEqual(self.INSTALLED_PROGRAM.read_bytes(), PROGRAM.read_bytes())
        self.assertEqual(self.INSTALLED_PROGRAM.stat().st_mode & 0o777, 0o755)
        self.assertTrue(command.startswith('-'), 'a name that cannot be set must not hold the access point back')
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertEqual(self.announced(root), 'Viindoo-IoT-b827eb1a2b3c')
