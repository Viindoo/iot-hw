import serial

from odoo.addons.hw_drivers.iot_handlers.drivers import SerialScaleDriver

global SerialScaleDriver

_init = SerialScaleDriver.AdamEquipmentDriver.__init__
stop_reading = SerialScaleDriver.ScaleDriver._stop_reading_action

ViindooQuipmentProtocol = SerialScaleDriver.ScaleProtocol(
        name='Scales',
        baudrate=9600,
        bytesize=serial.EIGHTBITS,
        stopbits=serial.STOPBITS_ONE,
        parity=serial.PARITY_NONE,
        timeout=0.2,
        writeTimeout=0.2,
        measureRegexp=b"\s*([0-9.]+)kg",  # LABEL format 3 + KG in the scale settings, but Label 1/2 should work
        statusRegexp=None,
        commandTerminator=b"\r\n",
        commandDelay=0.2,
        measureDelay=0.5,
        newMeasureDelay=2,
        measureCommand=b'P',
        zeroCommand=b'Z',
        tareCommand=b'T',
        clearCommand=None,  # No clear command -> Tare again
        emptyAnswerValid=True,  # AZExtra does not answer unless a new non-zero weight has been detected
        autoResetWeight=True,  # AZExtra will not return 0 after removing products
)

def FnOrg_init(self, identifier, device):
    _init(self, identifier, device)
    self.device_manufacturer = 'Serial'

def FnOrg_stop_reading_action(self, data):
    stop_reading(self, data)
    self.data['value'] = 0

SerialScaleDriver.AdamEquipmentDriver._protocol = ViindooQuipmentProtocol
SerialScaleDriver.AdamEquipmentDriver.__init__ = FnOrg_init
SerialScaleDriver.ScaleDriver._stop_reading_action = FnOrg_stop_reading_action
