from odoo.addons.hw_drivers.iot_handlers.drivers import KeyboardUSBDriver_L

global KeyboardUSBDriver_L

check_device = KeyboardUSBDriver_L.KeyboardUSBDriver._set_device_type

def FnOrg_set_device_type(self, device_type='keyboard'):
    check_device(self, device_type='keyboard')
    if device_type == 'scanner':
        self.device_type = 'barcode_scanner'

KeyboardUSBDriver_L.KeyboardUSBDriver._set_device_type = FnOrg_set_device_type
