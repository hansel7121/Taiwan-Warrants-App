"""One-off: encrypt this machine's Fubon .env credentials + cert into Supabase.

Run once (`python scripts/seed_fubon_credentials.py`) after setting
BROKER_CRED_KEY and the FUBON_* vars in .env. Reads FUBON_ID/FUBON_PASSWORD/
FUBON_CERT_PATH/FUBON_CERT_PASSWORD, uploads the cert file to the private
broker-certs bucket, and upserts the fubon_credentials row (see
services/broker/credentials.py). Safe to re-run — it upserts.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.broker import credentials

LABEL = os.environ.get("FUBON_CRED_LABEL", credentials.DEFAULT_LABEL)


def main():
    fubon_id = os.environ.get("FUBON_ID")
    fubon_password = os.environ.get("FUBON_PASSWORD")
    cert_path = os.environ.get("FUBON_CERT_PATH")
    cert_password = os.environ.get("FUBON_CERT_PASSWORD")

    missing = [k for k, v in {
        "FUBON_ID": fubon_id, "FUBON_PASSWORD": fubon_password,
        "FUBON_CERT_PATH": cert_path, "FUBON_CERT_PASSWORD": cert_password,
    }.items() if not v]
    if missing:
        print(f"Missing .env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    if not os.environ.get("BROKER_CRED_KEY"):
        print("BROKER_CRED_KEY not set — generate one with:\n"
              '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"',
              file=sys.stderr)
        sys.exit(1)

    with open(cert_path, "rb") as f:
        cert_bytes = f.read()
    ext = cert_path.rsplit(".", 1)[-1] if "." in cert_path else "p12"

    credentials.upsert_credential(LABEL, {
        "fubon_id": fubon_id,
        "fubon_password": fubon_password,
        "cert_password": cert_password,
    })
    credentials.upload_cert(LABEL, cert_bytes, ext)
    print(f"Seeded fubon_credentials row '{LABEL}' and uploaded cert "
          f"({len(cert_bytes)} bytes) to broker-certs/fubon/{LABEL}/cert.{ext}")


if __name__ == "__main__":
    main()
