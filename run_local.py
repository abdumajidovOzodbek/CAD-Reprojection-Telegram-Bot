"""Local test harness for the building-in-plot autofix feature.

This entry point exists so the new `/fix` flow can be exercised against a
throwaway testing bot without modifying bot.py. It imports the production
handlers by reference and registers the new ones alongside them, so the
testing bot behaves exactly like production plus `/fix`.

Run it with the testing bot's token in .env under TESTING_BOT:

    .venv\\Scripts\\python.exe run_local.py

Once the behaviour is approved, the same handlers get registered inside
bot.build_application and this file is no longer needed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import geopandas as gpd
from dotenv import load_dotenv
from shapely.geometry.base import BaseGeometry
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import autofix
import bot

LOGGER = logging.getLogger("cad-reprojection-bot.local")

# Key under which a repair in progress keeps its state.
PENDING_FIX_KEY = "pending_fix"

# Callback payloads. The `fix_` prefix keeps them clear of the existing
# approve_/deny_/revoke_ payloads handled by bot.handle_approval_callback.
CALLBACK_OVERLAP = "fix_overlap"
CALLBACK_NEIGHBOURS = "fix_neighbours"
CALLBACK_SIBLINGS = "fix_siblings"
CALLBACK_SKIP_NEIGHBOURS = "fix_skip_neighbours"
CALLBACK_CANCEL = "fix_cancel"

# Stages of the two-step upload.
STAGE_AWAIT_PARCEL = "await_parcel"
STAGE_AWAIT_NEIGHBOURS = "await_neighbours"

# Optional checks. Containment always runs: whatever else is corrected, the
# buildings must end up inside their own land plot. The neighbour check needs no
# flag, since it is driven by whether a neighbours file was supplied.
CHECK_CONTAINMENT = "containment"
CHECK_SIBLINGS = "siblings"

CHECKS_FOR_MODE = {
    CALLBACK_OVERLAP: frozenset({CHECK_CONTAINMENT}),
    CALLBACK_NEIGHBOURS: frozenset({CHECK_CONTAINMENT}),
    CALLBACK_SIBLINGS: frozenset({CHECK_CONTAINMENT, CHECK_SIBLINGS}),
}

POLYGON_GEOMETRY_TYPES = {"Polygon", "MultiPolygon"}


@dataclass(frozen=True)
class FixResult:
    """Everything needed to reply after a repair run."""

    archive_path: Path
    report: autofix.AutofixReport
    feature_count: int
    shapefile_count: int
    # Buildings still protruding in the exported CRS, as (row, area) pairs.
    # Normally empty; a non-empty list means the export needs manual review.
    residual_violations: list[tuple[int, float]]
    # Present only when a neighbours file was supplied.
    neighbour_report: autofix.NeighbourReport | None = None
    # Present only when the building-to-building check ran.
    sibling_report: autofix.SiblingReport | None = None
    # Rows still overlapping registered neighbours in the exported CRS.
    neighbour_violations: list[tuple[int, float]] = field(default_factory=list)
    # Building pairs still overlapping each other, as (row, row, area).
    sibling_violations: list[tuple[int, int, float]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Conversion pipeline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadedDrawing:
    """One drawing held in two CRSs, row for row.

    `working` is in bot.SOURCE_CRS, whose units are true metres, because that
    is the only place the tolerances mean what they say. Web Mercator inflates
    distances by roughly 1/cos(latitude), about 1.33x at latitude 41.

    `native` is the same rows in the output CRS, reached with the fewest
    possible transformations: no reprojection at all for a ZIP that already
    arrives in EPSG:3857. Rows that need no repair are exported straight from
    here, so their coordinates are returned exactly as received rather than
    being nudged by a reprojection round trip.
    """

    working: gpd.GeoDataFrame
    native: gpd.GeoDataFrame


def _filter_polygons(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Keep the non-empty polygons, which is what a plot and its buildings are."""
    polygons = frame.loc[
        frame.geometry.geom_type.isin(POLYGON_GEOMETRY_TYPES)
        & frame.geometry.notna()
        & ~frame.geometry.is_empty
    ].copy()
    return polygons.reset_index(drop=True)


