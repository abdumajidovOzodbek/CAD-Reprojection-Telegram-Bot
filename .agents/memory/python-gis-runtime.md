---
name: Python GIS runtime
description: Local validation requirements for the CAD bot's Fiona and GeoPandas stack.
---

The CAD conversion code is intended to run in the Docker image, where GDAL,
LibreDWG, and their shared libraries are installed together. For local smoke
tests in this Replit environment, use the Python Tools runtime and provide the
Nix `expat`, `gdal`, and `proj` libraries before importing Fiona. The live
polling workflow also needs the Nix `libredwg` package for `dwg2dxf`; the
Railway Dockerfile installs LibreDWG separately.

**Why:** The base workspace Python may not include pip or the shared libraries
required by Fiona, so a failure during local import can be an environment
problem rather than a defect in the conversion code.

**How to apply:** Validate the source with `py_compile` first; if GIS imports
fail locally, configure the development runtime before changing the bot or
Dockerfile.