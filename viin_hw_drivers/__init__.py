import sys

from odoo.addons.hw_drivers.main import drivers
from odoo.addons.hw_drivers.driver import DriverMetaClass
from odoo.addons.viin_hw_drivers.tools import viin_helpers
from odoo.addons.hw_drivers.tools import helpers

global DriverMetaClass

def FnOrg__new(cls, clsname, bases, attrs):
    newclass = super(DriverMetaClass, cls).__new__(cls, clsname, bases, attrs)
    if hasattr(newclass, 'priority'):
        newclass.priority += 1
    else:
        newclass.priority = 0
    if newclass.__name__  == 'PrinterDriver' and newclass.__name__ in [driver.__name__ for driver in drivers]:
        return
    drivers.append(newclass)
    return newclass

DriverMetaClass.__new__ = FnOrg__new

helpers.load_certificate = viin_helpers.load_certificate
helpers.load_iot_handlers = viin_helpers.load_iot_handlers
helpers.download_iot_handlers = viin_helpers.download_iot_handlers

sys.modules['odoo.addons.hw_drivers.tools.helpers'] = helpers

from . import DisplayDriver_L
from . import PrinterDriver_L
from . import SerialScaleDriver
from . import controllers
from . import NetworkSwitch
from . import SendDomain
