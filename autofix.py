"""Repair buildings that extend slightly outside their land plot polygon.

CAD operators digitise a land plot and the buildings on it as separate closed
polylines in one drawing. Rounding and snapping errors routinely leave a
building a fraction of a millimetre outside the plot, which makes downstream
cadastral validation reject the whole file with "the building is not fully
inside the land plot". The manual repair is to drag the offending building
vertices back onto the plot boundary.

This module automates that repair:

1. The largest polygon in the drawing is the land plot. Every other polygon is
   a building. (Layer names and colours are not reliable; real drawings put
   everything on layer "0".)
2. Building vertices that fall outside the plot but within a small tolerance
   are projected onto the nearest point of the plot boundary.
3. Anything still protruding after snapping, for instance a long edge bulging
   out between two snapped corners, is clipped away.
4. Buildings whose overhang is too large to be a digitising slip are left
   untouched and reported. A building far outside its plot indicates a wrong
   coordinate, a wrong CRS zone or the wrong plot attached to the record.
   Clipping it would produce a plausible-looking but incorrect footprint that
   passes validation, which is worse than an error message.

The land plot geometry is never modified.

All geometry is expected in a projected CRS whose units are metres, because
every threshold is expressed in metres. Callers holding EPSG:3857 data must
reproject first: Web Mercator distances are inflated by roughly 1/cos(latitude),
about 1.33x at latitude 41, so tolerances evaluated there would not mean what
they say.

No Telegram imports live here, so the logic is unit testable on its own.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

import geopandas as gpd
from shapely.geometry import (
    GeometryCollection,
    MultiPolygon,
    Point,
    Polygon,
)
from shapely.geometry.base import BaseGeometry

LOGGER = logging.getLogger("cad-reprojection-bot.autofix")

POLYGON_GEOMETRY_TYPES = frozenset({"Polygon", "MultiPolygon"})

# Precision for measuring how wide an intrusion is. A tenth of a millimetre is
# far finer than any tolerance the repair works with.
_WIDTH_PRECISION_M = 1e-4
# Measuring stops here. Anything this wide is a gross conflict, and the exact
# figure adds nothing to the verdict.
_WIDTH_SEARCH_CAP_M = 100.0


class AutofixError(RuntimeError):
    """An expected, user-actionable problem with the drawing's geometry."""


class Verdict(str, Enum):
    """How a single building relates to its land plot."""

    ALREADY_INSIDE = "already_inside"
    FIXABLE = "fixable"
    TOO_FAR = "too_far"


class Action(str, Enum):
    """What was done to a single building."""

    UNCHANGED = "unchanged"
    FIXED = "fixed"
    FLAGGED = "flagged"


def _positive_float_env(name: str, default: float) -> float:
    """Read a strictly positive float from the environment.

    Mirrors bot.py's helper so configuration behaves consistently across the
    project: an unset variable falls back to the default, a malformed or
    non-positive value is a startup error rather than a silent surprise.
    """
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


@dataclass(frozen=True)
class AutofixConfig:
    """Tunable limits for the repair, all in metres or square metres.

    The defaults are calibrated against a real failing drawing whose building
    protruded 0.6 mm over 0.005 m^2. They are deliberately tight: the tolerance
    is what separates "obvious digitising slip, safe to correct" from "real
    error, must be reported", so a loose value would quietly reshape genuinely
    wrong geometry.
    """

    # Furthest a single vertex may be moved while snapping.
    snap_tolerance_m: float = 0.05
    # Widest overlap still corrected automatically between two buildings of the
    # same parcel. Looser than the plot tolerance on purpose: where two
    # buildings share a wall, the two drawn lines routinely disagree by about a
    # wall thickness, and the measured sample was 10.14 cm. A plot boundary
    # carries legal weight and gets the tighter limit.
    sibling_tolerance_m: float = 0.15
    # Absolute ceiling on the area protruding outside the plot.
    max_outside_area_m2: float = 0.25
    # Alternative relative ceiling, as a fraction of the building's own area.
    # The effective limit is whichever of the two is larger.
    max_outside_area_ratio: float = 0.01
    # Clip fragments smaller than this are numerical dust and get dropped.
    min_part_area_m2: float = 0.05
    # Residual area tolerated when verifying that a fixed building is inside
    # the plot. This is a validation epsilon only. It is never used to shrink
    # the plot before clipping: a 1 mm shrink would shave the building's entire
    # perimeter and remove far more area than the defect being repaired.
    residual_epsilon_m2: float = 1e-6

    @classmethod
    def from_env(cls) -> "AutofixConfig":
        """Build a configuration from AUTOFIX_* environment variables."""
        defaults = cls()
        return cls(
            snap_tolerance_m=_positive_float_env(
                "AUTOFIX_SNAP_TOLERANCE_M", defaults.snap_tolerance_m
            ),
            sibling_tolerance_m=_positive_float_env(
                "AUTOFIX_SIBLING_TOLERANCE_M", defaults.sibling_tolerance_m
            ),
            max_outside_area_m2=_positive_float_env(
                "AUTOFIX_MAX_OUTSIDE_AREA_M2", defaults.max_outside_area_m2
            ),
            max_outside_area_ratio=_positive_float_env(
                "AUTOFIX_MAX_OUTSIDE_AREA_RATIO", defaults.max_outside_area_ratio
            ),
            min_part_area_m2=_positive_float_env(
                "AUTOFIX_MIN_PART_AREA_M2", defaults.min_part_area_m2
            ),
            residual_epsilon_m2=_positive_float_env(
                "AUTOFIX_RESIDUAL_EPSILON_M2", defaults.residual_epsilon_m2
            ),
        )

    @classmethod
    def finishing(cls) -> "AutofixConfig":
        """Very tight limits for a second pass in the output CRS.

        Reprojection is not linear, so a vertex snapped exactly onto a plot
        segment in the source CRS does not stay on it: the segment is redrawn
        as a straight chord between its transformed endpoints while the vertex
        follows the true curve. The gap is minute, well under a millimetre over
        a 10 m edge, but it is enough to make a cadastral validator reject the
        exported file. A finishing pass with a tolerance far below any real
        surveying error removes it without being able to touch genuine defects.
        """
        return cls(
            snap_tolerance_m=0.01,
            max_outside_area_m2=0.05,
            max_outside_area_ratio=0.0001,
            min_part_area_m2=0.05,
        )

    def for_siblings(self) -> "AutofixConfig":
        """This configuration with the looser building-to-building tolerance."""
        return replace(self, snap_tolerance_m=self.sibling_tolerance_m)

    def allowed_outside_area(self, building_area: float) -> float:
        """Largest protrusion area still considered a digitising slip."""
        return max(self.max_outside_area_m2, self.max_outside_area_ratio * building_area)