def _load_drawing_frame(input_path: Path, work_dir: Path) -> gpd.GeoDataFrame:
    """Read a CAD file or Shapefile ZIP as polygons in its own CRS.

    A DXF carries no usable CRS, so bot._read_dxf assigns the configured local
    one. A Shapefile without a .prj is assumed to be in the output CRS, matching
    what the rest of the bot already does.
    """
    suffix = input_path.suffix.lower()
    work_dir.mkdir(parents=True, exist_ok=True)

    if suffix == bot.SUPPORTED_ZIP_EXTENSION:
        frame = bot._read_shapefile_from_zip(input_path, work_dir)
        if frame.crs is None:
            frame = frame.set_crs(bot.TARGET_CRS)
    elif suffix in bot.SUPPORTED_EXTENSIONS:
        dxf_path = input_path
        if suffix == ".dwg":
            dxf_path = work_dir / f"{bot._safe_stem(input_path.name)}_converted.dxf"
            bot._convert_dwg_to_dxf(input_path, dxf_path)
        frame = bot._read_dxf(dxf_path)
    else:
        raise bot.ConversionError("Only .dxf, .dwg and .zip files are supported.")

    return _filter_polygons(bot._convert_lines_to_polygons(frame))


def _load_drawing(input_path: Path, work_dir: Path) -> LoadedDrawing:
    """Read our own parcel, ready for repair in both CRSs."""
    frame = _load_drawing_frame(input_path, work_dir / "parcel")
    _reject_unusable(frame)
    native = frame if frame.crs == bot.TARGET_CRS else frame.to_crs(bot.TARGET_CRS)
    working = frame if frame.crs == bot.SOURCE_CRS else frame.to_crs(bot.SOURCE_CRS)
    return LoadedDrawing(working=working, native=native)


def _reject_unusable(polygons: gpd.GeoDataFrame) -> None:
    """Explain clearly why a drawing cannot be checked, before any work starts."""
    if polygons.empty:
        raise bot.ConversionError(
            "No polygons were found. The land plot and the buildings must be "
            "drawn as closed polylines."
        )
    if len(polygons) < 2:
        raise bot.ConversionError(
            "Only one polygon was found, so there is no building to check "
            "against a land plot. The drawing needs the plot and at least one "
            "building."
        )


def _load_obstacles(
    neighbours_path: Path, work_dir: Path
) -> tuple[BaseGeometry, BaseGeometry, int]:
    """Read the registered neighbours as blocked area, in both CRSs.

    Returned read-only and never exported: this data belongs to parcels already
    in the system and must not be altered.
    """
    frame = _load_drawing_frame(neighbours_path, work_dir / "neighbours")
    count = len(frame)
    working = autofix.build_obstacles(frame.to_crs(bot.SOURCE_CRS))
    native = autofix.build_obstacles(
        frame if frame.crs == bot.TARGET_CRS else frame.to_crs(bot.TARGET_CRS)
    )
    return working, native, count


