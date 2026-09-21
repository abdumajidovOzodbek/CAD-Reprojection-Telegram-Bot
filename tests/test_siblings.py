"""Tests for separating buildings of the same parcel that overlap each other.

Both buildings are ours, so the rule is by agreement rather than by necessity:
the larger footprint yields and the smaller keeps its shape. The land plot is
never modified by this pass.
"""

import itertools
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Polygon, box

import autofix
import bot
import run_local
from autofix import (
    Action,
    AutofixConfig,
    identify_plot_and_buildings,
    resolve_sibling_overlaps,
    verify_containment,
    verify_no_sibling_overlap,
)

CRS = (
    "+proj=tmerc +lat_0=0 +lon_0=69 +k=1 +x_0=-182.2698 +y_0=-4500719.7668 "
    "+ellps=krass +units=m +no_defs"
)

REAL_ZIP = Path(r"C:\Users\ozodq\Downloads\overlapped buildings.zip")


@pytest.fixture
def config() -> AutofixConfig:
    return AutofixConfig()


@pytest.fixture
def plot() -> Polygon:
    return box(0.0, 0.0, 60.0, 60.0)


def make_frame(geometries, labels=None) -> gpd.GeoDataFrame:
    labels = labels or [f"row{i}" for i in range(len(geometries))]
    return gpd.GeoDataFrame(
        {"Layer": ["0"] * len(geometries), "Label": labels},
        geometry=list(geometries),
        crs=CRS,
    )


# --------------------------------------------------------------------------
# Who yields
# --------------------------------------------------------------------------


def test_the_larger_building_is_the_one_trimmed(config, plot):
    small = box(10.0, 10.0, 15.0, 15.0)  # 25 m²
    large = box(14.9, 10.0, 30.0, 25.0)  # ~226 m², overlaps by 10 cm
    frame = make_frame([small, large, plot], labels=["small", "large", "plot"])

    cleared, report = resolve_sibling_overlaps(frame, config)

    assert len(report.fixed) == 1
    pair = report.fixed[0]
    assert pair.trimmed_row == 1  # the large one
    assert pair.kept_row == 0  # the small one
    assert pair.max_width == pytest.approx(0.1, abs=1e-3)

    # The small building is returned exactly as it arrived.
    assert cleared.geometry.iloc[0].equals_exact(small, 0.0)
    assert cleared.geometry.iloc[1].area < large.area
    assert verify_no_sibling_overlap(cleared) == []


def test_row_order_does_not_change_the_outcome(config, plot):
    small = box(10.0, 10.0, 15.0, 15.0)
    large = box(14.9, 10.0, 30.0, 25.0)

    forwards, _ = resolve_sibling_overlaps(
        make_frame([small, large, plot]), config
    )
    backwards, _ = resolve_sibling_overlaps(
        make_frame([large, small, plot]), config
    )

    # Whichever order the rows arrive in, the small one survives untouched.
    assert forwards.geometry.iloc[0].equals_exact(small, 0.0)
    assert backwards.geometry.iloc[1].equals_exact(small, 0.0)
    assert forwards.geometry.iloc[1].area == pytest.approx(
        backwards.geometry.iloc[0].area
    )


def test_equal_areas_resolve_by_row_index(config, plot):
    """A tie must not depend on dictionary or sort instability."""
    first = box(10.0, 10.0, 20.0, 20.0)
    second = box(19.95, 10.0, 29.95, 20.0)
    assert first.area == pytest.approx(second.area)

    _, report = resolve_sibling_overlaps(make_frame([first, second, plot]), config)
    assert len(report.fixed) == 1
    assert report.fixed[0].kept_row == 0
    assert report.fixed[0].trimmed_row == 1


def test_the_plot_is_never_touched_by_this_pass(config, plot):
    small = box(10.0, 10.0, 15.0, 15.0)
    large = box(14.9, 10.0, 30.0, 25.0)
    frame = make_frame([small, large, plot])

    cleared, report = resolve_sibling_overlaps(frame, config)
    assert report.plot_row_index == 2
    assert cleared.geometry.iloc[2].equals_exact(plot, 0.0)


def test_trimming_keeps_buildings_inside_the_plot(config, plot):
    """Subtraction only removes area, so containment cannot be broken."""
    small = box(10.0, 10.0, 15.0, 15.0)
    large = box(14.9, 10.0, 30.0, 25.0)
    frame = make_frame([small, large, plot])

    cleared, _ = resolve_sibling_overlaps(frame, config)
    assert verify_containment(cleared) == []


