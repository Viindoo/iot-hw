import json
import logging
import socket
import subprocess
import re

import urllib3
import jinja2
import ngrok
from pathlib import Path
from lxml import html

from odoo import http
from odoo.http import request
from odoo.modules.module import get_resource_path, loaded
from odoo.addons.hw_drivers.main import manager
from odoo.addons.hw_drivers.tools import helpers
from odoo.addons.viin_hw_drivers.tools import viin_helpers
from odoo.addons.hw_posbox_homepage.controllers.main import IoTboxHomepage
from odoo.addons.hw_drivers.server_logger import get_odoo_config_log_to_server_option

_logger = logging.getLogger(__name__)

loader = jinja2.FileSystemLoader(["/home/pi/odoo/addons/viin_hw_homepage/views", "/home/pi/odoo/addons/hw_posbox_homepage/views"])

jinja_env = jinja2.Environment(loader=loader, autoescape=True)
jinja_env.filters["json"] = json.dumps

server_config_template = jinja_env.get_template('viin_server_config.html')
change_hostname_template = jinja_env.get_template('change_hostname.html')
handler_list_template = jinja_env.get_template('handler_list.html')
public_method_template = jinja_env.get_template('public_method.html')
remote_connect_template = jinja_env.get_template('viin_remote_connect.html')
timeout = 1000