@dataclass(frozen=True)
class Measurement:
    """How far, and by how much, a building leaves its plot."""

    verdict: Verdict
    outside_area: float
    max_outward_distance: float
    allowed_outside_area: float

    @property
    def needs_fixing(self) -> bool:
        return self.verdict is Verdict.FIXABLE


@dataclass(frozen=True)
class BuildingFix:
    """Outcome for one building row."""

    row_index: int
    action: Action
    outside_area_before: float
    outside_area_after: float
    max_vertex_shift: float
    area_removed: float
    vertices_snapped: int
    was_clipped: bool
    warning: str | None = None


@dataclass(frozen=True)
class ObstacleFix:
    """Outcome for one row checked against immutable neighbouring land."""

    row_index: int
    action: Action
    is_plot: bool
    overlap_area_before: float
    overlap_area_after: float
    max_penetration: float
    area_before: float
    area_after: float
    warning: str | None = None

    @property
    def area_removed(self) -> float:
        return max(self.area_before - self.area_after, 0.0)


@dataclass
class NeighbourReport:
    """Aggregate outcome of clearing our parcel off neighbouring land."""

    plot_row_index: int | None = None
    obstacle_count: int = 0
    fixes: list[ObstacleFix] = field(default_factory=list)
    z_was_dropped: bool = False

    @property
    def fixed(self) -> list[ObstacleFix]:
        return [fix for fix in self.fixes if fix.action is Action.FIXED]

    @property
    def flagged(self) -> list[ObstacleFix]:
        return [fix for fix in self.fixes if fix.action is Action.FLAGGED]

    @property
    def plot_fix(self) -> ObstacleFix | None:
        for fix in self.fixes:
            if fix.is_plot:
                return fix
        return None

    @property
    def changed_anything(self) -> bool:
        return bool(self.fixed)

    def summary_lines(self) -> list[str]:
        """Human-readable account of what the neighbour check changed."""
        lines = [f"Neighbouring polygons checked against: {self.obstacle_count}"]

        plot_fix = self.plot_fix
        if plot_fix is not None and plot_fix.action is Action.FIXED:
            lines.append(
                f"Land plot trimmed clear of neighbours: "
                f"{plot_fix.area_before:,.3f} → {plot_fix.area_after:,.3f} m² "
                f"(−{plot_fix.area_removed:.6f} m²)"
            )
        elif plot_fix is not None and plot_fix.action is Action.UNCHANGED:
            lines.append("Land plot: no overlap with neighbours")

        building_fixes = [fix for fix in self.fixed if not fix.is_plot]
        if building_fixes:
            removed = sum(fix.area_removed for fix in building_fixes)
            lines.append(
                f"Buildings trimmed clear of neighbours: {len(building_fixes)} "
                f"(−{removed:.6f} m² total)"
            )

        if self.flagged:
            lines.append(f"⚠️ Conflicts left alone: {len(self.flagged)}")
            for fix in self.flagged:
                lines.append(f"   • row {fix.row_index}: {fix.warning}")
        return lines


@dataclass(frozen=True)
class SiblingFix:
    """Outcome for one pair of overlapping buildings on the same parcel."""

    # The building that gave up the overlap, and the one left untouched.
    trimmed_row: int
    kept_row: int
    action: Action
    overlap_area: float
    max_width: float
    area_removed: float
    warning: str | None = None


@dataclass
class SiblingReport:
    """Aggregate outcome of separating buildings that overlapped each other."""

    plot_row_index: int | None = None
    building_count: int = 0
    pairs: list[SiblingFix] = field(default_factory=list)
    z_was_dropped: bool = False

    @property
    def fixed(self) -> list[SiblingFix]:
        return [pair for pair in self.pairs if pair.action is Action.FIXED]

    @property
    def flagged(self) -> list[SiblingFix]:
        return [pair for pair in self.pairs if pair.action is Action.FLAGGED]

    @property
    def changed_anything(self) -> bool:
        return bool(self.fixed)

    def summary_lines(self) -> list[str]:
        lines = [f"Buildings checked against each other: {self.building_count}"]
        if not self.pairs:
            lines.append("No buildings overlap each other")
            return lines

        for pair in self.fixed:
            lines.append(
                f"Separated buildings {pair.kept_row} and {pair.trimmed_row}: "
                f"{pair.overlap_area:.6f} m² overlap, {pair.max_width * 100:.2f} cm "
                f"wide, trimmed off building {pair.trimmed_row}"
            )
        if self.flagged:
            lines.append(f"⚠️ Overlaps left alone: {len(self.flagged)}")
            for pair in self.flagged:
                lines.append(
                    f"   • buildings {pair.kept_row} and {pair.trimmed_row}: "
                    f"{pair.warning}"
                )
        return lines


