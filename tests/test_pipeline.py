"""End-to-end tests for the /fix conversion pipeline.

These exercise the same code path the Telegram handler uses, minus Telegram
itself: read a drawing, repair the buildings, export a Shapefile ZIP.
"""

import zipfile
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Polygon, box

import bot
import run_local

REAL_DXF = Path(r"C:\Users\ozodq\Downloads\fortestingoverlap.dxf")

# Coordinates near the real drawing, so the projected units and magnitudes are
# realistic rather than toy values around the origin.
ORIGIN_X = 19170.0
ORIGIN_Y = 75040.0


def _plot() -> Polygon:
    return box(ORIGIN_X, ORIGIN_Y, ORIGIN_X + 40.0, ORIGIN_Y + 30.0)


def _building_with_micro_overhang() -> Polygon:
    """Two corners 0.6 mm and 0.4 mm beyond the plot's top edge."""
    top = ORIGIN_Y + 30.0
    return Polygon(
        [
            (ORIGIN_X + 10.0, ORIGIN_Y + 20.0),
            (ORIGIN_X + 20.0, ORIGIN_Y + 20.0),
            (ORIGIN_X + 20.0, top + 0.0006),
            (ORIGIN_X + 10.0, top + 0.0004),
            (ORIGIN_X + 10.0, ORIGIN_Y + 20.0),
        ]
    )


def _read_archive(archive_path: Path, extract_to: Path) -> gpd.GeoDataFrame:
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(extract_to)
    shapefiles = sorted(extract_to.glob("*.shp"))
    assert shapefiles, "the archive contains no Shapefile"
    frames = [gpd.read_file(path) for path in shapefiles]
    import pandas as pd

    return gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True),
        geometry=frames[0].geometry.name,
        crs=frames[0].crs,
    )


def _assert_buildings_are_contained(frame: gpd.GeoDataFrame) -> None:
    areas = frame.geometry.area
    plot = frame.geometry.iloc[int(areas.idxmax())]
    for position, geometry in enumerate(frame.geometry):
        if position == int(areas.idxmax()):
            continue
        outside = geometry.difference(plot).area
        assert outside == pytest.approx(0.0, abs=1e-9), (
            f"building {position} still protrudes by {outside} m²"
        )


# ---------------------------------------------------------------------------
# DXF input
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not REAL_DXF.is_file(), reason="sample drawing unavailable")
def test_real_dxf_produces_a_repaired_shapefile_zip(tmp_path: Path) -> None:
    result = run_local.convert_with_autofix(REAL_DXF, tmp_path)

    assert result.archive_path.is_file()
    assert result.feature_count == 2  # the plot plus one building
    assert len(result.report.fixed) == 1
    assert result.report.flagged == []

    with zipfile.ZipFile(result.archive_path) as archive:
        suffixes = {Path(name).suffix.lower() for name in archive.namelist()}
    assert {".shp", ".shx", ".dbf", ".prj"} <= suffixes

    exported = _read_archive(result.archive_path, tmp_path / "out")
    assert exported.crs.to_epsg() == 3857
    assert result.residual_violations == []
    _assert_buildings_are_contained(exported)


@pytest.mark.skipif(not REAL_DXF.is_file(), reason="sample drawing unavailable")
def test_caption_reports_the_correction(tmp_path: Path) -> None:
    result = run_local.convert_with_autofix(REAL_DXF, tmp_path)
    caption = run_local.build_caption(result)
    assert "Corrected: 1" in caption
    assert "852.49 m²" in caption  # the land plot area, unchanged
    assert bot.TARGET_CRS in caption


# ---------------------------------------------------------------------------
# Shapefile ZIP input
# ---------------------------------------------------------------------------


def _write_shapefile_zip(destination: Path, work: Path) -> Path:
    """Create a Shapefile ZIP in EPSG:3857, as the bot's own output would be."""
    frame = gpd.GeoDataFrame(
        {"Label": ["building", "plot"]},
        geometry=[_building_with_micro_overhang(), _plot()],
        crs=bot.SOURCE_CRS,
    ).to_crs(bot.TARGET_CRS)

    shapefile_dir = work / "source_shp"
    shapefile_dir.mkdir()
    shapefile_path = shapefile_dir / "parcel.shp"
    frame.to_file(shapefile_path, driver="ESRI Shapefile", engine="fiona")

    with zipfile.ZipFile(destination, "w") as archive:
        for component in sorted(shapefile_dir.iterdir()):
            archive.write(component, arcname=component.name)
    return destination


