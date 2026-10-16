{
    'name': 'Viindoo IoT Box Homepage',
    'category': 'IoT',
    'sequence': 6,
    'author': 'Viindoo',
    'website': 'https://viindoo.com',
    'summary': 'What an installer reaches on a Viindoo IoT Box to connect it to its server and to set it up',
    'description': """
Viindoo IoT Box Homepage
========================

This module runs on a Viindoo IoT Box, never in a database. It decides what
the box answers to whoever reaches it on its network:

* an installer connects the box to its server with a one-time pairing code, and
  can give the box the certificate of the customer;
* once the box is connected, changing its server, its network, its
  log levels or its certificate, what a keyboard device is used as and its
  layout, restarting the box and opening shell access all ask for the settings
  code that the administrators read on the box record in Viindoo;
* whoever reaches the box on its network sends its screen, without that code,
  only to a page of the server of the box or of the box itself, and on a box
  that has no server only to a page of the box; where the server of the box
  sends the screen is the word of that server;
* the box serves no online service of another vendor, no database management,
  and no route that replaces its software - its image, its code, its driver
  files - from elsewhere than Viindoo;
* of the routes of the box software, only those of the devices answer the pages
  of another web site, because the pages of the server call them from the
  browser of a user; the page of the box, its state and its log allow no other
  web site;
* a change of a setting that a browser marks as asked by a page of another web
  site is refused, whether the box has a server or not.

Editions Supported
==================

1. Community Edition
2. Viindoo Cloud

""",
    # The box serves the static files of a module that cannot be installed only when its manifest declares assets.
    'assets': {
        'web.assets_backend': [
            'viin_hw_homepage/static/**/*',
        ],
    },
    'installable': False,
    'license': 'OPL-1',
}
