from pathlib import Path

from odoo.addons.hw_drivers.interface import Interface

DEVICE_FILES = {
    'scale': Path('/var/lib/softbox/scale-weight'),
    'scanner': Path('/var/lib/softbox/scanner-scans'),
}


class SimulatedDeviceInterface(Interface):
    """A simulated device is plugged in for as long as its file exists."""
    connection_type = 'simulated'

    def get_devices(self):
        return {
            f'simulated_{kind}': {'kind': kind, 'path': path}
            for kind, path in DEVICE_FILES.items()
            if path.exists()
        }
