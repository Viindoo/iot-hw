from odoo.addons.hw_drivers.iot_handlers.drivers import SerialScaleDriver

global SerialScaleDriver

init_AdamEquipmentDriver = SerialScaleDriver.AdamEquipmentDriver.__init__
UPA_Q_quipmentProtocol = SerialScaleDriver.ADAMEquipmentProtocol._replace(
    name='UPA-Q',
    baudrate=9600,
)

def FnOrg_stop_reading_action(self, data):
    """Stops asking for the scale value."""
    self._is_reading = False
    self.data['value'] = 0

def FnOrg__init__(self, identifier, device):
    init_AdamEquipmentDriver(self, identifier, device)
    self._is_reading = False
    self._last_weight_time = 0
    self.device_manufacturer = 'UTE - TAIWAN'

SerialScaleDriver.AdamEquipmentDriver._protocol = UPA_Q_quipmentProtocol
SerialScaleDriver.ScaleDriver._stop_reading_action = FnOrg_stop_reading_action
SerialScaleDriver.AdamEquipmentDriver.__init__ = FnOrg__init__
