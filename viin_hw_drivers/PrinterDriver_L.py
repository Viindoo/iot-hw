import io
import logging
import subprocess

from base64 import b64decode
from PIL import Image, ImageOps

from odoo.addons.hw_drivers.iot_handlers.drivers import PrinterDriver_L
from odoo.addons.hw_drivers.main import iot_devices
from odoo.addons.hw_escpos.escpos.printer import Network
from odoo.addons.hw_drivers.iot_handlers.interfaces.PrinterInterface_L import PPDs, conn, cups_lock
from odoo.addons.hw_drivers.driver import Driver

_logger = logging.getLogger(__name__)

global PrinterDriver_L

def _init_New(self, identifier, device):
    Driver.__init__(self, identifier, device)
    self.device_type = 'printer'
    self.device_connection = device['device-class'].lower()
    self.device_name = device['device-make-and-model']
    self.ip_socket = device.get('ip_socket', False)
    self.port = 9100
    self.state = {
        'status': 'connecting',
        'message': 'Connecting to printer',
        'reason': None,
    }
    self.send_status()

    self._actions.update({
        'cashbox': self.open_cashbox,
        'print_receipt': self.print_receipt,
        '': self._action_default,
    })

    self.receipt_protocol = 'star' if 'STR_T' in device['device-id'] else 'escpos'
    if 'direct' in self.device_connection and any(cmd in device['device-id'] for cmd in ['CMD:STAR;', 'CMD:ESC/POS;']):
        self.print_status()

@classmethod
def _supported(cls, device):
    if device.get('supported', False):
        return True
    if device.get('ip_socket', False):
        return True
    else:
        protocol = ['dnssd', 'lpd', 'socket']
        if any(x in device['url'] for x in protocol) and device['device-make-and-model'] != 'Unknown' or 'direct' in device['device-class']:
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

def _print_raw(self, data):
    if self.ip_socket:
        try:
            print_data = Network(self.ip_socket, self.port)
            print_data._raw(data)
            print_data.__del__()
        except:
            _logger.error('Could not open socket ip %s' % self.ip_socket)
    else:
        process = subprocess.Popen(["lp", "-d", self.device_identifier], stdin=subprocess.PIPE)
        process.communicate(data)

def _print_receipt(self, data):
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

PrinterDriver_L.PrinterDriver.__init__ = _init_New
PrinterDriver_L.PrinterDriver.supported = _supported
PrinterDriver_L.PrinterDriver.print_raw = _print_raw
PrinterDriver_L.PrinterDriver.print_receipt = _print_receipt
