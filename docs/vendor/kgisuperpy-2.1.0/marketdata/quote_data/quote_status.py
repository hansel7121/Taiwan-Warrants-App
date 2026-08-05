from enum import Enum

class QuotationError(Exception):
    """Exception raised for errors in the quotation api."""

    def __init__(self, message, code):
        # print(code)
        self.message = message
        self.code = code
        formated_message = f"StatusCode: {code.value}, Detail: {message}"
        super().__init__(formated_message)

class QuotationCode(Enum):
    QUOTE_SERVER_UNKNOWN_ERROR = "S999"  # An unknown server error occurred. (will disconnect)
    QUOTE_SERVER_TOO_MANY_CONNECTIONS = "S001"  # Server rejected connection due to too many concurrent connections. (will disconnect)
    QUOTE_SERVER_INVALID_TOKEN = "S002"  # Authentication token is invalid or expired. (will disconnect)
    QUOTE_SERVER_TOO_MANY_SUBSCRIPTIONS = "S003"  # Too many active subscriptions on this connection. (will disconnect)
    QUOTE_SERVER_NO_PERMISSION_SUBSCRIPTION = "S004"  # No permission to subscribe to the specified topic. (subscription failed)
    QUOTE_SERVER_INVALID_SYMBOL_SUBSCRIPTION = "S005"   # Invalid symbol provided in subscription. (subscription accepted but no data)
    QUOTE_SERVER_SUBSCRIPTION_ID_DUPLICATED = "S006"  # Subscription ID is duplicated. (subscription failed)
    
    SUCCESSFUL_OPERATION = "Q000"
    QUOTE_UNKNOWN_ERROR = "Q999"  # An unknown error occurred.
    QUOTE_SUBSCRIPTION_PARAM_QUOTE_TYPE_NONE = "Q001"  # The quote_type parameter is missing in subscription.
    QUOTE_SUBSCRIPTION_PARAM_SYMBOL_EMPTY = "Q002"  # The symbol parameter is empty in subscription.
    QUOTE_SUBSCRIPTION_PARAM_SESSION_NOT_INT = "Q003"  # The session parameter must be an integer in subscription.
    QUOTE_SUBSCRIPTION_PARAM_VERSION_NONE = "Q004"  # The version parameter is missing in subscription.
    QUOTE_SUBSCRIPTION_TYPE_UNKNOWN = "Q005"  # The subscription type is invalid.
    QUOTE_WS_PERMISSION_FAILED_FETCH = "Q006"  # Failed to fetch permission in WebSocket.
    QUOTE_WS_PERMISSION_FAILED_HTTP = "Q007"  # WebSocket permission request failed due to an HTTP error or timeout.
    QUOTE_WS_PERMISSION_FAILED_JSON_PARSE = "Q008"  # WebSocket permission response could not be parsed as JSON.
    QUOTE_WS_PERMISSION_NOT_ALLOWED_RULE = "Q009"  # The permission is not allowed by rule in WebSocket.
    QUOTE_WS_SUBSCRIPTION_REJECTED = "Q010"  # The subscription was rejected in WebSocket.
    QUOTE_WS_SUBSCRIPTION_NO_PERMISSION = "Q011"  # The subscription was rejected due to missing permission in WebSocket.
    QUOTE_WS_SUBSCRIPTION_LIMIT_EXCEEDED = "Q012"  # The subscription exceeded the topic limit in WebSocket.
    QUOTE_WS_SUBSCRIPTION_DUPLICATE_ID = "Q013"  # The subscription ID is duplicated in WebSocket.
    QUOTE_WS_SUBSCRIPTION_INVALID_QUOTE_TYPE = "Q014"  # The quote type is invalid for WebSocket subscription.
    QUOTE_WS_SUBSCRIPTION_INVALID_SUB_ID = "Q015"  # The sub_id is invalid for WebSocket subscription.
    QUOTE_WS_SUBSCRIPTION_INVALID_PARAMETER = "Q016"  # The parameter is invalid for WebSocket subscription.
    QUOTE_WS_TIMEOUT = "Q017"  # The WebSocket connection timed out or failed.
    QUOTE_WS_DATA_JSON_DECODE_ERROR = "Q018"  # Failed to decode JSON data in WebSocket.

    # QUOTE_QUEUE_OVERFLOW_DROPPED = "QUEUE_OVERFLOW_DROPPED"  # Queue overflow; items dropped (when SHOW_DROPPED_WARNING=True)
    # QUOTE_WS_RECONNECT_START = "WS_RECONNECT_START"  # Starting auto-reconnect
    # QUOTE_WS_RECONNECT_IN_PROGRESS = "WS_RECONNECT_IN_PROGRESS"  # Reconnect already in progress
    # QUOTE_WS_RECONNECT_MAX_RETRY_INVALID = "WS_RECONNECT_MAX_RETRY_INVALID"  # Max reconnect retries must be a positive integer
    # QUOTE_WS_RECONNECT_DELAY_INVALID = "WS_RECONNECT_DELAY_INVALID"  # Reconnect delay must be a positive integer

    QUOTE_WS_RECONNECT_ATTEMPT_FAILED = "Q019"  # A WebSocket reconnection attempt failed.
    QUOTE_WS_RECONNECT_RESUBSCRIBE_EXCEPTION = "Q020"  # An exception occurred during WebSocket resubscription.
    QUOTE_WS_RECONNECT_MAX_REACHED = "Q021"  # The maximum number of WebSocket reconnection attempts was reached.
    QUOTE_CALLBACK_EXCEPTION = "Q022"  # An exception occurred during callback execution.
    QUOTE_CALLBACK_NAME_INVALID = "Q023"  # The callback function is invalid.
    QUOTE_CALLBACK_PARAM_COUNT_INVALID = "Q024"  # The callback has too many parameters.
    QUOTE_CALLBACK_VERSION_MISMATCH = "Q025"  # The callback parameter type does not match the expected version.
    QUOTE_CALLBACK_SUBSCRIPTION_TYPE_UNSUPPORTED = "Q026"  # The subscription type is not supported in callback.
    QUOTE_CALLBACK_VERSION_UNSUPPORTED = "Q027"  # The callback version is not supported.
    QUOTE_DATA_INVALID_MARKET_TYPE = "Q028"  # The market type is invalid in data access.
    QUOTE_DATA_FACADE_NOT_FOUND = "Q029"  # The specified Facade class was not found.
    QUOTE_DATA_FACADE_CALLBACK_ERROR = "Q030"  # An exception occurred while executing the Facade callback.
    QUOTE_DATA_FACADE_NAME_NOT_FOUND = "Q031"  # The specified facade_name was not found in the module.
    QUOTE_DATA_GETTER_MISSING = "Q032"  # A getter is missing for a property in data.
    QUOTE_DATA_ACCESS_DENIED = "Q033"  # Access to the attribute is not allowed.
    QUOTE_STATICDATA_HTTP_STATUS_NOT_200 = "Q034"  # The HTTP response status is not 200 in static data.
    QUOTE_STATICDATA_FETCH_PARSE_EXCEPTION = "Q035"  # An exception occurred during static data fetch or parsing.
    QUOTE_STATICDATA_EMPTY_DATA = "Q036"  # The static data was retrieved but is empty.
    QUOTE_STATICDATA_PERIODIC_FETCH_EXCEPTION = "Q037"  # An exception occurred during periodic static data fetch.
    QUOTE_STATICDATA_PRODUCT_FILE_PARSE_ERROR = "Q038"  # Failed to parse the product file in static data.
    QUOTE_STATICDATA_UNSUBSCRIBE_ID_FORMAT_INVALID = "Q039"  # The sub_id format is invalid in static data unsubscribe.
    QUOTE_STATICDATA_KBAR_MINUTE_UNSUPPORTED = "Q040"  # The specified minute value is not supported in KBar.
    QUOTE_STATICDATA_KBAR_DUPLICATE_SUBSCRIPTION = "Q041"  # The KBar subscription already exists for this symbol and minute.
    QUOTE_STATICDATA_KBAR_KEY_NOT_FOUND = "Q042"  # The sub_id to unsubscribe was not found in KBar.
    QUOTE_STATICDATA_KBAR_INVALID_SUB_ID = "Q043"  # The subscription ID is invalid in KBar data.
    
    QUOTE_WS_ERROR = "Q044"
    QUOTE_WS_DISCONNECTED = "Q045"

