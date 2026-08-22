"""Dtype-exact JSON codec for the DataFrame fixtures under tests/fixtures/.

Fixtures have to survive a round-trip with their dtypes intact — the option
frames deliberately carry ``None`` in object-dtype columns while the warrant
frames carry ``NaN`` in float columns, and a port that silently turns one into
the other passes every value-level test while breaking ``.fillna``, ``> 0``
comparisons and the Supabase writer. CSV loses that distinction and Parquet
would mean adding pyarrow to the deploy's requirements for test data, so the
frames are stored as readable, diffable JSON with an explicit dtype map.

``None`` encodes as JSON null; ``NaN``/``NaT`` encode as the sentinel below, so
the two stay distinguishable inside an object column.
"""
import json

import numpy as np
import pandas as pd

NAN = "__NaN__"


def _enc(v):
    if v is None:
        return None
    if isinstance(v, float) and np.isnan(v):
        return NAN
    if v is pd.NaT:
        return NAN
    if isinstance(v, (pd.Timestamp, np.datetime64)):
        ts = pd.Timestamp(v)
        return NAN if pd.isna(ts) else ts.isoformat()
    if isinstance(v, np.generic):
        return _enc(v.item())
    if isinstance(v, dict):
        return {k: _enc(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_enc(x) for x in v]
    return v


def _dec(v):
    if v == NAN:
        return np.nan
    if isinstance(v, dict):
        return {k: _dec(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_dec(x) for x in v]
    return v


def dump_frame(df):
    """DataFrame -> a JSON-safe dict carrying values, dtypes and the index."""
    return {
        "dtypes": {str(c): str(df[c].dtype) for c in df.columns},
        "index": [_enc(i) for i in df.index],
        "columns": {str(c): [_enc(v) for v in df[c].tolist()] for c in df.columns},
    }


def load_frame(blob):
    """Inverse of `dump_frame`, restoring the original dtypes exactly."""
    cols = {c: [_dec(v) for v in vals] for c, vals in blob["columns"].items()}
    df = pd.DataFrame(cols, index=[_dec(i) for i in blob["index"]])
    for c, dt in blob["dtypes"].items():
        if dt.startswith("datetime64"):
            df[c] = pd.to_datetime(df[c])
        elif dt != "object" and str(df[c].dtype) != dt:
            df[c] = df[c].astype(dt)
    return df[list(blob["dtypes"])]


def dump_rows(rows):
    """`list[dict]` matcher output -> JSON-safe, keeping None and NaN distinct."""
    return [{k: _enc(v) for k, v in r.items()} for r in rows]


def load_rows(blob):
    return [{k: _dec(v) for k, v in r.items()} for r in blob]


def write_json(path, obj):
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=False) + "\n")


def read_json(path):
    return json.loads(path.read_text())
