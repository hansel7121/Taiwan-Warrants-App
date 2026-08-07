"""Issue #48: fetch KGI/Fubon's proprietary binaries into the worker container at boot.

Run by worker-entrypoint.sh, as root, before broker_worker.py starts. These
binaries can't be committed to this public repo (docs/vendor/README.md), so
they're staged once by hand in a private Supabase Storage bucket
(supabase/migrations/015_broker_binaries_bucket.sql) and pulled down here on
every container start instead of baked into the image at build time.

Missing objects are a warning, not a fatal error: before the bucket is
seeded, or if only one broker's binary has been staged, the worker should
still start and run everything that doesn't need the missing piece (its
poll loop, any other broker's connections) rather than crash-looping. A
broker whose binary is missing just keeps landing as 'disconnected', same as
before this script existed.
"""
import os
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import db


BUCKET = "broker-binaries"

# KGI's per-account cert data (output of running `KGI_CGCrypt genDat` against
# a real account) is deliberately NOT handled here: kgisuperpy.login() takes
# no cert-path argument (docs/vendor/kgisuperpy-2.1.0/main.py), so wherever
# genDat's output needs to live is not yet confirmed against KGI's own install
# docs. Do not invent a path here — see Dockerfile.worker's note.
LIBCGCRYPT_OBJECT = "libCGCrypt.so"
LIBCGCRYPT_DEST = "/lib/x86_64-linux-gnu/libCGCrypt.so"


def _download(object_name):
    try:
        return db.client().storage.from_(BUCKET).download(object_name)
    except Exception as e:
        print(f"install_worker_binaries: {object_name} not available yet ({e}); skipping", flush=True)
        return None


def install_libcgcrypt():
    data = _download(LIBCGCRYPT_OBJECT)
    if data is None:
        return
    with open(LIBCGCRYPT_DEST, "wb") as f:
        f.write(data)
    os.chmod(LIBCGCRYPT_DEST, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)  # 755
    print(f"install_worker_binaries: installed {LIBCGCRYPT_DEST}", flush=True)


def install_fubon_wheel():
    # pip parses the package name/version/platform tags out of the wheel's
    # OWN filename, so it must be uploaded under its real name (e.g.
    # fubon_neo-2.2.8-cp311-cp311-manylinux_2_17_x86_64.whl) rather than a
    # fixed generic key — list() + endswith(".whl") finds it without this
    # script needing to know the exact version pinned at upload time.
    try:
        objects = db.client().storage.from_(BUCKET).list()
    except Exception as e:
        print(f"install_worker_binaries: could not list {BUCKET} ({e}); skipping fubon_neo wheel", flush=True)
        return
    wheel_name = next((o["name"] for o in objects if o["name"].endswith(".whl")), None)
    if wheel_name is None:
        print("install_worker_binaries: no .whl object in broker-binaries yet; skipping fubon_neo wheel", flush=True)
        return

    data = _download(wheel_name)
    if data is None:
        return
    tmp_dir = tempfile.mkdtemp()
    path = os.path.join(tmp_dir, wheel_name)
    with open(path, "wb") as f:
        f.write(data)
    subprocess.run([sys.executable, "-m", "pip", "install", path], check=True)
    print(f"install_worker_binaries: installed {wheel_name}", flush=True)


if __name__ == "__main__":
    install_libcgcrypt()
    install_fubon_wheel()
