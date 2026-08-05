# -*- coding: utf-8 -*-
import logging
from enum import Enum
from ...log._log import new_log, MicrosecondFormatter
#----------------------------------------------------------------------------
SW_LOG_FILE_NAME = "SWQuote"
#----------------------------------------------------------------------------
class LoggingType(Enum):
    INFO = 1
    WARNING = 2
    ERROR = 3
    DEBUG = 4
    CRITICAL = 5
#----------------------------------------------------------------------------
_LEVEL_METHOD = {
    LoggingType.INFO: "info",
    LoggingType.WARNING: "warning",
    LoggingType.ERROR: "error",
    LoggingType.DEBUG: "debug",
    LoggingType.CRITICAL: "critical",
}
#----------------------------------------------------------------------------
_CONSOLE_FMT = MicrosecondFormatter(
    fmt='[%(asctime)s] %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S.%f',
)
#----------------------------------------------------------------------------
class ConsoleFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, "hide_console", False)


def _add_console_filter(handler: logging.Handler):
    has_filter = any(isinstance(f, ConsoleFilter) for f in handler.filters)
    if not has_filter:
        handler.addFilter(ConsoleFilter())
#----------------------------------------------------------------------------

def _build_logger(filename: str, to_console: bool = True) -> logging.Logger:
    lg = new_log(filename)
    if to_console:
        console_handlers = [
            h for h in lg.handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        ]
        if console_handlers:
            for handler in console_handlers:
                _add_console_filter(handler)
        else:
            ch = logging.StreamHandler()
            ch.setFormatter(_CONSOLE_FMT)
            _add_console_filter(ch)
            lg.addHandler(ch)
    return lg

#----------------------------------------------------------------------------
_logger = _build_logger(SW_LOG_FILE_NAME)
#----------------------------------------------------------------------------
def set_sw_log_file(filename: str, to_console: bool = True):
    global _logger
    _logger = _build_logger(filename, to_console)
#----------------------------------------------------------------------------
def write_log(logging_type: LoggingType = LoggingType.INFO, message: str = "",
              api_id: str = "", show_api_id_in_log: bool = False,
              show_console: bool = True):
    if not message:
        return
    msg = f"[{api_id}] {message}" if (show_api_id_in_log and api_id) else message
    method_name = _LEVEL_METHOD.get(logging_type)
    if method_name:
        extra = None if show_console else {"hide_console": True}
        getattr(_logger, method_name)(msg, extra=extra)
#----------------------------------------------------------------------------