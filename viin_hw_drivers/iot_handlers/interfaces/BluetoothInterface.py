from ..lib.gatt import DeviceManager
import logging
import threading

from odoo.addons.hw_drivers.interface import Interface

blt_devices = {}
ble_error = {}
lock = threading.Lock()

_logger = logging.getLogger(__name__)

class NotifyScanBLT(DeviceManager):
    def device_discovered(self, device):
        identifier = "blt_%s" % device.mac_address
        if identifier not in blt_devices.keys():
            with lock:
                blt_devices[identifier] = device
                print(device.alias())

class BLEDeviceManager(threading.Thread):
    def run(self):
        try:
            dm = NotifyScanBLT(adapter_name='hci0')
            dm.start_discovery()
            dm.run()
        except Exception as e:
            _logger.error(f"Failed scanner bluetooth: {str(e)}")

class UWAVEInterface(Interface):
    connection_type = 'bluetooth'

    def get_devices(self):
        ble_devices = blt_devices.copy()
        _detected_devices = dict()
        for key, value in ble_devices.items():
            if key in self._detected_devices:
                if key not in ble_error:
                    _detected_devices[key] = value
                else:
                    del ble_error[key]
        self._detected_devices = _detected_devices.keys() if _detected_devices else {}

        return ble_devices

blemanager = BLEDeviceManager()
blemanager.daemon = True
blemanager.start()
