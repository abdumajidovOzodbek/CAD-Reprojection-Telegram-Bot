"""Unit tests for the building-in-plot geometry repair.

Geometry is expressed in metres in a fictional local grid, matching what the
bot works with after reading a DXF in the Krassovsky source CRS.

The "micro" fixtures reproduce the defect measured in a real failing drawing:
a building protruding 0.6 mm over roughly 0.005 m^2.
"""

import geopandas as gpd
import pytest
from shapely.geometry import MultiPolygon, Point, Polygon, box

from autofix import (
    Action,
    AutofixConfig,
    AutofixError,
    Verdict,
    autofix_frame,
    classify_building,
    clip_to_plot,
    fix_building,
    identify_plot_and_buildings,
    snap_to_plot,
    verify_containment,
)

CRS = (
    "+proj=tmerc +lat_0=0 +lon_0=69 +k=1 +x_0=-182.2698 +y_0=-4500719.7668 "
    "+ellps=krass +units=m +no_defs"
)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def config() -> AutofixConfig:
    return AutofixConfig()


@pytest.fixture
def plot() -> Polygon:
    """A 40 x 30 m land plot, 1200 m^2."""
    return box(0.0, 0.0, 40.0, 30.0)


@pytest.fixture
def notched_plot() -> Polygon:
    """Land plot whose top edge dips 2 cm between x=18 and x=22.

    Reproduces the case where a building's corners are all inside the plot but
    a long edge bulges out across a concave stretch of the boundary. Snapping
    cannot help there, only clipping can.
    """
    return Polygon(
        [
            (0.0, 0.0),
            (40.0, 0.0),
            (40.0, 30.0),
            (22.0, 30.0),
            (22.0, 29.98),
            (18.0, 29.98),
            (18.0, 30.0),
            (0.0, 30.0),
        ]
    )


@pytest.fixture
def building_inside() -> Polygon:
    """Comfortably inside the plot; must never be touched."""
    return box(5.0, 5.0, 15.0, 12.0)


@pytest.fixture
def building_micro_overhang() -> Polygon:
    """Two corners 0.6 mm and 0.4 mm past the plot's top edge."""
    return Polygon(
        [
            (20.0, 20.0),
            (30.0, 20.0),
            (30.0, 30.0006),
            (20.0, 30.0004),
            (20.0, 20.0),
        ]
    )


@pytest.fixture
def building_cm_overhang() -> Polygon:
    """A 4 cm overhang: still within tolerance, but visible in CAD."""
    return box(20.0, 20.0, 30.0, 30.04)


@pytest.fixture
def building_far_outside() -> Polygon:
    """Half the building sits beyond the plot: a real error, not a slip."""
    return box(20.0, 25.0, 30.0, 35.0)


@pytest.fixture
def building_fully_outside() -> Polygon:
    """Entirely off the plot."""
    return box(50.0, 50.0, 60.0, 58.0)


def make_frame(geometries, labels=None) -> gpd.GeoDataFrame:
    """Build a GeoDataFrame shaped like the bot's DXF output."""
    labels = labels or [f"row{i}" for i in range(len(geometries))]
    return gpd.GeoDataFrame(
        {"Layer": ["0"] * len(geometries), "Label": labels},
        geometry=list(geometries),
        crs=CRS,
    )


# --------------------------------------------------------------------------
# Task 1: configuration
# --------------------------------------------------------------------------


def test_config_defaults_are_tight(config: AutofixConfig) -> None:
    """Defaults are calibrated on a 0.6 mm real defect, not guessed loosely."""
    assert config.snap_tolerance_m == 0.05
    assert config.max_outside_area_m2 == 0.25
    assert config.max_outside_area_ratio == 0.01
    assert config.min_part_area_m2 == 0.05


def test_config_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOFIX_SNAP_TOLERANCE_M", "0.2")
    monkeypatch.setenv("AUTOFIX_MAX_OUTSIDE_AREA_M2", "3")
    loaded = AutofixConfig.from_env()
    assert loaded.snap_tolerance_m == 0.2
    assert loaded.max_outside_area_m2 == 3.0
    # Untouched values keep their defaults.
    assert loaded.min_part_area_m2 == AutofixConfig().min_part_area_m2


