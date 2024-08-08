import time
import logging
import json
import websocket
from threading import Thread, Event

from odoo.addons.hw_drivers import websocket_client
from odoo.addons.hw_drivers.main import iot_devices
from odoo.addons.hw_drivers.tools import helpers

_logger = logging.getLogger(__name__)
global websocket_client

on_close = websocket_client.on_close
on_open = websocket_client.WebsocketClient.on_open

def update_presence(self, ws):
    """
    Function to send a periodic message to the server every 5 seconds.
    """
    while True:
        if self.stop_update_presence.is_set():
            return
        try:
            token = helpers.read_file_first_line('token')
            if token:
                devices_list = list()
                for device in iot_devices:
                    identifier = iot_devices[device].device_identifier
                    devices_list.append(identifier)
                data = {'viin.iot.device': devices_list, 'token': token,}
                ws.send(
                    json.dumps({
                        "event_name": "update_presence",
                        "data": {
                            "inactivity_period": "",
                            "im_status_ids_by_model": data
                            }
                        })
                    )
        except Exception as e:
            _logger.exception("Failed to send heartbeat message: %s", e)
            break
        time.sleep(5)

def FnOrg_on_open(self, ws):
    on_open(self, ws)
    ws.send(
        json.dumps({'event_name': 'subscribe', 'data': {'channels': [self.iot_channel], 'last': 0}})
    )

    # Start the periodic message sender in a separate thread
    self.stop_update_presence.clear()
    self.periodic_thread = Thread(target=self.update_presence, args=(self.ws,))
    self.periodic_thread.start()

def FnOrrg_on_close(self, ws, close_status_code, close_msg):
    on_close(ws, close_status_code, close_msg)
    self.stop_update_presence.set()
    self.periodic_thread.join()

def FnOrg_run(self):
    self.stop_update_presence = Event()

    self.ws = websocket.WebSocketApp(self.url,
        on_open=self.on_open, on_message=websocket_client.on_message,
        on_error=websocket_client.on_error, on_close=self.on_close)

    while True:
        try:
            run_res = self.ws.run_forever(reconnect=10)
            _logger.debug("websocket run_forever return with %s", run_res)
        except Exception:
            _logger.exception("An unexpected exception happened when running the websocket")
        _logger.debug('websocket will try to restart in 10 seconds')
        self.stop_update_presence.set()
        self.periodic_thread.join()
        time.sleep(10)

websocket_client.WebsocketClient.run = FnOrg_run
websocket_client.WebsocketClient.on_open = FnOrg_on_open
websocket_client.WebsocketClient.on_close = FnOrrg_on_close
websocket_client.WebsocketClient.update_presence = update_presence

