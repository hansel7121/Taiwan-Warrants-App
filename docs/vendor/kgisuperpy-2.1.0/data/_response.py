import pandas as pd
import requests
from ..log._log import new_log

class GetDataError(Exception):
    """Exception raised for errors in the get data api."""

    def __init__(self, message, code):
        # print(code)
        self.message = message
        self.code = code
        formated_message = f"StatusCode: {code.value}, Detail: {message}"
        super().__init__(formated_message)

from enum import Enum
class GetDataCode(Enum):
    #SUCCESSFUL_OPERATION = "D200"
    INVALID_PARAMETER = "D400"
    UNAUTHORIZED = "D401"
    FORBIDDEN = "D403"
    NOT_FOUND = "D404"
    INTERNAL_SERVER_ERROR = "D500"
    INVALID_TABLE_NAME = "D001"
    OTHER_HTTP_ERROR = "D00X"

log = new_log('Data')

def _response(response):
    if response.status_code == 200:
        log.info(f"API Request Successful - Status: {response.status_code}")
        return response
    elif response.status_code == 400:
        log.error(f"API Error 400 - Invalid Parameter: {response.text}")
        raise GetDataError("The request parameters are invalid, please verify the correctness of the request data.", GetDataCode.INVALID_PARAMETER)

    elif response.status_code == 401:
        log.error(f"API Error 401 - Unauthorized: {response.text}")
        raise GetDataError("Not authenticated or the authentication token has expired and is invalid, please re-authenticate.", GetDataCode.UNAUTHORIZED)

    elif response.status_code == 403:
        log.error(f"API Error 403 - Forbidden: {response.text}")
        raise GetDataError("Access to the specified request is denied, please confirm that you have the relevant permissions.", GetDataCode.FORBIDDEN)

    elif response.status_code == 404:
        log.error(f"API Error 404 - Not Found: {response.text}")
        raise GetDataError("The requested resource could not be found, please confirm the correctness of the request URL.", GetDataCode.NOT_FOUND)

    elif response.status_code == 500:
        log.error(f"API Error 500 - Internal Server Error: {response.text}")
        raise GetDataError("Internal system error, please try again later.", GetDataCode.INTERNAL_SERVER_ERROR)
    else:
        log.error(f"API Error {response.status_code} - Other HTTP Error: {response.text}")
        raise GetDataError(f"HTTP Error {response}", GetDataCode.OTHER_HTTP_ERROR )
    