@pytest.mark.parametrize("bad_value", ["0", "-1", "abc", ""])
def test_config_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch, bad_value: str
) -> None:
    monkeypatch.setenv("AUTOFIX_SNAP_TOLERANCE_M", bad_value)
    with pytest.raises(RuntimeError, match="positive number"):
        AutofixConfig.from_env()


def test_allowed_outside_area_takes_the_larger_limit(config: AutofixConfig) -> None:
    # Small building: the absolute floor applies.
    assert config.allowed_outside_area(10.0) == config.max_outside_area_m2
    # Large building: the proportional limit applies.
    assert config.allowed_outside_area(1000.0) == pytest.approx(10.0)


# --------------------------------------------------------------------------
# Task 2: identifying the plot
# --------------------------------------------------------------------------


def test_largest_polygon_is_the_plot(plot, building_inside, building_micro_overhang):
    frame = make_frame([building_inside, plot, building_micro_overhang])
    plot_position, buildings = identify_plot_and_buildings(frame)
    assert plot_position == 1
    assert buildings == [0, 2]


def test_plot_detection_ignores_row_order(plot, building_inside):
    for geometries, expected in (
        ([plot, building_inside], 0),
        ([building_inside, plot], 1),
    ):
        plot_position, _ = identify_plot_and_buildings(make_frame(geometries))
        assert plot_position == expected


def test_plot_detection_handles_multipolygon_plot(building_inside):
    multi_plot = MultiPolygon([box(0, 0, 40, 30), box(45, 0, 55, 10)])
    frame = make_frame([building_inside, multi_plot])
    plot_position, buildings = identify_plot_and_buildings(frame)
    assert plot_position == 1
    assert buildings == [0]


def test_plot_detection_skips_non_polygons(plot, building_inside):
    from shapely.geometry import LineString

    frame = make_frame([LineString([(0, 0), (5, 5)]), plot, building_inside])
    plot_position, buildings = identify_plot_and_buildings(frame)
    assert plot_position == 1
    assert buildings == [2]


def test_single_polygon_means_no_buildings(plot):
    plot_position, buildings = identify_plot_and_buildings(make_frame([plot]))
    assert plot_position == 0
    assert buildings == []


def test_no_polygons_is_an_error():
    from shapely.geometry import LineString

    with pytest.raises(AutofixError, match="no polygons"):
        identify_plot_and_buildings(make_frame([LineString([(0, 0), (1, 1)])]))

    empty = gpd.GeoDataFrame({"Layer": []}, geometry=[], crs=CRS)
    with pytest.raises(AutofixError, match="no polygons"):
        identify_plot_and_buildings(empty)


def test_area_ties_resolve_deterministically(building_inside):
    twin = box(20.0, 5.0, 30.0, 12.0)
    assert twin.area == pytest.approx(building_inside.area)
    plot_position, buildings = identify_plot_and_buildings(
        make_frame([building_inside, twin])
    )
    assert plot_position == 0
    assert buildings == [1]


# --------------------------------------------------------------------------
# Task 3: classification
# --------------------------------------------------------------------------


def test_contained_building_is_already_inside(plot, building_inside, config):
    measurement = classify_building(building_inside, plot, config)
    assert measurement.verdict is Verdict.ALREADY_INSIDE
    assert measurement.outside_area == pytest.approx(0.0)


def test_micro_overhang_is_fixable(plot, building_micro_overhang, config):
    measurement = classify_building(building_micro_overhang, plot, config)
    assert measurement.verdict is Verdict.FIXABLE
    assert measurement.max_outward_distance == pytest.approx(0.0006, abs=1e-9)
    assert measurement.outside_area < 0.01


def test_centimetre_overhang_is_fixable(plot, building_cm_overhang, config):
    measurement = classify_building(building_cm_overhang, plot, config)
    assert measurement.verdict is Verdict.FIXABLE
    assert measurement.max_outward_distance == pytest.approx(0.04)


def test_half_outside_building_is_too_far(plot, building_far_outside, config):
    measurement = classify_building(building_far_outside, plot, config)
    assert measurement.verdict is Verdict.TOO_FAR
    assert measurement.outside_area == pytest.approx(50.0)


def test_fully_outside_building_is_too_far(plot, building_fully_outside, config):
    measurement = classify_building(building_fully_outside, plot, config)
    assert measurement.verdict is Verdict.TOO_FAR


