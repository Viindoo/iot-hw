import time
import logging

from bluepy.btle import Peripheral, BTLEDisconnectError, UUID, DefaultDelegate

from odoo.addons.hw_drivers.driver import Driver
from odoo.addons.viin_hw_drivers.iot_handlers.interfaces.BluetoothInterface import blt_devices, ble_error, lock
from odoo.addons.hw_drivers.event_manager import event_manager

_logger = logging.getLogger(__name__)


class NotifyDelegate(DefaultDelegate):
    ble_device = False

    def __init__(self):
        DefaultDelegate.__init__(self)

    def handleNotification(self, cHandle, data):
        value = self.convert_data_to_value(data)
        self.ble_device.data['value'] = value
        event_manager.device_changed(self.ble_device)
    
    def convert_data_to_value(self, data):
        measurement_bytes = data[3:6]
        measurement_value = int.from_bytes(measurement_bytes, byteorder='little')
        actual_value = measurement_value / 100.0
        return actual_value

class UWAVEBluetoothDriver(Driver):
    connection_type = 'bluetooth'
    guage_name = ["UWAVE"]
    service_uuid = "7eafd361-f150-4785-b307-47d34ed52c3c"
    char_uuid = "7eafd361-f151-4785-b307-47d34ed52c3c"
    device_id_uuid = "00002a00-0000-1000-8000-00805f9b34fb"

    def __init__(self, identifier, device):
        super(UWAVEBluetoothDriver, self).__init__(identifier, device)
        self.device_type = 'device'
        self.device_connection = 'bluetooth'
        self.notification = None
        self.device_name = None
        self.device = device      

    @classmethod
    def supported(cls, device):
        if device.alias() in cls.guage_name:
            return True
        return False

    def connect_to_device(self, address):
        try:
            _logger.info(f"Connecting to {address}...")
            device = Peripheral(address)
            return device
        except BTLEDisconnectError as e:
            _logger.error(f"Failed connecting: {str(e)}")
            self._remove_ble_error()
            return

    def get_service_and_characteristic(self, device):
        try:
            service = device.getServiceByUUID(self.service_uuid)
            characteristic = service.getCharacteristics(self.char_uuid)[0]
            return characteristic
        except Exception as e:
            _logger.error(f"Failed to find the Measurement service or characteristic: {str(e)}")
            return
        
    def enable_notifications(self, device):
        notifications = NotifyDelegate()
        notifications.ble_device = self
        device.setDelegate(notifications)
        characteristic = self.get_service_and_characteristic(device)
        if not characteristic:
            device.disconnect()
            self._remove_ble_error()
            return
        try:
            device.writeCharacteristic(characteristic.valHandle + 1, b'\x01\x00')
            return True
        except Exception as e:
            _logger.error(f"Failed to enable notifications: {str(e)}")
            device.disconnect()
            self._remove_ble_error()
            return
    
    def read_characteristic(self, device, uuid):
        try:
            characteristic = device.getCharacteristics(uuid=UUID(uuid))[0]
            if characteristic.supportsRead():
                value = characteristic.read()
                return value.decode('utf-8')
            else:
                _logger.warning(f"Characteristic {uuid} does not support reading.")
                return
        except Exception as e:
            _logger.error(f"Error reading characteristic {uuid}: {str(e)}")
            return
    
    def _remove_ble_error(self):
        # When a connected device fails,
        # it must be removed from the list of found devices
        identifier = "blt_%s" % self.device.mac_address
        ble_error[identifier] = self.device
        with lock:
            del blt_devices[identifier]
    
    def run(self):
        while not self.device:
            time.sleep(1)
        device = self.connect_to_device(self.device.mac_address)
        if device:
            device_name = self.read_characteristic(device, self.device_id_uuid)
            self.device_name = f"Uwave_{device_name}"
            self.notification = self.enable_notifications(device)
        while True:
            if self.notification and device.waitForNotifications(1):
                continue
