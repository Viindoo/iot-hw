from threading import Lock

from odoo.addons.hw_drivers.driver import Driver
from odoo.addons.hw_drivers.event_manager import event_manager


class SimulatedScaleDriver(Driver):
    """A scale whose weight, in kilograms, is the content of the device file.

    It takes the actions and reports the data of the serial scales of core.
    """
    connection_type = 'simulated'
    _poll_delay = 0.5

    def __init__(self, identifier, device):
        super().__init__(identifier, device)
        self.device_type = 'scale'
        self.device_connection = 'serial'
        self.device_name = 'Simulated scale'
        self.device_manufacturer = 'Viindoo software box'
        self._weight_file = device['path']
        self._device_lock = Lock()
        self._is_reading = True
        self._last_sent_value = None
        self._actions.update({
            'read_once': self._read_once_action,
            'start_reading': self._start_reading_action,
            'stop_reading': self._stop_reading_action,
        })

    @classmethod
    def supported(cls, device):
        return device['kind'] == 'scale'

    def run(self):
        while not self._stopped.wait(self._poll_delay):
            if not self._is_reading:
                continue
            with self._device_lock:
                self._read_weight()
                if self.data['value'] != self._last_sent_value:
                    self._last_sent_value = self.data['value']
                    event_manager.device_changed(self)

    def _read_weight(self):
        # A file that holds no number reads as zero, as a scale that has no stable weight.
        try:
            weight = float(self._weight_file.read_text())
        except (OSError, ValueError):
            weight = 0.0
        self.data = {
            'value': weight,
            'status': {'status': 'connected', 'message_title': '', 'message_body': ''},
        }

    def _read_once_action(self, data):
        with self._device_lock:
            self._read_weight()
            self._last_sent_value = self.data['value']
            event_manager.device_changed(self)

    def _start_reading_action(self, data):
        self._is_reading = True

    def _stop_reading_action(self, data):
        self._is_reading = False
