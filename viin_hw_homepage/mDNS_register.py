import logging
import subprocess
from pathlib import Path

from odoo.addons.hw_drivers.tools import helpers

_logger = logging.getLogger(__name__)

class MDNSRegister():
    def __init__(self, service_name, service_type, port, txt_record, file_service_name=None):
        self.service_name = service_name
        self.service_type = service_type
        self.port = port
        self.txt_record = txt_record
        self.file_service_name = file_service_name or "viindoo-iot"
        self.create_service_file()

    def create_service_file(self):
        service_config = f"""
        <service-group>
            <name replace-wildcards="yes">{self.service_name}</name>
            <service>
                <type>{self.service_type}</type>
                <port>{self.port}</port>
                <txt-record>{self.txt_record}</txt-record>
            </service>
        </service-group>
        """
        try:
            with helpers.writable():
                Path(f'/etc/avahi/services/{self.file_service_name}.service').write_text(service_config)
                # Path(f'/root_bypass_ramdisks/etc/avahi/services/{self.file_service_name}.service').write_text(service_config)
        except Exception as e:
            _logger.error(f'Error create service file: {e}')
    
    @staticmethod
    def restart_avahi_daemon():
        subprocess.check_call(["sudo", "service", "avahi-daemon", "restart"])

    @staticmethod
    def read_name_box():
        name = helpers.read_file_first_line('name_of_iot_box')
        if not name:
            mac = helpers.get_mac_address().split(':')
            name = "Viindoo-IoT-{}".format(mac[-2] + mac[-1])
            helpers.write_file('name_of_iot_box', name)
        return name
    
    @classmethod
    def register_service(cls):
        name_iot = cls.read_name_box()
        service_name = helpers.get_mac_address().replace(":", "-")
        service_type = "_viindoo-iot._tcp"
        port = 5353
        server = helpers.get_odoo_server_url()
        version = helpers.read_file_first_line('iotbox_version')
        txt_record = f"name_iot={name_iot}, server={server}, version={version}"

        mdns_register = cls(service_name, service_type, port, txt_record)
        mdns_register.restart_avahi_daemon()

MDNSRegister.register_service()
