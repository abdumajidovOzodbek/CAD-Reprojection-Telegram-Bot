# CAD File Filtering Guide

## What Gets Kept vs. Filtered Out

### ✅ KEPT (Exported to Shapefile)
- **Polygons** - Closed polylines that form complete shapes
- **MultiPolygons** - Multiple polygons grouped together

### ❌ FILTERED OUT (Not exported)
- **Points** - Single point features
- **MultiPoints** - Groups of points
- **Lines** - Open lines (single lines that don't close)
- **MultiLineStrings** - Groups of open lines
- **Text** - CAD text annotations
- **Blocks** - CAD block references
- **Dimensions** - Dimension annotations
- **Leaders** - Dimension leader lines
- **Hatches** (as lines) - Hatch boundaries if they're not closed

---

## Processing Flow

```
1. Read DXF/DWG file
   ↓
2. Try to convert CLOSED lines to polygons
   ↓
3. Filter: Keep ONLY Polygons and MultiPolygons
   ↓
4. Reproject to EPSG:3857
   ↓
5. Export to Shapefile ZIP
```

---

## What You'll Get

### Input CAD File Contains:
- 50 closed polylines (building footprints) → ✅ Exported as polygons
- 200 single lines (dimension lines, guides) → ❌ Filtered out
- 30 text annotations (labels) → ❌ Filtered out
- 10 points (survey markers) → ❌ Filtered out

### Output Shapefile Contains:
- **Only 50 polygon features** (the building footprints)

---

## Error Handling

### If No Polygons Found:
```
❌ No polygons found in the CAD file.
The file may only contain points, lines, or text.
Only closed polylines that form polygons are exported.
```

This happens when:
- CAD file has only open lines
- No closed polylines exist
- All features are points or text

---

## Examples

### Example 1: Building Site Plan
**Input:**
- 15 building outlines (closed polylines) ✅
- 100 property lines (open lines) ❌
- 50 text labels ❌

**Output:** 15 polygon features

---

### Example 2: Topographic Map
**Input:**
- 5 water body polygons (closed) ✅
- 200 contour lines (open) ❌
- 30 elevation points ❌
- 100 labels ❌

**Output:** 5 polygon features

---

### Example 3: Utility Map (No Polygons)
**Input:**
- 500 utility lines (pipes, cables - all open) ❌
- 100 junction points ❌
- 200 labels ❌

**Output:** ❌ Error - "No polygons found"

---

## Why Filter This Way?

1. **Clean output** - No garbage lines cluttering your shapefile
2. **Focus on areas** - Most GIS analysis works with area features (polygons)
3. **Remove CAD artifacts** - Construction lines, guides, and annotations removed
4. **Consistent data** - Only meaningful geographic features exported

---

## If You Need Lines or Points

If you need to export lines or points from your CAD files, the current bot filters them out. The bot is configured specifically to export only area features (polygons).

To export lines or points, you would need a different configuration or tool.

---

## Success Messages

### When polygons are found:
```
✅ Success! Extracted 15 polygon features into 1 Shapefile (EPSG:3857).

Points, lines, and text have been filtered out.
```

### When no polygons found:
```
❌ No polygons found in the CAD file.
The file may only contain points, lines, or text.
Only closed polylines that form polygons are exported.
```