def test_overhang_just_beyond_tolerance_is_too_far(plot, config):
    """6 cm out with a 5 cm tolerance must be refused, not quietly reshaped."""
    building = box(20.0, 20.0, 30.0, 30.06)
    measurement = classify_building(building, plot, config)
    assert measurement.verdict is Verdict.TOO_FAR


def test_invalid_self_intersecting_input_is_repaired_not_crashed(plot, config):
    bowtie = Polygon([(5, 5), (15, 15), (15, 5), (5, 15), (5, 5)])
    assert not bowtie.is_valid
    measurement = classify_building(bowtie, plot, config)
    assert measurement.verdict is Verdict.ALREADY_INSIDE


# --------------------------------------------------------------------------
# Task 4: snapping
# --------------------------------------------------------------------------


def test_snap_moves_only_the_offending_vertices(plot, building_micro_overhang, config):
    snapped, count, shift = snap_to_plot(
        building_micro_overhang, plot, config.snap_tolerance_m
    )
    assert count == 2
    assert shift == pytest.approx(0.0006, abs=1e-9)

    # Vertex count and ring closure are preserved: a rectangle stays a rectangle.
    assert len(snapped.exterior.coords) == len(building_micro_overhang.exterior.coords)

    boundary = plot.boundary
    for x, y in snapped.exterior.coords:
        assert Point(x, y).distance(boundary) < 1e-9 or plot.covers(Point(x, y))

    # The two bottom corners did not move.
    assert (20.0, 20.0) in list(snapped.exterior.coords)
    assert (30.0, 20.0) in list(snapped.exterior.coords)


def test_snap_lands_vertices_exactly_on_the_plot_edge(
    plot, building_micro_overhang, config
):
    snapped, _, _ = snap_to_plot(
        building_micro_overhang, plot, config.snap_tolerance_m
    )
    top_ys = [y for _, y in snapped.exterior.coords if y > 25.0]
    assert top_ys
    for y in top_ys:
        assert y == pytest.approx(30.0, abs=1e-9)


def test_snap_leaves_distant_vertices_alone(plot, building_far_outside):
    snapped, count, shift = snap_to_plot(building_far_outside, plot, 0.05)
    assert count == 0
    assert shift == 0.0
    assert snapped.equals(building_far_outside)


def test_snap_is_a_no_op_for_contained_buildings(plot, building_inside, config):
    snapped, count, _ = snap_to_plot(
        building_inside, plot, config.snap_tolerance_m
    )
    assert count == 0
    assert snapped.equals(building_inside)


def test_snap_produces_valid_geometry(plot, building_cm_overhang, config):
    snapped, count, _ = snap_to_plot(
        building_cm_overhang, plot, config.snap_tolerance_m
    )
    assert count == 2
    assert snapped.is_valid
    assert snapped.area > 0


# --------------------------------------------------------------------------
# Task 5: clipping and the combined fix
# --------------------------------------------------------------------------


def test_clip_removes_a_bulge_between_inside_corners(notched_plot, config):
    """All four corners are inside, yet an edge crosses the plot's notch."""
    building = box(15.0, 20.0, 25.0, 29.99)
    assert not building.difference(notched_plot).is_empty

    snapped, count, _ = snap_to_plot(building, notched_plot, config.snap_tolerance_m)
    assert count == 0  # snapping cannot help here

    clipped = clip_to_plot(snapped, notched_plot, config.min_part_area_m2)
    assert clipped.difference(notched_plot).area == pytest.approx(0.0, abs=1e-12)
    assert clipped.area < building.area


def test_clip_drops_dust_fragments():
    plot = MultiPolygon([box(0, 0, 10, 10), box(11, 0, 11.02, 10)])
    building = box(5, 2, 11.02, 4)
    clipped = clip_to_plot(building, plot, min_part_area=0.05)
    # The 0.04 m^2 sliver in the detached strip is dropped as dust.
    assert clipped.geom_type == "Polygon"
    assert clipped.area == pytest.approx(10.0)


def test_clip_keeps_multiple_genuine_parts():
    plot = MultiPolygon([box(0, 0, 10, 10), box(11, 0, 20, 10)])
    building = box(5, 2, 15, 4)
    clipped = clip_to_plot(building, plot, min_part_area=0.05)
    assert clipped.geom_type == "MultiPolygon"
    assert len(clipped.geoms) == 2


