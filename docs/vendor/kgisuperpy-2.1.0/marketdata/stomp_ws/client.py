import time
import threading
import websocket
import logging
from ..md_logger import LoggingType, LoggingBase
from .frame import Frame

VERSIONS = '1.0,1.1'

#avoid websocket debug logs
websocket.enableTrace(False)
logging.getLogger("websocket").setLevel(logging.CRITICAL)
logging.getLogger("websocket").propagate = False


class WSClient(LoggingBase):

    def __init__(self, url:str, api_id: str = "", show_api_id_in_log: bool = False, file_only: bool = False):
        super().__init__(api_id=api_id, show_api_id_in_log=show_api_id_in_log)
        self.url = url
        self.token = None

        self._ws_app = None
        self._ws_thread = None
        self._ws_lock = threading.Lock()
        
        self.opened = False
        self.connected = False
        self.counter = 0
        self.subscriptions = {}

        self._first_connect = True

        self._external_close_callback = None
        self._external_connect_callback = None
        self._external_error_callback = None
        self._external_STOMP_error_callback = None

        self._heartbeat_thread = None
        self._heartbeat_stop_event = threading.Event()
        self._heartbeat_lock= threading.Lock()

    def set_close_callback(self, callback):
        self._external_close_callback = callback
    
    def set_connect_callback(self, callback):
        self._external_connect_callback = callback

    def set_error_callback(self, callback):
        self._external_error_callback = callback

    def set_STOMP_error_callback(self, callback):
        self._external_STOMP_error_callback = callback

    def _create_ws_app(self):
        self._ws_app = websocket.WebSocketApp(
            self.url,
            on_open=self._on_open,
            on_close=self._on_close,
            on_error=self._on_error,
            on_message=self._on_message
        )

    def _run_ws(self):
        try:
            self._ws_app.run_forever()
        except Exception as e:
            self._log_message(LoggingType.ERROR, f"WebSocket run_forever exception: {e}")
        finally:
            self.opened = False
            self.connected = False
    
    def _start_heartbeat(self, interval=30):
        # if self._heartbeat_thread and self._heartbeat_thread.is_alive():
        #     return  # already running
        with self._heartbeat_lock:
            if self._heartbeat_thread is not None:
                return
            
            self._heartbeat_stop_event.clear()

            def run():
                try:
                    while not self._heartbeat_stop_event.is_set() and \
                        self.opened and \
                        self._ws_app and \
                        self._ws_app.sock and \
                        self._ws_app.sock.connected:
                        try:
                            self._ws_app.send("\n")  # STOMP heartbeat
                            self._log_message(LoggingType.DEBUG, ">>> HEARTBEAT", file_only=True)
                        except Exception as e:
                            self._log_message(LoggingType.WARNING, f"Failed to send heartbeat: {e}")
                            break
                        time.sleep(interval)
                finally:
                    with self._heartbeat_lock:
                        self._heartbeat_thread = None

            t = threading.Thread(target=run, daemon=True)
            self._heartbeat_thread = t
            t.start()

    def _stop_heartbeat(self):
        # if self._heartbeat_thread and self._heartbeat_thread.is_alive():
        with self._heartbeat_lock:
            if self._heartbeat_thread:
                self._heartbeat_stop_event.set()
                self._heartbeat_thread.join(timeout=2)
                self._heartbeat_thread = None

            self._heartbeat_stop_event.clear()
        
    def stomp_connect(self):
        headers = {} 
        headers['host'] = self.url
        headers['accept-version'] = VERSIONS
        headers['heart-beat'] = '10000,10000'
        headers['Bearer'] = self.token

        self._transmit('CONNECT', headers)
        
    def _connect(self):
        if self._ws_thread and self._ws_thread.is_alive():
            self._log_message(LoggingType.DEBUG, "WebSocket thread already running and connected. Skipping _connect.", file_only=True)
        else:
            self._create_ws_app()
            self._ws_thread = threading.Thread(target=self._run_ws, daemon=True)
            self._ws_thread.start()

    def connect(self):
        self._log_message(LoggingType.DEBUG, "Opening WebSocket connection...", file_only=True)
        self._connect()

    def _on_open(self, ws, *args):
        self._log_message(LoggingType.DEBUG, "WebSocket opened.", file_only=True)
        # logging.info("WebSocket opened: " + self.url)
        self.opened = True
        self._stop_heartbeat()
        self.stomp_connect()
        self._start_heartbeat(interval=14)

    def _on_close(self, ws, *args):
        self._log_message(LoggingType.INFO, "WebSocket closed.", file_only=True)
        self.subscriptions.clear()
        self._stop_heartbeat()
        self.connected = False
        self.opened = False

        close_status_code = None
        close_msg = None
        if len(args) >= 1:
            close_status_code = args[0]
        if len(args) >= 2:
            close_msg = args[1]

        # 也可從 ws 屬性再嘗試補一下
        if close_status_code is None:
            close_status_code = getattr(ws, "close_status_code", None)
        if close_msg is None:
            close_msg = getattr(ws, "close_reason", None)

        self._log_message(LoggingType.INFO, f"WebSocket closed with code: {close_status_code}, reason: {close_msg}", file_only=True)
        if self._external_close_callback:
            threading.Timer(0.5 , self._external_close_callback).start()  # Delay to allow any pending messages to be processed
            
    def _on_error(self, ws, error, *args):
        self._log_message(LoggingType.ERROR, f"WebSocket error: {error}", file_only=True)
        self.connected = False
        if self._external_error_callback:
            self._external_error_callback(ws, error)

    def _on_message(self, ws, message, *args):
        self._log_message(LoggingType.DEBUG, f"\n<<< {message}", file_only=True)
        frame = Frame.unmarshall_single(message)
        _results = []

        if frame.command == "CONNECTED":
            self.connected = True
            if self._external_connect_callback:
                _results.append(self._external_connect_callback(ws, frame))
        elif frame.command == "MESSAGE":
            subscription = frame.headers.get('subscription')
            if subscription in self.subscriptions:
                onreceive = self.subscriptions[subscription]
                messageID = frame.headers['message-id']
                frame.ack = lambda headers=None: self.ack(messageID, subscription, headers or {})
                frame.nack = lambda headers=None: self.nack(messageID, subscription, headers or {})
                _results.append(onreceive(frame))
        elif frame.command == "ERROR":
            if self._external_STOMP_error_callback:
                _results.append(self._external_STOMP_error_callback(ws, frame))
        elif not frame.command:
            m = (message or "").strip()
            suffix = "" if m == "" else ": " + message
            self._log_message(LoggingType.DEBUG, f"HEARTBEAT received{suffix}", file_only=True)
        else:
            self._log_message(LoggingType.DEBUG, "Unhandled command: " + str(frame.command), file_only=True)

        return _results

    def _transmit(self, command, headers, body=None):
        if self.opened and self._ws_app and self._ws_app.sock and self._ws_app.sock.connected:
            out = Frame.marshall(command, headers, body)
            self._log_message(LoggingType.DEBUG, f"\n>>> {out}", file_only=True)
            self._ws_app.send(out)

    def stompconnect(self, token):
        self.token = token
        self.connect()

    def disconnect(self):
        self._stop_heartbeat()
        headers = {}
        self._transmit("DISCONNECT", headers)

        if self._ws_app:
            self._ws_app.on_close = None
            try:
                self._ws_app.close()
            except Exception as e:
                self._log_message(LoggingType.WARNING, f"Error closing WebSocket: {e}")
            self._ws_app = None

        try:
            if self._ws_app:
                self._ws_app.close() 
                if self._ws_app.sock:
                    self._ws_app.sock.close() 
        except Exception as e:
            self._log_message(LoggingType.WARNING, f"Error closing WebSocket socket: {e}")
        #self.subscriptions.clear()
        self._ws_thread = None
        self.opened = False
        self.connected = False

    def reconnect(self, token=None):
        self.disconnect()
        #self.subscriptions.clear()
        if token is not None:
            self.token = token
        self.connect()

    def send(self, destination, headers=None, body=None):
        headers = headers or {}
        headers['destination'] = destination
        return self._transmit("SEND", headers, body or '')

    def subscribe(self, destination, callback=None, headers=None):
        headers = headers or {}
        if 'id' not in headers:
            headers["id"] = f"sub-{self.counter}"
            self.counter += 1
        headers['destination'] = destination
        self.subscriptions[headers["id"]] = callback
        self._transmit("SUBSCRIBE", headers)

        def unsubscribe():
            self.unsubscribe(headers["id"])

        return headers["id"], unsubscribe

    def unsubscribe(self, id):
        if id in self.subscriptions:
            del self.subscriptions[id]
        return self._transmit("UNSUBSCRIBE", {"id": id})

    def ack(self, message_id, subscription, headers):
        headers = headers or {}
        headers["message-id"] = message_id
        headers['subscription'] = subscription
        return self._transmit("ACK", headers)

    def nack(self, message_id, subscription, headers):
        headers = headers or {}
        headers["message-id"] = message_id
        headers['subscription'] = subscription
        return self._transmit("NACK", headers)
