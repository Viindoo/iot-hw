{
    'name': 'Viindoo IoT Box Homepage',
    'category': 'IoT',
    'sequence': 6,
    'website': 'https://viindoo.com',
    'summary': 'A homepage for the IoT Box',
    'description': """
Viindoo IoT Box Homepage
========================

This module overrides Odoo web interface to display a simple
Homepage that explains what's the iotbox and shows the status,
and where to find documentation.

If you activate this module, you won't be able to access the 
regular Odoo interface anymore.

""",
    'assets': {
        'web.assets_backend': [
            'viin_hw_homepage/static/img/*',
        ],
    },
    'installable': False,
    'license': 'OPL-1',
}
