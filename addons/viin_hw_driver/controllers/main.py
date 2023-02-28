# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

import json
import jinja2
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import urllib3
import urllib.parse

from odoo import http
from odoo.http import Response,request
from odoo.modules.module import get_resource_path

from odoo.addons.hw_drivers.main import iot_devices, manager
from odoo.addons.hw_drivers.tools import helpers
from odoo.addons.web.controllers import main as web


from odoo.addons.hw_posbox_homepage.controllers.main import IoTboxHomepage
from odoo.addons.hw_drivers.iot_handlers.drivers.DisplayDriver import DisplayDriver
_logger = logging.getLogger(__name__)


#----------------------------------------------------------
# Controllers
#----------------------------------------------------------

if hasattr(sys, 'frozen'):
    # When running on compiled windows binary, we don't have access to package loader.
    path = os.path.realpath(os.path.join(os.path.dirname(__file__), '..', 'views'))
    loader = jinja2.FileSystemLoader(path)
else:
    loader = jinja2.PackageLoader('odoo.addons.viin_hw_driver', "views")

jinja_env = jinja2.Environment(loader=loader, autoescape=True)
jinja_env.filters["json"] = json.dumps

homepage_template = jinja_env.get_template('homepage.html')
server_config_template = jinja_env.get_template('server_config.html')
server_login_template = jinja_env.get_template('server_login.html')
change_hostname_template = jinja_env.get_template('change_hostname.html')
timeout = 1000

class DisplayDriver(DisplayDriver):
    def update_url(self, url=None):
        if not helpers.get_odoo_server_url() and not helpers.access_point():
            url_config = 'http://' + helpers.get_ip() + ':8069/server'
        else :
            url_config = url
        super(DisplayDriver, self).update_url(url_config)

class IoTboxHomepage(web.Home):
    @http.route('/', type='http', auth='none')
    def index(self):
        wifi = Path.home() / 'wifi_network.txt'
        remote_server = Path.home() / 'odoo-remote-server.conf'
        if (wifi.exists() == False or remote_server.exists() == False) and helpers.access_point():
            return "<meta http-equiv='refresh' content='0; url=http://" + helpers.get_ip() + ":8069/steps'>"
        else:
            return homepage_template.render(self.get_homepage_data())
        
    # Set server address
    @http.route('/server', type='http', auth='none', website=True)
    def server(self):
        return server_config_template.render({
            'title': 'IoT -> Odoo server configuration',
            'breadcrumb': 'Configure Odoo Server',
            'hostname': subprocess.check_output('hostname').decode('utf-8').strip('\n'),
            'server_status': helpers.get_odoo_server_url() or 'Not configured yet',
            'loading_message': 'Configure Domain Server'
        })
    
    @http.route('/server_connect', type='http', methods=['POST'], auth='none', cors='*', csrf=False)
    def connect_to_server(self, **kw):
        url_server = kw.get('url', '')
        url_iot = url_server
        url_server = 'http://' + url_server + '/iot/check_server'
        urllib3.disable_warnings()
        http = urllib3.PoolManager(cert_reqs='CERT_NONE', timeout=timeout)
        
        try :
            response = http.request('GET', url_server)
            if response.status == 200 :
                url_login = 'http://' + helpers.get_ip() + ':8069/render_login'
                data_return = {
                                'code_id' : 200,
                                'url_login' : url_login,
                                'url_server': url_iot
                               }
                return json.dumps(data_return)
        except :
            url_login = 'http://' + helpers.get_ip() + ':8069/server'
            data_return = {
                            'code_id' : 404,
                            'error_message' : 'Configure Domain Server Wrong'
                            }
            return json.dumps(data_return)
    
    @http.route('/render_login', type='http', auth='none', website=True, methods=['GET'])
    def render_login(self, url):
        return server_login_template.render({
            'title': 'IoT Login Server',
            'breadcrumb': 'Configure Odoo Server',
            'url_server': url,
            'server_status': helpers.get_odoo_server_url() or 'Not configured yet',
            'loading_message': 'Configure Domain Server'
        })
    
    @http.route('/server_login', type='http', methods=['POST'], auth='none', cors='*', csrf=False)
    def server_login(self, **kw):
        domain = helpers.get_ip()   
        user_name =  kw.get('user_name', '')
        password = kw.get('password', '')
        if not user_name or not password:
            data_return = {
                            'code_id' : 404,
                            'error_message' : 'Please fill in all information'
                            }
            return json.dumps(data_return)
        
        data = {'user_name': kw.get('user_name', ''),
                'password' :  kw.get('password', ''),
                'name': socket.gethostname(),
                'identifier': helpers.get_mac_address(),
                'ip': domain,
                'token': helpers.get_token(),
                'version': helpers.get_version(),
                }     
        url_server = 'http://' + kw.get('url_server','') 
        encoder_data = urllib.parse.urlencode(data)
        urllib3.disable_warnings()
        http = urllib3.PoolManager(cert_reqs='CERT_NONE', timeout=timeout)
        try:
            response = http.request(
                    'POST',
                    url_server + "/iot/login",
                    body = encoder_data.encode('utf-8'),
                    headers={
                        'Content-type': 'application/x-www-form-urlencoded',
                        'Accept': 'text/plain',
                    },
                )
            if response.status == 200 :
                result = json.loads(response.data.decode('utf-8').replace('\'', '\"'))
                if result :
                    # luu dia chi server
                    token = result.get('token','')
                    if token:
                        subprocess.check_call([get_resource_path('point_of_sale', 'tools/posbox/configuration/connect_to_server.sh'), url_server, '', token, 'noreboot'])
                        manager.send_alldevices()
                        data_return = {
                                'code_id' : 200,
                                }
                        return json.dumps(data_return)
                    else:
                        data_return = {
                                'code_id' : 400,
                                'error_message' : result.get('error_message','Error')
                                }
                        return json.dumps(data_return)
                else:
                    data_return = {
                            'code_id' : 400,
                            'error_message' : 'Error Data'
                            }
                    return json.dumps(data_return)
        except:
            data_return = {
                            'code_id' : 404,
                            'error_message' : 'Error server Raspberry'
                            }
            return json.dumps(data_return)
    
    @http.route('/change_hostname', type='http', auth='none', cors='*', csrf=False)
    def change_hostname(self, iotname):
        if iotname:
            subprocess.check_call([get_resource_path('point_of_sale', 'tools/posbox/configuration/connect_to_server.sh'), '', iotname, '', 'reboot'])
        return Response(response=str(True))
    
    @http.route('/render_hostname', type='http', auth='none', website=True)
    def render_hostname(self):
        return change_hostname_template.render({
            'title': 'IoT Change Hostname',
            'breadcrumb': 'Configure IoT Box',
            'hostname': subprocess.check_output('hostname').decode('utf-8').strip('\n'),
            'server_status': helpers.get_odoo_server_url() or 'Not configured yet',
            'loading_message': 'Configure Hostname'
        })
