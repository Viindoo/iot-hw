import json
import logging
import os
import socket
import subprocess
import sys
import re

import urllib3
import jinja2
import ngrok
from pathlib import Path
from lxml import html

from odoo import http
from odoo.http import request
from odoo.modules.module import get_resource_path
from odoo.addons.hw_drivers.main import manager
from odoo.addons.hw_drivers.tools import helpers
from odoo.addons.viin_hw_drivers.tools import viin_helpers
from odoo.addons.hw_posbox_homepage.controllers.main import IoTboxHomepage

_logger = logging.getLogger(__name__)

if hasattr(sys, 'frozen'):
    # When running on compiled windows binary, we don't have access to package loader.
    path = os.path.realpath(os.path.join(os.path.dirname(__file__), '..', 'views'))
    loader = jinja2.FileSystemLoader(path)
else:
    loader = jinja2.PackageLoader('odoo.addons.viin_hw_posbox_homepage', "views")

jinja_env = jinja2.Environment(loader=loader, autoescape=True)
jinja_env.filters["json"] = json.dumps

homepage_template = jinja_env.get_template('homepage.html')
server_config_template = jinja_env.get_template('server_config.html')
change_hostname_template = jinja_env.get_template('change_hostname.html')
handler_list_template = jinja_env.get_template('handler_list.html')
wifi_config_template = jinja_env.get_template('wifi_config.html')
remote_method_template = jinja_env.get_template('remote_method.html')
remote_connect_template = jinja_env.get_template('remote_connect.html')
timeout = 1000


