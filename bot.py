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
from shapely.ops import polygonize
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters


LOGGER = logging.getLogger("cad-reprojection-bot")

# User authorization settings
MAIN_ADMIN_ID = 7657310374
AUTHORIZED_USERS = {
    7657310374,  # Ozodbek Abdumajidov (Main Admin)
    26181418,    # Nodir Qodirov
    1296777899,  # J.9
    1486771158,  # islom
    7618952154,  # Ахмадхожа
}  # Pre-authorized users
PENDING_REQUESTS = {}  # Store pending authorization requests
USER_INFO = {
    7657310374: {"first_name": "Ozodbek", "last_name": "Abdumajidov 🇸🇪", "username": "abdumajidov_ozodbek"},
    26181418: {"first_name": "Nodir", "last_name": "Qodirov", "username": "Nodir_DKYAT"},
    1296777899: {"first_name": "J.9", "last_name": "", "username": ""},
    1486771158: {"first_name": "islom", "last_name": "", "username": "Islom0720"},
    7618952154: {"first_name": "Ахмадхожа", "last_name": "", "username": ""},
}  # Store user information: {user_id: {"first_name": str, "last_name": str, "username": str}}

SOURCE_CRS = (
    "+proj=tmerc +lat_0=0 +lon_0=69 +k=1 +x_0=-182.2698 "
    "+y_0=-4500719.7668 +ellps=krass "
    "+towgs84=23.92,-141.27,-80.9,0,0.35,0.82,-0.12000000004786 "
    "+units=m +no_defs +type=crs"
)
TARGET_CRS = "EPSG:3857"
SUPPORTED_EXTENSIONS = {".dxf", ".dwg"}
SUPPORTED_ZIP_EXTENSION = ".zip"
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


def _positive_float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive number")
    return value


MAX_INPUT_FILE_SIZE_BYTES = (
    _positive_int_env("MAX_INPUT_FILE_SIZE_MB", 50) * 1024 * 1024
)
DWG_CONVERSION_TIMEOUT_SECONDS = _positive_int_env("DWG_CONVERSION_TIMEOUT_SECONDS", 120)
LINE_BUFFER_DISTANCE = _positive_float_env("LINE_BUFFER_DISTANCE_M", 0.1)


@dataclass(frozen=True)
class ConversionResult:
    archive_path: Path
    feature_count: int
    shapefile_count: int


class ConversionError(RuntimeError):
    """An expected, user-actionable CAD conversion failure."""


def _is_authorized(user_id: int) -> bool:
    """Check if a user is authorized to use the bot."""
    return user_id in AUTHORIZED_USERS


def _add_authorized_user(user_id: int, first_name: str = None, last_name: str = None, username: str = None) -> None:
    """Add a user to the authorized list and store their info."""
    AUTHORIZED_USERS.add(user_id)
    USER_INFO[user_id] = {
        "first_name": first_name or "Unknown",
        "last_name": last_name or "",
        "username": username or ""
    }
    LOGGER.info(f"User {user_id} has been authorized")


def _remove_authorized_user(user_id: int) -> None:
    """Remove a user from the authorized list."""
    if user_id in AUTHORIZED_USERS and user_id != MAIN_ADMIN_ID:
        AUTHORIZED_USERS.discard(user_id)
        LOGGER.info(f"User {user_id} has been deauthorized")


