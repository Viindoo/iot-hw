"""What an installer reaches at the address of the box: the Viindoo page, its log page and their data.

The routes stay core's addresses, so that whatever already calls them gets the
Viindoo answer. The page data is core's, without what belongs to the services
of the vendor of core, and with the status of the Viindoo layer. The version
and update routes answer for the Viindoo release of the box instead of core's
own update. The box-wide log level "debug" is not offered: with it, core's log
sender keeps the box service from stopping, and the box hangs at its next
restart.
"""
import functools
import json
import logging
import traceback
from pathlib import Path

import jinja2

from odoo import http
from odoo.http import request

from odoo.addons.hw_posbox_homepage.controllers import homepage
from odoo.addons.viin_hw_drivers import lock, page_use, seams, selftest, update

_logger = logging.getLogger(__name__)

MODULE = Path(__file__).resolve().parents[1]
STATIC_URL = f'/{MODULE.name}/static/'
# The page application and its stylesheet; the page shows the self-test on its own while the application is absent.
APP = 'src/app/main.js'
STYLESHEET = 'src/app/css/viindoo.css'
# The icon of the tab: browsers ask for it at /favicon.ico, and the pages of the box declare it by its static address.
ICON = 'img/icon.png'
ICON_TYPE = 'image/png'
ICON_MAX_AGE = 86400
# Keys of core's page data that belong to the services of the vendor of core: its pairing code, its customer
# credential, a payment terminal it supports.
VENDOR_KEYS = ('pairing_code', 'db_uuid', 'enterprise_code', 'six_terminal')
# The name under which core's log-level route sets the level of the whole box.
BOX_LOGGER = f'{homepage.IOT_LOGGING_PREFIX}root'
BOX_LEVELS_NOT_OFFERED = ('debug',)
LEVEL_NOT_OFFERED = {'status': 'refused', 'reason': 'level_not_offered'}
BOX_ERROR = {'status': 'failed', 'reason': 'box_error'}
NO_STORE = [('Cache-Control', 'no-store')]

_templates = jinja2.Environment(loader=jinja2.FileSystemLoader(str(MODULE / 'views')), autoescape=True)


def _contained(endpoint):
    """Decorator of a route: an error the route did not foresee is answered BOX_ERROR, quoting nothing it was sent."""
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


def _static(path):
    """The address of a static file of the module, '' when the box does not have the file."""
    return STATIC_URL + path if (MODULE / 'static' / path).is_file() else ''


def _page(template, **values):
    html = _templates.get_template(template).render(**values)
    return request.make_response(html, headers=[('Content-Type', 'text/html; charset=utf-8'), *NO_STORE])


def offered_log_levels():
    """The levels the log-level dialog may offer, per kind of logger that core's log-level route sets.

    `root` is the whole box, `odoo` the box software, `interface` and `driver`
    each handler file of that kind; `parent` means "same as the box".
    """
    return {
        'root': [level for level in homepage.AVAILABLE_LOG_LEVELS if level not in BOX_LEVELS_NOT_OFFERED],
        'odoo': list(homepage.AVAILABLE_LOG_LEVELS),
        'interface': list(homepage.AVAILABLE_LOG_LEVELS_WITH_PARENT),
        'driver': list(homepage.AVAILABLE_LOG_LEVELS_WITH_PARENT),
    }


def _layer_status():
    """`selftest.status()` with the offered log levels; when it cannot be had, `error` says so and core's data stays."""
    try:
        status = selftest.status()
    except Exception as error:
        origin = traceback.extract_tb(error.__traceback__)[-1]
        _logger.error(
            'The status of the Viindoo layer could not be read for the page data: %s at %s:%s',
            type(error).__name__, origin.filename, origin.lineno)
        status = {'error': BOX_ERROR['reason']}
    return dict(status, offered_log_levels=offered_log_levels())


def version_info(software):
    """What the version route answers for `software`, an `update.status()`.

    Core's three keys keep their meaning for the release of the box: the box
    software is up to date when no release is offered, the image when no
    release asks for a newer one.
    """
    last = software['last_check'] or {}
    offered = software['offered'] or {}
    return {
        'status': 'success',
        'odooIsUpToDate': not offered,
        'imageIsUpToDate': last.get('state') != update.NEEDS_NEW_IMAGE,
        'currentCommitHash': software['running']['odoo_commit'],
        'release': software['release'],
        'offered': offered.get('release', ''),
        'update_state': selftest.update_state(software),
        'update': software,
    }


class BoxPage(homepage.IotBoxOwlHomePage):

    @http.route()
    def index(self):
        return _page(
            'index.html', app=_static(APP), stylesheet=_static(STYLESHEET),
            box_name=seams.core('tools.helpers').get_hostname())

    # Core's log page declares no public access, which a box without a database never serves.
    @http.route(auth='none')
    def logs_page(self):
        return _page('logs.html', box_name=seams.core('tools.helpers').get_hostname())

    @http.route()
    def get_iot_logs(self):
        """The log page reads it regularly while shown: each read is a use of the box page, noted by `page_use`."""
        page_use.note(request.httprequest)
        return super().get_iot_logs()

    @http.route()
    def get_homepage_data(self):
        data = json.loads(super().get_homepage_data().get_data(as_text=True))
        for key in VENDOR_KEYS:
            data.pop(key, None)
        data['viindoo'] = _layer_status()
        return request.make_json_response(data, headers=NO_STORE)

    @http.route()
    @_contained
    @lock.refusing_other_sites
    def get_version_info(self, check='1'):
        """The release the box runs and whether one is offered; unless `check` is '0', a look for one starts now.

        The look ends in the background: `update.busy` says it is under way.
        """
        software = update.status() if check == '0' else update.check_now()
        return request.make_json_response(version_info(software), headers=NO_STORE)

    @http.route()
    @_contained
    @lock.protected
    def update_git_tree(self):
        """Look for a release and apply the one found: the box restarts into it. It changes what the box runs."""
        return {'status': 'started', 'update': update.apply_now()}

    @http.route()
    def update_log_level(self, name, value):
        if name == BOX_LOGGER and value in BOX_LEVELS_NOT_OFFERED:
            _logger.warning('A box-wide log level that the box does not offer was refused')
            return dict(LEVEL_NOT_OFFERED)
        answer = super().update_log_level(name, value)
        if name == BOX_LOGGER:
            # The web server logs every request under `werkzeug`, which the dialog offers no level for: it follows
            # the box-wide level as it follows the box software's.
            self._update_logger_level('werkzeug', value, homepage.AVAILABLE_LOG_LEVELS)
        return answer


class BoxIcon(http.Controller):

    @http.route('/favicon.ico', type='http', auth='none', methods=['GET'], save_session=False)
    def favicon(self):
        """The icon of the tab, for the browsers that ask for it at the root of the site, whatever the page."""
        path = MODULE / 'static' / ICON
        try:
            content = path.read_bytes()
        except OSError:
            return request.not_found()
        return request.make_response(
            content, headers=[('Content-Type', ICON_TYPE), ('Cache-Control', f'public, max-age={ICON_MAX_AGE}')])
