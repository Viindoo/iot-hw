import json
import logging
import socket
import subprocess
import re

import urllib3
import jinja2
import ngrok
from lxml import html

from odoo import http
from odoo.http import request, Response
from odoo.modules.module import get_resource_path, loaded
from odoo.addons.hw_drivers.tools import helpers
from odoo.addons.viin_hw_drivers.tools import viin_helpers
from odoo.addons.hw_posbox_homepage.controllers.main import IoTboxHomepage
from odoo.addons.viin_hw_homepage.Session import sessions, authenticate
from odoo.addons.viin_hw_homepage.mDNS_register import MDNSRegister
from odoo.addons.hw_drivers.server_logger import get_odoo_config_log_to_server_option, close_server_log_sender_handler

_logger = logging.getLogger(__name__)

loader = jinja2.FileSystemLoader(["/home/pi/odoo/addons/viin_hw_homepage/views", "/home/pi/odoo/addons/hw_posbox_homepage/views"])

jinja_env = jinja2.Environment(loader=loader, autoescape=True)
jinja_env.filters["json"] = json.dumps

server_config_template = jinja_env.get_template('viin_server_config.html')
change_box_name_template = jinja_env.get_template('change_box_name.html')
handler_list_template = jinja_env.get_template('handler_list.html')
remote_connect_template = jinja_env.get_template('viin_remote_connect.html')
wifi_config_template = jinja_env.get_template('wifi_config.html')
login_template = jinja_env.get_template('viin_login.html')
timeout = 1000

# Replace odoo's link with viindoo's link
def replace_link(page):
    for link in page.xpath('//div[@class="footer"]/a'):
        if "help" in link.text_content().lower():
            link.set('href', "https://viindoo.com/vi/ticket")
        elif "documentation" in link.text_content().lower():
            link.set('href', "https://viindoo.com/documentation/16.0/applications/internet-of-thinks.html")
    return page