class QuoteEventCode(str, Enum):
    # Market Data Connection Events
    CONNECTING = "EVENT_CONNECTING"
    CONNECTED = "EVENT_CONNECTED"
    MANUAL_DISCONNECT = "EVENT_MANUAL_DISCONNECT"
    DISCONNECTED = "EVENT_DISCONNECTED"
    RECONNECTING = "EVENT_RECONNECTING"
    RECONNECTED = "EVENT_RECONNECTED"
    RECONNECT_FAILED = "EVENT_RECONNECT_FAILED"
    RECONNECT_MAX_REACHED = "EVENT_RECONNECT_MAX_REACHED"
    HEARTBEAT_TIMEOUT = "EVENT_HEARTBEAT_TIMEOUT"
    WS_TIMEOUT = "EVENT_WS_TIMEOUT"
    WS_ERROR = "EVENT_WS_ERROR"
    
    # Subscription Events
    SUBSCRIBE_REQUEST = "EVENT_SUBSCRIBE_REQUEST"
    SUBSCRIBE_OK_KBAR = "EVENT_SUBSCRIBE_OK_KBAR"
    SUBSCRIBE_OK = "EVENT_SUBSCRIBE_OK"
    SUBSCRIBE_FAIL = "EVENT_SUBSCRIBE_FAIL"
    UNSUBSCRIBE_REQUEST = "EVENT_UNSUBSCRIBE_REQUEST"
    UNSUBSCRIBE_OK_KBAR = "EVENT_UNSUBSCRIBE_OK_KBAR"
    UNSUBSCRIBE_OK = "EVENT_UNSUBSCRIBE_OK"
    UNSUBSCRIBE_FAIL = "EVENT_UNSUBSCRIBE_FAIL"
    UNSUBSCRIBE_ALL = "EVENT_UNSUBSCRIBE_ALL"
    MO_SUBSCRIPTIONS = "EVENT_MO_SUBSCRIPTIONS"
    # Data Events
    DATA_ERROR = "EVENT_DATA_ERROR"
    NO_DATA_TIMEOUT = "EVENT_NO_DATA_TIMEOUT"
    BACKPRESSURE = "EVENT_BACKPRESSURE"
    CALLBACK_SLOW = "EVENT_CALLBACK_SLOW"
    DROPPED_MESSAGE = "EVENT_DROPPED_MESSAGE"

    # Callback Events
    CALLBACK_REGISTERED = "EVENT_CALLBACK_REGISTERED"
    CALLBACK_UNREGISTERED = "EVENT_CALLBACK_UNREGISTERED"
    CALLBACK_EXCEPTION = "EVENT_CALLBACK_EXCEPTION"

    #Other Events
    INTERNAL_ERROR = "EVENT_INTERNAL_ERROR"
    USER_ERROR = "EVENT_USER_ERROR"