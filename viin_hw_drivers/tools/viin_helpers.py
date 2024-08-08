import subprocess
import logging
import json
import time

from importlib import util
from pathlib import Path
import urllib3

from odoo import _, http
from odoo.tools.func import lazy_property
from odoo.tools.misc import file_path
from odoo.addons.hw_drivers.tools import helpers
from odoo.addons.viin_hw_homepage.Session import Session

_logger = logging.getLogger(__name__)

def load_iot_handlers(modules: list = ['hw_drivers', 'viin_hw_drivers']):
    for load_module in modules:
        for directory in ['interfaces', 'drivers']:
            path = file_path(f"{load_module}/iot_handlers/{directory}")
            filesList = helpers.list_file_by_os(path)
            for file in filesList:
                spec = util.spec_from_file_location(compute_iot_handlers_addon_name(load_module, directory, file), str(Path(path).joinpath(file)))
                if spec:
                    module = util.module_from_spec(spec)
                    try:
                        spec.loader.exec_module(module)
                    except Exception as e:
                        _logger.error('Unable to load file: %s ', file)
                        _logger.error('An error encountered : %s ', e)
    lazy_property.reset_all(http.root)

def compute_iot_handlers_addon_name(module, handler_kind, handler_file_name):
    return "odoo.addons.{module}.iot_handlers.{handler_kind}.{handler_name}".\
        format(module=module, handler_kind=handler_kind, handler_name=handler_file_name.removesuffix('.py'))

def sync_box_to_viindoo_saas(url_server, username, password):
    data = {
        'params': {
            'fqdn': url_server,
            'username': username,
            'password': password,
            'token': helpers.get_token(),
        }
    }
    url = 'https://viindoo.com/saas/iot/add-box'
    urllib3.disable_warnings()
    http = urllib3.PoolManager(cert_reqs='CERT_NONE', retries=urllib3.Retry(4))
    try:
        http.request(
            'POST',
            url,
            body=json.dumps(data).encode('utf8'),
            headers={'Content-type': 'application/json', 'Accept': 'text/plain'}
        )
    except Exception as e:
        _logger.error('Could not reach configured server')
        _logger.error('An error encountered : %s ' % e)

def get_wifi_essid():
    wifi_options = []
    result = subprocess.check_output(['sudo', 'nmcli', '-f', 'SSID', 'dev', 'wifi', 'list'])
    for ssid in result.decode().split('\n'):
        if ssid and 'SSID' not in ssid:
            wifi_options.append(ssid.strip())
    return wifi_options

def load_certificate(ip=None):
    """
    Send a request to Viindoo with iot box identifier and iot ip to get a true certificate
    """
    url = 'https://viindoo.com/saas/iot/ssl_cert'
    data = {
        'params': {
            'token': helpers.get_token(),
            'ip': ip or helpers.get_ip(),
        }
    }
    urllib3.disable_warnings()
    http = urllib3.PoolManager(cert_reqs='CERT_NONE', retries=urllib3.Retry(4))
    try:
        response = http.request(
            'POST',
            url,
            body=json.dumps(data).encode('utf8'),
            headers={'Content-type': 'application/json', 'Accept': 'text/plain'}
        )
    except Exception as e:
        _logger.exception("An error occurred while trying to reach odoo.com servers.")
        return "ERR_IOT_HTTPS_LOAD_REQUEST_EXCEPTION\n\n%s" % e

    if response.status != 200:
        return "ERR_IOT_HTTPS_LOAD_REQUEST_STATUS %s\n\n%s" % (response.status, response.reason)

    result = json.loads(response.data.decode('utf8'))['result']
    if not result:
        return "ERR_IOT_HTTPS_LOAD_REQUEST_NO_RESULT"

    subject_cn = result['subject_cn']
    helpers.write_file('odoo-subject.conf', subject_cn)
    with helpers.writable():
        Path('/etc/ssl/certs/nginx-cert.crt').write_text(result['cert'])
        Path('/root_bypass_ramdisks/etc/ssl/certs/nginx-cert.crt').write_text(result['cert'])
        Path('/etc/ssl/private/nginx-cert.key').write_text(result['cert_key'])
        Path('/root_bypass_ramdisks/etc/ssl/private/nginx-cert.key').write_text(result['cert_key'])
    time.sleep(3)
    helpers.start_nginx_server()
    return True