@dataclass
class AutofixReport:
    """Aggregate outcome, suitable for a user-facing summary."""

    plot_row_index: int | None = None
    plot_area: float = 0.0
    building_count: int = 0
    fixes: list[BuildingFix] = field(default_factory=list)
    # True when 3D input had to be flattened to keep the output layer uniform.
    z_was_dropped: bool = False

    @property
    def fixed(self) -> list[BuildingFix]:
        return [fix for fix in self.fixes if fix.action is Action.FIXED]

    @property
    def flagged(self) -> list[BuildingFix]:
        return [fix for fix in self.fixes if fix.action is Action.FLAGGED]

    @property
    def unchanged(self) -> list[BuildingFix]:
        return [fix for fix in self.fixes if fix.action is Action.UNCHANGED]

    @property
    def total_area_removed(self) -> float:
        return sum(fix.area_removed for fix in self.fixes)

    @property
    def max_vertex_shift(self) -> float:
        return max((fix.max_vertex_shift for fix in self.fixes), default=0.0)

    @property
    def changed_anything(self) -> bool:
        return bool(self.fixed)

    def summary_lines(self) -> list[str]:
        """Human-readable report lines describing exactly what changed."""
        lines: list[str] = []
        if self.plot_row_index is None:
            lines.append("No land plot polygon was identified.")
            return lines

        lines.append(f"Land plot: {self.plot_area:,.2f} m² (unchanged)")
        lines.append(f"Buildings checked: {self.building_count}")

        if self.fixed:
            lines.append(
                f"Corrected: {len(self.fixed)} "
                f"(max vertex shift {self.max_vertex_shift * 1000:.2f} mm, "
                f"area removed {self.total_area_removed:.6f} m²)"
            )
        if self.unchanged:
            lines.append(f"Already inside: {len(self.unchanged)}")
        if self.flagged:
            lines.append(f"⚠️ Not corrected: {len(self.flagged)}")
            for fix in self.flagged:
                lines.append(f"   • building {fix.row_index}: {fix.warning}")
        if self.z_was_dropped:
            lines.append(
                "Note: elevation (Z) values were removed so the exported layer "
                "has a single geometry type."
            )
        return lines


def _repair(geometry: BaseGeometry) -> BaseGeometry:
    """Return a valid version of a possibly self-intersecting polygon.

    CAD linework polygonised into rings is frequently invalid by OGC rules.
    buffer(0) is the standard repair and is a no-op on already valid input.
    """
    if geometry is None or geometry.is_empty:
        return geometry
    if geometry.is_valid:
        return geometry
    return geometry.buffer(0)


def _polygon_parts(geometry: BaseGeometry) -> list[Polygon]:
    """Flatten any geometry into its constituent polygons."""
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, (MultiPolygon, GeometryCollection)):
        parts: list[Polygon] = []
        for part in geometry.geoms:
            parts.extend(_polygon_parts(part))
        return parts
    return []


def _all_ring_coords(geometry: BaseGeometry) -> list[tuple[float, float]]:
    """Every exterior and interior ring coordinate, flattened to 2D.

    CAD drawings and Shapefiles are frequently 3D, so a coordinate can arrive
    as (x, y, z). Only the plan position matters for containment, and slicing
    keeps this working for both 2D and 3D input.
    """
    coords: list[tuple[float, float]] = []
    for polygon in _polygon_parts(geometry):
        coords.extend((c[0], c[1]) for c in polygon.exterior.coords)
        for interior in polygon.interiors:
            coords.extend((c[0], c[1]) for c in interior.coords)
    return coords


def _max_outward_distance(outside: BaseGeometry, plot: BaseGeometry) -> float:
    """How deep the protruding part reaches beyond the plot boundary.

    Measured as the largest distance from any vertex of the protrusion to the
    plot boundary. Vertices of the protrusion that lie on the boundary
    contribute zero, so this reports the depth of the overhang rather than its
    width along the edge.
    """
    boundary = plot.boundary
    return max(
        (Point(x, y).distance(boundary) for x, y in _all_ring_coords(outside)),
        default=0.0,
    )


def identify_plot_and_buildings(
    frame: gpd.GeoDataFrame,
) -> tuple[int, list[int]]:
    """Split polygon rows into the land plot and its buildings.

    The largest polygon by area is the land plot. Ties resolve to the first
    row so the result is deterministic regardless of row ordering.

    Returns the plot's positional index and the buildings' positional indices.
    Raises AutofixError when the frame holds no polygonal geometry at all.
    """
    if frame.empty:
        raise AutofixError("The drawing contains no polygons to check.")

    geometry_types = frame.geometry.geom_type
    polygon_positions = [
        position
        for position, geometry_type in enumerate(geometry_types)
        if geometry_type in POLYGON_GEOMETRY_TYPES
    ]
    if not polygon_positions:
        raise AutofixError("The drawing contains no polygons to check.")

    areas = frame.geometry.area
    plot_position = max(polygon_positions, key=lambda position: areas.iloc[position])
    buildings = [
        position for position in polygon_positions if position != plot_position
    ]
    return plot_position, buildings


