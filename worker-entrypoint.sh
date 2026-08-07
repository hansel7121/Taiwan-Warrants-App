#!/bin/sh
# Runs as root (the image's default user — see Dockerfile.worker): installs
# KGI/Fubon's proprietary binaries from private Supabase Storage, which needs
# root to copy libCGCrypt.so into /lib/x86_64-linux-gnu/, then drops to the
# unprivileged `worker` user for the actual broker_worker.py process (#48).
set -e

python -u scripts/install_worker_binaries.py

exec gosu worker "$@"
