# CAD Reprojection Telegram Bot

An async Telegram bot that converts DXF/DWG CAD uploads into reprojected
ESRI Shapefile ZIP archives.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

The bot itself is a standalone Python service:

- `python bot.py` — start Telegram long polling
- `docker build -t cad-reprojection-bot . && docker run --rm -e TELEGRAM_BOT_TOKEN=... cad-reprojection-bot` — run the production container
- Required secret: `TELEGRAM_BOT_TOKEN`
- Railway uses `railway.json` and the included `Dockerfile`

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `bot.py` — Telegram handlers, DWG conversion, Fiona/GeoPandas reprojection, Shapefile writing, ZIP packaging, and cleanup
- `Dockerfile` — GDAL plus LibreDWG runtime image
- `requirements.txt` — pinned Python runtime dependencies
- `railway.json` — Railway Docker build and restart policy
- `CAD_BOT_README.md` — local and Railway setup instructions

## Architecture decisions

- The bot uses long polling rather than webhooks, so Railway does not need a public webhook endpoint.
- The requested source CRS is always assigned with override semantics; CAD metadata cannot silently change the known local coordinate system.
- LineString and MultiLineString features are converted into filled polygons; closed linework is polygonized and open lines use a configurable source-unit buffer.
- Mixed CAD geometry types are written as separate homogeneous Shapefiles in one ZIP because the Shapefile format cannot safely store mixed geometry types.
- All processing occurs inside a temporary directory and the archive is sent before that directory is removed.

## Product

Users send a DXF or DWG document to the Telegram bot and receive a ZIP containing
the vector data transformed from the local Krassovsky CRS to EPSG:3857.

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

_Populate as you build — sharp edges, "always run X before Y" rules._

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
