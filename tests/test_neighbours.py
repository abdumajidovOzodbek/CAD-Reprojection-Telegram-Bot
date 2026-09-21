"""Tests for clearing our parcel off registered neighbouring land.

Our land plot and our buildings may both be trimmed. The neighbour data is
immutable: it belongs to parcels already accepted into the system.
"""

import zipfile
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.ops import unary_union

import autofix
import bot
import run_local
from autofix import (
    Action,
    AutofixConfig,
    Verdict,
    build_obstacles,
    classify_against_obstacles,
    clear_of_obstacles,
    resolve_neighbour_conflicts,
    subtract_obstacles,
    verify_containment,
    verify_no_overlap,
)

CRS = (
    "+proj=tmerc +lat_0=0 +lon_0=69 +k=1 +x_0=-182.2698 +y_0=-4500719.7668 "
    "+ellps=krass +units=m +no_defs"
)

# The user's real files: our parcel, and the parcels already registered.
CURRENT_ZIP = Path(
    r"C:\Users\ozodq\Downloads\Telegram Desktop\shablon_sheyp_fixed (2).zip"
)
NEIGHBOURS_ZIP = Path(
    r"C:\Users\ozodq\Downloads\houses that are already on the system.zip"
)
HAVE_REAL_FILES = CURRENT_ZIP.is_file() and NEIGHBOURS_ZIP.is_file()


@pytest.fixture
def config() -> AutofixConfig:
    return AutofixConfig()


@pytest.fixture
def neighbour_plot() -> Polygon:
    """Registered parcel immediately north of ours."""
    return box(0.0, 30.0, 40.0, 60.0)


@pytest.fixture
def our_plot_overlapping() -> Polygon:
    """Our plot crosses 2 cm into the neighbour along the shared edge."""
    return box(0.0, 0.0, 40.0, 30.02)


def make_frame(geometries, labels=None) -> gpd.GeoDataFrame:
    labels = labels or [f"row{i}" for i in range(len(geometries))]
    return gpd.GeoDataFrame(
        {"Layer": ["0"] * len(geometries), "Label": labels},
        geometry=list(geometries),
        crs=CRS,
    )


# --------------------------------------------------------------------------
# Measuring an overlap
# --------------------------------------------------------------------------


def test_penetration_is_measured_as_width_not_area(config, neighbour_plot):
    """A long hairline overlap must not be judged by its area.

    Area grows with the length of the shared edge and says nothing about
    severity: a 40 m boundary out by 2 cm covers more ground than a deliberate
    0.5 m encroachment over 1 m. Width is what distinguishes them.
    """
    hairline = box(0.0, 0.0, 40.0, 30.02)  # 40 m long, 2 cm deep
    encroachment = box(10.0, 0.0, 11.0, 30.5)  # 1 m long, 50 cm deep

    hairline_measure = classify_against_obstacles(hairline, neighbour_plot, config)
    encroach_measure = classify_against_obstacles(encroachment, neighbour_plot, config)

    assert hairline_measure.outside_area > encroach_measure.outside_area
    assert hairline_measure.max_outward_distance == pytest.approx(0.02, abs=1e-3)
    assert encroach_measure.max_outward_distance == pytest.approx(0.5, abs=1e-2)
    assert hairline_measure.verdict is Verdict.FIXABLE
    assert encroach_measure.verdict is Verdict.TOO_FAR


def test_no_overlap_classifies_as_clear(config, neighbour_plot):
    ours = box(0.0, 0.0, 40.0, 30.0)  # shares the edge, does not cross it
    measurement = classify_against_obstacles(ours, neighbour_plot, config)
    assert measurement.verdict is Verdict.ALREADY_INSIDE
    assert measurement.max_outward_distance == 0.0


# --------------------------------------------------------------------------
# Snapping out and subtracting
# --------------------------------------------------------------------------


def test_only_the_shared_land_is_given_up(config, neighbour_plot):
    """The trim removes the overlap and not a square centimetre more.

    Snapping vertices out of the obstacle first, as the in-plot repair does,
    was measured on the real parcels to remove fifteen times the overlapping
    area: moving a vertex drags its adjacent edges along and sweeps away land
    that was never in conflict. Subtraction is exact, so that is what runs here.
    """
    ours = box(5.0, 0.0, 35.0, 30.02)
    overlap = ours.intersection(neighbour_plot).area

    cleared, fix = clear_of_obstacles(ours, neighbour_plot, config)
    assert fix.action is Action.FIXED
    assert fix.area_removed == pytest.approx(overlap, rel=1e-12)
    assert cleared.intersection(neighbour_plot).area == pytest.approx(0.0, abs=1e-12)