class ViinIoTboxHomepage(IoTboxHomepage):

    def get_homepage_data(self):
        res = super(ViinIoTboxHomepage, self).get_homepage_data()
        iot_server = helpers.read_file_first_line('odoo-subject.conf')
        res.update({
            'iot_server': iot_server or None
        })
        return res

    @http.route('/', type='http', auth='none')
    def index(self):
        """Overide to change endpoint from  `http://ip_addr:8069/steps` to `http://ip_addr:8069/wifi`
        Because the "/steps" page is used for users to log in to the server with an Odoo Token and set up WiFi.
        Viindoo has designed a separate page for connecting to Viindoo server.
        Therefore, we only need to navigate to the WiFi setup page.
        """
        wifi = Path.home() / 'wifi_network.txt'
        remote_server = Path.home() / 'odoo-remote-server.conf'
        if (wifi.exists() == False or remote_server.exists() == False) and helpers.access_point():
            return "<meta http-equiv='refresh' content='0; url=http://" + helpers.get_ip() + ":8069/wifi'>"
        else:
            return homepage_template.render(self.get_homepage_data())

    @http.route('/server', type='http', auth='none', website=True)
    def server(self):
        """
        Navigate to Viindoo server login page.
        """
        return server_config_template.render({
            'title': 'IoT -> Viindoo server configuration',
            'breadcrumb': 'Configure Viindoo Server',
            'hostname': subprocess.check_output('hostname').decode('utf-8').strip('\n'),
            'server_status': helpers.get_odoo_server_url() or 'Not configured yet',
            'loading_message': 'Configure Domain Server'
            })

    @http.route('/list_handlers', type='http', auth='none', website=True)
    def list_handlers(self):
        """
        Add a list of drivers and interfaces that Viindoo supports.
        """
        drivers_list = helpers.list_file_by_os(get_resource_path('hw_drivers', 'iot_handlers', 'drivers'))
        drivers_list.extend(helpers.list_file_by_os(get_resource_path('viin_hw_drivers', 'iot_handlers', 'drivers')))
        interfaces_list = helpers.list_file_by_os(get_resource_path('hw_drivers', 'iot_handlers', 'interfaces'))
        interfaces_list.extend(helpers.list_file_by_os(get_resource_path('viin_hw_drivers', 'iot_handlers', 'interfaces')))
        return handler_list_template.render({
            'title': "Viindoo's IoT Box - Handlers list",
            'breadcrumb': 'Handlers list',
            'drivers_list': drivers_list,
            'interfaces_list': interfaces_list,
            'server': helpers.get_odoo_server_url()
            })

    @http.route('/load_iot_handlers', type='http', auth='none', website=True)
    def load_iot_handlers(self):
        """
        Reload the list of drivers and interfaces, including those supported by Viindoo.
        """
        viin_helpers.load_iot_handlers()
        helpers.load_iot_handlers()
        helpers.odoo_restart(0)
        return "<meta http-equiv='refresh' content='20; url=http://" + helpers.get_ip() + ":8069/list_handlers'>"

    @http.route('/wifi', type='http', auth='none', website=True)
    def wifi(self):
        """
        Replace the Odoo links with Viindoo links.
        """
        return wifi_config_template.render({
            'title': 'Wifi configuration',
            'breadcrumb': 'Configure Wifi',
            'loading_message': 'Connecting to Wifi',
            'ssid': helpers.get_wifi_essid(),
            })

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

    @http.route('/change_hostname', type='http', auth='none', cors='*', csrf=False)
    def change_hostname(self, iotname):
        """
        Requires hostname name to be standard
        """
        if len(iotname) > 63:
            data_return = {'error_message': 'Hostname is too long'}
            return json.dumps(data_return)
        elif iotname.startswith('-') or iotname.endswith('-'):
            data_return = {'error_message': 'Do not use the "-" character at the beginning and end the hostname'}
            return json.dumps(data_return)
        elif re.match('^[a-zA-Z0-9-]+$', iotname): 
            subprocess.check_call([get_resource_path('point_of_sale', 'tools/posbox/configuration/connect_to_server_wifi.sh'), '', iotname, '', '', '', ' '])
        else:
            data_return = {'error_message': 'The hostname is not correct'}
            return json.dumps(data_return)

    @http.route('/config_hostname', type='http', auth='none', website=True)
    def config_hostname(self):
        """
        Replace the Odoo links with Viindoo links.
        """
        return change_hostname_template.render({
            'title': 'IoT Change Hostname',
            'breadcrumb': 'Configure IoT Box',
            'hostname': subprocess.check_output('hostname').decode('utf-8').strip('\n'),
            'server_status': helpers.get_odoo_server_url() or 'Not configured yet',
            'loading_message': 'Configure Hostname'
            })

    @http.route('/remote_method', type='http', auth='none', website=True)
    def remote_method(self):
        return remote_method_template.render({
            'title': 'Configuration remote method',
            'breadcrumb': 'Configuration Remote Method',
            'loading_message': 'Configuring your remote method',
            'method': ["Your Domain", "DNS Viindoo"],
        })

    @http.route('/remote_method_config', type='http', auth='none', cors='*', csrf=False)
    def remote_method_config(self):
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
                key = private_key.read().decode()
                cer = certificate.read().decode()
                with helpers.writable():
                    Path('/etc/ssl/certs/nginx-cert.crt').write_text(cer)
                    Path('/root_bypass_ramdisks/etc/ssl/certs/nginx-cert.crt').write_text(cer)
                    Path('/etc/ssl/private/nginx-cert.key').write_text(key)
                    Path('/root_bypass_ramdisks/etc/ssl/private/nginx-cert.key').write_text(key)
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
                    - Hàm helpers.load_certificate() để lấy chứng chỉ
                    - Hiện tại hàm này vẫn chưa ghi đè vì vậy nó vẫn đang
                        dẫn tới trang lấy chứng chỉ của odoo
                    - Sẽ ghi đè hàm này khi DNS Viindoo có api để cấp chứng chỉ cho hộp IoT
            """
            helpers.load_certificate()
            manager.send_alldevices()
            res_payload = {
                'message': 'Connecting to ' + 'http://' + helpers.get_ip() + ':8069',
                'server': 'http://' + helpers.get_ip() + ':8069',
            }
        return json.dumps(res_payload)

    @http.route('/remote_method_default', type='http', auth='none', cors='*', csrf=False)
    def remote_method_default(self):
        helpers.unlink_file('odoo-subject.conf')
        """TODO:
                - Hàm helpers.load_certificate() để lấy chứng chỉ
                - Hiện tại hàm này vẫn chưa ghi đè vì vậy nó vẫn đang
                    dẫn tới trang lấy chứng chỉ của odoo
                - Sẽ ghi đè hàm này khi DNS Viindoo có api để cấp chứng chỉ cho hộp IoT
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
        Changing the view overwrites odoo's
        """
        return remote_connect_template.render({
            'title': 'Remote debugging',
            'breadcrumb': 'Remote Debugging',
        })
