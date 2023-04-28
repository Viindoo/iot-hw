import serial

from odoo.addons.hw_drivers.iot_handlers.drivers import SerialScaleDriver


global SerialScaleDriver

def _FnOrgrin_stop_reading_action(self, data):
    """Stops asking for the scale value."""
    self._is_reading = False
    self.data['value'] = 0

SerialScaleDriver.ScaleDriver._stop_reading_action = _FnOrgrin_stop_reading_action

class SerialEquipmentDriver(SerialScaleDriver.AdamEquipmentDriver, SerialScaleDriver.ScaleDriver):
    """Driver for serial scale."""
    _protocol = SerialScaleDriver.ScaleProtocol(name='Scales',
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

    def __init__(self, identifier, device):
        super(SerialEquipmentDriver, self).__init__(identifier, device)
        self._is_reading = False
        self._last_weight_time = 0
        self.device_manufacturer = 'Serial'
