"""IV-surface grid interpolation.

Both surface routes (warrants and TW options) plot the same thing: a scattered
cloud of (strike, days-to-expiry, IV) points interpolated onto a regular grid
for Plotly's `surface` trace. The interpolation lived inline in each route and
had silently drifted apart (80x80 for warrants, 60x60 for options) with nothing
documenting why. It is one computation, so it lives in one place and the
resolution is an explicit argument at each call site.

Pure: no Flask, no I/O, JSON-serializable output.
"""
import numpy as np
from scipy.interpolate import griddata

# What CLAUDE.md documents as *the* IV Surface spec. Kept here as the name of
# the canonical choice; callers still pass it explicitly so the value used by a
# given route is visible at that route.
DEFAULT_RESOLUTION = 80


def interpolate_grid(x, y, z, resolution):
    """Interpolate the scattered points (x, y, z) onto a regular grid.

    `x`/`y`/`z` are equal-length sequences of raw point coordinates (strike,
    days-to-expiry, IV in percent). Returns `(xi, yi, zi)` where `xi` and `yi`
    are `resolution`-long axis lists spanning each input's min..max, and `zi` is
    a `resolution` x `resolution` nested list indexed `zi[iy][ix]`.

    Grid cells outside the convex hull of the input points have no linear
    interpolant; scipy yields NaN there, which is not valid JSON, so those cells
    are returned as `None` and Plotly renders them as gaps. Everything comes
    back as plain Python lists — the caller can hand the result straight to
    `jsonify` without further conversion.

    Requires at least 3 non-collinear points (a Qhull triangulation); fewer and
    scipy raises. Callers own that guard, since the useful error message is
    domain-specific ("not enough data points to build a surface").
    """
    xi = np.linspace(min(x), max(x), resolution)
    yi = np.linspace(min(y), max(y), resolution)
    xi_grid, yi_grid = np.meshgrid(xi, yi)
    zi = griddata((x, y), z, (xi_grid, yi_grid), method="linear")
    # np.where with None forces an object array, which .tolist() turns into
    # nested lists of float/None — the shape the frontend already expects.
    zi = np.where(np.isnan(zi), None, zi)
    return xi.tolist(), yi.tolist(), zi.tolist()