def classify_building(
    building: BaseGeometry, plot: BaseGeometry, config: AutofixConfig
) -> Measurement:
    """Decide whether a building is inside, slightly out, or genuinely wrong."""
    building = _repair(building)
    plot = _repair(plot)

    outside = building.difference(plot)
    outside_area = outside.area
    allowed = config.allowed_outside_area(building.area)

    if outside.is_empty or outside_area <= config.residual_epsilon_m2:
        return Measurement(
            verdict=Verdict.ALREADY_INSIDE,
            outside_area=outside_area,
            max_outward_distance=0.0,
            allowed_outside_area=allowed,
        )

    max_distance = _max_outward_distance(outside, plot)
    fixable = outside_area <= allowed and max_distance <= config.snap_tolerance_m
    return Measurement(
        verdict=Verdict.FIXABLE if fixable else Verdict.TOO_FAR,
        outside_area=outside_area,
        max_outward_distance=max_distance,
        allowed_outside_area=allowed,
    )


def _snap_ring_coords(
    coords: list[tuple[float, ...]],
    boundary: BaseGeometry,
    tolerance: float,
    should_move: "Callable[[Point], bool]",
) -> tuple[list[tuple[float, ...]], int, float]:
    """Project misplaced ring vertices onto a reference boundary.

    `should_move` decides which vertices are misplaced, which is what lets one
    routine serve both jobs: pulling a building back inside its own plot, and
    pushing geometry out of a neighbour it has crept into. Vertices further
    away than the tolerance are left alone; the caller's classification step
    has already ensured such geometry is not altered silently.

    Shapely's own shapely.ops.snap is not usable here because it only snaps
    vertex-to-vertex. The common defect is a corner crossing an *edge* with no
    vertex anywhere near it, so the target has to be computed by projecting
    onto the boundary.

    Input may be 2D or 3D. Only the plan position is adjusted; an elevation
    value rides along untouched so 3D drawings keep their Z values.
    """
    snapped: list[tuple[float, ...]] = []
    snapped_count = 0
    max_shift = 0.0

    for coordinate in coords:
        x, y = coordinate[0], coordinate[1]
        extra = tuple(coordinate[2:])
        vertex = Point(x, y)
        if not should_move(vertex):
            snapped.append(coordinate)
            continue
        distance = vertex.distance(boundary)
        # A vertex already sitting on the boundary needs no move, and counting
        # it would overstate what the repair actually did.
        if distance <= 0.0 or distance > tolerance:
            snapped.append(coordinate)
            continue
        target = boundary.interpolate(boundary.project(vertex))
        snapped.append((target.x, target.y) + extra)
        snapped_count += 1
        max_shift = max(max_shift, distance)

    # A closed ring must keep matching first and last coordinates, which stay
    # in step automatically only if both were snapped identically.
    if snapped and snapped[0] != snapped[-1]:
        snapped[-1] = snapped[0]

    return snapped, snapped_count, max_shift


def _snap_geometry(
    geometry: BaseGeometry,
    reference: BaseGeometry,
    tolerance: float,
    should_move: "Callable[[Point], bool]",
) -> tuple[BaseGeometry, int, float]:
    """Rebuild a polygonal geometry with its misplaced vertices snapped.

    Returns the adjusted geometry, how many vertices moved, and the largest
    distance any vertex travelled. Vertex count and ring structure are
    preserved, so a rectangular building stays rectangular. If snapping would
    produce invalid or degenerate geometry, the original is returned unchanged.
    """
    boundary = _repair(reference).boundary
    total_snapped = 0
    largest_shift = 0.0
    rebuilt_parts: list[Polygon] = []

    for polygon in _polygon_parts(_repair(geometry)):
        exterior, count, shift = _snap_ring_coords(
            list(polygon.exterior.coords), boundary, tolerance, should_move
        )
        total_snapped += count
        largest_shift = max(largest_shift, shift)

        interiors: list[list[tuple[float, ...]]] = []
        for interior in polygon.interiors:
            ring, count, shift = _snap_ring_coords(
                list(interior.coords), boundary, tolerance, should_move
            )
            total_snapped += count
            largest_shift = max(largest_shift, shift)
            if len(ring) >= 4:
                interiors.append(ring)

        if len(exterior) < 4:
            # Not enough distinct vertices to form a ring; keep the original.
            rebuilt_parts.append(polygon)
            continue

        try:
            candidate = Polygon(exterior, interiors)
        except (ValueError, TypeError):
            LOGGER.debug("Snapped ring could not form a polygon", exc_info=True)
            rebuilt_parts.append(polygon)
            continue

        candidate = _repair(candidate)
        if candidate.is_empty or candidate.area <= 0:
            rebuilt_parts.append(polygon)
            continue
        rebuilt_parts.append(candidate)

    if not rebuilt_parts or total_snapped == 0:
        return geometry, 0, 0.0

    result = rebuilt_parts[0] if len(rebuilt_parts) == 1 else MultiPolygon(rebuilt_parts)
    return _repair(result), total_snapped, largest_shift


def snap_to_plot(
    building: BaseGeometry, plot: BaseGeometry, tolerance: float
) -> tuple[BaseGeometry, int, float]:
    """Snap a building's outlying vertices onto its own plot boundary."""
    container = _repair(plot)
    return _snap_geometry(
        building,
        container,
        tolerance,
        should_move=lambda vertex: not container.covers(vertex),
    )


