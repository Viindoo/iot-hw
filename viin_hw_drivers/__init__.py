from . import DisplayDriver_L
from . import PrinterDriver_L
from . import SerialScaleDriver
from . import KeyboardUSBDriver_L
from . import controllers
from odoo.addons.viin_hw_drivers.tools import viin_helpers

viin_helpers.load_iot_handlers()