"""IV-surface grid interpolation, shared by the warrant and TW-option surface routes.

Interpolates a scattered (strike, days-to-expiry, IV) point cloud onto a regular
grid for Plotly's `surface` trace. Pure: no Flask, no I/O, JSON-serializable output.
"""
import numpy as np
from scipy.interpolate import griddata

# The canonical resolution per CLAUDE.md's IV Surface spec; callers still pass
# it explicitly so the value used at a given route is visible there.
DEFAULT_RESOLUTION = 80


def interpolate_grid(x, y, z, resolution):
    """Grid-interpolate scattered (x, y, z) points; NaN cells (outside the convex hull) become None for JSON/Plotly. Requires >= 3 non-collinear points."""
    xi = np.linspace(min(x), max(x), resolution)
    yi = np.linspace(min(y), max(y), resolution)
    xi_grid, yi_grid = np.meshgrid(xi, yi)
    zi = griddata((x, y), z, (xi_grid, yi_grid), method="linear")
    # np.where with None forces an object array, which .tolist() turns into
    # nested lists of float/None — the shape the frontend already expects.
    zi = np.where(np.isnan(zi), None, zi)
    return xi.tolist(), yi.tolist(), zi.tolist()


def surface_from_points(x, y, z, labels, resolution=DEFAULT_RESOLUTION):
    """One quote side's surface: the interpolated grid plus the raw points it came from.

    Bid and ask are built independently rather than from one shared point set —
    an instrument can carry a solvable ask IV and no bid IV at all, so the two
    sides legitimately cover different strikes and expiries. Returns None when
    fewer than three points survive, which is the minimum a triangulation needs.
    """
    if len(x) < 3:
        return None
    xi, yi, zi = interpolate_grid(x, y, z, resolution)
    return {"x": xi, "y": yi, "z": zi,
            "scatter_x": list(x), "scatter_y": list(y), "scatter_z": list(z),
            "labels": list(labels)}