def test_clip_never_deletes_a_building_entirely():
    tiny_plot = box(0, 0, 0.1, 0.1)
    building = box(0, 0, 0.05, 0.05)
    clipped = clip_to_plot(building, tiny_plot, min_part_area=0.05)
    assert not clipped.is_empty
    assert clipped.area > 0


def test_fix_building_contains_the_micro_overhang(
    plot, building_micro_overhang, config
):
    fixed, record = fix_building(building_micro_overhang, plot, config)
    assert record.action is Action.FIXED
    assert record.vertices_snapped == 2
    assert record.max_vertex_shift == pytest.approx(0.0006, abs=1e-9)
    assert fixed.within(plot)
    assert fixed.difference(plot).area == pytest.approx(0.0, abs=1e-12)
    # The repair is proportionate: barely any area is lost.
    assert record.area_removed < 0.01


def test_fix_building_leaves_contained_buildings_untouched(
    plot, building_inside, config
):
    fixed, record = fix_building(building_inside, plot, config)
    assert record.action is Action.UNCHANGED
    assert record.area_removed == 0.0
    assert fixed.equals(building_inside)


def test_fix_building_flags_instead_of_clipping_a_misplaced_building(
    plot, building_far_outside, config
):
    fixed, record = fix_building(building_far_outside, plot, config)
    assert record.action is Action.FLAGGED
    assert record.warning is not None
    assert "too far" in record.warning
    # Crucially, the geometry is returned as-is rather than silently clipped.
    assert fixed.equals(building_far_outside)
    assert fixed.area == pytest.approx(building_far_outside.area)


def test_fix_building_reports_area_removed_for_a_clipped_bulge(notched_plot, config):
    building = box(15.0, 20.0, 25.0, 29.99)
    fixed, record = fix_building(building, notched_plot, config)
    assert record.action is Action.FIXED
    assert record.was_clipped
    assert record.area_removed == pytest.approx(building.area - fixed.area, abs=1e-12)
    assert fixed.within(notched_plot)


# --------------------------------------------------------------------------
# Task 6: the frame-level pipeline
# --------------------------------------------------------------------------


def test_autofix_frame_mixed_drawing(
    plot, building_inside, building_micro_overhang, building_far_outside
):
    frame = make_frame(
        [building_inside, building_micro_overhang, plot, building_far_outside],
        labels=["good", "sliver", "plot", "misplaced"],
    )
    fixed_frame, report = autofix_frame(frame)

    assert report.plot_row_index == 2
    assert report.building_count == 3
    assert len(report.fixed) == 1
    assert len(report.unchanged) == 1
    assert len(report.flagged) == 1
    assert report.fixed[0].row_index == 1
    assert report.flagged[0].row_index == 3

    # Nothing is dropped and attributes survive.
    assert len(fixed_frame) == len(frame)
    assert list(fixed_frame["Label"]) == ["good", "sliver", "plot", "misplaced"]
    assert fixed_frame.crs == frame.crs


def test_autofix_frame_never_modifies_the_plot(
    plot, building_micro_overhang
):
    frame = make_frame([building_micro_overhang, plot])
    fixed_frame, report = autofix_frame(frame)
    plot_geometry = fixed_frame.geometry.iloc[report.plot_row_index]
    assert plot_geometry.equals_exact(plot, tolerance=0.0)


def test_autofix_frame_result_is_contained(plot, building_micro_overhang):
    frame = make_frame([building_micro_overhang, plot])
    fixed_frame, report = autofix_frame(frame)
    plot_geometry = fixed_frame.geometry.iloc[report.plot_row_index]
    for position, geometry in enumerate(fixed_frame.geometry):
        if position == report.plot_row_index:
            continue
        assert geometry.difference(plot_geometry).area == pytest.approx(0.0, abs=1e-12)


def test_autofix_frame_is_idempotent(plot, building_micro_overhang):
    frame = make_frame([building_micro_overhang, plot])
    once, first_report = autofix_frame(frame)
    twice, second_report = autofix_frame(once)
    assert len(first_report.fixed) == 1
    assert len(second_report.fixed) == 0
    assert len(second_report.unchanged) == 1
    assert twice.geometry.iloc[0].equals(once.geometry.iloc[0])


