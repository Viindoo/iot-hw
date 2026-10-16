"""Loads each controller file of the module on its own.

A file that cannot be loaded costs its own routes and is reported in the state
of the layer; the others are served.
"""
import importlib
import logging
import pkgutil

from odoo.addons.viin_hw_drivers import seams

_logger = logging.getLogger(__name__)

# Odoo gives the last word on a route to the controller defined last: no other file may open what this one closes.
LAST = 'hw_posbox_homepage_lock'


def _load():
    names = sorted(found.name for found in pkgutil.iter_modules(__path__) if found.name != LAST)
    for name in [*names, LAST]:
        try:
            importlib.import_module(f'{__name__}.{name}')
        except Exception as error:
            _logger.critical(
                'Viindoo layer integrity: the controller file %s cannot be loaded; its routes are off on this box.',
                name, exc_info=True)
            seams.report_problem(f'viin_hw_homepage/controllers/{name}.py', f'{type(error).__name__}: {error}')


_load()