def test_subtract_obstacles_creates_a_shared_boundary(
    neighbour_plot, our_plot_overlapping
):
    cleared = subtract_obstacles(our_plot_overlapping, neighbour_plot, 0.05)
    assert cleared.intersection(neighbour_plot).area == pytest.approx(0.0, abs=1e-12)
    assert cleared.touches(neighbour_plot)


def test_subtract_obstacles_never_deletes_a_feature(config):
    """A feature entirely on the neighbour's land is refused, not erased.

    Subtracting would leave nothing at all. Deleting a building because it
    landed on the wrong parcel would hide the problem instead of surfacing it,
    so the geometry is returned intact and the classifier flags it.
    """
    obstacle = box(0.0, 0.0, 10.0, 10.0)
    ours = box(1.0, 1.0, 2.0, 2.0)

    kept = subtract_obstacles(ours, obstacle, min_part_area=0.05)
    assert kept.equals(ours)

    cleared, fix = clear_of_obstacles(ours, obstacle, config)
    assert fix.action is Action.FLAGGED
    assert cleared.equals(ours)


def test_clear_of_obstacles_flags_a_real_encroachment(config, neighbour_plot):
    ours = box(0.0, 0.0, 40.0, 32.0)  # 2 m into the neighbour
    cleared, fix = clear_of_obstacles(ours, neighbour_plot, config, is_plot=True)
    assert fix.action is Action.FLAGGED
    assert "surveyor" in fix.warning
    assert cleared.equals(ours)  # untouched


def test_clear_of_obstacles_reports_the_area_given_up(
    config, neighbour_plot, our_plot_overlapping
):
    cleared, fix = clear_of_obstacles(
        our_plot_overlapping, neighbour_plot, config, is_plot=True
    )
    assert fix.action is Action.FIXED
    assert fix.is_plot
    assert fix.area_removed == pytest.approx(0.8, abs=1e-9)  # 40 m x 2 cm
    assert fix.area_after == pytest.approx(cleared.area)
    assert fix.overlap_area_after == pytest.approx(0.0, abs=1e-12)
    # Parcels sharing a full edge: the corners sit on the neighbour's sides, so
    # only subtraction can resolve this. It leaves the boundaries coincident.
    assert cleared.touches(neighbour_plot)


# --------------------------------------------------------------------------
# Building the obstacle set
# --------------------------------------------------------------------------


def test_obstacles_merge_plots_and_buildings(neighbour_plot):
    """Registered data can be self-inconsistent, so both are merged.

    In the real sample a neighbour's building sits 0.00098 m² outside its own
    plot. Merging plots and buildings means that stray fringe is still treated
    as occupied land.
    """
    stray_building = box(39.5, 29.5, 41.0, 31.0)  # crosses its own plot edge
    frame = make_frame([neighbour_plot, stray_building])
    obstacles = build_obstacles(frame)
    assert obstacles.area == pytest.approx(
        unary_union([neighbour_plot, stray_building]).area
    )
    assert obstacles.covers(box(39.6, 29.6, 39.9, 29.9))


def test_obstacles_reject_a_file_without_polygons():
    from shapely.geometry import LineString

    frame = make_frame([LineString([(0, 0), (1, 1)])])
    with pytest.raises(autofix.AutofixError, match="no polygons"):
        build_obstacles(frame)


# --------------------------------------------------------------------------
# Frame level
# --------------------------------------------------------------------------


def test_resolve_trims_both_plot_and_buildings(config, neighbour_plot):
    our_plot = box(0.0, 0.0, 40.0, 30.02)
    our_building = box(10.0, 25.0, 20.0, 30.01)
    frame = make_frame([our_building, our_plot], labels=["building", "plot"])

    cleared, report = resolve_neighbour_conflicts(
        frame, neighbour_plot, config, obstacle_count=1
    )

    assert report.plot_row_index == 1
    assert len(report.fixed) == 2
    assert report.plot_fix is not None and report.plot_fix.is_plot
    assert verify_no_overlap(cleared, neighbour_plot) == []
    assert list(cleared["Label"]) == ["building", "plot"]


def test_resolve_leaves_a_clear_parcel_alone(config, neighbour_plot):
    our_plot = box(0.0, 0.0, 40.0, 30.0)
    our_building = box(10.0, 20.0, 20.0, 28.0)
    frame = make_frame([our_building, our_plot])

    cleared, report = resolve_neighbour_conflicts(frame, neighbour_plot, config)
    assert report.fixed == []
    assert not report.changed_anything
    for before, after in zip(frame.geometry, cleared.geometry):
        assert after.equals_exact(before, 0.0)


