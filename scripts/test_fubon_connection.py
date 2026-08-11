"""Standalone sanity check for Fubon Neo SDK connectivity — not part of the
main app. Run it directly (`python scripts/test_fubon_connection.py`) and
open http://127.0.0.1:5099 to see whether login succeeds, using credentials
from .env (FUBON_ID, FUBON_PASSWORD, FUBON_CERT_PATH, FUBON_CERT_PASSWORD).
"""
import os

from flask import Flask

app = Flask(__name__)


def _load_dotenv():
    """Same minimal loader as services/db.py, duplicated to keep this script standalone."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, ".env")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv()


def _try_login():
    """Attempt sdk.login() with .env credentials; returns (ok, message)."""
    person_id = os.environ.get("FUBON_ID")
    password = os.environ.get("FUBON_PASSWORD")
    cert_path = os.environ.get("FUBON_CERT_PATH")
    cert_pass = os.environ.get("FUBON_CERT_PASSWORD")

    missing = [
        name for name, val in [
            ("FUBON_ID", person_id),
            ("FUBON_PASSWORD", password),
            ("FUBON_CERT_PATH", cert_path),
            ("FUBON_CERT_PASSWORD", cert_pass),
        ] if not val
    ]
    if missing:
        return False, f"Missing .env vars: {', '.join(missing)}"

    try:
        from fubon_neo.sdk import FubonSDK
    except ImportError:
        return False, "fubon_neo is not installed (see docs/vendor/README.md for the vendored wheel)"

    try:
        sdk = FubonSDK()
        result = sdk.login(person_id, password, cert_path, cert_pass)
    except Exception as e:
        return False, f"login() raised: {e}"

    if not getattr(result, "is_success", False):
        return False, f"login failed: {getattr(result, 'message', result)}"

    accounts = getattr(result, "data", None)
    try:
        sdk.logout()
    except Exception:
        pass

    return True, f"Login succeeded. Accounts: {accounts}"


@app.route("/")
def index():
    ok, message = _try_login()
    status = "CONNECTED" if ok else "FAILED"
    color = "#1a7f37" if ok else "#c1121f"
    return f"""
    <html><body style="font-family: sans-serif; padding: 2rem;">
      <h1 style="color: {color};">Fubon connection: {status}</h1>
      <p>{message}</p>
    </body></html>
    """


if __name__ == "__main__":
    app.run(port=5099, debug=False)
