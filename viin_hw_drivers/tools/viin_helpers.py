import subprocess
import logging
import json
import time

from importlib import util
from pathlib import Path
import urllib3

from odoo import _, http
from odoo.tools.func import lazy_property
from odoo.modules.module import get_resource_path
from odoo.addons.hw_drivers.tools import helpers

_logger = logging.getLogger(__name__)

def load_iot_handlers(modules: list = ['hw_drivers', 'viin_hw_drivers']):
    for load_module in modules:
        for directory in ['interfaces', 'drivers']:
            path = get_resource_path(load_module, 'iot_handlers', directory)
            filesList = helpers.list_file_by_os(path)
            for file in filesList:
                spec = util.spec_from_file_location(helpers.compute_iot_handlers_addon_name(directory, file), str(Path(path).joinpath(file)))
                if spec:
                    module = util.module_from_spec(spec)
                    try:
                        spec.loader.exec_module(module)
                    except Exception as e:
                        _logger.error('Unable to load file: %s ', file)
                        _logger.error('An error encountered : %s ', e)
    lazy_property.reset_all(http.root)

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

    subject_cn = "*." + result['subject_cn']
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
