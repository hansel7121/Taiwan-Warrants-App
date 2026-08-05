# Vendor reference

Read-only reference copies of third-party broker SDK source, kept here so Claude
(and anyone else) can grep real API signatures instead of guessing from memory or
stale docs. This is reference material, not code the app imports — the app installs
these SDKs normally via `requirements.txt` / vendored wheels (see there for how
they're actually wired into `services/broker/`).

**What's copied in:** `.py` source files only. Compiled binaries (`.so`, `.pyd`,
`.dll`) and bundled data files are deliberately excluded — they're not readable
reference material, they're large, and vendoring compiled proprietary binaries is a
license risk this repo (public) shouldn't take on. If a question needs the compiled
side, pull the package into a disposable venv and inspect it locally instead.

## Contents

- `kgisuperpy-2.1.0/` — KGI's Python SDK, pinned version from `requirements.txt`.
  Source-only extract (57 `.py` files, ~924K); marketdata/msmp/usmsmp/pushClient
  binaries stripped out.

## Planned additions

- KGI pythonnet SDK reference (see `docs/research/kgisuperpy-vs-pythonnet-comparison.md`
  for why this is being evaluated as a kgisuperpy alternative for issue #39).
- Fubon (`fubon_neo`) SDK reference — not on PyPI, installed from a vendored wheel
  in `Dockerfile.worker`; same source-only extraction approach applies here too.

## Updating

When a pinned SDK version bumps, add a new `<package>-<version>/` folder rather than
overwriting — old versions stay for history since worker code may lag the pin.
