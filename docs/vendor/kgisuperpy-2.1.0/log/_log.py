# -*- coding: utf-8 -*-
import os
import logging
import base64
import json
from datetime import datetime, timedelta

# 當前腳本路徑
script_path = os.path.abspath(__file__)
script_dir = os.path.dirname(script_path)

# 取得台灣時間（UTC+8）
taiwan_time = datetime.utcnow() + timedelta(hours=8)
date_folder = taiwan_time.strftime('%Y%m%d')
log_folder = os.path.join(script_dir, date_folder)
os.makedirs(log_folder, exist_ok=True)

class MicrosecondFormatter(logging.Formatter):
    """自訂 formatter，支援微秒輸出"""
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")

def encrypt_message(msg, pwd: str) -> str:
    """簡單 XOR + Base64 加密，支援 dict/list/bytes，自動轉成字串"""
    if not pwd:
        return msg

    if isinstance(msg, (dict, list, tuple)):
        data = json.dumps(msg, ensure_ascii=False).encode('utf-8')
    elif isinstance(msg, (bytes, bytearray)):
        data = bytes(msg)
    else:
        data = str(msg).encode('utf-8')

    key = pwd.encode("utf-8")
    enc = bytes([b ^ key[i % len(key)] for i, b in enumerate(data)])
    return base64.urlsafe_b64encode(enc).decode("utf-8")

def new_log(name, extra_path=None, show_console=True, pwd=None):
    """
    建立 logger
    - show_console: 是否印 info/debug/warning 訊息到 console
    - pwd: 加密訊息密碼，logger.pwd(msg) 會加密並只寫檔案
    """
    log_filename = f"{name}.log"
    log_path = os.path.join(extra_path or log_folder, log_filename)

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # 不傳到 root logger

    # --- 普通 console handler（info/debug） ---
    if show_console and not any(isinstance(h, logging.StreamHandler) and getattr(h, 'level', None) == logging.INFO
                                for h in logger.handlers):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(MicrosecondFormatter('[%(asctime)s] %(levelname)s - %(message)s'))
        console_handler.setLevel(logging.INFO)
        console_handler.addFilter(lambda record: record.levelno < logging.WARNING)  # 過濾 warning / error
        logger.addHandler(console_handler)

    # --- WARNING / ERROR 專用 console handler ---
    if not any(isinstance(h, logging.StreamHandler) and getattr(h, 'level', None) == logging.WARNING
            for h in logger.handlers):
        warn_error_console = logging.StreamHandler()
        warn_error_console.setFormatter(MicrosecondFormatter('[%(asctime)s] %(levelname)s - %(message)s'))
        warn_error_console.setLevel(logging.WARNING)
        logger.addHandler(warn_error_console)

    # --- file handler（寫所有訊息） ---
    if not any(isinstance(h, logging.FileHandler) and h.baseFilename == os.path.abspath(log_path)
               for h in logger.handlers):
        file_handler = logging.FileHandler(log_path, encoding='utf-8')
        file_handler.setFormatter(MicrosecondFormatter('[%(asctime)s] %(levelname)s - %(message)s'))
        logger.addHandler(file_handler)

    # --- 加密訊息方法（只寫檔案，不印 console） ---
    def log_pwd(msg):
        if pwd:
            enc_msg = encrypt_message(msg, pwd)
            for h in logger.handlers:
                if isinstance(h, logging.FileHandler):
                    h.emit(logging.LogRecord(name, logging.INFO, "", 0, enc_msg, None, None))
        else:
            logger.info(msg)

    logger.pwd = log_pwd
    logger.file_path = log_path
    return logger

# ===== 使用範例 =====
if __name__ == "__main__":
    log = new_log("test_log", pwd="test")
    log.info("這是普通訊息")
    log.warning("這是警告訊息")
    log.pwd("這是加密訊息，不會顯示在 console")
