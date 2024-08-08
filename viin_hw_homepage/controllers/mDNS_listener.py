import json
import jinja2
from lxml import html

from odoo import http
from odoo.addons.viin_hw_homepage.controllers.main import replace_link_and_branding
from odoo.addons.viin_hw_drivers.tools import viin_helpers

loader = jinja2.FileSystemLoader(["/home/pi/odoo/addons/viin_hw_homepage/views", "/home/pi/odoo/addons/hw_posbox_homepage/views"])

jinja_env = jinja2.Environment(loader=loader, autoescape=True)
jinja_env.filters["json"] = json.dumps

scan_iot_box_template = jinja_env.get_template('scan_iot_box.html')


class mDNSListener(http.Controller):
    @http.route('/scan_iot_box', type='http', auth='none', website=True)
    def scan_iot_box(self):
        str_scan = scan_iot_box_template.render({
            'title': 'Viindoo - Scan IoT Box',
            'breadcrumb': 'Scan IoT Box',
            'items': viin_helpers.mDNS_listener(),
        })

        html_scan = html.fromstring(str_scan)
        html_scan = replace_link_and_branding(html_scan)
        return html.tostring(html_scan, pretty_print=True, method='html', encoding='unicode')