# --------------------------------------------------------------------------
# What is left alone
# --------------------------------------------------------------------------


def test_touching_buildings_are_not_changed(config, plot):
    """Sharing a wall exactly is correct data, not a defect."""
    left = box(10.0, 10.0, 20.0, 20.0)
    right = box(20.0, 10.0, 30.0, 20.0)
    assert left.touches(right)

    cleared, report = resolve_sibling_overlaps(
        make_frame([left, right, plot]), config
    )
    assert report.pairs == []
    assert not report.changed_anything
    for before, after in zip([left, right, plot], cleared.geometry):
        assert after.equals_exact(before, 0.0)


def test_separated_buildings_are_not_changed(config, plot):
    first = box(10.0, 10.0, 20.0, 20.0)
    second = box(30.0, 30.0, 40.0, 40.0)
    _, report = resolve_sibling_overlaps(make_frame([first, second, plot]), config)
    assert report.pairs == []


def test_a_gross_overlap_is_flagged_not_trimmed(config, plot):
    """Half of one building inside another is a wrong footprint, not a slip."""
    small = box(10.0, 10.0, 20.0, 20.0)
    large = box(15.0, 10.0, 35.0, 30.0)
    frame = make_frame([small, large, plot])

    cleared, report = resolve_sibling_overlaps(frame, config)
    assert report.fixed == []
    assert len(report.flagged) == 1
    assert "one of the two footprints is likely wrong" in report.flagged[0].warning
    for before, after in zip([small, large, plot], cleared.geometry):
        assert after.equals_exact(before, 0.0)


def test_the_sibling_tolerance_is_looser_than_the_plot_tolerance(config):
    """A 10 cm overlap is fixable between buildings, but not against a plot."""
    assert config.sibling_tolerance_m > config.snap_tolerance_m
    assert config.for_siblings().snap_tolerance_m == config.sibling_tolerance_m
    # Other limits are carried over untouched.
    assert config.for_siblings().min_part_area_m2 == config.min_part_area_m2


def test_sibling_tolerance_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOFIX_SIBLING_TOLERANCE_M", "0.3")
    assert AutofixConfig.from_env().sibling_tolerance_m == 0.3


def test_a_ten_centimetre_overlap_needs_the_looser_tolerance(plot):
    """Pins down why the tolerance was split in two.

    The real file overlaps by 10.14 cm, twice the 5 cm plot tolerance. Under the
    strict limit this would be reported instead of repaired.
    """
    small = box(10.0, 10.0, 15.0, 15.0)
    large = box(14.9, 10.0, 30.0, 25.0)
    frame = make_frame([small, large, plot])

    strict = AutofixConfig(sibling_tolerance_m=0.05)
    _, strict_report = resolve_sibling_overlaps(frame, strict)
    assert len(strict_report.flagged) == 1

    lenient = AutofixConfig(sibling_tolerance_m=0.15)
    _, lenient_report = resolve_sibling_overlaps(frame, lenient)
    assert len(lenient_report.fixed) == 1


# --------------------------------------------------------------------------
# Chains of overlaps
# --------------------------------------------------------------------------


def test_one_building_overlapping_two_others(config, plot):
    """The largest absorbs both corrections, the two smaller stay intact."""
    small_a = box(10.0, 10.0, 14.0, 14.0)
    small_b = box(10.0, 20.0, 14.0, 24.0)
    large = box(13.9, 5.0, 30.0, 30.0)
    frame = make_frame([small_a, small_b, large, plot])

    cleared, report = resolve_sibling_overlaps(frame, config)

    assert len(report.fixed) == 2
    assert {pair.trimmed_row for pair in report.fixed} == {2}
    assert cleared.geometry.iloc[0].equals_exact(small_a, 0.0)
    assert cleared.geometry.iloc[1].equals_exact(small_b, 0.0)
    assert verify_no_sibling_overlap(cleared) == []


def test_is_idempotent(config, plot):
    small = box(10.0, 10.0, 15.0, 15.0)
    large = box(14.9, 10.0, 30.0, 25.0)
    frame = make_frame([small, large, plot])

    once, first_report = resolve_sibling_overlaps(frame, config)
    twice, second_report = resolve_sibling_overlaps(once, config)

    assert len(first_report.fixed) == 1
    assert second_report.pairs == []
    assert twice.geometry.iloc[1].equals_exact(once.geometry.iloc[1], 0.0)


