# Bidirectional Conversion Feature

## New Feature: ZIP → DXF Conversion

The bot now supports **bidirectional conversion**:

### 1. DXF/DWG → Shapefile ZIP (Original Feature)
- Upload: DXF or DWG file
- Process: Extract closed polylines → convert to polygons
- Output: ZIP with Shapefile (EPSG:3857)
- Filtering: Points, lines, text removed

### 2. ZIP → DXF (NEW Feature) ✨
- Upload: ZIP file containing Shapefiles
- Process: Convert polygons → polylines, reproject to local CRS
- Output: DXF file with polylines (Krassovsky 1942 local CRS)
- Input assumption: Shapefiles are in EPSG:3857

---

## How It Works

### ZIP to DXF Processing Pipeline

```
1. User uploads ZIP file
   ↓
2. Bot extracts ZIP and finds .shp files
   ↓
3. Read all Shapefiles with GeoPandas
   ↓
4. Convert Polygon/MultiPolygon → LineString/MultiLineString
   - Polygon → exterior ring as LineString
   - MultiPolygon → multiple LineStrings (one per polygon)
   ↓
5. Reproject from EPSG:3857 → SOURCE_CRS (local)
   ↓
6. Export as DXF file
   ↓
7. Send DXF back to user
```

---

## Technical Implementation

### New Functions Added

**`_polygon_to_linestring(geometry)`**
- Converts Polygon to LineString (exterior ring)
- Converts MultiPolygon to MultiLineString
- Returns geometry unchanged if already a line

**`_read_shapefile_from_zip(zip_path, work_dir)`**
- Extracts ZIP archive
- Finds all .shp files recursively
- Reads and combines them into single GeoDataFrame
- Error handling for invalid ZIPs or missing shapefiles

**`convert_zip_to_dxf(zip_path, work_dir)`**
- Main conversion function
- Reads shapefiles from ZIP
- Applies polygon→line conversion
- Reprojects to SOURCE_CRS
- Exports to DXF format
- Returns path to generated DXF

### Modified Functions

**`handle_document()`**
- Now detects both CAD files (.dxf, .dwg) and ZIP files
- Routes to appropriate conversion function based on file type
- Updated status messages for each conversion type
- Updated error messages

**`start_command()`**
- Updated help text to explain both conversion directions
- Shows clear examples of both workflows

---

## User Experience

### For ZIP Upload:
1. User sends ZIP file with Shapefiles
2. Bot replies: "Extracting ZIP..."
3. Bot updates: "Converting Shapefile polygons to polylines and reprojecting to local CRS..."
4. Bot sends DXF file with caption: "✅ Success! Converted Shapefile polygons to polylines in local CRS."

### Help Message (/start):
```
Send me a document and I'll convert it:

📤 DXF/DWG → Shapefile ZIP
   • Extracts only polygon features (closed polylines)
   • Reprojects to EPSG:3857
   • Filters out points, lines, and text

📥 ZIP (Shapefile) → DXF
   • Converts polygons to polylines
   • Reprojects to local CRS (Krassovsky 1942)
```

---

## Error Handling

### Invalid ZIP:
```
❌ The uploaded file is not a valid ZIP archive.
```

### No Shapefiles in ZIP:
```
❌ No Shapefile (.shp) found in the ZIP archive.
```

### Empty/Invalid Shapefiles:
```
❌ Could not read any valid Shapefiles from the ZIP archive.
```

### No Valid Geometries:
```
❌ The Shapefile contains no valid geometries.
```

### DXF Write Failure:
```
❌ Could not write the DXF file.
```

---

## Code Changes Summary

**Modified Constants:**
- Added `SUPPORTED_ZIP_EXTENSION = ".zip"`

**New Functions:** 3
- `_polygon_to_linestring()`
- `_read_shapefile_from_zip()`
- `convert_zip_to_dxf()`

**Modified Functions:** 2
- `handle_document()` - handles both CAD and ZIP files
- `start_command()` - updated help message

**Lines Changed:** ~150 lines added/modified

---

## Testing Checklist

- [ ] Upload DXF file → still produces Shapefile ZIP (existing feature preserved)
- [ ] Upload DWG file → still produces Shapefile ZIP (existing feature preserved)
- [ ] Upload ZIP with Shapefile → produces DXF with polylines
- [ ] Upload ZIP with multiple Shapefiles → combines and converts all
- [ ] Upload invalid ZIP → proper error message
- [ ] Upload ZIP without .shp files → proper error message
- [ ] Upload unsupported file type → proper error message
- [ ] Verify DXF output is in SOURCE_CRS (local coordinate system)
- [ ] Verify polygons converted to closed polylines in DXF

---

## Deployment Notes

This feature is backward compatible - all existing DXF/DWG → Shapefile functionality remains unchanged.

**Dependencies:** No new packages required
- Uses existing: geopandas, fiona, shapely, zipfile (stdlib)

**Environment Variables:** No changes needed

**Docker Image:** No changes needed (all dependencies already present)