def test_autofix_frame_with_only_a_plot_reports_no_buildings(plot):
    fixed_frame, report = autofix_frame(make_frame([plot]))
    assert report.building_count == 0
    assert report.fixes == []
    assert not report.changed_anything
    assert len(fixed_frame) == 1


def test_report_summary_mentions_corrections_and_warnings(
    plot, building_micro_overhang, building_far_outside
):
    frame = make_frame([building_micro_overhang, plot, building_far_outside])
    _, report = autofix_frame(frame)
    text = "\n".join(report.summary_lines())
    assert "Land plot" in text
    assert "Corrected: 1" in text
    assert "Not corrected: 1" in text


# --------------------------------------------------------------------------
# 3D input
#
# CAD drawings and Shapefiles routinely carry elevations, so coordinates arrive
# as (x, y, z). Unpacking them as pairs used to raise
# "ValueError: too many values to unpack".
# --------------------------------------------------------------------------


@pytest.fixture
def plot_3d() -> Polygon:
    return Polygon(
        [
            (0.0, 0.0, 12.5),
            (40.0, 0.0, 12.5),
            (40.0, 30.0, 12.5),
            (0.0, 30.0, 12.5),
            (0.0, 0.0, 12.5),
        ]
    )


@pytest.fixture
def building_3d_micro_overhang() -> Polygon:
    return Polygon(
        [
            (20.0, 20.0, 14.0),
            (30.0, 20.0, 14.0),
            (30.0, 30.0006, 14.0),
            (20.0, 30.0004, 14.0),
            (20.0, 20.0, 14.0),
        ]
    )


def test_classify_handles_three_dimensional_geometry(
    plot_3d, building_3d_micro_overhang, config
):
    assert building_3d_micro_overhang.has_z
    measurement = classify_building(building_3d_micro_overhang, plot_3d, config)
    assert measurement.verdict is Verdict.FIXABLE
    assert measurement.max_outward_distance == pytest.approx(0.0006, abs=1e-9)


def test_snap_preserves_elevation_values(
    plot_3d, building_3d_micro_overhang, config
):
    snapped, count, shift = snap_to_plot(
        building_3d_micro_overhang, plot_3d, config.snap_tolerance_m
    )
    assert count == 2
    assert shift == pytest.approx(0.0006, abs=1e-9)
    assert snapped.has_z
    # Elevations ride along untouched; only the plan position moves.
    assert {round(c[2], 6) for c in snapped.exterior.coords} == {14.0}


def test_autofix_frame_repairs_three_dimensional_drawing(
    plot_3d, building_3d_micro_overhang
):
    frame = make_frame([building_3d_micro_overhang, plot_3d])
    fixed_frame, report = autofix_frame(frame)
    assert len(report.fixed) == 1
    assert verify_containment(fixed_frame) == []
    # Snapping alone was enough, so the drawing stays 3D.
    assert not report.z_was_dropped
    assert all(geometry.has_z for geometry in fixed_frame.geometry)


def test_clipping_keeps_elevations(notched_plot):
    """GEOS carries Z through an overlay, interpolating on new vertices.

    Verified against GEOS 3.11.4 as bundled with Shapely 2.0.6: clipping a
    building at Z=11 against a plot at Z=10 yields Z=10.5 on the vertices the
    clip creates. Elevations therefore survive a clip and the layer stays 3D.
    """
    plot_3d = Polygon([(x, y, 10.0) for x, y in notched_plot.exterior.coords])
    building = Polygon(
        [
            (15.0, 20.0, 11.0),
            (25.0, 20.0, 11.0),
            (25.0, 29.99, 11.0),
            (15.0, 29.99, 11.0),
            (15.0, 20.0, 11.0),
        ]
    )
    frame = make_frame([building, plot_3d])
    fixed_frame, report = autofix_frame(frame)

    assert len(report.fixed) == 1
    assert report.fixed[0].was_clipped
    assert not report.z_was_dropped
    assert all(geometry.has_z for geometry in fixed_frame.geometry)
    assert verify_containment(fixed_frame) == []


