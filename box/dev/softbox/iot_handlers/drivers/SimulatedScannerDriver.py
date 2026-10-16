import json

from odoo.addons.hw_drivers.driver import Driver
from odoo.addons.hw_drivers.event_manager import event_manager

# Same name and JSON layout as the saved choice of core's keyboard driver, kept beside the scans file.
IS_SCANNER_FILE_NAME = 'odoo-keyboard-is-scanner.conf'


class SimulatedScannerDriver(Driver):
    """A scanner that reads one barcode from each line appended to the device file.

    As the keyboard handler of core, it can be switched to a keyboard, which sends
    the characters of a line one by one. The choice is saved beside the scans file,
    so that the box starts with the device type it had before a restart.
    """
    connection_type = 'simulated'
    _poll_delay = 0.2

    def __init__(self, identifier, device):
        super().__init__(identifier, device)
        self.device_connection = 'direct'
        self.device_name = 'Simulated barcode scanner'
        self.device_manufacturer = 'Viindoo software box'
        self._scans_file = device['path']
        self._is_scanner_file = self._scans_file.parent / IS_SCANNER_FILE_NAME
        # Lines written before the scanner was plugged in were never scanned by it.
        self._offset = self._scans_file.stat().st_size
        self._actions.update({
            '': self._action_default,
            'update_is_scanner': self._update_is_scanner_action,
        })
        self.device_type = 'scanner' if self._load_is_scanner() else 'keyboard'

    @classmethod
    def supported(cls, device):
        return device['kind'] == 'scanner'

    def run(self):
        while not self._stopped.wait(self._poll_delay):
            for line in self._read_new_lines():
                values = [line] if self.device_type == 'scanner' else line
                for value in values:
                    self.data['value'] = value
                    event_manager.device_changed(self)

    def _read_new_lines(self):
        try:
            with self._scans_file.open('rb') as scans:
                # An emptied file starts a new series of scans.
                self._offset = min(self._offset, self._scans_file.stat().st_size)
                scans.seek(self._offset)
                lines = []
                while (line := scans.readline()).endswith(b'\n'):
                    self._offset = scans.tell()
                    lines.append(line.decode(errors='replace').strip())
        except OSError:
            return []
        return [line for line in lines if line]

    def _read_saved_choices(self):
        try:
            return json.loads(self._is_scanner_file.read_text())
        except (OSError, ValueError):
            return {}

    def _load_is_scanner(self):
        # Without a saved choice the device starts as a scanner.
        return self._read_saved_choices().get(self.device_identifier, {}).get('is_scanner', True)

    def _save_is_scanner(self, is_scanner):
        choices = self._read_saved_choices()
        choices[self.device_identifier] = {'is_scanner': is_scanner}
        self._is_scanner_file.write_text(json.dumps(choices))

    def _action_default(self, data):
        self.data['value'] = ''
        event_manager.device_changed(self)

    def _update_is_scanner_action(self, data):
        is_scanner = bool(data.get('is_scanner'))
        self._save_is_scanner(is_scanner)
        self.device_type = 'scanner' if is_scanner else 'keyboard'