def test_report_summary_names_both_buildings(config, plot):
    small = box(10.0, 10.0, 15.0, 15.0)
    large = box(14.9, 10.0, 30.0, 25.0)
    _, report = resolve_sibling_overlaps(make_frame([small, large, plot]), config)
    text = "\n".join(report.summary_lines())
    assert "Separated buildings" in text
    assert "cm" in text


# --------------------------------------------------------------------------
# End to end on the real file
# --------------------------------------------------------------------------


@pytest.mark.skipif(not REAL_ZIP.is_file(), reason="sample parcel unavailable")
def test_real_overlapping_buildings_are_separated(tmp_path: Path) -> None:
    result = run_local.convert_with_autofix(
        REAL_ZIP,
        tmp_path,
        checks=frozenset({run_local.CHECK_SIBLINGS, run_local.CHECK_CONTAINMENT}),
    )

    report = result.sibling_report
    assert report is not None
    assert report.building_count == 4

    # Measured from the file: two genuine overlaps, 10.14 cm and 5.06 cm wide,
    # plus one pair that merely touches and must be left alone.
    assert len(report.fixed) == 2
    assert report.flagged == []
    widths = sorted(pair.max_width for pair in report.fixed)
    assert widths[0] == pytest.approx(0.0506, abs=5e-3)
    assert widths[1] == pytest.approx(0.1014, abs=5e-3)

    # In each pair the larger building yielded.
    assert result.sibling_violations == []
    assert result.residual_violations == []
    assert result.feature_count == 5

    caption = run_local.build_caption(result)
    assert "Buildings against each other" in caption
    assert "Separated buildings" in caption


@pytest.mark.skipif(not REAL_ZIP.is_file(), reason="sample parcel unavailable")
def test_real_file_smaller_buildings_and_plot_are_untouched(tmp_path: Path) -> None:
    """Only the two larger buildings of each pair may differ from the input."""
    before = run_local._load_drawing_frame(REAL_ZIP, tmp_path / "before")
    plot_position, building_positions = identify_plot_and_buildings(before)

    result = run_local.convert_with_autofix(
        REAL_ZIP,
        tmp_path,
        checks=frozenset({run_local.CHECK_SIBLINGS, run_local.CHECK_CONTAINMENT}),
    )
    after = run_local._load_drawing_frame(result.archive_path, tmp_path / "after")

    trimmed_rows = {pair.trimmed_row for pair in result.sibling_report.fixed}
    assert trimmed_rows, "nothing was trimmed, so this test proves nothing"

    for position in [plot_position, *building_positions]:
        original = before.geometry.iloc[position]
        exported = after.geometry.iloc[position]
        if position in trimmed_rows:
            assert not exported.equals_exact(original, 0.0)
            assert exported.area < original.area
        else:
            assert exported.equals_exact(original, 0.0), (
                f"row {position} changed but should not have"
            )


@pytest.mark.skipif(not REAL_ZIP.is_file(), reason="sample parcel unavailable")
def test_real_file_untouched_when_the_check_is_not_selected(tmp_path: Path) -> None:
    """The other two menu options must not silently reshape buildings."""
    result = run_local.convert_with_autofix(
        REAL_ZIP, tmp_path, checks=frozenset({run_local.CHECK_CONTAINMENT})
    )
    assert result.sibling_report is None
    assert result.sibling_violations == []

    before = run_local._load_drawing_frame(REAL_ZIP, tmp_path / "before")
    after = run_local._load_drawing_frame(result.archive_path, tmp_path / "after")
    for original, exported in zip(before.geometry, after.geometry):
        assert exported.equals_exact(original, 0.0)


@pytest.mark.skipif(not REAL_ZIP.is_file(), reason="sample parcel unavailable")
def test_real_file_overlaps_really_exist_first(tmp_path: Path) -> None:
    """Guard: if the sample stops overlapping, the tests above mean nothing."""
    frame = run_local._load_drawing_frame(REAL_ZIP, tmp_path).to_crs(bot.SOURCE_CRS)
    _, buildings = identify_plot_and_buildings(frame)
    overlapping = [
        (a, b)
        for a, b in itertools.combinations(buildings, 2)
        if frame.geometry.iloc[a].intersection(frame.geometry.iloc[b]).area > 1e-6
    ]
    assert len(overlapping) == 2