def clip_to_plot(
    building: BaseGeometry, plot: BaseGeometry, min_part_area: float
) -> BaseGeometry:
    """Cut away whatever still lies outside the plot.

    The intersection is taken against the plot exactly, not a shrunken copy: a
    building sharing an edge with its plot is still contained, and shrinking
    the plot would trim the building's whole perimeter instead of just the
    defect. Fragments below min_part_area are numerical dust and are dropped,
    unless that would discard everything, in which case the largest fragment
    survives so a building is never silently deleted.
    """
    clipped = _repair(building).intersection(_repair(plot))
    parts = _polygon_parts(clipped)
    if not parts:
        return clipped

    kept = [part for part in parts if part.area >= min_part_area]
    if not kept:
        kept = [max(parts, key=lambda part: part.area)]
    if len(kept) == 1:
        return kept[0]
    return MultiPolygon(kept)


def subtract_obstacles(
    geometry: BaseGeometry, obstacles: BaseGeometry, min_part_area: float
) -> BaseGeometry:
    """Cut away the part of our geometry that lies on someone else's land.

    Subtracting produces a boundary that coincides exactly with the neighbour's,
    which is what cadastral data wants: no overlap and no sliver gap. Fragments
    below min_part_area are dropped as numerical dust, unless that would discard
    everything, in which case the largest fragment survives so a feature is
    never silently deleted.
    """
    remaining = _repair(geometry).difference(_repair(obstacles))
    parts = _polygon_parts(remaining)
    if not parts:
        # Everything we had lies on the neighbour's land. Subtracting would
        # delete the feature outright, which is never an acceptable automatic
        # outcome; the caller's classification step flags this case instead.
        LOGGER.warning(
            "Refusing to subtract: the whole feature lies inside the obstacle"
        )
        return geometry

    kept = [part for part in parts if part.area >= min_part_area]
    if not kept:
        kept = [max(parts, key=lambda part: part.area)]
    if len(kept) == 1:
        return kept[0]
    return MultiPolygon(kept)


def _part_width(part: Polygon) -> float:
    """Width of a polygon at its widest point, twice the inscribed radius.

    Found by erosion: shrinking a shape by d leaves nothing exactly when d
    reaches the inscribed radius, so the width is located by doubling until the
    shape vanishes and then bisecting.

    shapely.ops.polylabel computes the same quantity, but it subdivides the
    bounding box down to the requested precision, and a parcel boundary
    mismatch is a sliver tens of metres long and a centimetre wide. Asking for
    0.1 mm precision across that made a single measurement take minutes.
    Erosion costs a handful of buffer operations regardless of the shape.
    """
    if part.is_empty or part.area <= 0:
        return 0.0

    upper = _WIDTH_PRECISION_M
    while upper < _WIDTH_SEARCH_CAP_M and not part.buffer(-upper / 2).is_empty:
        upper *= 2

    lower = upper / 2
    while upper - lower > _WIDTH_PRECISION_M:
        middle = (lower + upper) / 2
        if part.buffer(-middle / 2).is_empty:
            upper = middle
        else:
            lower = middle
    return upper


def _max_penetration(overlap: BaseGeometry) -> float:
    """How wide the intrusion is at its widest point.

    A boundary mismatch between two parcels shows up as a long, hair-thin
    sliver whose area grows with the length of the shared edge, which says
    nothing about severity. Width says everything: a 40 m boundary out by 2 cm
    covers more ground than a deliberate half-metre encroachment over 1 m, yet
    only one of them is a digitising error.
    """
    return max(
        (_part_width(part) for part in _polygon_parts(overlap)),
        default=0.0,
    )


def classify_against_obstacles(
    geometry: BaseGeometry, obstacles: BaseGeometry, config: AutofixConfig
) -> Measurement:
    """Decide whether an overlap with a neighbour is a slip or a real conflict."""
    geometry = _repair(geometry)
    obstacles = _repair(obstacles)

    overlap = geometry.intersection(obstacles)
    overlap_area = overlap.area
    allowed = config.allowed_outside_area(geometry.area)

    if overlap.is_empty or overlap_area <= config.residual_epsilon_m2:
        return Measurement(
            verdict=Verdict.ALREADY_INSIDE,
            outside_area=overlap_area,
            max_outward_distance=0.0,
            allowed_outside_area=allowed,
        )

    penetration = _max_penetration(overlap)
    fixable = overlap_area <= allowed and penetration <= config.snap_tolerance_m
    return Measurement(
        verdict=Verdict.FIXABLE if fixable else Verdict.TOO_FAR,
        outside_area=overlap_area,
        max_outward_distance=penetration,
        allowed_outside_area=allowed,
    )


