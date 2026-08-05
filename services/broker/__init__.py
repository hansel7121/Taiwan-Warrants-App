"""Broker-feed integration (KGI / Fubon).

Currently just credential storage: crypto.py (Fernet at rest) and
credentials.py (broker_credentials CRUD). Both are safe to import from the web
app — they pull in no broker SDK. The per-broker market-data clients and the
Connection Pool land with the worker slice; when they do, only credentials.py
and crypto.py stay web-app-importable."""
