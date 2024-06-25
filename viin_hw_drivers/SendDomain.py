from threading import Thread
import time

from odoo.addons.hw_drivers.main import manager
from odoo.addons.hw_drivers.tools import helpers


class SendDomain(Thread):

    def run(self):
        while 1:
            subject_cn = helpers.read_file_first_line('odoo-subject.conf')
            server = helpers.get_odoo_server_url()
            if server and subject_cn:
                manager.send_alldevices()
                break
            time.sleep(3)

send_domain = SendDomain()
send_domain.daemon = True
send_domain.start()
