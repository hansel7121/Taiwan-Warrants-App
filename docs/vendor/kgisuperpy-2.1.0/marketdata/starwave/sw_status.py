# -*- coding: utf-8 -*-
"""
StarWave event / response codes (self-contained).

Mirrors the role of quote_status.py for the WebSocket side, but kept separate so
StarWave stays independent. These enums are consumed by LocaleManager via their
``.name`` (for the message key) and ``.value`` (for the status code).
"""

from enum import Enum


class SWRespondCode(Enum):
    SUCCESSFUL_OPERATION = "SW000"
    SW_DISCONNECTED      = "SW001"
    SW_LOGIN_FAILED      = "SW002"
    SW_SUBSCRIBE_FAILED  = "SW003"
    SW_SUBSCRIBE_REJECTED = "SW004"
    SW_UNSUBSCRIBE_FAILED = "SW005"
    SW_SUBSCRIBE_LIMITED  = "SW006"
    SW_LOGIN_EXHAUSTED    = "SW007"
    SW_LOGIN_NO_AUTH_SOURCE = "SW008"
    SW_LOGIN_NO_CHANNEL   = "SW009"
    SW_LOGIN_MISSING_CREDENTIALS = "SW010"


class SWEventCode(Enum):
    CONNECTED       = "connected"
    DISCONNECTED    = "disconnected"
    LOGIN_OK        = "login_ok"
    LOGIN_FAIL      = "login_fail"
    LOGIN_EXHAUSTED = "login_exhausted"
    LOGIN_NOT_ATTEMPTED = "login_not_attempted"
    CONTRACTS_READY = "contracts_ready"
    SUBSCRIBE_OK    = "subscribe_ok"
    SUBSCRIBE_FAIL  = "subscribe_fail"
    UNSUBSCRIBE_OK  = "unsubscribe_ok"
    UNSUBSCRIBE_FAIL = "unsubscribe_fail"