def convert_with_autofix(
    input_path: Path,
    work_dir: Path,
    neighbours_path: Path | None = None,
    checks: frozenset[str] = frozenset({CHECK_CONTAINMENT}),
) -> FixResult:
    """Repair our parcel and export Shapefiles.

    Up to three problems are corrected, in this order:

    1. Our land plot and buildings are trimmed off any registered neighbouring
       parcel. Runs whenever a neighbours file is supplied; supplying one is the
       request, so there is no separate flag to forget.
    2. Our buildings are separated from each other where they overlap, when
       CHECK_SIBLINGS is requested.
    3. Our buildings are brought back inside our own land plot. Always runs.

    The order is not arbitrary. Step 3 has to come last because trimming the
    plot in step 1 is exactly what can push a building outside it. Steps 1 and 2
    only ever remove area, so neither can undo the other's work.

    Accepts a DXF, DWG or Shapefile ZIP and always returns a Shapefile ZIP in
    bot.TARGET_CRS containing only our parcel.
    """
    drawing = _load_drawing(input_path, work_dir)
    config = autofix.AutofixConfig.from_env()

    obstacles_working: BaseGeometry | None = None
    obstacles_native: BaseGeometry | None = None
    neighbour_report: autofix.NeighbourReport | None = None
    sibling_report: autofix.SiblingReport | None = None
    working = drawing.working

    if neighbours_path is not None:
        obstacles_working, obstacles_native, obstacle_count = _load_obstacles(
            neighbours_path, work_dir
        )
        working, neighbour_report = autofix.resolve_neighbour_conflicts(
            working, obstacles_working, config, obstacle_count=obstacle_count
        )
        LOGGER.info(
            "Neighbour report: %s", " | ".join(neighbour_report.summary_lines())
        )

    if CHECK_SIBLINGS in checks:
        working, sibling_report = autofix.resolve_sibling_overlaps(working, config)
        LOGGER.info("Sibling report: %s", " | ".join(sibling_report.summary_lines()))

    fixed, report = autofix.autofix_frame(working, config)
    LOGGER.info("Autofix report: %s", " | ".join(report.summary_lines()))

    changed_rows = {fix.row_index for fix in report.fixed}
    if neighbour_report is not None:
        changed_rows |= {fix.row_index for fix in neighbour_report.fixed}
    if sibling_report is not None:
        changed_rows |= {pair.trimmed_row for pair in sibling_report.fixed}

    reprojected = _merge_repairs(drawing.native, fixed, sorted(changed_rows), report)

    # The repairs above are exact in metres, which is where the thresholds are
    # meaningful. Reprojecting repaired geometry then reintroduces a minute
    # error, because a vertex lying on a segment does not stay on it once that
    # segment is redrawn as a chord between its transformed endpoints.
    # Validation happens on the exported EPSG:3857 geometry, so the file has to
    # be clean in that CRS: hence the finishing passes here.
    if obstacles_native is not None:
        reprojected, obstacle_finishing = autofix.resolve_neighbour_conflicts(
            reprojected, obstacles_native, autofix.AutofixConfig.finishing()
        )
        if obstacle_finishing.changed_anything:
            LOGGER.info(
                "Finishing pass cleared reprojection residue off neighbours on "
                "%d row(s)",
                len(obstacle_finishing.fixed),
            )

    if sibling_report is not None:
        reprojected, sibling_finishing = autofix.resolve_sibling_overlaps(
            reprojected, autofix.AutofixConfig.finishing()
        )
        if sibling_finishing.changed_anything:
            LOGGER.info(
                "Finishing pass re-separated %d building pair(s) after "
                "reprojection",
                len(sibling_finishing.fixed),
            )

    reprojected, finishing_report = autofix.autofix_frame(
        reprojected, autofix.AutofixConfig.finishing()
    )
    if finishing_report.changed_anything:
        LOGGER.info(
            "Finishing pass in %s absorbed reprojection residue on %d building(s)",
            bot.TARGET_CRS,
            len(finishing_report.fixed),
        )

    # Verify with the same epsilon the repair treats as clean. Holding
    # verification to a stricter threshold than the repair itself would report
    # violations that no pass is ever going to act on: 1e-6 m² is one square
    # millimetre, which over a 36 m perimeter is a hundredth of a micron wide.
    epsilon = config.residual_epsilon_m2
    residual_violations = autofix.verify_containment(reprojected, epsilon)
    if residual_violations:
        LOGGER.warning(
            "Buildings still outside the plot after repair: %s", residual_violations
        )

    neighbour_violations: list[tuple[int, float]] = []
    if obstacles_native is not None:
        neighbour_violations = autofix.verify_no_overlap(
            reprojected, obstacles_native, epsilon
        )
        if neighbour_violations:
            LOGGER.warning(
                "Rows still overlapping neighbours after repair: %s",
                neighbour_violations,
            )

    sibling_violations: list[tuple[int, int, float]] = []
    if sibling_report is not None:
        sibling_violations = autofix.verify_no_sibling_overlap(reprojected, epsilon)
        if sibling_violations:
            LOGGER.warning(
                "Buildings still overlapping each other after repair: %s",
                sibling_violations,
            )

    shapefile_dir = work_dir / "shapefiles"
    shapefile_dir.mkdir(exist_ok=True)
    output_stem = f"{bot._safe_stem(input_path.name)}_fixed"
    shapefile_count = bot._write_shapefiles(
        _prepare_attributes(reprojected), shapefile_dir, output_stem
    )

    archive_path = work_dir / f"{output_stem}.zip"
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
        for component in sorted(shapefile_dir.iterdir()):
            if component.is_file():
                archive.write(component, arcname=component.name)

    return FixResult(
        archive_path=archive_path,
        report=report,
        neighbour_report=neighbour_report,
        sibling_report=sibling_report,
        feature_count=len(reprojected),
        shapefile_count=shapefile_count,
        residual_violations=residual_violations,
        neighbour_violations=neighbour_violations,
        sibling_violations=sibling_violations,
    )


