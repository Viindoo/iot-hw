"""The routes by which an installer connects the box, unlocks its settings and gives it a certificate.

They take a JSON body, allow no other origin and refuse a request a browser
marks as asked by a page of another web site, so no other web site can drive
them through the browser of someone on the network of the box. What they receive
- a pairing code, a settings code, a private key - is never logged nor sent back.
"""
import functools
import logging
import traceback

from odoo import http
from odoo.http import request

from odoo.addons.viin_hw_drivers import lock, pairing, trust

_logger = logging.getLogger(__name__)


def _contained(answer):
    """Decorator of a route: an error the route did not foresee is answered `answer`.

    Left to the framework, the error would be logged and sent back with its
    message, which may quote what the caller sent. Only its kind and the line
    that raised it are logged.
    """
    def decorator(endpoint):
        @functools.wraps(endpoint)
        def contained(*args, **kwargs):
            try:
                return endpoint(*args, **kwargs)
            except Exception as error:
                origin = traceback.extract_tb(error.__traceback__)[-1]
                _logger.error(
                    '%s failed on this box: %s at %s:%s',
                    request.httprequest.path, type(error).__name__, origin.filename, origin.lineno)
                return dict(answer)

        return contained

    return decorator


class BoxSecurity(http.Controller):

    @http.route('/viin_hw_homepage/pair', type='json', auth='none', methods=['POST'], save_session=False)
    @_contained({'status': 'failed', 'reason': 'box_error'})
    @lock.protected
    def pair(self, link=None, server_url=None, code=None, trust_fingerprint=None, authority=None):
        """Connect the box to a server; the answer is the one of `pairing.pair`, or the refusal of the lock.

        The lock is asked twice: here, and by the pairing once no other change
        of the server is under way - a request that came while the box had no
        server does not pair after another one gave it one.
        """
        answer = pairing.pair(
            link=link, server_url=server_url, code=code, trust_fingerprint=trust_fingerprint, authority=authority,
            permitted=lock.is_unlocked)
        return lock.refusal() if answer is None else answer

    @http.route('/viin_hw_homepage/unlock', type='json', auth='none', methods=['POST'], save_session=False)
    @_contained({'status': 'refused', 'reason': 'box_error'})
    @lock.refusing_other_sites
    def unlock(self, code=None):
        """Exchange the settings code for the unlock proof, which travels in a header of the answer and nowhere else.

        {'status': 'unlocked', 'valid_for': seconds}, {'status': 'refused',
        'reason': 'invalid_code'}, or {'status': 'not_locked'} on a box that
        has no server, where nothing asks for a proof.
        """
        if not lock.is_locked():
            return {'status': 'not_locked'}
        proof = lock.unlock(code)
        if not proof:
            return {'status': 'refused', 'reason': 'invalid_code'}
        headers = request.future_response.headers
        headers.set(lock.PROOF_HEADER, proof)
        headers.set('Cache-Control', 'no-store')
        return {'status': 'unlocked', 'valid_for': lock.PROOF_LIFETIME}

    @http.route('/viin_hw_homepage/certificate', type='json', auth='none', methods=['POST'], save_session=False)
    @_contained({'status': 'refused', 'reason': 'box_error'})
    @lock.protected
    def certificate(self, certificate=None, key=None, host_name=None, restore=False):
        """Install the certificate of the customer, or with `restore` go back to one the box makes itself.

        The answer is the one of `trust.install_certificate`, or {'status':
        'restored'} with what `trust.box_certificate` says of the new one.
        """
        if restore is True:
            trust.restore_box_certificate()
            return dict(trust.box_certificate(), status='restored')
        return trust.install_certificate(certificate, key, host_name)
