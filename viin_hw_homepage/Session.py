import logging
from datetime import datetime, timedelta

from functools import wraps
from odoo.http import request
from odoo.addons.hw_drivers.tools import helpers

_logger = logging.getLogger(__name__)

sessions = {}

class Session():

    def __init__(self, cookie):
        self.cookie = cookie
        self.session = None
        self.expires = datetime.now() - timedelta(days=1)
        self._update(cookie)
        
    def _check_session(self, session):
        if self.session == session:
            if datetime.now() <= self.expires:
                return True
            else:
                sessions.pop(session)
        return

    def _update(self, cookie):
        for ck in cookie.split(";"):
            if "session_id" in ck:
                self.session = ck.split("=")[1].strip()
            if "Max-Age" in ck:
                max_age = int(ck.split("=")[1].strip())
                self.expires = datetime.now() + timedelta(seconds=max_age)

        if self.session:
            sessions[self.session] = self

def authenticate(func):

    fname = f"<function {func.__module__}.{func.__name__}>"
    @wraps(func)
    def wrapper(self, *args, **params):
        sid = request.session.sid
        server = helpers.get_odoo_server_url()
        if server and (sid not in sessions or not sessions[sid]._check_session(sid)):
            return "<meta http-equiv='refresh' content='0; url=http://" + helpers.get_ip() + ":8069/web/login'>"
        try:
            return func(self, *args, **params)
        except Exception as e:
            _logger.error('Could not load function: %s ', fname)
            _logger.exception(e)

    return wrapper
