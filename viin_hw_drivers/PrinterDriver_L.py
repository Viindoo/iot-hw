import io
from base64 import b64decode
from PIL import Image, ImageOps

from odoo.addons.hw_drivers.iot_handlers.drivers import PrinterDriver_L


global PrinterDriver_L


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

PrinterDriver_L.PrinterDriver.print_receipt = _print_receipt