def download_iot_handlers(auto=True):
    """
    TODO:
        Rewrite this function when you want to update the driver
    """
    return None

def get_db(username, password, server=None):
    if not server:
        server = helpers.get_odoo_server_url()
    if server:
        data_login = {
            "jsonrpc": "2.0",
            "params": {
                "login": username,
                "password": password,
            }
        }
        urllib3.disable_warnings()
        http = urllib3.PoolManager(cert_reqs='CERT_NONE', timeout=10)
        try:
            response = http.request(
                'POST',
                server + "/iot/login",
                body=json.dumps(data_login).encode('utf8'),
                headers={
                    'Content-type': 'application/json',
                    'Accept': 'text/plain',
                },
            )
            data = json.loads(response.data.decode('utf-8'))
            result = data.get('result', '')
            if result:
                return result[0]
        except Exception as e:
            _logger.error('An error encountered : %s ', e)
    return
            
def login(username, password, server=None):
    if not server:
        server = helpers.get_odoo_server_url()
    if server:
        data_login = {
            "jsonrpc": "2.0",
            "params": {
                "login": username,
                "password": password,
            }
        }
        urllib3.disable_warnings()
        http = urllib3.PoolManager(cert_reqs='CERT_NONE', timeout=10)
        try:
            db = get_db(username, password, server)
            if db:
                data_login['params']['db'] = db
                response = http.request(
                    'POST',
                    server + "/web/session/authenticate",
                    body=json.dumps(data_login).encode('utf8'),
                    headers={
                        'Content-type': 'application/json',
                        'Accept': 'text/plain',
                    },
                )
                data = json.loads(response.data.decode('utf-8'))
                result = data.get('result', None)
                cookie = response.headers.get('Set-Cookie', None)
                if result and result.get('is_iot_manager', None) and cookie:
                    Session(cookie)
                    return cookie

        except Exception as e:
            _logger.error('An error encountered : %s ', e)

def mDNS_listener() -> list:
    """
    Returns:
        List[Dict{str: str}]: A list of dictionaries, each containing information about an mDNS service:
            - 'interface': Network interface (str)
            - 'service_name': Service name (using MAC address) (str)
            - 'domain': Domain name (str)
            - 'ip': IP address (str)
            - 'port': Port (str)
    """
    def prepare_browse_mdns_data(data: list) -> list:
        result_mdns = list()
        try:
            for r in data:
                line = dict()
                parts = r.rsplit(";")
                line["interface"] = parts[1]
                line["service_name"] = parts[3]
                line["domain"] = parts[6]
                line["ip"] = parts[7]
                line["port"] = parts[8]
                txt = parts[9].strip("\"").split(', ')
                for pair in txt:
                    k, v = pair.split('=')
                    line[k] = v
                result_mdns.append(line)
        except IndexError as e:
            _logger.error(f'Error preparing mDNS data: {e}')
        finally:
            return result_mdns

    mdns_services = list()
    command = "avahi-browse -trkp _viindoo-iot._tcp | grep -v '^+'"
    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        _logger.error(f"Error running command: {stderr.decode()}")
        return mdns_services
    else:
        output = stdout.decode().strip()
        lines = output.split('\n')
        
        eth0_results = [line for line in lines if ";eth0;" in line and ";IPv4;" in line]
        wlan0_results = [line for line in lines if ";wlan0;" in line and ";IPv4;" in line]

        if eth0_results:
            mdns_services = prepare_browse_mdns_data(eth0_results)
        elif wlan0_results:
            mdns_services = prepare_browse_mdns_data(wlan0_results)
        return mdns_services

def read_name_box():
    name = helpers.read_file_first_line('name_of_iot_box')
    if not name:
        mac = helpers.get_mac_address().split(':')
        name = "Viindoo-IoT-{}".format(mac[-2] + mac[-1])
        helpers.write_file('name_of_iot_box', name)
    return name
