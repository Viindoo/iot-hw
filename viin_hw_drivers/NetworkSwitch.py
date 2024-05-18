import subprocess
import logging
import time
from traceback import format_exc
from threading import Thread

from netifaces import ifaddresses, AF_INET

from odoo.addons.hw_drivers.tools import helpers
from odoo.addons.viin_hw_drivers.tools import viin_helpers
from odoo.addons.hw_drivers.main import manager
from odoo.modules.module import get_resource_path

_logger = logging.getLogger(__name__)

class NetworkSwitch(Thread):
    def get_ip(self, interface):
        if ifaddresses(interface).get(AF_INET):
            return ifaddresses(interface).get(AF_INET)[0]['addr']
        return None

    def run(self):
        ip_old = helpers.get_ip()
        eth0_ip_old = None
        wlan0_ip_old = None

        if not self.get_ip('wlan0') and helpers.read_file_first_line('wifi_network.txt'):
            subprocess.check_call([get_resource_path('point_of_sale', 'tools/posbox/configuration/connect_to_wifi.sh'), '', '', ''])

        while 1:
            try:
                if self.get_ip('eth0') and eth0_ip_old != self.get_ip('eth0'):
                    eth0_ip_old = self.get_ip('eth0')
                    viin_helpers.load_certificate(eth0_ip_old)

                if self.get_ip('wlan0') and wlan0_ip_old != self.get_ip('wlan0'):
                    wlan0_ip_old = self.get_ip('wlan0')
                    viin_helpers.load_certificate(wlan0_ip_old)

                if helpers.get_ip() and ip_old != helpers.get_ip():
                    ip_old = helpers.get_ip()
                    manager.send_alldevices()
                    time.sleep(2)
                time.sleep(3)
            except Exception:
                _logger.error(format_exc())

network_switch = NetworkSwitch()
network_switch.daemon = True
network_switch.start()