def test_trimming_the_plot_pushes_a_building_outside_it(config, neighbour_plot):
    """Why the containment pass has to run again after the neighbour pass.

    The building starts inside our plot. Trimming the plot off the neighbour
    moves the plot's edge inwards, and the building is then outside it.
    """
    our_plot = box(0.0, 0.0, 40.0, 30.02)
    our_building = box(10.0, 25.0, 20.0, 30.02)
    frame = make_frame([our_building, our_plot])
    assert our_building.within(our_plot)

    cleared, _ = resolve_neighbour_conflicts(frame, neighbour_plot, config)
    trimmed_plot = cleared.geometry.iloc[1]
    trimmed_building = cleared.geometry.iloc[0]
    assert trimmed_building.difference(trimmed_plot).area == pytest.approx(
        0.0, abs=1e-12
    )

    # And the full pipeline order leaves both invariants satisfied.
    finished, _ = autofix.autofix_frame(cleared, config)
    assert verify_containment(finished) == []
    assert verify_no_overlap(finished, neighbour_plot) == []


def test_report_summary_states_the_plot_area_change(config, neighbour_plot):
    our_plot = box(0.0, 0.0, 40.0, 30.02)
    our_building = box(10.0, 20.0, 20.0, 28.0)
    frame = make_frame([our_building, our_plot])
    _, report = resolve_neighbour_conflicts(
        frame, neighbour_plot, config, obstacle_count=1
    )
    text = "\n".join(report.summary_lines())
    assert "Land plot trimmed" in text
    assert "1,200.000" in text or "1200.000" in text  # area before


# --------------------------------------------------------------------------
# End to end on the user's real files
# --------------------------------------------------------------------------


def _expected_overlap(work: Path) -> tuple[float, float]:
    """Our plot's area and how much of it sits on neighbouring land.

    Derived from the files rather than hardcoded: the user updates these
    exports, and a test that pins yesterday's numbers fails for the wrong
    reason. It went from 2 neighbour polygons to 3 mid-development.
    """
    ours = run_local._load_drawing_frame(CURRENT_ZIP, work / "ours").to_crs(
        bot.SOURCE_CRS
    )
    theirs = run_local._load_drawing_frame(NEIGHBOURS_ZIP, work / "theirs").to_crs(
        bot.SOURCE_CRS
    )
    plot = ours.geometry.iloc[int(ours.geometry.area.idxmax())]
    obstacles = build_obstacles(theirs)
    return plot.area, plot.intersection(obstacles).area


@pytest.mark.skipif(not HAVE_REAL_FILES, reason="sample parcels unavailable")
def test_real_parcel_is_cleared_of_real_neighbours(tmp_path: Path) -> None:
    plot_area, overlap_area = _expected_overlap(tmp_path / "expected")
    assert overlap_area > 0, "the sample files no longer overlap; test is moot"

    result = run_local.convert_with_autofix(
        CURRENT_ZIP, tmp_path, neighbours_path=NEIGHBOURS_ZIP
    )

    report = result.neighbour_report
    assert report is not None
    assert report.obstacle_count >= 2

    plot_fix = report.plot_fix
    assert plot_fix is not None
    assert plot_fix.action is Action.FIXED
    assert plot_fix.area_before == pytest.approx(plot_area, abs=1e-6)
    # The plot gives up exactly the land it was sharing, no more.
    assert plot_fix.area_removed == pytest.approx(overlap_area, rel=1e-3)
    assert plot_fix.area_after < plot_fix.area_before

    assert len([fix for fix in report.fixed if not fix.is_plot]) == 2
    assert report.flagged == []

    # Both invariants hold in the delivered file.
    assert result.residual_violations == []
    assert result.neighbour_violations == []

    # Only our three polygons are exported; neighbour data is never written.
    assert result.feature_count == 3
    with zipfile.ZipFile(result.archive_path) as archive:
        names = archive.namelist()
    assert not any("already" in name.lower() for name in names)

    caption = run_local.build_caption(result)
    assert "Neighbouring parcels" in caption
    assert "Land plot trimmed" in caption


@pytest.mark.skipif(not HAVE_REAL_FILES, reason="sample parcels unavailable")
def test_real_neighbour_file_is_not_modified(tmp_path: Path) -> None:
    """The immutable file must be untouched on disk and absent from the output."""
    before = NEIGHBOURS_ZIP.read_bytes()
    run_local.convert_with_autofix(
        CURRENT_ZIP, tmp_path, neighbours_path=NEIGHBOURS_ZIP
    )
    assert NEIGHBOURS_ZIP.read_bytes() == before


@pytest.mark.skipif(not HAVE_REAL_FILES, reason="sample parcels unavailable")
def test_real_parcel_without_neighbours_still_works(tmp_path: Path) -> None:
    """Skipping the neighbours file falls back to the plot-containment repair."""
    result = run_local.convert_with_autofix(CURRENT_ZIP, tmp_path)
    assert result.neighbour_report is None
    assert result.neighbour_violations == []
    assert result.residual_violations == []