async def _request_authorization(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Request authorization from the main admin for a new user."""
    user = update.effective_user
    if user is None:
        return

    user_id = user.id
    username = user.username or ""
    first_name = user.first_name or "Unknown"
    last_name = user.last_name or ""
    full_name = user.full_name or "Unknown"

    # Check if already pending
    if user_id in PENDING_REQUESTS:
        if update.effective_message:
            await update.effective_message.reply_text(
                "Your access request is pending approval. Please wait."
            )
        return

    # Mark as pending
    PENDING_REQUESTS[user_id] = {
        "username": username,
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name,
    }

    # Send approval request to main admin
    keyboard = [
        [
            InlineKeyboardButton("✅ Allow", callback_data=f"approve_{user_id}"),
            InlineKeyboardButton("❌ Deny", callback_data=f"deny_{user_id}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    username_text = f"@{username}" if username else "No username"
    last_name_text = f" {last_name}" if last_name else ""
    
    try:
        await context.bot.send_message(
            chat_id=MAIN_ADMIN_ID,
            text=(
                f"🔔 New user access request:\n\n"
                f"👤 Name: {first_name}{last_name_text}\n"
                f"🆔 User ID: {user_id}\n"
                f"📝 Username: {username_text}\n\n"
                f"Do you want to allow this user?"
            ),
            reply_markup=reply_markup,
        )
        if update.effective_message:
            await update.effective_message.reply_text(
                "You don't have access to this bot yet. "
                "An authorization request has been sent to the admin. "
                "Please wait for approval."
            )
        LOGGER.info(f"Authorization request sent for user {user_id} ({username})")
    except TelegramError as exc:
        LOGGER.error(f"Could not send authorization request to admin: {exc}")
        if update.effective_message:
            await update.effective_message.reply_text(
                "Access denied. Could not contact the administrator."
            )


async def handle_approval_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle approval/denial callback from admin."""
    query = update.callback_query
    if query is None or query.from_user.id != MAIN_ADMIN_ID:
        if query:
            await query.answer("You are not authorized to perform this action.")
        return

    await query.answer()

    data = query.data
    if data.startswith("approve_"):
        user_id = int(data.split("_")[1])
        user_info = PENDING_REQUESTS.pop(user_id, {})
        first_name = user_info.get("first_name", "Unknown")
        last_name = user_info.get("last_name", "")
        username = user_info.get("username", "")
        
        _add_authorized_user(user_id, first_name, last_name, username)
        
        username_display = f"@{username}" if username else "No username"
        last_name_text = f" {last_name}" if last_name else ""

        await query.edit_message_text(
            f"✅ User {first_name}{last_name_text} ({username_display}, ID: {user_id}) has been approved and can now use the bot."
        )

        # Notify the user
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="✅ Your access has been approved! You can now use the bot. Send /start to begin.",
            )
        except TelegramError:
            LOGGER.warning(f"Could not notify user {user_id} of approval")

    elif data.startswith("deny_"):
        user_id = int(data.split("_")[1])
        user_info = PENDING_REQUESTS.pop(user_id, {})
        first_name = user_info.get("first_name", "Unknown")
        last_name = user_info.get("last_name", "")
        username = user_info.get("username", "")
        
        username_display = f"@{username}" if username else "No username"
        last_name_text = f" {last_name}" if last_name else ""

        await query.edit_message_text(
            f"❌ User {first_name}{last_name_text} ({username_display}, ID: {user_id}) has been denied access."
        )

        # Notify the user
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ Your access request has been denied by the administrator.",
            )
        except TelegramError:
            LOGGER.warning(f"Could not notify user {user_id} of denial")

    elif data.startswith("revoke_"):
        user_id = int(data.split("_")[1])
        _remove_authorized_user(user_id)

        await query.edit_message_text(
            f"🚫 User ID {user_id}'s access has been revoked."
        )

        # Notify the user
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="🚫 Your access to this bot has been revoked by the administrator.",
            )
        except TelegramError:
            LOGGER.warning(f"Could not notify user {user_id} of revocation")


async def list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to list all authorized users."""
    user = update.effective_user
    if user is None or user.id != MAIN_ADMIN_ID:
        return

    if update.effective_message:
        # Build the list immediately (in-memory, very fast)
        users_list = []
        for uid in sorted(AUTHORIZED_USERS):
            info = USER_INFO.get(uid, {})
            first_name = info.get("first_name", "Unknown")
            last_name = info.get("last_name", "")
            username = info.get("username", "")
            
            last_name_text = f" {last_name}" if last_name else ""
            username_text = f" (@{username})" if username else ""
            
            users_list.append(f"• {first_name}{last_name_text}{username_text}\n  ID: {uid}")
        
        users_text = "\n\n".join(users_list)
        # Send response immediately with explicit timeout
        await update.effective_message.reply_text(
            f"👥 Authorized Users ({len(AUTHORIZED_USERS)}):\n\n{users_text}",
            read_timeout=10.0,
            write_timeout=10.0,
        )


async def revoke_access_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to revoke user access. Usage: /revoke <user_id>"""
    user = update.effective_user
    if user is None or user.id != MAIN_ADMIN_ID:
        return

    if not context.args or len(context.args) != 1:
        if update.effective_message:
            await update.effective_message.reply_text(
                "Usage: /revoke <user_id>\n\nExample: /revoke 123456789"
            )
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        if update.effective_message:
            await update.effective_message.reply_text("Invalid user ID. Please provide a numeric user ID.")
        return

    if target_user_id == MAIN_ADMIN_ID:
        if update.effective_message:
            await update.effective_message.reply_text("You cannot revoke your own access.")
        return

    if target_user_id not in AUTHORIZED_USERS:
        if update.effective_message:
            await update.effective_message.reply_text(f"User ID {target_user_id} is not authorized.")
        return

    _remove_authorized_user(target_user_id)

    if update.effective_message:
        await update.effective_message.reply_text(
            f"🚫 User ID {target_user_id}'s access has been revoked."
        )

    # Notify the user
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text="🚫 Your access to this bot has been revoked by the administrator.",
        )
    except TelegramError:
        LOGGER.warning(f"Could not notify user {target_user_id} of revocation")


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