def clear_of_obstacles(
    geometry: BaseGeometry,
    obstacles: BaseGeometry,
    config: AutofixConfig,
    is_plot: bool = False,
) -> tuple[BaseGeometry, ObstacleFix]:
    """Subtract a neighbour's land so our geometry no longer overlaps it.

    Returns the adjusted geometry and a record of what changed. Geometry that
    is already clear, or whose overlap is too large to be a boundary mismatch,
    comes back untouched.

    Unlike the in-plot repair, this does not snap vertices first, and the
    difference is not academic. Measured on the sample parcels, snapping then
    subtracting removed 0.094981 m² from an 82 m² building whose actual overlap
    was 0.006263 m², fifteen times too much, in exchange for saving one vertex.
    Moving a vertex out of the obstacle drags its two adjacent edges with it and
    sweeps away land that was never in conflict. Subtraction removes exactly the
    shared area and nothing else, which is the only defensible outcome for
    cadastral geometry. Snapping stays in the in-plot repair because there it
    *avoids* cutting a building instead of causing it.
    """
    original = _repair(geometry)
    measurement = classify_against_obstacles(original, obstacles, config)

    def record(action: Action, **kwargs: Any) -> ObstacleFix:
        defaults: dict[str, Any] = {
            "row_index": -1,
            "action": action,
            "is_plot": is_plot,
            "overlap_area_before": measurement.outside_area,
            "overlap_area_after": measurement.outside_area,
            "max_penetration": measurement.max_outward_distance,
            "area_before": original.area,
            "area_after": original.area,
            "warning": None,
        }
        defaults.update(kwargs)
        return ObstacleFix(**defaults)

    if measurement.verdict is Verdict.ALREADY_INSIDE:
        return geometry, record(Action.UNCHANGED, max_penetration=0.0)

    if measurement.verdict is Verdict.TOO_FAR:
        what = "land plot" if is_plot else "building"
        return geometry, record(
            Action.FLAGGED,
            warning=(
                f"the {what} overlaps registered neighbouring land by "
                f"{measurement.outside_area:.3f} m², up to "
                f"{measurement.max_outward_distance:.3f} m wide. That is too "
                "much to be a boundary mismatch, so nothing was changed. This "
                "needs a surveyor, not an automatic trim."
            ),
        )

    cleared = subtract_obstacles(original, obstacles, config.min_part_area_m2)
    overlap_after = cleared.intersection(_repair(obstacles)).area
    if overlap_after > config.residual_epsilon_m2:
        LOGGER.warning(
            "Could not clear an overlap: %.9f m² still shared", overlap_after
        )
        return geometry, record(
            Action.FLAGGED,
            warning=(
                "the overlap with the neighbour could not be removed "
                "automatically; the geometry was left unchanged."
            ),
        )

    return cleared, record(
        Action.FIXED,
        overlap_area_after=overlap_after,
        area_after=cleared.area,
    )


def fix_building(
    building: BaseGeometry, plot: BaseGeometry, config: AutofixConfig
) -> tuple[BaseGeometry, BuildingFix]:
    """Snap then clip one building so it sits inside its land plot.

    Returns the corrected geometry and a record of what changed. A building
    already inside, or one whose overhang is too large to be a digitising
    slip, is returned untouched.
    """
    original = _repair(building)
    measurement = classify_building(original, plot, config)

    if measurement.verdict is Verdict.ALREADY_INSIDE:
        return building, BuildingFix(
            row_index=-1,
            action=Action.UNCHANGED,
            outside_area_before=measurement.outside_area,
            outside_area_after=measurement.outside_area,
            max_vertex_shift=0.0,
            area_removed=0.0,
            vertices_snapped=0,
            was_clipped=False,
        )

    if measurement.verdict is Verdict.TOO_FAR:
        warning = (
            f"{measurement.outside_area:.3f} m² lies outside the plot, reaching "
            f"{measurement.max_outward_distance:.3f} m beyond the boundary. "
            "That is too far to be a digitising error, so the geometry was left "
            "as it is. Check the coordinates, the CRS zone and the plot itself."
        )
        return building, BuildingFix(
            row_index=-1,
            action=Action.FLAGGED,
            outside_area_before=measurement.outside_area,
            outside_area_after=measurement.outside_area,
            max_vertex_shift=0.0,
            area_removed=0.0,
            vertices_snapped=0,
            was_clipped=False,
            warning=warning,
        )

    snapped, vertices_snapped, max_shift = snap_to_plot(
        original, plot, config.snap_tolerance_m
    )

    residual = snapped.difference(_repair(plot))
    was_clipped = residual.area > config.residual_epsilon_m2
    fixed = (
        clip_to_plot(snapped, plot, config.min_part_area_m2) if was_clipped else snapped
    )

    outside_after = fixed.difference(_repair(plot)).area
    if outside_after > config.residual_epsilon_m2:
        # Snapping and clipping both failed to contain the building. Returning
        # the original is safer than shipping a half-repaired footprint.
        LOGGER.warning(
            "Autofix could not contain a building: %.9f m² still outside",
            outside_after,
        )
        return building, BuildingFix(
            row_index=-1,
            action=Action.FLAGGED,
            outside_area_before=measurement.outside_area,
            outside_area_after=outside_after,
            max_vertex_shift=0.0,
            area_removed=0.0,
            vertices_snapped=0,
            was_clipped=False,
            warning=(
                "The overhang could not be repaired automatically; the geometry "
                "was left unchanged."
            ),
        )

    return fixed, BuildingFix(
        row_index=-1,
        action=Action.FIXED,
        outside_area_before=measurement.outside_area,
        outside_area_after=outside_after,
        max_vertex_shift=max_shift,
        area_removed=max(original.area - fixed.area, 0.0),
        vertices_snapped=vertices_snapped,
        was_clipped=was_clipped,
    )