def test_mixed_two_and_three_dimensional_rows_are_flattened(
    plot_3d, building_inside
):
    """A 2D row beside a 3D one would make the Shapefile layer inconsistent."""
    frame = make_frame([building_inside, plot_3d])
    assert not building_inside.has_z and plot_3d.has_z

    fixed_frame, report = autofix_frame(frame)
    assert report.z_was_dropped
    assert not any(geometry.has_z for geometry in fixed_frame.geometry)
    assert "Z" in "\n".join(report.summary_lines())


def test_nan_elevations_are_flattened_rather_than_exported(plot_3d):
    """NaN Z from overlaying 3D with 2D geometry must not reach the output."""
    building_2d_overhang = Polygon(
        [
            (20.0, 20.0),
            (30.0, 20.0),
            (30.0, 30.0006),
            (20.0, 30.0004),
            (20.0, 20.0),
        ]
    )
    frame = make_frame([building_2d_overhang, plot_3d])
    fixed_frame, report = autofix_frame(frame)

    assert len(report.fixed) == 1
    assert report.z_was_dropped
    for geometry in fixed_frame.geometry:
        assert not geometry.has_z
    assert verify_containment(fixed_frame) == []


# --------------------------------------------------------------------------
# Containment verification and the finishing configuration
# --------------------------------------------------------------------------


def test_finishing_config_is_far_tighter_than_the_main_pass() -> None:
    """The post-reprojection pass must only absorb projection chord error."""
    finishing = AutofixConfig.finishing()
    main = AutofixConfig()
    assert finishing.snap_tolerance_m < main.snap_tolerance_m
    assert finishing.max_outside_area_m2 < main.max_outside_area_m2
    assert finishing.max_outside_area_ratio < main.max_outside_area_ratio


def test_finishing_config_cannot_reshape_a_real_defect(plot, config) -> None:
    """A 4 cm overhang is fixable normally but must be refused when finishing."""
    building = box(20.0, 20.0, 30.0, 30.04)
    assert classify_building(building, plot, config).verdict is Verdict.FIXABLE
    finishing = classify_building(building, plot, AutofixConfig.finishing())
    assert finishing.verdict is Verdict.TOO_FAR


def test_verify_containment_reports_nothing_for_a_clean_drawing(
    plot, building_inside
):
    frame = make_frame([building_inside, plot])
    assert verify_containment(frame) == []


def test_verify_containment_reports_the_offending_row(plot, building_micro_overhang):
    frame = make_frame([building_micro_overhang, plot])
    violations = verify_containment(frame)
    assert len(violations) == 1
    row_index, area = violations[0]
    assert row_index == 0
    assert area == pytest.approx(0.005, abs=0.005)


def test_verify_containment_passes_after_autofix(plot, building_micro_overhang):
    frame = make_frame([building_micro_overhang, plot])
    fixed_frame, _ = autofix_frame(frame)
    assert verify_containment(fixed_frame) == []


# --------------------------------------------------------------------------
# Integration: the real drawing that fails cadastral validation
# --------------------------------------------------------------------------

REAL_DXF = r"C:\Users\ozodq\Downloads\fortestingoverlap.dxf"


@pytest.mark.skipif(
    not __import__("pathlib").Path(REAL_DXF).is_file(),
    reason="the sample drawing is not available on this machine",
)
def test_real_drawing_is_repaired():
    import bot

    frame = bot._read_dxf(__import__("pathlib").Path(REAL_DXF))
    frame = bot._convert_lines_to_polygons(frame)
    frame = frame[frame.geometry.geom_type.isin({"Polygon", "MultiPolygon"})]
    frame = frame.reset_index(drop=True)

    fixed_frame, report = autofix_frame(frame)

    assert report.building_count == 1
    assert len(report.fixed) == 1, report.summary_lines()

    fix = report.fixed[0]
    # The defect measured in this drawing: 0.005241 m^2 protruding, 0.591 mm deep.
    assert fix.outside_area_before == pytest.approx(0.005241, abs=1e-5)
    assert fix.max_vertex_shift == pytest.approx(0.000591, abs=1e-5)
    assert fix.outside_area_after == pytest.approx(0.0, abs=1e-9)

    plot_geometry = fixed_frame.geometry.iloc[report.plot_row_index]
    building = fixed_frame.geometry.iloc[1 - report.plot_row_index]
    assert building.difference(plot_geometry).area == pytest.approx(0.0, abs=1e-12)
    # The building keeps essentially all of its area.
    assert fix.area_removed < 0.01
