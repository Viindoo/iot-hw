import logging

from importlib import util
from pathlib import Path
import ngrok

from odoo import _, http
from odoo.tools.func import lazy_property
from odoo.modules.module import get_resource_path
from odoo.addons.hw_drivers.tools import helpers

_logger = logging.getLogger(__name__)

def load_iot_handlers():

    for directory in ['interfaces']:
        path = get_resource_path('viin_hw_drivers', 'iot_handlers', directory)
        filesList = helpers.list_file_by_os(path)
        for file in filesList:
            spec = util.spec_from_file_location(file, str(Path(path).joinpath(file)))
            if spec:
                module = util.module_from_spec(spec)
                try:
                    spec.loader.exec_module(module)
                except Exception as e:
                    _logger.error('Unable to load file: %s ', file)
                    _logger.error('An error encountered : %s ', e)
    lazy_property.reset_all(http.root)

def ngrok_connect(authtoken=None, domain=None):
    try:
        ngrok.set_auth_token(str(authtoken))
        ngrok.kill()
        listener = ngrok.forward(
            addr="localhost:8069",
            authtoken_from_env=True,
            domain=str(domain) or None,
            schemes=["HTTPS"],
        )
    except ValueError as e:
        _logger.error('A error ngrok connect HTTP: %s ' % e)
        return {'error': e.args} 
    return {'url': listener.url()}