def autofix_frame(
    frame: gpd.GeoDataFrame, config: AutofixConfig | None = None
) -> tuple[gpd.GeoDataFrame, AutofixReport]:
    """Correct every building in a drawing so it lies inside the land plot.

    The frame must be in a metre-based projected CRS. Row order, row count and
    attribute values are preserved; only building geometries change, and the
    land plot row is returned exactly as it arrived.
    """
    config = config or AutofixConfig()
    plot_position, building_positions = identify_plot_and_buildings(frame)

    geometry_name = frame.geometry.name
    plot = _repair(frame.geometry.iloc[plot_position])

    report = AutofixReport(
        plot_row_index=plot_position,
        plot_area=plot.area,
        building_count=len(building_positions),
    )

    result = frame.copy()
    geometries = list(result.geometry)

    for position in building_positions:
        fixed_geometry, fix = fix_building(geometries[position], plot, config)
        geometries[position] = fixed_geometry
        report.fixes.append(
            BuildingFix(
                row_index=position,
                action=fix.action,
                outside_area_before=fix.outside_area_before,
                outside_area_after=fix.outside_area_after,
                max_vertex_shift=fix.max_vertex_shift,
                area_removed=fix.area_removed,
                vertices_snapped=fix.vertices_snapped,
                was_clipped=fix.was_clipped,
                warning=fix.warning,
            )
        )

    geometries, report.z_was_dropped = harmonise_dimensions(geometries)

    result[geometry_name] = gpd.GeoSeries(
        geometries, index=result.index, crs=frame.crs
    )
    result = result.set_geometry(geometry_name)
    return result, report


def harmonise_dimensions(
    geometries: list[BaseGeometry],
) -> tuple[list[BaseGeometry], bool]:
    """Make every geometry consistently 2D or 3D.

    Snapping carries Z values through, but a clip is a GEOS overlay and returns
    plan geometry, so a clipped building comes back 2D. Writing a mix of 2D and
    3D polygons into one Shapefile layer produces a file whose declared
    geometry type does not match all of its records, which readers handle
    inconsistently. When any geometry has lost its Z, the whole set is flattened
    so the output is uniform. Z values are never invented: a clip creates
    vertices along the plot boundary that have no meaningful elevation.
    """
    from shapely import force_2d

    present = [geometry.has_z for geometry in geometries if geometry is not None]
    if not present:
        return geometries, False

    mixed = any(present) and not all(present)
    corrupt = any(
        _has_unusable_z(geometry) for geometry in geometries if geometry is not None
    )
    if not mixed and not corrupt:
        return geometries, False

    flattened = [
        force_2d(geometry) if geometry is not None else geometry
        for geometry in geometries
    ]
    LOGGER.info(
        "Flattened geometry to 2D so the exported layer stays uniform "
        "(mixed=%s, unusable_z=%s)",
        mixed,
        corrupt,
    )
    return flattened, True


def _has_unusable_z(geometry: BaseGeometry) -> bool:
    """True when a geometry carries NaN elevations.

    Overlaying a 3D geometry with a 2D one leaves GEOS nothing to interpolate
    from, so the new vertices come back with NaN Z. Writing those into a
    Shapefile produces elevations that readers report as garbage, so such
    geometry is better flattened than shipped.
    """
    if not geometry.has_z:
        return False
    for polygon in _polygon_parts(geometry):
        rings = [polygon.exterior, *polygon.interiors]
        for ring in rings:
            for coordinate in ring.coords:
                if len(coordinate) > 2 and coordinate[2] != coordinate[2]:
                    return True
    return False


def build_obstacles(frame: gpd.GeoDataFrame) -> BaseGeometry:
    """Merge neighbouring parcels and their buildings into one blocked area.

    Plots and buildings are merged together rather than kept apart because our
    geometry must avoid both. That also copes with registered data that is
    itself slightly inconsistent: in the sample files a neighbour's building
    sits 0.00098 m² outside its own plot, so assuming buildings are contained
    in their plots would leave part of the blocked area unaccounted for.
    """
    from shapely.ops import unary_union

    polygons: list[BaseGeometry] = []
    for geometry in frame.geometry:
        if geometry is None or geometry.is_empty:
            continue
        if geometry.geom_type in POLYGON_GEOMETRY_TYPES:
            polygons.append(_repair(geometry))

    if not polygons:
        raise AutofixError(
            "The neighbours file contains no polygons, so there is nothing to "
            "check against."
        )
    return unary_union(polygons)


def resolve_neighbour_conflicts(
    frame: gpd.GeoDataFrame,
    obstacles: BaseGeometry,
    config: AutofixConfig | None = None,
    obstacle_count: int = 0,
) -> tuple[gpd.GeoDataFrame, NeighbourReport]:
    """Clear our parcel off land that already belongs to a registered neighbour.

    Both the land plot and the buildings are trimmed, since our geometry may
    not encroach on a neighbouring plot or on a neighbouring building. The
    neighbour geometry is read-only and is never returned.

    The frame must be in a metre-based projected CRS. Row order, row count and
    attributes are preserved.
    """
    config = config or AutofixConfig()
    plot_position, building_positions = identify_plot_and_buildings(frame)

    report = NeighbourReport(
        plot_row_index=plot_position, obstacle_count=obstacle_count
    )

    result = frame.copy()
    geometries = list(result.geometry)

    # The plot goes first: trimming it changes what the buildings must fit in.
    for position in [plot_position, *building_positions]:
        cleared, fix = clear_of_obstacles(
            geometries[position],
            obstacles,
            config,
            is_plot=position == plot_position,
        )
        geometries[position] = cleared
        report.fixes.append(
            ObstacleFix(
                row_index=position,
                action=fix.action,
                is_plot=fix.is_plot,
                overlap_area_before=fix.overlap_area_before,
                overlap_area_after=fix.overlap_area_after,
                max_penetration=fix.max_penetration,
                area_before=fix.area_before,
                area_after=fix.area_after,
                warning=fix.warning,
            )
        )

    geometries, report.z_was_dropped = harmonise_dimensions(geometries)

    geometry_name = result.geometry.name
    result[geometry_name] = gpd.GeoSeries(
        geometries, index=result.index, crs=frame.crs
    )
    return result.set_geometry(geometry_name), report


