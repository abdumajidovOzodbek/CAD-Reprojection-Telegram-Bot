# CAD Reprojection Telegram Bot

This bot accepts `.dxf` and `.dwg` files in Telegram, assigns the requested
Krassovsky 1942 local CRS, reprojects every vector feature to `EPSG:3857`
(WGS 84 / Pseudo-Mercator), and returns the resulting ESRI Shapefile
components in a ZIP archive.

## Run locally with Docker

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Build the image:

   ```bash
   docker build -t cad-reprojection-bot .
   ```

3. Start the bot with the token supplied through the environment, never in
   the image or source code:

   ```bash
   docker run --rm \
     -e TELEGRAM_BOT_TOKEN='your-token-from-botfather' \
     cad-reprojection-bot
   ```

The bot uses Telegram long polling, so no public webhook URL is required.
Send `/start`, then attach a DXF or DWG document.

## Railway deployment

1. Create a new Railway project from this repository.
2. Railway detects `railway.json` and builds the included `Dockerfile`.
3. Open the service's **Variables** tab and add:
   - `TELEGRAM_BOT_TOKEN`: the token from BotFather
4. Deploy and inspect the service logs for:
   `Starting CAD reprojection Telegram bot in polling mode`.
5. Send a DXF or DWG document to the bot in Telegram.

`TELEGRAM_BOT_TOKEN` is the only required variable. Optional variables:

- `MAX_INPUT_FILE_SIZE_MB` (default `50`)
- `DWG_CONVERSION_TIMEOUT_SECONDS` (default `120`)
- `LINE_BUFFER_DISTANCE_M` (default `0.1`, used only for open lines)
- `LOG_LEVEL` (default `INFO`)

LineString and MultiLineString features are exported as filled polygons. Closed
linework is polygonized; open lines are converted to narrow polygons using
`LINE_BUFFER_DISTANCE_M` in the source CRS's metre units.

## Output behavior

The bot includes `.shp`, `.shx`, `.dbf`, `.prj`, and `.cpg` components in the
ZIP archive. CAD files can contain more than one geometry type, while one
Shapefile must contain one geometry type. When that happens, the bot creates
one correctly typed Shapefile per geometry type in the same ZIP so no features
are silently discarded.

Temporary downloads and generated files are deleted automatically after the
Telegram response is sent, including when processing fails.