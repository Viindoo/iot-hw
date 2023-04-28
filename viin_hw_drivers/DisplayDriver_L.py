import jinja2
import json
import os
import netifaces as ni
import subprocess
import time

from odoo import http
from odoo.addons.hw_drivers.tools import helpers
from odoo.addons.hw_drivers.iot_handlers.drivers import DisplayDriver_L


global DisplayDriver_L

path = os.path.realpath(os.path.join(os.path.dirname(__file__), './views'))
loader = jinja2.FileSystemLoader(path)

jinja_env = jinja2.Environment(loader=loader, autoescape=True)
jinja_env.filters["json"] = json.dumps

pos_display_template = jinja_env.get_template('pos_display.html')


def _run(self):
    while self.device_identifier != 'distant_display' and not self._stopped.isSet():
        time.sleep(60)
        if self.url != 'http://localhost:8069/point_of_sale/viin_display/' + self.device_identifier:
            # Refresh the page every minute
            self.call_xdotools('F5')

def _update_url(self, url=None):
    if not helpers.get_odoo_server_url():
        if not helpers.access_point():
            url = 'http://' + helpers.get_ip() + ':8069/server'
        else:
            url = 'http://' + helpers.get_ip() + ':8069/wifi'
    os.environ['DISPLAY'] = ":0." + self._x_screen
    os.environ['XAUTHORITY'] = '/run/lightdm/pi/xauthority'
    firefox_env = os.environ.copy()
    firefox_env['HOME'] = '/tmp/' + self._x_screen
    self.url = url or 'http://localhost:8069/point_of_sale/viin_display/' + self.device_identifier
    new_window = subprocess.call(['xdotool', 'search', '--onlyvisible', '--screen', self._x_screen, '--class', 'Firefox'])
    subprocess.Popen(['firefox', self.url], env=firefox_env)
    if new_window:
        self.call_xdotools('F11')

DisplayDriver_L.DisplayDriver.run = _run
DisplayDriver_L.DisplayDriver.update_url = _update_url

class Viin_DisplayController(http.Controller):

    @http.route(['/point_of_sale/viin_display', '/point_of_sale/viin_display/<string:display_identifier>'], type='http', auth='none')
    def display(self, display_identifier=None):
        cust_js = None
        interfaces = ni.interfaces()

        with open(os.path.join(os.path.dirname(__file__), "../hw_drivers/static/src/js/worker.js")) as js:
            cust_js = js.read()

        display_ifaces = []
        for iface_id in interfaces:
            if 'wlan' in iface_id or 'eth' in iface_id:
                iface_obj = ni.ifaddresses(iface_id)
                ifconfigs = iface_obj.get(ni.AF_INET, [])
                essid = helpers.get_ssid()
                for conf in ifconfigs:
                    if conf.get('addr'):
                        display_ifaces.append({
                            'iface_id': iface_id,
                            'essid': essid,
                            'addr': conf.get('addr'),
                            'icon': 'sitemap' if 'eth' in iface_id else 'wifi',
                        })

        if not display_identifier:
            display_identifier = DisplayDriver_L.DisplayDriver.get_default_display().device_identifier

        return pos_display_template.render({
            'title': "Viindoo -- Point of Sale",
            'breadcrumb': 'POS Client display',
            'cust_js': cust_js,
            'display_ifaces': display_ifaces,
            'display_identifier': display_identifier,
        })
