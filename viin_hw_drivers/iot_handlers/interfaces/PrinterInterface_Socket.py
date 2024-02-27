import socket
import subprocess
import logging

import netifaces
import ipaddress

from odoo.addons.hw_drivers.interface import Interface

_logger = logging.getLogger(__name__)


class PrinterSocketInterface(Interface):
    _loop_delay = 20
    connection_type = 'printer'

    def get_devices(self):
        port = 9100
        printer_socket_devices = {}
        interfaces = netifaces.interfaces()
        for interface in interfaces:
            if netifaces.ifaddresses(interface).get(netifaces.AF_INET):
                addr = netifaces.ifaddresses(interface).get(netifaces.AF_INET)[0]['addr']
                if addr != '127.0.0.1':
                    default_interface = netifaces.gateways()['default'][netifaces.AF_INET][1]
                    subnet_mask = netifaces.ifaddresses(default_interface)[netifaces.AF_INET][0]['netmask']
                    subnet_mask_CIDR = sum([bin(int(x)).count('1') for x in subnet_mask.split('.')])
                    network = ipaddress.IPv4Network(addr + '/' + subnet_mask, strict=False)
                    network_address = network.network_address

                    target_ip = str(network_address) + "/" + str(subnet_mask_CIDR)
                    devices = subprocess.run(["sudo", "nmap", "-sn", target_ip], capture_output=True, text=True)
                    devices = devices.stdout.split('\n')
                    for device in devices:
                        if 'Nmap scan report for' in device:
                            ip = device.split(' ')[4]
                        if 'MAC Address:' in device:
                            mac = device.split(' ')[2]
                            try:
                                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                                sock.settimeout(0.2)
                                result = sock.connect_ex((ip, port))
                                if result == 0:
                                    sock.close()
                                    identifier = mac
                                    printer_socket_devices[identifier] = {
                                        'device-class': 'network',
                                        'device-make-and-model': 'printsocket' + ip,
                                        'identifier': identifier,
                                        'device-id': '',
                                        'ip_socket': ip
                                    }
                                    continue
                                sock.close()
                            except:
                                _logger.error(('Scan IP %s Error') % (ip))
        return printer_socket_devices
