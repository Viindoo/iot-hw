import io
import logging

from base64 import b64decode
from PIL import Image, ImageOps

from odoo.addons.hw_escpos.escpos.printer import Network
from odoo.addons.hw_drivers.controllers.proxy import proxy_drivers
from odoo.addons.hw_drivers.main import iot_devices

_logger = logging.getLogger(__name__)

try:
    from odoo.addons.hw_drivers.iot_handlers.interfaces.PrinterInterface_L import PPDs, conn, cups_lock
    from odoo.addons.hw_drivers.iot_handlers.drivers import PrinterDriver_L

    global PrinterDriver_L

    supported = PrinterDriver_L.PrinterDriver.supported
    init_PrinterDriver = PrinterDriver_L.PrinterDriver.__init__
    print_raw = PrinterDriver_L.PrinterDriver.print_raw

    def FnOrg_init(self, identifier, device):
        self.ip_socket = device.get('ip_socket', False)
        self.port = 9100
        init_PrinterDriver(self, identifier, device)

    @classmethod
    def FnOrg_supported(cls, device):
        if device.get('ip_socket', False):
            return True
        elif 'direct' in device['device-class']:
            model = cls.get_device_model(device)
            ppdFile = ''
            for ppd in PPDs:
                if model and model in PPDs[ppd]['ppd-product']:
                    ppdFile = ppd
                    break
            with cups_lock:
                if ppdFile:
                    conn.addPrinter(name=device['identifier'], ppdname=ppdFile, device=device['url'])
                else:
                    conn.addPrinter(name=device['identifier'], device=device['url'])
                conn.setPrinterInfo(device['identifier'], device['device-make-and-model'])
                conn.enablePrinter(device['identifier'])
                conn.acceptJobs(device['identifier'])
                conn.setPrinterUsersAllowed(device['identifier'], ['all'])
                conn.addPrinterOptionDefault(device['identifier'], "usb-no-reattach", "true")
                conn.addPrinterOptionDefault(device['identifier'], "usb-unidir", "true")
            return True
        return False

    def FnOrg_print_raw(self, data):
        if self.ip_socket:
            try:
                print_data = Network(self.ip_socket, self.port)
                print_data._raw(data)
                print_data.__del__()
            except:
                _logger.error('Could not open socket ip %s' % self.ip_socket)
        else:
            print_raw(self, data)

    def FnOrg_print_receipt(self, data):
        receipt = b64decode(data['receipt'])
        im = Image.open(io.BytesIO(receipt))

        # Convert to greyscale then to black and white
        im = im.convert("L")
        im = ImageOps.invert(im)
        im = im.convert("1")

        print_command = getattr(self, 'format_%s' % self.receipt_protocol)(im)
        commands = PrinterDriver_L.RECEIPT_PRINTER_COMMANDS[self.receipt_protocol]
        self.print_raw(commands['center'])
        self.print_raw(print_command)

    @classmethod
    def FnOrg_get_status(cls):
        status = 'connected' if any(iot_devices[d].device_type == "printer" and iot_devices[d].device_connection in ['direct', 'network'] for d in iot_devices) else 'disconnected'
        return {'status': status, 'messages': ''}
    
    PrinterDriver_L.PrinterDriver.__init__ = FnOrg_init
    PrinterDriver_L.PrinterDriver.supported = FnOrg_supported
    PrinterDriver_L.PrinterDriver.print_raw = FnOrg_print_raw
    PrinterDriver_L.PrinterDriver.print_receipt = FnOrg_print_receipt
    PrinterDriver_L.PrinterDriver.get_status = FnOrg_get_status

    proxy_drivers['printer'] = PrinterDriver_L.PrinterDriver

except Exception as e:
    _logger.error(f"couldn't load file driver PrinterDriver_L: {e}")