class IoTboxHomepage(IoTboxHomepage):

    # Replace odoo's link with viindoo's link
    def _replace_link(self, page):
        for link in page.xpath('//div[@class="footer"]/a'):
            if "help" in link.text_content().lower():
                link.set('href', "https://viindoo.com/vi/ticket")
            elif "documentation" in link.text_content().lower():
                link.set('href', "https://viindoo.com/vi/documentation")
        return page

    @http.route('/', type='http', auth='none')
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
            homepage_odoo = self._replace_link(homepage_odoo)

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

                # Change the old link to the hostname change link
                if "Name" in pair.text_content():
                    btn = pair.xpath("./following-sibling::td/a[contains(@href, 'server')]")[0]
                    btn.set('href', '/hostname')

                # Replace odoo's link with viindoo's link
                if "HTTPS certificate" in pair.text_content():
                    href = pair.xpath("./following-sibling::td/a[contains(@href, 'www.odoo.com')]")[0]
                    href.set('href', 'https://viindoo.com/vi/ticket')

            for btn in homepage_odoo.xpath("//button[contains(@class, 'btn')] | //a[@class='btn']"):
                # # Remove the path to pos_display
                # # This button will be added in the viin_hw_pos_driver module
                # if btn.get('href') == '/point_of_sale/display':
                #     btn.getparent().remove(btn)

                # Since Viindoo does not use this authentication method, remove this button
                if btn.get('href') == '/list_credential':
                    btn.getparent().remove(btn)

                if btn.text_content() == "Restart Odoo service":
                    btn.text = "Restart IoT service"

                # add button "Public Methods"
                if btn.get('href') == '/remote_connect':
                    public_method_str = """
                        <a class="btn" style="margin-left: 10px;" href="/public_method">Public Methods</a>
                    """
                    public_method = html.fromstring(public_method_str)
                    btn.addprevious(public_method)

            homepage_viidoo = html.tostring(homepage_odoo, pretty_print=True, method='html', encoding='unicode')
            return homepage_viidoo

    @http.route('/server', type='http', auth='none', website=True)
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
        html_server = self._replace_link(html_server)
        return html.tostring(html_server, pretty_print=True, method='html', encoding='unicode')

    @http.route('/list_handlers', type='http', auth='none', website=True, csrf=False, save_session=False)
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
            'server': helpers.get_odoo_server_url(),
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
        handler_list = self._replace_link(handler_list)
        return html.tostring(handler_list, pretty_print=True, method='html', encoding='unicode')

    @http.route('/load_iot_handlers', type='http', auth='none', website=True)
    def load_iot_handlers(self):
        """
        Reload the list of drivers and interfaces, including those supported by Viindoo.
        """
        global loaded
        for module in loaded:
            viin_helpers.load_iot_handlers(module)
        helpers.odoo_restart(0)
        return "<meta http-equiv='refresh' content='20; url=http://" + helpers.get_ip() + ":8069/list_handlers'>"

    @http.route('/wifi', type='http', auth='none', website=True)
    def wifi(self):
        """
        Replace the Odoo links with Viindoo links.
        """
        html_wifi = html.fromstring(super(IoTboxHomepage, self).wifi().data.decode())
        html_wifi = self._replace_link(html_wifi)
        return html.tostring(html_wifi, pretty_print=True, method='html', encoding='unicode')

    @http.route('/wifi_connect', type='http', auth='none', cors='*', csrf=False)
    def connect_to_wifi(self, essid, password, persistent=False):
        """
        Replace the Odoo links with Viindoo links.
        """
        res = super(IoTboxHomepage, self).connect_to_wifi(essid, password, persistent=False)
        res_dict = json.loads(res.data.decode())
        server = res_dict.get('server', None)
        if server and server.get('message', None).lower() == "redirect to odoo server":
            res_dict['server']['message'] = "Redirect to Viindoo Server"
        return json.dumps(res_dict)

    @http.route('/server_connect', type='http', methods=['POST'], auth='none', cors='*', csrf=False)
    def connect_to_server(self):
        data = request.params
        url_server = data.get('url', '')
        username = data.get('user_name', '')
        password = data.get('password', '')
        data_login = {
                    "jsonrpc": "2.0",
                    "params": {
                            "login": username,
                            "password": password,
                    }
        }
        urllib3.disable_warnings()
        http = urllib3.PoolManager(cert_reqs='CERT_NONE', timeout=timeout)
        try:
            response = http.request(
                'POST',
                url_server + "/iot/login",
                body=json.dumps(data_login).encode('utf8'),
                headers={
                    'Content-type': 'application/json',
                    'Accept': 'text/plain',
                },
            )
            data = json.loads(response.data.decode('utf-8'))
            result = data.get('result', '')
            if result:
                data_login['params']['db'] = result[0]
                response = http.request(
                    'POST',
                    url_server + "/web/session/authenticate",
                    body=json.dumps(data_login).encode('utf8'),
                    headers={
                        'Content-type': 'application/json',
                        'Accept': 'text/plain',
                    },
                )
                data = json.loads(response.data.decode('utf-8'))
                result = data.get('result', '')
                error = data.get('error', '')
                if result:
                    Cookie = response.headers.get('Set-Cookie', None)
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
                                helpers.save_conf_server(url_server, token, False, False)
                                manager.send_alldevices()
                                data_return = {'code_id': response.status }
                            else:
                                data_return = {
                                            'code_id': response.status,
                                            'error_message': json.loads(result).get('message', 'Incorrect information')
                                }
                        else:
                            error = json.loads(response.data.decode('utf-8')).get('error', {})
                            data = error.get('data', {})
                            data_return = {
                                        'code_id': error.get('code', 200),
                                        'error_message': data.get('name', 'Incorrect information')
                            }
                    else:
                        data_return = {
                                    'code_id': error.get('code', '400'),
                                    'error_message': error.get('message', 'Incorrect information')
                        }
                else:
                    data_return = {
                                'code_id': 200,
                                'error_message': "Access Error"
                    }
            else:
                data_return = {
                            'code_id': 404,
                            'error_message': "Incorrect information"
                }
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
                        'error_message': e
            }
        finally:
            return json.dumps(data_return)

    @http.route('/hostname_config', type='http', auth='none', cors='*', csrf=False)
    def hostname_config(self, iotname):
        """
        Requires hostname name to be standard
        """
        old_hostname = subprocess.check_output('hostname').decode('utf-8').strip('\n')
        if len(iotname) > 63:
            data_return = {'error_message': 'Hostname is too long'}
            return json.dumps(data_return)
        elif iotname.startswith('-') or iotname.endswith('-'):
            data_return = {'error_message': 'Do not use the "-" character at the beginning and end the hostname'}
            return json.dumps(data_return)
        elif iotname == old_hostname:
            data_return = {'error_message': 'The new hostname is the same as the old hostname'}
            return json.dumps(data_return)
        elif re.match('^[a-zA-Z0-9-]+$', iotname): 
            subprocess.check_call([get_resource_path('point_of_sale', 'tools/posbox/configuration/rename_iot.sh'), iotname])
        else:
            data_return = {'error_message': 'The hostname is not correct'}
            return json.dumps(data_return)

    @http.route('/hostname', type='http', auth='none', website=True)
    def config_hostname(self):
        """
        Replace the Odoo links with Viindoo links.
        """
        html_hostname = change_hostname_template.render({
            'title': 'IoT Change Hostname',
            'breadcrumb': 'Configure IoT Box',
            'hostname': subprocess.check_output('hostname').decode('utf-8').strip('\n'),
            'server_status': helpers.get_odoo_server_url() or 'Not configured yet',
            'loading_message': 'Configure Hostname'
        })
        html_hostname = html.fromstring(html_hostname)
        html_hostname = self._replace_link(html_hostname)
        return html.tostring(html_hostname, pretty_print=True, method='html', encoding='unicode')

    @http.route('/public_method', type='http', auth='none', website=True)
    def public_method(self):
        return public_method_template.render({
            'title': 'Configuration public method',
            'breadcrumb': 'Configuration Public Method',
            'loading_message': 'Configuring your Public method',
            'method': ["Your Domain", "DNS Viindoo"],
        })

    @http.route('/public_method_config', type='http', auth='none', cors='*', csrf=False)
    def public_method_config(self):
        data = request.params
        method = data.get('method', None)
        domain = data.get('domain', None)
        private_key = data.get('private_key', None)
        certificate = data.get('certificate', None)
        res_payload = dict()
        
        if method == "Your Domain" and domain and private_key and certificate:
            if viin_helpers.is_valid_ssl(certificate) and viin_helpers.is_valid_ssl(private_key):
                helpers.write_file('odoo-subject.conf', domain)
                private_key.stream.seek(0)
                certificate.stream.seek(0)
                pri = private_key.read().decode()
                cer = certificate.read().decode()
                with helpers.writable():
                    Path('/etc/ssl/certs/nginx-cert.crt').write_text(cer)
                    Path('/root_bypass_ramdisks/etc/ssl/certs/nginx-cert.crt').write_text(cer)
                    Path('/etc/ssl/private/nginx-cert.key').write_text(pri)
                    Path('/root_bypass_ramdisks/etc/ssl/private/nginx-cert.key').write_text(pri)
                manager.send_alldevices()
                res_payload = {
                    'message': 'Connecting to ' + domain,
                    'server' : domain,
                }
            else:
                res_payload = {
                    'error' : "Unable to read certificate file",
                }
        elif method == "DNS Viindoo":
            helpers.unlink_file('odoo-subject.conf')
            """TODO:
                    - ham helpers.load_certificate() de  lay chung chi
                    - Hien tai van chua ghi de ham nay (van load den trang cua odoo)
            """
            helpers.load_certificate()
            manager.send_alldevices()
            res_payload = {
                'message': 'Connecting to ' + 'http://' + helpers.get_ip() + ':8069',
                'server': 'http://' + helpers.get_ip() + ':8069',
            }
        return json.dumps(res_payload)

    @http.route('/public_method_default', type='http', auth='none', cors='*', csrf=False)
    def public_method_default(self):
        helpers.unlink_file('odoo-subject.conf')
        """TODO:
                - ham helpers.load_certificate() de  lay chung chi
                - Hien tai van chua ghi de ham nay (van load den trang cua odoo)
        """
        helpers.load_certificate()
        manager.send_alldevices()
        return "<meta http-equiv='refresh' content='0; url=http://" + helpers.get_ip() + ":8069'>"

    @http.route('/enable_ngrok', type='http', auth='none', cors='*', csrf=False)
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
    def remote_connect(self):
        """
        Ghi de lai boi vi thay doi view
        """
        remote_connect_str = remote_connect_template.render({
            'title': 'Remote debugging',
            'breadcrumb': 'Remote Debugging',
        })
        htm_remote_connect = html.fromstring(remote_connect_str)
        htm_remote_connect = self._replace_link(htm_remote_connect)
        return html.tostring(htm_remote_connect, pretty_print=True, method='html', encoding='unicode')

    def _get_iot_handler_logger(self, handler_name, handler_folder_name):
        # change text odoo to viindoo
        res = super(IoTboxHomepage, self)._get_iot_handler_logger(handler_name, handler_folder_name)
        if res and res.parent and res.parent.name and isinstance(res.parent.name, str) and res.parent.name == "odoo":
            res.parent.name = "viindoo"
        return res
