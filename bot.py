"""Telegram bot for reprojecting CAD vector files into Shapefile ZIP archives."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import fiona
import geopandas as gpd
import pandas as pd
from shapely.geometry.base import BaseGeometry
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


LOGGER = logging.getLogger("cad-reprojection-bot")

SOURCE_CRS = (
    "+proj=tmerc +lat_0=0 +lon_0=69 +k=1 +x_0=-182.2698 "
    "+y_0=-4500719.7668 +ellps=krass "
    "+towgs84=23.92,-141.27,-80.9,0,0.35,0.82,-0.12000000004786 "
    "+units=m +no_defs +type=crs"
)
TARGET_CRS = "EPSG:3857"
SUPPORTED_EXTENSIONS = {".dxf", ".dwg"}
SUPPORTED_GEOMETRY_TYPES = {
    "Point",
    "MultiPoint",
    "LineString",
    "MultiLineString",
    "Polygon",
    "MultiPolygon",
}


def _positive_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


MAX_INPUT_FILE_SIZE_BYTES = (
    _positive_int_env("MAX_INPUT_FILE_SIZE_MB", 50) * 1024 * 1024
)
DWG_CONVERSION_TIMEOUT_SECONDS = _positive_int_env("DWG_CONVERSION_TIMEOUT_SECONDS", 120)


@dataclass(frozen=True)
class ConversionResult:
    archive_path: Path
    feature_count: int
    shapefile_count: int


class ConversionError(RuntimeError):
    """An expected, user-actionable CAD conversion failure."""


def _safe_stem(filename: str) -> str:
    """Create a filesystem-safe, non-empty name from a Telegram filename."""
    stem = Path(filename).stem
    safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("._-")
    return safe_stem[:80] or "cad_file"


def _serialise_attribute(value: Any) -> Any:
    """Convert nested CAD attributes to values supported by DBF fields."""
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if value is None:
        return None
    if isinstance(value, BaseGeometry):
        return value.wkt
    return value


def _shapefile_field_name(name: str, used_names: set[str]) -> str:
    """Return a unique DBF-compatible field name (max 10 ASCII characters)."""
    candidate = re.sub(r"[^A-Za-z0-9_]+", "_", str(name)).strip("_").upper()
    candidate = candidate[:10] or "FIELD"
    base_name = candidate
    suffix = 1
    while candidate in used_names:
        suffix_text = str(suffix)
        candidate = f"{base_name[:10 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    used_names.add(candidate)
    return candidate


def _prepare_for_shapefile(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Normalise attributes without discarding vector features."""
    prepared = frame.copy()
    used_names: set[str] = set()
    column_mapping: dict[str, str] = {}

    for column in prepared.columns:
        if column == prepared.geometry.name:
            continue
        new_name = _shapefile_field_name(column, used_names)
        column_mapping[column] = new_name
        if prepared[column].dtype == "object":
            prepared[column] = prepared[column].map(_serialise_attribute)

    prepared = prepared.rename(columns=column_mapping)
    return prepared


def _read_dxf(dxf_path: Path) -> gpd.GeoDataFrame:
    """Read every available DXF layer through Fiona-backed GeoPandas."""
    try:
        layers = list(fiona.listlayers(str(dxf_path)))
    except (fiona.errors.DriverError, ValueError) as exc:
        raise ConversionError(
            "The DXF file could not be opened by GDAL/Fiona. "
            "Confirm that it is a valid vector DXF file."
        ) from exc

    if not layers:
        layers = [None]

    frames: list[gpd.GeoDataFrame] = []
    for layer in layers:
        try:
            read_options: dict[str, Any] = {"driver": "DXF", "engine": "fiona"}
            if layer is not None:
                read_options["layer"] = layer
            layer_frame = gpd.read_file(dxf_path, **read_options)
        except (fiona.errors.DriverError, ValueError, OSError) as exc:
            layer_label = f" layer {layer!r}" if layer is not None else ""
            raise ConversionError(
                f"GDAL/Fiona could not read DXF{layer_label}."
            ) from exc
        if not layer_frame.empty:
            frames.append(layer_frame)

    if not frames:
        raise ConversionError("The CAD file contains no vector features to reproject.")

    combined = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True),
        geometry=frames[0].geometry.name,
        crs=frames[0].crs,
    )
    # The requested local CRS is authoritative even when the CAD file carries
    # no CRS or carries an incorrect one.
    combined = combined.set_crs(SOURCE_CRS, allow_override=True)
    return combined


