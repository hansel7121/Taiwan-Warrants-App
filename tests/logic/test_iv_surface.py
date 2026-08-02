"""interpolate_grid's contract: axis lengths, grid shape, JSON-safe output.

No I/O to patch — the function is pure, so a small synthetic point cloud is the
whole fixture.
"""
import math

import pytest

from logic import iv_surface

# A 3x3 grid of (strike, dte) with a plane-ish IV surface: z = strike + dte.
# Linear griddata reproduces a plane exactly (up to float error) inside the
# hull, which makes the interior spot-check meaningful.
X = [100.0, 110.0, 120.0] * 3
Y = [30.0] * 3 + [60.0] * 3 + [90.0] * 3
Z = [x + y for x, y in zip(X, Y)]


@pytest.mark.parametrize("resolution", [8, 80])
def test_axes_and_grid_have_the_requested_resolution(resolution):
    xi, yi, zi = iv_surface.interpolate_grid(X, Y, Z, resolution)

    assert len(xi) == resolution
    assert len(yi) == resolution
    assert len(zi) == resolution
    assert all(len(row) == resolution for row in zi)


def test_axes_span_the_input_range():
    xi, yi, _ = iv_surface.interpolate_grid(X, Y, Z, 10)

    assert xi[0] == pytest.approx(min(X))
    assert xi[-1] == pytest.approx(max(X))
    assert yi[0] == pytest.approx(min(Y))
    assert yi[-1] == pytest.approx(max(Y))


def test_output_is_plain_json_serializable_lists():
    xi, yi, zi = iv_surface.interpolate_grid(X, Y, Z, 6)

    assert isinstance(xi, list) and all(isinstance(v, float) for v in xi)
    assert isinstance(yi, list) and all(isinstance(v, float) for v in yi)
    assert isinstance(zi, list) and all(isinstance(row, list) for row in zi)
    for row in zi:
        for v in row:
            assert v is None or isinstance(v, float)
            # NaN is not valid JSON — it must have been replaced by None.
            assert v is None or not math.isnan(v)


def test_interpolated_value_near_an_input_point_matches_it():
    # Grid corners coincide with input points, so zi[0][0] is the (100, 30)
    # sample and zi[-1][-1] the (120, 90) one. Loose tolerance: linear
    # triangulation, not an exact reconstruction.
    xi, yi, zi = iv_surface.interpolate_grid(X, Y, Z, 21)

    assert zi[0][0] == pytest.approx(130.0, abs=1.0)      # 100 + 30
    assert zi[-1][-1] == pytest.approx(210.0, abs=1.0)    # 120 + 90

    # An interior cell: the midpoint of the plane, ~ (110, 60) -> 170.
    mid = len(xi) // 2
    assert xi[mid] == pytest.approx(110.0)
    assert yi[mid] == pytest.approx(60.0)
    assert zi[mid][mid] == pytest.approx(170.0, abs=1.0)


def test_points_outside_the_hull_come_back_as_none():
    # A single triangle leaves most of the bounding box outside the convex
    # hull; those cells must be None (Plotly gaps), never NaN.
    x = [0.0, 10.0, 0.0]
    y = [0.0, 0.0, 10.0]
    z = [1.0, 2.0, 3.0]

    _, _, zi = iv_surface.interpolate_grid(x, y, z, 9)

    flat = [v for row in zi for v in row]
    assert any(v is None for v in flat)
    assert any(v is not None for v in flat)
