"""Broker-feed integration (KGI / Fubon): credential storage, the per-broker
market-data clients, and the Connection Pool the Singapore worker opens.

Only credentials.py and crypto.py are safe to import from the web app; the
client modules pull in worker-only broker SDKs."""
