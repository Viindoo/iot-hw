"""The routes of the box services: its status, its diagnostic file, its clock, its devices and its software.

They are the page of the box's own: none allows another origin, and each
refuses a request a browser marks as asked by a page of another web site.
What changes a setting, or what the box runs, asks for the unlock proof on a
paired box. An error a route did not foresee is answered without quoting what
the caller sent.
"""
import functools
import logging
import traceback

from odoo import http
from odoo.http import content_disposition, request

from odoo.addons.viin_hw_drivers import device_tests, lock, page_use, selftest, update

_logger = logging.getLogger(__name__)

BOX_ERROR = {'status': 'failed', 'reason': 'box_error'}
# What `/viin_hw/update` does: `check` changes nothing the box runs; the others do, or change a setting.
UPDATE_ACTIONS = ('check', 'apply', 'auto_update')
UPDATE_LOCKED = ('apply', 'auto_update')
CLOCK_ACTIONS = ('read', 'apply')
NO_STORE = [('Cache-Control', 'no-store')]


def _contained(endpoint):
    """Decorator of a route: an error the route did not foresee is answered BOX_ERROR.

    Left to the framework, the error would be logged and sent back with its
    message, which may quote what the caller sent. Only its kind and the line
    that raised it are logged.
    """
    @functools.wraps(endpoint)
    def contained(*args, **kwargs):
        try:
            return endpoint(*args, **kwargs)
        except Exception as error:
            origin = traceback.extract_tb(error.__traceback__)[-1]
            _logger.error(
                '%s failed on this box: %s at %s:%s',
                request.httprequest.path, type(error).__name__, origin.filename, origin.lineno)
            if request.dispatcher.routing_type == 'json':
                return dict(BOX_ERROR)
            return request.make_json_response(BOX_ERROR, headers=NO_STORE, status=500)

    return contained


class BoxServices(http.Controller):

    @http.route('/viin_hw/status', type='http', auth='none', methods=['GET'], save_session=False)
    @_contained
    @lock.refusing_other_sites
    def status(self, refresh=None):
        """What the page of the box shows, with the self-test: `selftest.status`. `refresh=1` looks at the server
        anew even when it was looked at a moment ago. The page reads it regularly while it is shown: each read is
        a use of the page, noted by `page_use`."""
        page_use.note(request.httprequest)
        return request.make_json_response(selftest.status(refresh=refresh == '1'), headers=NO_STORE)

    @http.route('/viin_hw/diagnostic', type='http', auth='none', methods=['GET'], save_session=False)
    @_contained
    @lock.refusing_other_sites
    def diagnostic(self):
        """The diagnostic file, as a download: `selftest.diagnostic`."""
        text, name = selftest.diagnostic()
        return request.make_response(text, headers=[
            ('Content-Type', 'application/json; charset=utf-8'),
            ('Content-Disposition', content_disposition(name)),
            *NO_STORE,
        ])

    @http.route('/viin_hw/clock', type='json', auth='none', methods=['POST'], save_session=False)
    @_contained
    @lock.refusing_other_sites
    def clock(self, action='read', server_url=None):
        """Read the time of the server beside the one of the box (`read`), or set the box to it (`apply`).

        The answers of `selftest.read_server_clock` and `selftest.set_clock_from_server`;
        {'status': 'refused', 'reason': 'invalid_action'} for any other action.
        """
        if action not in CLOCK_ACTIONS:
            return {'status': 'refused', 'reason': 'invalid_action'}
        if action == 'read':
            return selftest.read_server_clock(server_url)
        return selftest.set_clock_from_server(server_url)

    @http.route('/viin_hw/device/test', type='json', auth='none', methods=['POST'], save_session=False)
    @_contained
    @lock.refusing_other_sites
    def device_test(self, device_identifier=None):
        """Test one device with no server: `device_tests.test`. It prints or reads, and changes no setting."""
        return device_tests.test(device_identifier)

    @http.route('/viin_hw/device/use_as', type='json', auth='none', methods=['POST'], save_session=False)
    @_contained
    @lock.protected
    def device_use_as(self, device_identifier=None, is_scanner=None):
        """Use a keyboard device as a scanner, or a scanner as a keyboard: `device_tests.use_as`."""
        return device_tests.use_as(device_identifier, is_scanner)

    @http.route('/viin_hw/device/label_printer', type='json', auth='none', methods=['POST'], save_session=False)
    @_contained
    @lock.protected
    def device_label_printer(self, device_identifier=None):
        """Have an office printer taken for the label printer it is: `device_tests.make_label_printer`."""
        return device_tests.make_label_printer(device_identifier)

    @http.route('/viin_hw/update', type='json', auth='none', methods=['POST'], save_session=False)
    @_contained
    @lock.refusing_other_sites
    def software_update(self, action=None, enabled=None):
        """Look for a release now (`check`), apply the one found (`apply`), or turn automatic updates on or off.

        {'status': 'started' or 'saved', 'update': `update.status()`}; {'status':
        'refused', 'reason': 'invalid_action' or 'invalid_choice'}; on a paired
        box `apply` and `auto_update` need the unlock proof.
        """
        if action not in UPDATE_ACTIONS:
            return {'status': 'refused', 'reason': 'invalid_action'}
        if action in UPDATE_LOCKED and not lock.is_unlocked():
            return lock.refusal()
        if action == 'check':
            return {'status': 'started', 'update': update.check_now()}
        if action == 'apply':
            return {'status': 'started', 'update': update.apply_now()}
        if not isinstance(enabled, bool):
            return {'status': 'refused', 'reason': 'invalid_choice'}
        update.set_auto_update(enabled)
        _logger.info('Automatic updates of the box software are now %s', 'on' if enabled else 'off')
        return {'status': 'saved', 'update': update.status()}
