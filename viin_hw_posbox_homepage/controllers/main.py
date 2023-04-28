from pathlib import Path
import json
import jinja2
import logging
import os
import socket
import subprocess
import sys
import urllib3
import re

from odoo import http
from odoo.modules.module import get_resource_path
from odoo.addons.hw_drivers.main import manager
from odoo.addons.hw_drivers.tools import helpers
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
timeout = 1000


class ViinIoTboxHomepage(IoTboxHomepage):

    @http.route('/', type='http', auth='none')
    def index(self):
        wifi = Path.home() / 'wifi_network.txt'
        remote_server = Path.home() / 'odoo-remote-server.conf'
        if (wifi.exists() == False or remote_server.exists() == False) and helpers.access_point():
            return "<meta http-equiv='refresh' content='0; url=http://" + helpers.get_ip() + ":8069/wifi'>"
        else:
            return homepage_template.render(self.get_homepage_data())

    @http.route('/server', type='http', auth='none', website=True)
    def server(self):
        return server_config_template.render({
                                            'title': 'IoT -> Viindoo server configuration',
                                            'breadcrumb': 'Configure Viindoo Server',
                                            'hostname': subprocess.check_output('hostname').decode('utf-8').strip('\n'),
                                            'server_status': helpers.get_odoo_server_url() or 'Not configured yet',
                                            'loading_message': 'Configure Domain Server'
        })

    @http.route('/list_handlers', type='http', auth='none', website=True)
    def list_handlers(self):
        drivers_list = helpers.list_file_by_os(get_resource_path('hw_drivers', 'iot_handlers', 'drivers'))
        interfaces_list = helpers.list_file_by_os(get_resource_path('hw_drivers', 'iot_handlers', 'interfaces'))
        return handler_list_template.render({
            'title': "Viindoo's IoT Box - Handlers list",
            'breadcrumb': 'Handlers list',
            'drivers_list': drivers_list,
            'interfaces_list': interfaces_list,
            'server': helpers.get_odoo_server_url()
        })

    @http.route('/load_iot_handlers', type='http', auth='none', website=True)
    def load_iot_handlers(self):
        helpers.load_iot_handlers()
        helpers.odoo_restart(0)
        return "<meta http-equiv='refresh' content='20; url=http://" + helpers.get_ip() + ":8069/list_handlers'>"
    
    @http.route('/wifi', type='http', auth='none', website=True)
    def wifi(self):
        return wifi_config_template.render({
            'title': 'Wifi configuration',
            'breadcrumb': 'Configure Wifi',
            'loading_message': 'Connecting to Wifi',
            'ssid': helpers.get_wifi_essid(),
        })

    @http.route('/server_connect', type='http', methods=['POST'], auth='none', cors='*', csrf=False)
    def connect_to_server(self, **kw):
        url_server = kw.get('url', '')
        username = kw.get('user_name', '')
        password = kw.get('password', '')
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
                                'error_message': "odoo exceptions AccessError"
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
        return change_hostname_template.render({
                                            'title': 'IoT Change Hostname',
                                            'breadcrumb': 'Configure IoT Box',
                                            'hostname': subprocess.check_output('hostname').decode('utf-8').strip('\n'),
                                            'server_status': helpers.get_odoo_server_url() or 'Not configured yet',
                                            'loading_message': 'Configure Hostname'
        })
