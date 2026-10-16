{
    'name': 'Viindoo IoT Hardware Drivers',
    'category': 'IoT',
    'sequence': 6,
    'author': 'Viindoo',
    'website': 'https://viindoo.com',
    'summary': 'Starts the hardware services of a Viindoo IoT Box and connects the box to its server',
    'description': """
Viindoo IoT Hardware Drivers
============================

This module runs on a Viindoo IoT Box, never in a database. The box loads it
before its hardware modules, so that they start under the control of Viindoo:

* the box software contacts the server of the box and the place where Viindoo
  publishes the box software, and no online service of another vendor;
* each box has a web certificate and a key of its own;
* a box is connected to its server with a one-time pairing code, over a
  connection it verifies whenever the address of the server is https;
* once a box is connected, changing its settings asks for the settings code
  that the administrators read on the box record in Viindoo;
* connected or not, a box refuses a change of its settings that a browser marks
  as asked by a page of another web site.

Editions Supported
==================

1. Community Edition
2. Viindoo Cloud

""",
    'installable': False,
    'license': 'OPL-1',
}