class IoTboxHomepage(IoTboxHomepage):

    @http.route('/web/login', type='http', auth="none", csrf=False)
    def web_login(self, redirect=None, **kw):
        server = helpers.get_odoo_server_url()
        if server and request.httprequest.method == 'POST':
            username = request.params['user_name']
            password = request.params['password']
            server = helpers.get_odoo_server_url()
            cookie = viin_helpers.login(username, password)
            if cookie:
                res = Response(response=str(True))
                res.headers['Set-Cookie'] = cookie
            else:
                res = Response(response=str(False))
            return res

        if not server:
            return "<meta http-equiv='refresh' content='0; url=http://" + helpers.get_ip() + ":8069/server'>"

        sid = http.request.session.sid
        if sid in sessions and sessions[sid]._check_session(sid):
            return "<meta http-equiv='refresh' content='0; url=http://" + helpers.get_ip() + ":8069/'>"

        html_login = login_template.render({
            'title': 'IoT -> Viindoo',
            'breadcrumb': 'IoT Login Viindoo',
            'server_status': helpers.get_odoo_server_url() or 'Not configured yet',
            'loading_message': 'IoT Login To Viindoo '
        })
        html_login = html.fromstring(html_login)
        html_login = replace_link(html_login)
        res = Response(response=html.tostring(html_login, pretty_print=True, method='html', encoding='unicode'))
        return res
    
    @http.route('/web/logout', type='http', auth="none")
    def web_logout(self):
        sid = http.request.session.sid
        if sid in sessions:
            sessions.pop(sid)
        return "<meta http-equiv='refresh' content='0; url=http://" + helpers.get_ip() + ":8069/web/login'>"

    @http.route('/', type='http', auth='none')
    @authenticate
    def index(self):
        """Overide to change endpoint from  `http://ip_addr:8069/steps` to `http://ip_addr:8069/wifi`
        Because the "/steps" page is used for users to log in to the server with an Odoo Token and set up WiFi.
        Viindoo has designed a separate page for connecting to Viindoo server.
        Therefore, we only need to navigate to the WiFi setup page.
        """
        homepage_odoo = super(IoTboxHomepage, self).index().data.decode()
        url_steps = "http://" + helpers.get_ip() + ":8069/steps"
        if url_steps in homepage_odoo:
            return "<meta http-equiv='refresh' content='0; url=http://" + helpers.get_ip() + ":8069/wifi'>"
        else:
            homepage_odoo = html.fromstring(homepage_odoo)

            # override odoo's title to Viindoo
            for title in homepage_odoo.xpath("//title"):
                if "odoo" in title.text_content().lower():
                    title.text = "Viindoo -- IoT"

            # Replace odoo's link with viindoo's link
            homepage_odoo = replace_link(homepage_odoo)

            for pair in homepage_odoo.xpath("//tr/td[@class='heading']"):
                # Delete the div tag containing "Pairing Code" if any
                if "Pairing code" in pair.text_content():
                    tr_element = pair.xpath("./ancestor::tr[1]")[0]
                    tr_element.getparent().remove(tr_element)

                # TODO:
                # Delete the update button
                # When there is an update function, remove this code 
                if "Version" in pair.text_content():
                    btn = pair.xpath("./following-sibling::td/a[contains(@href, 'upgrade')]")[0]
                    btn.getparent().remove(btn)

                # Change the old link to the /change_box_name change link
                if "Name" in pair.text_content():
                    btn = pair.xpath("./following-sibling::td/a[contains(@href, 'server')]")[0]
                    btn.set('href', '/change_box_name')
                    # Modify IoT box name
                    pair.xpath("./following-sibling::td")[0].text = viin_helpers.read_name_box()

                # Replace odoo's link with viindoo's link
                if "HTTPS certificate" in pair.text_content():
                    if pair.xpath("./following-sibling::td/a[contains(@href, 'www.odoo.com')]"):
                        href = pair.xpath("./following-sibling::td/a[contains(@href, 'www.odoo.com')]")[0]
                        href.set('href', 'https://viindoo.com/vi/ticket')

            for btn in homepage_odoo.xpath("//button[contains(@class, 'btn')] | //a[@class='btn']"):
                # Since Viindoo does not use this authentication method, remove this button
                if btn.get('href') == '/list_credential':
                    btn.getparent().remove(btn)

                if btn.text_content() == "Restart Odoo service":
                    btn.text = "Restart IoT service"
                    btn_logout_str = "<button class='btn btn-sm btn-sm-restart' onclick=\"window.location.href='./web/logout'\">Log Out IoT Box</button>"
                    btn_logout = html.fromstring(btn_logout_str)
                    btn.addprevious(btn_logout)

            homepage_viidoo = html.tostring(homepage_odoo, pretty_print=True, method='html', encoding='unicode')
            return homepage_viidoo

    @http.route('/server', type='http', auth='none', website=True)
    @authenticate
    def server(self):
        """
        Navigate to Viindoo server login page.
        """
        html_server = server_config_template.render({
            'title': 'IoT -> Viindoo server configuration',
            'breadcrumb': 'Configure Viindoo Server',
            'hostname': subprocess.check_output('hostname').decode('utf-8').strip('\n'),
            'server_status': helpers.get_odoo_server_url() or 'Not configured yet',
            'loading_message': 'Configure Domain Server'
        })
        html_server = html.fromstring(html_server)
        html_server = replace_link(html_server)
        return html.tostring(html_server, pretty_print=True, method='html', encoding='unicode')

    @http.route('/server_clear', type='http', auth='none', cors='*', csrf=False)
    @authenticate
    def clear_server_configuration(self):
        helpers.unlink_file('odoo-remote-server.conf')
        close_server_log_sender_handler()
        MDNSRegister.register_service()
        return "<meta http-equiv='refresh' content='0; url=http://" + helpers.get_ip() + ":8069'>"

    @http.route('/list_handlers', type='http', auth='none', website=True, csrf=False, save_session=False)
    @authenticate
    def list_handlers(self, **post):
        """
        Add a list of drivers and interfaces that Viindoo supports.
        """
        super(IoTboxHomepage, self).list_handlers(**post)
        global loaded
        AVAILABLE_LOG_LEVELS = ('debug', 'info', 'warning', 'error')
        drivers_list = list()
        interfaces_list = list()

        for module in loaded:
            fld_driver = get_resource_path(module, 'iot_handlers', 'drivers')
            fld_interface = get_resource_path(module, 'iot_handlers', 'interfaces')
            if fld_driver:
                drivers_list.extend(helpers.list_file_by_os(fld_driver))
            if fld_interface:
                interfaces_list.extend(helpers.list_file_by_os(fld_interface))

        handler_list = handler_list_template.render({
            'title': "Viindoo's IoT Box - Handlers list",
            'breadcrumb': 'Handlers list',
            'drivers_list': drivers_list,
            'interfaces_list': interfaces_list,
            # Replace with helpers.get_odoo_server_url() when we support downloading drivers
            'server': None,
            'is_log_to_server_activated': get_odoo_config_log_to_server_option(),
            'root_logger_log_level': self._get_logger_effective_level_str(logging.getLogger()),
            'odoo_current_log_level': self._get_logger_effective_level_str(logging.getLogger('odoo')),
            'recommended_log_level': 'warning',
            'available_log_levels': AVAILABLE_LOG_LEVELS,
            'drivers_logger_info': self._get_iot_handlers_logger(drivers_list, 'drivers'),
            'interfaces_logger_info': self._get_iot_handlers_logger(interfaces_list, 'interfaces'),
        })
        handler_list = html.fromstring(handler_list)
        # Replace odoo's link with viindoo's link
        handler_list = replace_link(handler_list)
        return html.tostring(handler_list, pretty_print=True, method='html', encoding='unicode')

    @http.route('/load_iot_handlers', type='http', auth='none', website=True)
    @authenticate
    def load_iot_handlers(self):
        return super(IoTboxHomepage, self).load_iot_handlers()

    @http.route('/handlers_clear', type='http', auth='none', cors='*', csrf=False)
    @authenticate
    def clear_handlers_list(self):
        return super(IoTboxHomepage, self).clear_handlers_list()

    @http.route('/wifi', type='http', auth='none', website=True)
    @authenticate
    def wifi(self):
        wifi_str = wifi_config_template.render({
            'title': 'Wifi configuration',
            'breadcrumb': 'Configure Wifi',
            'loading_message': 'Connecting to Wifi',
            'ssid': viin_helpers.get_wifi_essid(),
        })

        html_wifi = html.fromstring(wifi_str)
        html_wifi = replace_link(html_wifi)
        return html.tostring(html_wifi, pretty_print=True, method='html', encoding='unicode')

    @http.route('/wifi_clear', type='http', auth='none', cors='*', csrf=False)
    @authenticate
    def clear_wifi_configuration(self):
        return super(IoTboxHomepage, self).clear_wifi_configuration()

    @http.route('/wifi_connect', type='http', auth='none', cors='*', csrf=False)
    @authenticate
    def connect_to_wifi(self, essid, password, persistent=False):
        """
        Replace the Odoo links with Viindoo links.
        """
        res = super(IoTboxHomepage, self).connect_to_wifi(essid, password, persistent)
        res_dict = json.loads(res.data.decode())
        server = res_dict.get('server', None)
        if server and server.get('message', None).lower() == "redirect to odoo server":
            res_dict['server']['message'] = "Redirect to Viindoo Server"
        return json.dumps(res_dict)

    @http.route('/server_connect', type='http', methods=['POST'], auth='none', cors='*', csrf=False)
    @authenticate
    def connect_to_server(self):
        url_server = request.params.get('url', '')
        username = request.params.get('user_name', '')
        password = request.params.get('password', '')
        data_return = {
            'code_id': 404,
            'error_message': 'Incorrect information'
        }
        urllib3.disable_warnings()
        http = urllib3.PoolManager(cert_reqs='CERT_NONE', timeout=timeout)
        try:
            Cookie = viin_helpers.login(username, password, url_server)
            if Cookie:
                data = {
                    'name': socket.gethostname(),
                    'identifier': helpers.get_mac_address(),
                    'ip': helpers.get_ip(),
                    'token': helpers.get_token(),
                    'version': helpers.get_version(),
                }
                response = http.request(
                    'POST',
                    url_server + "/iot/create_iotbox",
                    body=json.dumps(data).encode('utf8'),
                    headers={
                        'Content-type': 'application/json',
                        'Accept': 'text/plain',
                        'Cookie': Cookie,
                    },
                )
                if response.status == 200:
                    result = json.loads(response.data.decode('utf-8')).get('result', {})
                    if result:
                        token = json.loads(result).get('token', '')
                        if token:
                            helpers.save_conf_server("https://" + url_server, token, False, False)
                            viin_helpers.sync_box_to_viindoo_saas(url_server, username, password)
                            MDNSRegister.register_service()
                            data_return = {'code_id': response.status }

        except urllib3.exceptions.HTTPError as e :
            _logger.error('A error encountered : %s ' % e)
            data_return = {
                        'code_id': 404,
                        'error_message': "Failed to establish a new connection: [Errno -2] Name or service not known"
            }
        except Exception as e:
            _logger.error('A error encountered : %s ' % e)
            data_return = {
                        'code_id': 404,
                        'error_message': str(e)
            }
        finally:
            res = request.make_response(
                json.dumps(data_return),
                headers=[('Content-Type', 'application/json; charset=utf-8')],
            )
            if Cookie:
                res.headers['Set-Cookie'] = Cookie
            return res

    @http.route('/change_boxname', type='http', auth='none', cors='*', csrf=False)
    @authenticate
    def change_boxname(self, iotboxname):
        helpers.write_file('name_of_iot_box', iotboxname)
        MDNSRegister.register_service()
        data_return = {'message': 'Change_OK'}
        return json.dumps(data_return)

    @http.route('/change_box_name', type='http', auth='none', website=True)
    @authenticate
    def change_box_name(self):
        """
        Change IoT box name
        """
        html_boxname = change_box_name_template.render({
            'title': 'Change IoT Box name',
            'breadcrumb': 'Change IoT Box name',
            'boxname': viin_helpers.read_name_box(),
            'server_status': helpers.get_odoo_server_url() or 'Not configured yet',
            'loading_message': 'Change IoT Box name'
        })
        html_boxname = html.fromstring(html_boxname)
        html_boxname = replace_link(html_boxname)
        return html.tostring(html_boxname, pretty_print=True, method='html', encoding='unicode')

    @http.route('/enable_ngrok', type='http', auth='none', cors='*', csrf=False)
    @authenticate
    def enable_ngrok(self, auth_token, url_tcp=None):
        ngrok.set_auth_token(auth_token)
        listeners = ngrok.get_listeners()
        for lis in listeners:
            if lis.proto() == 'tcp':
                res_payload = {
                    'message': "running",
                    'url_tcp': lis.url(),
                }
                return json.dumps(res_payload)
        try:
            listener = ngrok.forward(
                addr="localhost:22",
                authtoken_from_env=True,
                proto="tcp",
                remote_addr=url_tcp,
                log='/tmp/ngrok.log',
            )
            res_payload = {
                'message': "You can perform remote debugging using the following address",
                'url_tcp': listener.url(),
            }
        except ValueError as e:
            _logger.error('A error ngrok connect TCP : %s ' % e)
            error = dict(enumerate(e.args,0))
            res_payload = {
                'message': error.get(0, 'Error installing Ngrok'),
                'error_code': "Error Code: " + str(error.get(2, None)),
                'description': error.get(1, None)
            }
        return json.dumps(res_payload)

    @http.route('/remote_connect', type='http', auth='none', cors='*')
    @authenticate
    def remote_connect(self):
        """
        Overriding this function is because the view has changed compared to the original view
        """
        remote_connect_str = remote_connect_template.render({
            'title': 'Remote debugging',
            'breadcrumb': 'Remote Debugging',
        })
        htm_remote_connect = html.fromstring(remote_connect_str)
        htm_remote_connect = replace_link(htm_remote_connect)
        return html.tostring(htm_remote_connect, pretty_print=True, method='html', encoding='unicode')

    @http.route('/hw_posbox_homepage/password', type='json', auth='none', methods=['POST'])
    @authenticate
    def view_password(self):
        return helpers.generate_password()

    @http.route('/six_payment_terminal', type='http', auth='none', cors='*', csrf=False)
    @authenticate
    def six_payment_terminal(self):
        """
        Replace the Odoo links with Viindoo links.
        """
        html_six_payment_terminal = html.fromstring(super(IoTboxHomepage, self).six_payment_terminal().data.decode())
        html_six_payment_terminal = replace_link(html_six_payment_terminal)
        return html.tostring(html_six_payment_terminal, pretty_print=True, method='html', encoding='unicode')

    @http.route('/six_payment_terminal_add', type='http', auth='none', cors='*', csrf=False)
    @authenticate
    def add_six_payment_terminal(self, terminal_id):
        return super(IoTboxHomepage, self).add_six_payment_terminal(terminal_id)

    @http.route('/six_payment_terminal_clear', type='http', auth='none', cors='*', csrf=False)
    @authenticate
    def clear_six_payment_terminal(self):
        return super(IoTboxHomepage, self).clear_six_payment_terminal()

    def _get_iot_handler_logger(self, handler_name, handler_folder_name):
        # change text odoo to viindoo
        res = super(IoTboxHomepage, self)._get_iot_handler_logger(handler_name, handler_folder_name)
        if res and res.parent and res.parent.name and isinstance(res.parent.name, str) and res.parent.name == "odoo":
            res.parent.name = "viindoo"
        return res