def test_shapefile_zip_input_is_repaired(tmp_path: Path) -> None:
    source_zip = _write_shapefile_zip(tmp_path / "input.zip", tmp_path)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    result = run_local.convert_with_autofix(source_zip, work_dir)

    assert len(result.report.fixed) == 1
    fix = result.report.fixed[0]
    # The tolerance is applied in true metres, so the 0.6 mm defect is still
    # 0.6 mm after the round trip through Web Mercator.
    assert fix.max_vertex_shift == pytest.approx(0.0006, abs=5e-5)

    exported = _read_archive(result.archive_path, tmp_path / "out")
    assert exported.crs.to_epsg() == 3857
    # Reprojection reintroduces a sub-millimetre protrusion that the finishing
    # pass in the output CRS has to absorb, otherwise the delivered file still
    # fails validation.
    assert result.residual_violations == []
    _assert_buildings_are_contained(exported)


@pytest.mark.skipif(not REAL_DXF.is_file(), reason="sample drawing unavailable")
def test_dxf_plot_matches_the_production_pipeline_exactly(tmp_path: Path) -> None:
    """The land plot must be byte-for-byte what the normal conversion produces."""
    production = bot._read_dxf(REAL_DXF)
    production = bot._convert_lines_to_polygons(production)
    production = production[
        production.geometry.geom_type.isin({"Polygon", "MultiPolygon"})
    ]
    production = production.reset_index(drop=True).to_crs(bot.TARGET_CRS)
    production_plot = production.geometry.iloc[
        int(production.geometry.area.idxmax())
    ]

    result = run_local.convert_with_autofix(REAL_DXF, tmp_path)
    exported = _read_archive(result.archive_path, tmp_path / "out")
    exported_plot = exported.geometry.iloc[int(exported.geometry.area.idxmax())]

    assert exported_plot.equals_exact(production_plot, tolerance=0.0)


def test_zip_input_returns_the_plot_untouched(tmp_path: Path) -> None:
    """A ZIP already in the output CRS must not have its plot reprojected.

    Round-tripping through the working CRS shifts coordinates by a fraction of
    a millimetre. Harmless in itself, but the plot is not what is being
    corrected, so it must come back exactly as it arrived.
    """
    source_zip = _write_shapefile_zip(tmp_path / "input.zip", tmp_path)
    original = _read_archive(source_zip, tmp_path / "original")
    original_plot = original.geometry.iloc[int(original.geometry.area.idxmax())]

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    result = run_local.convert_with_autofix(source_zip, work_dir)
    assert len(result.report.fixed) == 1

    exported = _read_archive(result.archive_path, tmp_path / "out")
    exported_plot = exported.geometry.iloc[int(exported.geometry.area.idxmax())]

    assert exported_plot.equals_exact(original_plot, tolerance=0.0)
    assert exported_plot.area == pytest.approx(original_plot.area, rel=0, abs=0)


def test_zip_input_leaves_compliant_buildings_untouched(tmp_path: Path) -> None:
    """A building that was already inside keeps its exact coordinates."""
    good_building = box(
        ORIGIN_X + 2.0, ORIGIN_Y + 2.0, ORIGIN_X + 8.0, ORIGIN_Y + 8.0
    )
    frame = gpd.GeoDataFrame(
        {"Label": ["good", "sliver", "plot"]},
        geometry=[good_building, _building_with_micro_overhang(), _plot()],
        crs=bot.SOURCE_CRS,
    ).to_crs(bot.TARGET_CRS)

    shapefile_dir = tmp_path / "src"
    shapefile_dir.mkdir()
    frame.to_file(shapefile_dir / "parcel.shp", driver="ESRI Shapefile", engine="fiona")
    source_zip = tmp_path / "input.zip"
    with zipfile.ZipFile(source_zip, "w") as archive:
        for component in sorted(shapefile_dir.iterdir()):
            archive.write(component, arcname=component.name)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    result = run_local.convert_with_autofix(source_zip, work_dir)

    assert len(result.report.fixed) == 1
    exported = _read_archive(result.archive_path, tmp_path / "out")

    # The export uppercases attribute names for DBF compatibility.
    original = _read_archive(source_zip, tmp_path / "orig")
    original_good = original.loc[original["Label"] == "good"].geometry.iloc[0]
    exported_good = exported.loc[exported["LABEL"] == "good"].geometry.iloc[0]
    assert exported_good.equals_exact(original_good, tolerance=0.0)


def test_three_dimensional_shapefile_zip_is_repaired(tmp_path: Path) -> None:
    """Regression: a PolygonZ ZIP used to crash on coordinate unpacking."""
    plot_3d = Polygon([(x, y, 12.5) for x, y in _plot().exterior.coords])
    building_3d = Polygon(
        [
            (x, y, 14.0)
            for x, y in _building_with_micro_overhang().exterior.coords
        ]
    )
    frame = gpd.GeoDataFrame(
        {"Label": ["building", "plot"]},
        geometry=[building_3d, plot_3d],
        crs=bot.SOURCE_CRS,
    ).to_crs(bot.TARGET_CRS)
    assert all(frame.geometry.has_z)

    shapefile_dir = tmp_path / "source_3d"
    shapefile_dir.mkdir()
    frame.to_file(shapefile_dir / "parcel.shp", driver="ESRI Shapefile", engine="fiona")

    source_zip = tmp_path / "input.zip"
    with zipfile.ZipFile(source_zip, "w") as archive:
        for component in sorted(shapefile_dir.iterdir()):
            archive.write(component, arcname=component.name)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    result = run_local.convert_with_autofix(source_zip, work_dir)

    assert len(result.report.fixed) == 1
    assert result.residual_violations == []
    exported = _read_archive(result.archive_path, tmp_path / "out")
    _assert_buildings_are_contained(exported)


