"""The log level the box page sets for the whole box also holds for the requests its web server logs."""
import http.client
import time

import common

BOX_LEVEL = 'iot-logging-root'
SOFTWARE_LEVEL = 'iot-logging-odoo'
PING = '/hw_posbox_homepage/ping'


class TestBoxWideLogLevel(common.BoxCase):

    def set_level(self, name, level):
        answer = self.call('/hw_posbox_homepage/log_levels_update', name=name, value=level)
        self.assertEqual(answer.get('status'), 'success', answer)

    def requests_logged_for_three_pings(self):
        """How many lines the web server wrote to the log of the box for three requests to the box."""
        import odoo

        common.serve()
        before = common.LOG_FILE.stat().st_size
        for _ping in range(3):
            connection = http.client.HTTPConnection('127.0.0.1', odoo.tools.config['http_port'], timeout=10)
            connection.request('GET', PING)
            connection.getresponse().read()
            connection.close()
        time.sleep(1)
        written = common.LOG_FILE.read_bytes()[before:].decode(errors='replace')
        return len([line for line in written.splitlines() if 'werkzeug' in line])

    def setUp(self):
        super().setUp()
        # A box set to log requests, as a box whose installer looked at them.
        self.set_level(SOFTWARE_LEVEL, 'info')
        self.set_level(BOX_LEVEL, 'info')
        self.addCleanup(self.set_level, SOFTWARE_LEVEL, 'warning')
        self.addCleanup(self.set_level, BOX_LEVEL, 'warning')

    def test_box_set_to_info_logs_the_requests_it_serves(self):
        self.assertEqual(self.requests_logged_for_three_pings(), 3)

    def test_box_set_to_warning_logs_no_request_it_serves(self):
        self.set_level(BOX_LEVEL, 'warning')

        self.assertEqual(self.requests_logged_for_three_pings(), 0)