def _prepare_attributes(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Make attribute columns writable to a Shapefile.

    The ESRI Shapefile driver rejects datetime fields outright:
    "ESRI Shapefile does not support datetime fields". Registered cadastral
    exports carry one, the SANA date, so without this every such file fails at
    the very last step after all the geometry work has already succeeded.

    Datetime columns become ISO date strings, which is the most a Shapefile can
    hold anyway, since its date type has no time component. Empty values stay
    empty rather than becoming the string "NaT".
    """
    import pandas as pd

    prepared = frame.copy()
    for column in prepared.columns:
        if column == prepared.geometry.name:
            continue
        if pd.api.types.is_datetime64_any_dtype(prepared[column]):
            LOGGER.info("Writing datetime column %r as an ISO date string", column)
            prepared[column] = (
                prepared[column].dt.strftime("%Y-%m-%d").where(prepared[column].notna())
            )
    return prepared


def _merge_repairs(
    native: gpd.GeoDataFrame,
    fixed: gpd.GeoDataFrame,
    repaired_positions: list[int],
    report: autofix.AutofixReport,
) -> gpd.GeoDataFrame:
    """Carry only the repaired rows over into the output frame.

    Everything else is exported from the geometry as it arrived. Reprojecting a
    polygon out to the working CRS and back is not bit-exact; it shifts
    coordinates by a fraction of a millimetre. That is harmless numerically but
    it means a boundary no longer matches the archived one exactly, which is
    indefensible for a polygon nothing was wrong with. Untouched rows therefore
    never make the round trip.
    """
    if not repaired_positions:
        return native

    geometry_name = native.geometry.name
    fixed_in_target = fixed.to_crs(native.crs)
    geometries = list(native.geometry)
    for position in repaired_positions:
        geometries[position] = fixed_in_target.geometry.iloc[position]

    geometries, z_dropped = autofix.harmonise_dimensions(geometries)
    if z_dropped:
        report.z_was_dropped = True

    merged = native.copy()
    merged[geometry_name] = gpd.GeoSeries(
        geometries, index=merged.index, crs=native.crs
    )
    return merged.set_geometry(geometry_name)


def build_caption(result: FixResult) -> str:
    """User-facing summary of exactly what the repair changed."""
    report = result.report
    neighbour_report = result.neighbour_report
    sibling_report = result.sibling_report
    changed = any(
        section is not None and section.changed_anything
        for section in (report, neighbour_report, sibling_report)
    )
    lines = ["✅ Corrected and converted." if changed else "ℹ️ Nothing to correct.", ""]

    sectioned = neighbour_report is not None or sibling_report is not None

    if neighbour_report is not None:
        lines.append("— Neighbouring parcels —")
        lines.extend(neighbour_report.summary_lines())
        lines.append("")

    if sibling_report is not None:
        lines.append("— Buildings against each other —")
        lines.extend(sibling_report.summary_lines())
        lines.append("")

    if sectioned:
        lines.append("— Own land plot —")

    lines.extend(report.summary_lines())
    lines.append("")
    shapefile_label = "Shapefile" if result.shapefile_count == 1 else "Shapefiles"
    lines.append(
        f"Output: {result.feature_count} polygons in {result.shapefile_count} "
        f"{shapefile_label} ({bot.TARGET_CRS})."
    )
    if result.residual_violations:
        lines.append("")
        lines.append(
            "⚠️ Some buildings still extend past the plot in the exported file "
            "and need manual review:"
        )
        for row_index, area in result.residual_violations:
            lines.append(f"   • building {row_index}: {area:.6f} m² outside")
    if result.neighbour_violations:
        lines.append("")
        lines.append(
            "⚠️ Some geometry still overlaps a registered neighbour and needs "
            "manual review:"
        )
        for row_index, area in result.neighbour_violations:
            lines.append(f"   • row {row_index}: {area:.6f} m² overlapping")
    if result.sibling_violations:
        lines.append("")
        lines.append(
            "⚠️ Some buildings still overlap each other and need manual review:"
        )
        for first, second, area in result.sibling_violations:
            lines.append(
                f"   • buildings {first} and {second}: {area:.6f} m² overlapping"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------


def _discard_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forget any repair in progress and delete its scratch directory.

    The first upload of a two-step flow has to outlive the update that carried
    it, so it cannot live in a `with tempfile.TemporaryDirectory()` block. This
    is the single place that cleans it up, called on completion, on failure, on
    cancel, and when a new flow starts.
    """
    flow = context.user_data.pop(PENDING_FIX_KEY, None)
    if not flow:
        return
    scratch = flow.get("scratch")
    if scratch:
        shutil.rmtree(scratch, ignore_errors=True)


async def fix_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Offer the available repairs. More options can be added as buttons."""
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return

    if not bot._is_authorized(user.id):
        await bot._request_authorization(update, context)
        return

    _discard_flow(context)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🏠 Building outside its own land plot",
                    callback_data=CALLBACK_OVERLAP,
                )
            ],
            [
                InlineKeyboardButton(
                    "🏘 Overlap with neighbouring houses",
                    callback_data=CALLBACK_NEIGHBOURS,
                )
            ],
            [
                InlineKeyboardButton(
                    "🏚 Buildings overlapping each other",
                    callback_data=CALLBACK_SIBLINGS,
                )
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data=CALLBACK_CANCEL)],
        ]
    )
    await message.reply_text("🔧 What should I fix?", reply_markup=keyboard)


async def handle_fix_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Advance the repair flow from a button press.

    Registered before bot.handle_approval_callback, which has no pattern and
    would otherwise consume these callbacks and tell non-admin users they are
    not authorised.
    """
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    if query.data == CALLBACK_CANCEL:
        _discard_flow(context)
        await query.edit_message_text("Cancelled.")
        return

    if query.data == CALLBACK_OVERLAP:
        context.user_data[PENDING_FIX_KEY] = {
            "mode": CALLBACK_OVERLAP,
            "stage": STAGE_AWAIT_PARCEL,
            "scratch": None,
        }
        await query.edit_message_text(
            "🏠 Fixing buildings that stick out of their own land plot.\n\n"
            "Send the parcel as .dxf, .dwg or .zip (Shapefile).\n\n"
            "The largest polygon is treated as the land plot and is left exactly "
            "as it arrives. Buildings poking out by a tiny amount are snapped "
            "back onto the plot boundary. A building sitting far outside is "
            "reported instead of reshaped, because that usually means wrong "
            "coordinates rather than a drawing slip.\n\n"
            "You get back a Shapefile ZIP in EPSG:3857."
        )
        return

    if query.data == CALLBACK_SIBLINGS:
        context.user_data[PENDING_FIX_KEY] = {
            "mode": CALLBACK_SIBLINGS,
            "stage": STAGE_AWAIT_PARCEL,
            "scratch": None,
        }
        await query.edit_message_text(
            "🏚 Separating buildings that overlap each other.\n\n"
            "Send the parcel as .dxf, .dwg or .zip (Shapefile).\n\n"
            "Where two buildings overlap, the larger one is trimmed and the "
            "smaller keeps its shape, so small structures stay intact and only "
            "one boundary moves. The land plot is not touched, and trimming only "
            "removes area, so every building stays inside the plot.\n\n"
            "You get back a Shapefile ZIP in EPSG:3857."
        )
        return

    if query.data == CALLBACK_NEIGHBOURS:
        context.user_data[PENDING_FIX_KEY] = {
            "mode": CALLBACK_NEIGHBOURS,
            "stage": STAGE_AWAIT_PARCEL,
            "scratch": None,
        }
        await query.edit_message_text(
            "🏘 Fixing overlaps with houses already in the system.\n\n"
            "Step 1 of 2: send the parcel you are working on, the one that may "
            "be corrected.\n\n"
            "Step 2 will ask for the registered neighbours, which are read only "
            "and never altered."
        )
        return

    if query.data == CALLBACK_SKIP_NEIGHBOURS:
        flow = context.user_data.get(PENDING_FIX_KEY)
        if not flow or flow.get("stage") != STAGE_AWAIT_NEIGHBOURS:
            await query.edit_message_text(
                "That request has expired. Send /fix to start again."
            )
            return
        await query.edit_message_text(
            "Skipping the neighbour check. Fixing buildings against their own "
            "land plot only..."
        )
        await _process_flow(update, context, flow, neighbours_path=None)
        return


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route documents through the repair flow, or to the normal conversion."""
    flow = context.user_data.get(PENDING_FIX_KEY)
    if not flow:
        await bot.handle_document(update, context)
        return

    message = update.effective_message
    document = message.document if message else None
    user = update.effective_user
    if message is None or document is None or user is None:
        return

    if not bot._is_authorized(user.id):
        await message.reply_text(
            "You don't have access to this bot. Send /start to request access."
        )
        return

    filename = document.file_name or "file"
    suffix = Path(filename).suffix.lower()
    if suffix not in bot.SUPPORTED_EXTENSIONS and suffix != bot.SUPPORTED_ZIP_EXTENSION:
        await message.reply_text("Please send a .dxf, .dwg or .zip document.")
        return

    if document.file_size and document.file_size > bot.MAX_INPUT_FILE_SIZE_BYTES:
        max_size_mb = bot.MAX_INPUT_FILE_SIZE_BYTES // (1024 * 1024)
        await message.reply_text(
            f"This file is too large. Please send a file smaller than {max_size_mb} MB."
        )
        return

    if flow.get("scratch") is None:
        flow["scratch"] = tempfile.mkdtemp(prefix="cad-autofix-")
    scratch = Path(flow["scratch"])

    stage = flow.get("stage")
    role = "parcel" if stage == STAGE_AWAIT_PARCEL else "neighbours"
    saved_path = scratch / f"{role}_{bot._safe_stem(filename)}{suffix}"

    try:
        telegram_file = await context.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(custom_path=saved_path)
        if not saved_path.is_file() or saved_path.stat().st_size == 0:
            raise bot.ConversionError("Telegram returned an empty download.")
    except TelegramError:
        LOGGER.exception("Could not download %s", role)
        _discard_flow(context)
        await message.reply_text(
            "Telegram could not deliver that file. Send /fix to try again."
        )
        return
    except bot.ConversionError as exc:
        _discard_flow(context)
        await message.reply_text(f"Could not process the file: {exc}")
        return

    if stage == STAGE_AWAIT_PARCEL:
        flow["parcel"] = str(saved_path)

        if flow.get("mode") == CALLBACK_NEIGHBOURS:
            flow["stage"] = STAGE_AWAIT_NEIGHBOURS
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⏭ No neighbours, skip",
                            callback_data=CALLBACK_SKIP_NEIGHBOURS,
                        )
                    ],
                    [InlineKeyboardButton("❌ Cancel", callback_data=CALLBACK_CANCEL)],
                ]
            )
            await message.reply_text(
                f"Got the parcel: {filename}\n\n"
                "Step 2 of 2: now send the houses already in the system, as a "
                "Shapefile ZIP or CAD file. That file is read only and will not "
                "be changed or returned.",
                reply_markup=keyboard,
            )
            return

        await _process_flow(update, context, flow, neighbours_path=None)
        return

    await _process_flow(update, context, flow, neighbours_path=saved_path)


async def _process_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    flow: dict[str, Any],
    neighbours_path: Path | None,
) -> None:
    """Run the repair for a completed flow and reply with the result."""
    message = update.effective_message
    chat_id = update.effective_chat.id if update.effective_chat else None
    if message is None:
        _discard_flow(context)
        return

    parcel_path = Path(flow["parcel"])
    scratch = Path(flow["scratch"])
    work_dir = scratch / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    status_message = await context.bot.send_message(
        chat_id=chat_id or message.chat_id, text="Checking geometry..."
    )

    checks = CHECKS_FOR_MODE.get(
        flow.get("mode"), frozenset({CHECK_CONTAINMENT})
    )

    try:
        result = await asyncio.to_thread(
            convert_with_autofix, parcel_path, work_dir, neighbours_path, checks
        )

        await bot._edit_status(status_message, "Preparing ZIP archive...")
        if chat_id is not None:
            await context.bot.send_chat_action(
                chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT
            )

        with result.archive_path.open("rb") as archive_stream:
            await context.bot.send_document(
                chat_id=chat_id or message.chat_id,
                document=archive_stream,
                filename=result.archive_path.name,
                caption=build_caption(result),
                read_timeout=120.0,
                write_timeout=120.0,
            )
        await bot._edit_status(status_message, "Done.")

    except autofix.AutofixError as exc:
        LOGGER.info("Autofix rejected the drawing: %s", exc)
        await bot._edit_status(status_message, f"Could not process the file: {exc}")
    except bot.ConversionError as exc:
        LOGGER.info("Conversion rejected: %s", exc)
        await bot._edit_status(status_message, f"Could not process the file: {exc}")
    except TelegramError:
        LOGGER.exception("Telegram API error during autofix")
        await bot._edit_status(
            status_message,
            "Telegram could not deliver the file. Please try again.",
        )
    except Exception:
        LOGGER.exception("Unexpected autofix failure")
        await bot._edit_status(
            status_message, "An unexpected processing error occurred. Please try again."
        )
    finally:
        _discard_flow(context)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


async def _publish_commands(application: Application) -> None:
    """Show the commands in Telegram's UI menu."""
    from telegram import BotCommand

    await application.bot.set_my_commands(
        [
            BotCommand("start", "Show what the bot can do"),
            BotCommand("fix", "Fix geometry problems in a parcel"),
        ]
    )


def build_application(token: str) -> Application:
    """Production handlers plus the new /fix flow, with bot.py untouched."""
    from telegram.request import HTTPXRequest

    request = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=60.0,
        write_timeout=60.0,
        connect_timeout=30.0,
        pool_timeout=30.0,
    )
    application = (
        Application.builder()
        .token(token)
        .request(request)
        .post_init(_publish_commands)
        .build()
    )

    application.add_handler(CommandHandler("start", bot.start_command))
    application.add_handler(CommandHandler("help", bot.start_command))
    application.add_handler(CommandHandler("users", bot.list_users_command))
    application.add_handler(CommandHandler("revoke", bot.revoke_access_command))
    application.add_handler(CommandHandler("fix", fix_command))

    # Order matters. Within one handler group python-telegram-bot stops at the
    # first matching handler, and bot.handle_approval_callback matches every
    # callback query, so the patterned handler has to come first.
    application.add_handler(CallbackQueryHandler(handle_fix_choice, pattern="^fix_"))
    application.add_handler(CallbackQueryHandler(bot.handle_approval_callback))

    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_error_handler(bot.error_handler)
    return application


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
    # Bot tokens live in the Bot API URL, so HTTP request logs would leak them.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    token = os.getenv("TESTING_BOT") or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "TESTING_BOT is required in .env for the local test harness."
        )

    LOGGER.info("Starting LOCAL TEST bot (polling) with the /fix flow enabled")
    build_application(token).run_polling(
        allowed_updates=Update.ALL_TYPES, drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
