from odoo import http
from odoo.addons.hw_drivers.main import iot_devices


class ViinDriverController(http.Controller):

    @http.route('/hw_drivers/check_devices', type='json', auth='none', cors='*')
    def check_devices(self):
        devices_list = {}
        for device in iot_devices:
            identifier = iot_devices[device].device_identifier
            devices_list[identifier] = {
                'name': iot_devices[device].device_name,
                'type': iot_devices[device].device_type,
                'connection': iot_devices[device].device_connection,
            }
        return devices_list
