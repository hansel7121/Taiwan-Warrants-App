from enum import Enum
from ..log._log import *
from datetime import datetime

LOG_FILE_NAME = "Quote"
ENABLE_FILE_LOGGING = False
logger = logging.getLogger(__name__)

class ConsoleFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, "file_only", False)

def set_file_logging(enable: bool, name: str = __name__, log_filename: str = LOG_FILE_NAME):
    log_path_file_name = os.path.abspath(os.path.join(log_folder, f"{log_filename}.log"))

    global ENABLE_FILE_LOGGING, logger
    ENABLE_FILE_LOGGING = enable

    config_dict = {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {
            'simple': {
                '()': MicrosecondFormatter,
                'format': '[%(asctime)s] %(levelname)s - %(message)s',
                'datefmt': '%Y-%m-%d %H:%M:%S.%f',
            },
        },
        'filters': {
            'hide_file_only': {
                '()': ConsoleFilter,
            }
        },
        'handlers': {
            'console': {
                'level': 'DEBUG',
                'class': 'logging.StreamHandler',
                'formatter': 'simple',
                'filters': ['hide_file_only'],
            },
        },
        'loggers': {
            '': {
                'handlers': ['console'],
                'level': 'INFO',
                'propagate': False,
                # 'propagate': True, # If you want to propagate to root logger as well
            },
        },
        'websocket': {
            'handlers': ['console'],
            'level': 'CRITICAL',
            'propagate': False,
        },
    }

    if enable:
        config_dict['handlers']['file'] = {
            'level': 'DEBUG',
            'class': 'logging.FileHandler',
            'filename': log_path_file_name,
            'formatter': 'simple',
            'encoding': 'utf-8',
        }
        config_dict['loggers']['']['handlers'].append('file')

    logging.config.dictConfig(config_dict)
    if enable:
        logger = logging.getLogger(name)


# for handler in logger.handlers:
#     if isinstance(handler, logging.FileHandler) and hasattr(handler.stream, 'flush'):
#         handler.stream.flush()

for handler in logging.getLogger().handlers:
    if isinstance(handler, logging.FileHandler) and hasattr(handler.stream, 'flush'):
        handler.stream.flush()

class LoggingType(Enum):
    INFO = 1
    WARNING = 2
    ERROR = 3
    DEBUG = 4
    CRITICAL = 5

def write_log(logging_type: LoggingType = LoggingType.INFO, message: str = "", app_id:str = "", show_api_id_in_log: bool = False, file_only: bool = False):
    if not message:
        return 
    
    if file_only and not ENABLE_FILE_LOGGING:
        file_only = False

    msg = f"[{app_id}] {message}" if (show_api_id_in_log and app_id) else message
    extra = {"file_only": True} if file_only else None

    if logging_type == LoggingType.INFO:
        logger.info(msg, extra=extra)
    elif logging_type == LoggingType.WARNING:
        logger.warning(msg, extra=extra)
    elif logging_type == LoggingType.ERROR:
        logger.error(msg, extra=extra)
    elif logging_type == LoggingType.DEBUG:
        logger.debug(msg, extra=extra)
    elif logging_type == LoggingType.CRITICAL:
        logger.critical(msg, extra=extra)
        
class LoggingBase:
    def __init__(self, api_id: str = "", show_api_id_in_log: bool = False, file_only: bool = False):
        self._api_id = api_id
        self._show_api_id_in_log = show_api_id_in_log
        self._file_only = file_only

    def _log_message(self, logging_type: LoggingType = LoggingType.INFO, message: str = "", file_only: bool = False):
        write_log(
            logging_type=logging_type,
            message=message,
            app_id=self._api_id,
            show_api_id_in_log=self._show_api_id_in_log,
            file_only= self._file_only or file_only
        )