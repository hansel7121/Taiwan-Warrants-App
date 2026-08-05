import json
import os
from .quote_data.quote_status import QuotationCode, QuotationError, QuoteEventCode

class LocaleManager:
    def __init__(self, locale: str, fallback_locale: str = "en", locales_dir: str = None):
        self._locale = locale
        
        if locales_dir is None:
            locales_dir = os.path.join(os.path.dirname(__file__), "locales")

        file_path = os.path.join(locales_dir, f"{locale}.json")

        with open(file_path, "r", encoding="utf-8") as f:
            self._messages = json.load(f)

        if locale != fallback_locale:
            fallback_path = os.path.join(locales_dir, f"{fallback_locale}.json")
            with open(fallback_path, "r", encoding="utf-8") as f:
                self._fallback_messages = json.load(f)
        else:
            self._fallback_messages = {}

    def build_message(self, event_code: QuoteEventCode, respond_code: QuotationCode, 
                  log_key: str = None, **kwargs) -> str:
        combined_key = f"{event_code.name}.{respond_code.name}"
        template = (
            self._messages.get(log_key)                      # 1. 自訂 log_key 優先
            or self._messages.get(combined_key)              # 2. EVENT.CODE 組合
            or self._messages.get(event_code.name)           # 3. 只有 EVENT
            or self._fallback_messages.get(log_key)          # 4. fallback 自訂 key
            or self._fallback_messages.get(combined_key)     # 5. fallback 組合
            or self._fallback_messages.get(event_code.name)  # 6. fallback EVENT
            or combined_key                                  # 7. 找不到就回傳 key
        )
        try:
            return template.format(status_code=respond_code.value, **kwargs)
        except KeyError:
            return template