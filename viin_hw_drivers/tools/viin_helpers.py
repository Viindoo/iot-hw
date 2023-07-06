import logging

from importlib import util
from pathlib import Path

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