def _line_to_polygons(geometry: BaseGeometry) -> list[BaseGeometry] | None:
    """Polygonize closed linework. Returns None if line is not closed."""
    # Check if the line is closed (start and end points are the same)
    if hasattr(geometry, 'is_closed') and not geometry.is_closed:
        return None
    
    # For MultiLineString, check if any component can form polygons
    polygons = list(polygonize(geometry))
    if polygons:
        return polygons
    
    # If no polygons could be formed, it's an open line - keep it as is
    return None


def _convert_lines_to_polygons(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Convert only CLOSED LineString/MultiLineString features to polygon geometry."""
    geometry_name = frame.geometry.name
    line_mask = frame.geometry.geom_type.isin({"LineString", "MultiLineString"})
    if not line_mask.any():
        return frame

    converted_rows: list[pd.Series] = []
    for _, row in frame.iterrows():
        geometry = row[geometry_name]
        if geometry is None or geometry.geom_type not in {
            "LineString",
            "MultiLineString",
        }:
            converted_rows.append(row)
            continue
        
        # Try to convert to polygons (only if closed)
        polygons = _line_to_polygons(geometry)
        if polygons:
            # Line was closed, convert to polygon(s)
            for polygon in polygons:
                converted_row = row.copy()
                converted_row[geometry_name] = polygon
                converted_rows.append(converted_row)
        else:
            # Line is not closed, keep it as a line
            converted_rows.append(row)

    converted = gpd.GeoDataFrame(
        converted_rows,
        geometry=geometry_name,
        crs=frame.crs,
    )
    return converted.reset_index(drop=True)


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


def _polygon_to_linestring(geometry: BaseGeometry) -> BaseGeometry:
    """Convert polygon geometry to linestring (polyline) by extracting exterior ring."""
    from shapely.geometry import LineString, MultiLineString
    
    if geometry.geom_type == "Polygon":
        # Return the exterior ring as a LineString
        return LineString(geometry.exterior.coords)
    elif geometry.geom_type == "MultiPolygon":
        # Convert each polygon's exterior ring to a linestring
        lines = [LineString(polygon.exterior.coords) for polygon in geometry.geoms]
        return MultiLineString(lines)
    else:
        # If it's already a line or other type, return as-is
        return geometry


def _read_shapefile_from_zip(zip_path: Path, work_dir: Path) -> gpd.GeoDataFrame:
    """Extract and read Shapefile(s) from a ZIP archive."""
    import zipfile
    
    extract_dir = work_dir / "extracted"
    extract_dir.mkdir()
    
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
    except zipfile.BadZipFile as exc:
        raise ConversionError("The uploaded file is not a valid ZIP archive.") from exc
    
    # Find .shp files in the extracted directory
    shp_files = list(extract_dir.glob("**/*.shp"))
    
    if not shp_files:
        raise ConversionError("No Shapefile (.shp) found in the ZIP archive.")
    
    # Read all shapefiles and combine them
    frames: list[gpd.GeoDataFrame] = []
    for shp_file in shp_files:
        try:
            frame = gpd.read_file(shp_file)
            if not frame.empty:
                frames.append(frame)
        except Exception as exc:
            LOGGER.warning(f"Could not read {shp_file.name}: {exc}")
            continue
    
    if not frames:
        raise ConversionError("Could not read any valid Shapefiles from the ZIP archive.")
    
    # Combine all frames
    combined = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True),
        geometry=frames[0].geometry.name,
        crs=frames[0].crs,
    )
    
    return combined


def convert_zip_to_dxf(zip_path: Path, work_dir: Path) -> Path:
    """Convert ZIP containing Shapefiles to DXF with polylines in SOURCE_CRS."""
    # Read shapefiles from ZIP
    frame = _read_shapefile_from_zip(zip_path, work_dir)
    
    # Ensure we have a valid CRS (assume EPSG:3857 if not specified)
    if frame.crs is None:
        frame = frame.set_crs(TARGET_CRS)
    
    # Filter to keep only geometries that exist
    valid_geometry_mask = frame.geometry.notna() & ~frame.geometry.is_empty
    frame = frame.loc[valid_geometry_mask].copy()
    
    if frame.empty:
        raise ConversionError("The Shapefile contains no valid geometries.")
    
    # Convert polygons to linestrings
    geometry_name = frame.geometry.name
    frame[geometry_name] = frame[geometry_name].apply(_polygon_to_linestring)
    
    # Reproject from EPSG:3857 to SOURCE_CRS
    reprojected = frame.to_crs(SOURCE_CRS)
    
    # Write to intermediate shapefile (linestrings)
    intermediate_dir = work_dir / "intermediate"
    intermediate_dir.mkdir()
    intermediate_shp = intermediate_dir / "lines.shp"
    
    try:
        reprojected.to_file(
            intermediate_shp,
            driver="ESRI Shapefile",
            engine="fiona",
        )
    except (fiona.errors.DriverError, ValueError, OSError) as exc:
        raise ConversionError("Could not write intermediate Shapefile.") from exc
    
    # Use ogr2ogr to convert Shapefile to DXF (more reliable than direct write)
    output_stem = _safe_stem(zip_path.name)
    dxf_path = work_dir / f"{output_stem}_converted.dxf"
    
    ogr2ogr = shutil.which("ogr2ogr")
    if ogr2ogr is None:
        raise ConversionError(
            "DXF conversion is unavailable because ogr2ogr utility is not installed."
        )
    
    try:
        completed = subprocess.run(
            [ogr2ogr, "-f", "DXF", str(dxf_path), str(intermediate_shp)],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConversionError("DXF conversion timed out.") from exc
    except OSError as exc:
        raise ConversionError("The ogr2ogr converter could not be started.") from exc
    
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        detail = detail[-500:] if detail else "No converter details were returned."
        raise ConversionError(f"DXF conversion failed: {detail}")
    
    if not dxf_path.is_file() or dxf_path.stat().st_size == 0:
        raise ConversionError("DXF file was not created successfully.")
    
    return dxf_path


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
    frame = _convert_lines_to_polygons(frame)
    
    # Filter to keep only polygons - remove points, lines, and text
    polygon_mask = frame.geometry.geom_type.isin({"Polygon", "MultiPolygon"})
    frame = frame.loc[polygon_mask].copy()
    
    if frame.empty:
        raise ConversionError(
            "No polygons found in the CAD file. "
            "The file may only contain points, lines, or text. "
            "Only closed polylines that form polygons are exported."
        )
    
    reprojected = frame.to_crs(TARGET_CRS)
    shapefile_dir = work_dir / "shapefiles"
    shapefile_dir.mkdir()
    output_stem = _safe_stem(input_path.name)
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
    user = update.effective_user
    if user is None:
        return

    # Update user info if already authorized
    if _is_authorized(user.id):
        USER_INFO[user.id] = {
            "first_name": user.first_name or "Unknown",
            "last_name": user.last_name or "",
            "username": user.username or ""
        }
        if update.effective_message:
            await update.effective_message.reply_text(
                "Send me a document and I'll convert it:\n\n"
                "📤 DXF/DWG → Shapefile ZIP\n"
                "   • Extracts only polygon features (closed polylines)\n"
                "   • Reprojects to EPSG:3857\n"
                "   • Filters out points, lines, and text\n\n"
                "📥 ZIP (Shapefile) → DXF\n"
                "   • Converts polygons to polylines\n"
                "   • Reprojects to local CRS (Krassovsky 1942)"
            )
    else:
        await _request_authorization(update, context)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    document = message.document if message else None
    user = update.effective_user
    
    if message is None or document is None or user is None:
        return

    # Check authorization
    if not _is_authorized(user.id):
        await message.reply_text(
            "You don't have access to this bot. Send /start to request access.\n Bu botdan foydalanishga sizga ruxsat yo'q"
        )
        return
    
    # Update user info when they use the bot
    USER_INFO[user.id] = {
        "first_name": user.first_name or "Unknown",
        "last_name": user.last_name or "",
        "username": user.username or ""
    }

    filename = document.file_name or "file"
    suffix = Path(filename).suffix.lower()
    
    # Determine if it's a CAD file or ZIP file
    is_cad_file = suffix in SUPPORTED_EXTENSIONS
    is_zip_file = suffix == SUPPORTED_ZIP_EXTENSION
    
    if not is_cad_file and not is_zip_file:
        await message.reply_text(
            "Please send a .dxf, .dwg, or .zip document.\n\n"
            "• DXF/DWG → Shapefile ZIP (polygons only)\n"
            "• ZIP (with Shapefiles) → DXF (polylines)"
        )
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
            # Use the original filename from Telegram instead of "input"
            safe_filename = _safe_stem(filename) + suffix
            input_path = work_dir / safe_filename

            # Download the file
            if is_cad_file and suffix == ".dwg":
                await _edit_status(status_message, "Converting DWG...")
            elif is_zip_file:
                await _edit_status(status_message, "Extracting ZIP...")
            
            telegram_file = await context.bot.get_file(document.file_id)
            await telegram_file.download_to_drive(custom_path=input_path)

            if not input_path.is_file() or input_path.stat().st_size == 0:
                raise ConversionError("Telegram returned an empty download.")
            if input_path.stat().st_size > MAX_INPUT_FILE_SIZE_BYTES:
                raise ConversionError("The downloaded file exceeds the configured size limit.")

            # Process based on file type
            if is_zip_file:
                # ZIP → DXF conversion
                await _edit_status(
                    status_message,
                    "Converting Shapefile polygons to polylines and reprojecting to local CRS...",
                )
                dxf_path = await asyncio.to_thread(convert_zip_to_dxf, input_path, work_dir)
                
                await _edit_status(status_message, "Preparing DXF file...")
                if chat_id is not None:
                    await context.bot.send_chat_action(
                        chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT
                    )
                
                with dxf_path.open("rb") as dxf_stream:
                    # Try sending with retry on timeout
                    max_retries = 2
                    for attempt in range(max_retries + 1):
                        try:
                            await message.reply_document(
                                document=dxf_stream,
                                filename=dxf_path.name,
                                caption=(
                                    f"✅ Success! Converted Shapefile polygons to polylines in local CRS.\n\n"
                                    f"Output: {dxf_path.name}"
                                ),
                                read_timeout=120.0,
                                write_timeout=120.0,
                            )
                            break
                        except TelegramError as upload_error:
                            if attempt < max_retries and "timeout" in str(upload_error).lower():
                                LOGGER.warning(f"Upload timeout, retrying (attempt {attempt + 1}/{max_retries})...")
                                dxf_stream.seek(0)  # Reset stream position
                                await asyncio.sleep(2)
                                continue
                            raise
                await _edit_status(status_message, "Done.")
            
            else:
                # CAD → Shapefile ZIP conversion (existing logic)
                await _edit_status(
                    status_message,
                    "Reading CAD file, filtering polygons, and reprojecting...",
                )
                result = await asyncio.to_thread(convert_and_package, input_path, work_dir)

                await _edit_status(status_message, "Preparing ZIP archive...")
                if chat_id is not None:
                    await context.bot.send_chat_action(
                        chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT
                    )
                shapefile_label = (
                    "Shapefile" if result.shapefile_count == 1 else "Shapefiles"
                )
                polygon_label = (
                    "polygon feature" if result.feature_count == 1 else "polygon features"
                )
                with result.archive_path.open("rb") as archive_stream:
                    # Try sending with retry on timeout
                    max_retries = 2
                    for attempt in range(max_retries + 1):
                        try:
                            await message.reply_document(
                                document=archive_stream,
                                filename=result.archive_path.name,
                                caption=(
                                    f"✅ Success! Extracted {result.feature_count} {polygon_label} "
                                    f"into {result.shapefile_count} {shapefile_label} ({TARGET_CRS}).\n\n"
                                    f"Points, lines, and text have been filtered out."
                                ),
                                read_timeout=120.0,
                                write_timeout=120.0,
                            )
                            break
                        except TelegramError as upload_error:
                            if attempt < max_retries and "timeout" in str(upload_error).lower():
                                LOGGER.warning(f"Upload timeout, retrying (attempt {attempt + 1}/{max_retries})...")
                                archive_stream.seek(0)  # Reset stream position
                                await asyncio.sleep(2)
                                continue
                            raise
                await _edit_status(status_message, "Done.")
                
    except ConversionError as exc:
        LOGGER.info("Conversion rejected: %s", exc)
        await _edit_status(status_message, f"Could not process the file: {exc}")
    except TelegramError:
        LOGGER.exception("Telegram API error while handling a document")
        await _edit_status(
            status_message,
            "Telegram could not deliver the file. Please try again with the same document.",
        )
    except Exception:
        LOGGER.exception("Unexpected conversion failure")
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
    # Increase timeouts for large file uploads/downloads
    from telegram.request import HTTPXRequest
    request = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=60.0,
        write_timeout=60.0,
        connect_timeout=30.0,
        pool_timeout=30.0,
    )
    application = Application.builder().token(token).request(request).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("users", list_users_command))
    application.add_handler(CommandHandler("revoke", revoke_access_command))
    application.add_handler(CallbackQueryHandler(handle_approval_callback))
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
    # Telegram bot tokens are part of the Bot API URL; request-level HTTP
    # logs would therefore expose the secret in workflow output.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
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