def test_shapefile_zip_without_crs_is_assumed_web_mercator(tmp_path: Path) -> None:
    """A ZIP missing its .prj must not be measured in degrees or raw units."""
    source_zip = _write_shapefile_zip(tmp_path / "input.zip", tmp_path)

    stripped = tmp_path / "no_prj.zip"
    with zipfile.ZipFile(source_zip) as original, zipfile.ZipFile(
        stripped, "w"
    ) as target:
        for name in original.namelist():
            if name.lower().endswith(".prj"):
                continue
            target.writestr(name, original.read(name))

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    result = run_local.convert_with_autofix(stripped, work_dir)
    assert len(result.report.fixed) == 1


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


def test_single_polygon_is_rejected_with_a_clear_message(tmp_path: Path) -> None:
    frame = gpd.GeoDataFrame(
        {"Label": ["plot"]}, geometry=[_plot()], crs=bot.SOURCE_CRS
    ).to_crs(bot.TARGET_CRS)
    shapefile_dir = tmp_path / "only_plot"
    shapefile_dir.mkdir()
    frame.to_file(shapefile_dir / "plot.shp", driver="ESRI Shapefile", engine="fiona")

    source_zip = tmp_path / "input.zip"
    with zipfile.ZipFile(source_zip, "w") as archive:
        for component in sorted(shapefile_dir.iterdir()):
            archive.write(component, arcname=component.name)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    with pytest.raises(bot.ConversionError, match="only one polygon|Only one polygon"):
        run_local.convert_with_autofix(source_zip, work_dir)


def test_misplaced_building_still_returns_a_file_with_a_warning(
    tmp_path: Path,
) -> None:
    """A building far outside the plot is reported, never silently clipped."""
    misplaced = box(
        ORIGIN_X + 10.0, ORIGIN_Y + 25.0, ORIGIN_X + 20.0, ORIGIN_Y + 35.0
    )
    frame = gpd.GeoDataFrame(
        {"Label": ["building", "plot"]},
        geometry=[misplaced, _plot()],
        crs=bot.SOURCE_CRS,
    ).to_crs(bot.TARGET_CRS)

    shapefile_dir = tmp_path / "misplaced"
    shapefile_dir.mkdir()
    frame.to_file(shapefile_dir / "parcel.shp", driver="ESRI Shapefile", engine="fiona")
    source_zip = tmp_path / "input.zip"
    with zipfile.ZipFile(source_zip, "w") as archive:
        for component in sorted(shapefile_dir.iterdir()):
            archive.write(component, arcname=component.name)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    result = run_local.convert_with_autofix(source_zip, work_dir)

    # The user still gets their converted file.
    assert result.archive_path.is_file()
    assert len(result.report.flagged) == 1
    assert result.report.fixed == []

    caption = run_local.build_caption(result)
    assert "Not corrected: 1" in caption
    assert "too far" in caption

    # The building's geometry is unchanged, so nothing was quietly reshaped.
    exported = _read_archive(result.archive_path, tmp_path / "out")
    areas = exported.geometry.area
    building = exported.geometry.iloc[int(areas.idxmin())]
    assert building.area == pytest.approx(
        frame.geometry.iloc[0].area, rel=1e-9
    )


def test_discarding_a_flow_deletes_its_scratch_directory(tmp_path: Path) -> None:
    """The two-step upload keeps a directory alive between messages.

    It cannot sit in a `with tempfile.TemporaryDirectory()` block, so cleanup is
    explicit and this guards against leaking a copy of every parcel uploaded.
    """

    class FakeContext:
        def __init__(self, data):
            self.user_data = data

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "parcel.zip").write_bytes(b"payload")

    context = FakeContext({run_local.PENDING_FIX_KEY: {"scratch": str(scratch)}})
    run_local._discard_flow(context)

    assert not scratch.exists()
    assert run_local.PENDING_FIX_KEY not in context.user_data

    # Idempotent: a second call on empty state must not raise.
    run_local._discard_flow(context)


def test_unsupported_extension_is_rejected(tmp_path: Path) -> None:
    stray = tmp_path / "notes.txt"
    stray.write_text("not a drawing")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    with pytest.raises(bot.ConversionError, match="supported"):
        run_local.convert_with_autofix(stray, work_dir)