def _convert_dwg_to_dxf(dwg_path: Path, dxf_path: Path) -> None:
    dwg2dxf = shutil.which("dwg2dxf")
    if dwg2dxf is None:
        raise ConversionError(
            "DWG conversion is unavailable because the dwg2dxf utility is not "
            "installed in the runtime image."
        )

    try:
        completed = subprocess.run(
            [dwg2dxf, str(dwg_path), "-o", str(dxf_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=DWG_CONVERSION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConversionError(
            "DWG conversion timed out. Try a smaller or simpler DWG file."
        ) from exc
    except OSError as exc:
        raise ConversionError("The DWG converter could not be started.") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        detail = detail[-500:] if detail else "No converter details were returned."
        raise ConversionError(f"DWG conversion failed: {detail}")
    if not dxf_path.is_file() or dxf_path.stat().st_size == 0:
        raise ConversionError("DWG conversion completed without producing a DXF file.")


def _write_shapefiles(
    frame: gpd.GeoDataFrame, output_dir: Path, output_stem: str
) -> int:
    """Write homogeneous Shapefiles, preserving CAD features of each type."""
    geometries = frame.geometry
    valid_geometry_mask = geometries.notna() & ~geometries.is_empty
    frame = frame.loc[valid_geometry_mask].copy()
    if frame.empty:
        raise ConversionError("The CAD file contains no non-empty vector geometries.")

    geometry_types = set(frame.geometry.geom_type.dropna().unique())
    unsupported_types = geometry_types - SUPPORTED_GEOMETRY_TYPES
    if unsupported_types:
        unsupported = ", ".join(sorted(unsupported_types))
        raise ConversionError(
            f"The CAD file contains unsupported geometry types: {unsupported}. "
            "Only point, line, and polygon geometries can be exported to Shapefile."
        )

    sorted_types = sorted(geometry_types)
    if len(sorted_types) == 1:
        output_names = {sorted_types[0]: output_stem}
    else:
        output_names = {
            geometry_type: f"{output_stem}_{geometry_type.lower()}"
            for geometry_type in sorted_types
        }

    shapefile_count = 0
    for geometry_type in sorted_types:
        type_frame = frame.loc[frame.geometry.geom_type == geometry_type].copy()
        type_frame = _prepare_for_shapefile(type_frame)
        destination = output_dir / f"{output_names[geometry_type]}.shp"
        try:
            type_frame.to_file(
                destination,
                driver="ESRI Shapefile",
                engine="fiona",
                index=False,
                encoding="UTF-8",
            )
        except (fiona.errors.DriverError, ValueError, OSError) as exc:
            raise ConversionError(
                f"Could not write the {geometry_type} Shapefile."
            ) from exc
        required_components = {".shp", ".shx", ".dbf", ".prj"}
        existing_components = {
            path.suffix.lower()
            for path in output_dir.glob(f"{destination.stem}.*")
        }
        missing_components = required_components - existing_components
        if missing_components:
            missing = ", ".join(sorted(missing_components))
            raise ConversionError(
                f"The {geometry_type} Shapefile is missing required component(s): {missing}."
            )
        shapefile_count += 1

    return shapefile_count


def convert_and_package(input_path: Path, work_dir: Path) -> ConversionResult:
    """Convert one downloaded CAD file and return its temporary ZIP archive."""
    input_suffix = input_path.suffix.lower()
    if input_suffix not in SUPPORTED_EXTENSIONS:
        raise ConversionError("Only .dxf and .dwg files are supported.")

    dxf_path = input_path
    if input_suffix == ".dwg":
        dxf_path = work_dir / f"{_safe_stem(input_path.name)}_converted.dxf"
        _convert_dwg_to_dxf(input_path, dxf_path)

    frame = _read_dxf(dxf_path)
    reprojected = frame.to_crs(TARGET_CRS)
    shapefile_dir = work_dir / "shapefiles"
    shapefile_dir.mkdir()
    output_stem = f"{_safe_stem(input_path.name)}_reprojected"
    shapefile_count = _write_shapefiles(reprojected, shapefile_dir, output_stem)

    archive_path = work_dir / f"{output_stem}.zip"
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
        for component in sorted(shapefile_dir.iterdir()):
            if component.is_file():
                archive.write(component, arcname=component.name)

    return ConversionResult(
        archive_path=archive_path,
        feature_count=len(reprojected),
        shapefile_count=shapefile_count,
    )


async def _edit_status(status_message: Any, text: str) -> None:
    try:
        await status_message.edit_text(text)
    except TelegramError:
        LOGGER.debug("Could not edit the Telegram status message", exc_info=True)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if update.effective_message:
        await update.effective_message.reply_text(
            "Send me a DXF or DWG document and I’ll return a ZIP archive "
            "containing the reprojected Shapefile."
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    document = message.document if message else None
    if message is None or document is None:
        return

    filename = document.file_name or "cad_file"
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        await message.reply_text("Please send a .dxf or .dwg document.")
        return
    if document.file_size and document.file_size > MAX_INPUT_FILE_SIZE_BYTES:
        max_size_mb = MAX_INPUT_FILE_SIZE_BYTES // (1024 * 1024)
        await message.reply_text(
            f"This file is too large. Please send a file smaller than {max_size_mb} MB."
        )
        return

    status_message = await message.reply_text("Processing file...")
    chat_id = update.effective_chat.id if update.effective_chat else None

    try:
        with tempfile.TemporaryDirectory(prefix="cad-reprojection-") as temporary_dir:
            work_dir = Path(temporary_dir)
            input_path = work_dir / f"input{suffix}"

            if suffix == ".dwg":
                await _edit_status(status_message, "Converting DWG...")
            telegram_file = await context.bot.get_file(document.file_id)
            await telegram_file.download_to_drive(custom_path=input_path)

            if not input_path.is_file() or input_path.stat().st_size == 0:
                raise ConversionError("Telegram returned an empty download.")
            if input_path.stat().st_size > MAX_INPUT_FILE_SIZE_BYTES:
                raise ConversionError("The downloaded file exceeds the configured size limit.")

            await _edit_status(status_message, "Reading and reprojecting geometry...")
            result = await asyncio.to_thread(convert_and_package, input_path, work_dir)

            await _edit_status(status_message, "Preparing ZIP archive...")
            if chat_id is not None:
                await context.bot.send_chat_action(
                    chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT
                )
            shapefile_label = (
                "Shapefile" if result.shapefile_count == 1 else "Shapefiles"
            )
            with result.archive_path.open("rb") as archive_stream:
                await message.reply_document(
                    document=archive_stream,
                    filename=result.archive_path.name,
                    caption=(
                        f"Success. Reprojected {result.feature_count} feature(s) "
                        f"into {result.shapefile_count} {shapefile_label} "
                        f"({TARGET_CRS})."
                    ),
                )
            await _edit_status(status_message, "Done.")
    except ConversionError as exc:
        LOGGER.info("CAD conversion rejected: %s", exc)
        await _edit_status(status_message, f"Could not process the file: {exc}")
    except TelegramError:
        LOGGER.exception("Telegram API error while handling a document")
        await _edit_status(
            status_message,
            "Telegram could not deliver the file. Please try again with the same document.",
        )
    except Exception:
        LOGGER.exception("Unexpected CAD conversion failure")
        await _edit_status(
            status_message,
            "An unexpected processing error occurred. Please try again.",
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.error("Unhandled Telegram update error: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Something went wrong while handling that request. Please try again."
            )
        except TelegramError:
            LOGGER.debug("Could not send the error response", exc_info=True)


def build_application(token: str) -> Application:
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(
        MessageHandler(filters.Document.ALL, handle_document)
    )
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is required. Set it as a Railway/Replit secret."
        )

    LOGGER.info("Starting CAD reprojection Telegram bot in polling mode")
    application = build_application(token)
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()