def resolve_sibling_overlaps(
    frame: gpd.GeoDataFrame, config: AutofixConfig | None = None
) -> tuple[gpd.GeoDataFrame, SiblingReport]:
    """Separate buildings of the same parcel that overlap each other.

    Both buildings belong to us, so unlike the neighbour case there is no
    immutable side to defer to. The larger footprint yields: it is trimmed by
    the smaller one. That keeps small structures such as sheds and garages
    intact, and means exactly one boundary moves per overlap, so the correction
    stays traceable to a single feature.

    Buildings are settled smallest first, each one trimmed against those already
    settled. Since only smaller buildings are ever settled before a larger one,
    the smallest keeps its geometry untouched and the order is deterministic
    regardless of row order. Equal areas resolve by row index.

    The land plot is excluded from this pass and never modified. Trimming can
    only remove area, so a building that was inside the plot stays inside it.
    """
    config = config or AutofixConfig()
    sibling_config = config.for_siblings()

    plot_position, building_positions = identify_plot_and_buildings(frame)
    report = SiblingReport(
        plot_row_index=plot_position, building_count=len(building_positions)
    )

    geometries = list(frame.geometry)
    areas = {
        position: _repair(geometries[position]).area
        for position in building_positions
    }
    smallest_first = sorted(building_positions, key=lambda p: (areas[p], p))

    settled: list[int] = []
    for position in smallest_first:
        current = _repair(geometries[position])

        for other_position in settled:
            other = _repair(geometries[other_position])
            overlap = current.intersection(other)
            if overlap.is_empty or overlap.area <= config.residual_epsilon_m2:
                continue

            measurement = classify_against_obstacles(current, other, sibling_config)
            if measurement.verdict is Verdict.TOO_FAR:
                report.pairs.append(
                    SiblingFix(
                        trimmed_row=position,
                        kept_row=other_position,
                        action=Action.FLAGGED,
                        overlap_area=measurement.outside_area,
                        max_width=measurement.max_outward_distance,
                        area_removed=0.0,
                        warning=(
                            f"they share {measurement.outside_area:.3f} m², up to "
                            f"{measurement.max_outward_distance:.3f} m wide, which "
                            "is too much to be a drawing mismatch. Nothing was "
                            "changed: one of the two footprints is likely wrong."
                        ),
                    )
                )
                continue

            trimmed = subtract_obstacles(current, other, config.min_part_area_m2)
            report.pairs.append(
                SiblingFix(
                    trimmed_row=position,
                    kept_row=other_position,
                    action=Action.FIXED,
                    overlap_area=measurement.outside_area,
                    max_width=measurement.max_outward_distance,
                    area_removed=max(current.area - trimmed.area, 0.0),
                )
            )
            current = _repair(trimmed)

        geometries[position] = current
        settled.append(position)

    geometries, report.z_was_dropped = harmonise_dimensions(geometries)

    geometry_name = frame.geometry.name
    result = frame.copy()
    result[geometry_name] = gpd.GeoSeries(
        geometries, index=result.index, crs=frame.crs
    )
    return result.set_geometry(geometry_name), report


def verify_no_sibling_overlap(
    frame: gpd.GeoDataFrame, epsilon: float = 1e-9
) -> list[tuple[int, int, float]]:
    """List pairs of buildings that still overlap, with the shared area.

    The land plot is excluded, since every building is expected to lie inside it.
    """
    from itertools import combinations

    plot_position, building_positions = identify_plot_and_buildings(frame)
    violations: list[tuple[int, int, float]] = []
    for first, second in combinations(building_positions, 2):
        overlap_area = (
            _repair(frame.geometry.iloc[first])
            .intersection(_repair(frame.geometry.iloc[second]))
            .area
        )
        if overlap_area > epsilon:
            violations.append((first, second, overlap_area))
    return violations


def verify_no_overlap(
    frame: gpd.GeoDataFrame, obstacles: BaseGeometry, epsilon: float = 1e-9
) -> list[tuple[int, float]]:
    """List rows still overlapping neighbouring land, with the area involved.

    Checked on the exact geometry to be delivered, in its delivery CRS. An
    empty list means our parcel is clear of every neighbour.
    """
    blocked = _repair(obstacles)
    violations: list[tuple[int, float]] = []
    for position, geometry in enumerate(frame.geometry):
        if geometry is None or geometry.is_empty:
            continue
        overlap_area = _repair(geometry).intersection(blocked).area
        if overlap_area > epsilon:
            violations.append((position, overlap_area))
    return violations


def verify_containment(
    frame: gpd.GeoDataFrame, epsilon: float = 1e-9
) -> list[tuple[int, float]]:
    """List buildings still protruding from the plot, with the area involved.

    Intended as a final check on the exact geometry that will be delivered, in
    the CRS it will be delivered in. An empty list means every building is
    contained.
    """
    plot_position, building_positions = identify_plot_and_buildings(frame)
    plot = _repair(frame.geometry.iloc[plot_position])

    violations: list[tuple[int, float]] = []
    for position in building_positions:
        outside_area = _repair(frame.geometry.iloc[position]).difference(plot).area
        if outside_area > epsilon:
            violations.append((position, outside_area))
    return violations
