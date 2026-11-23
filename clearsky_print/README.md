# ClearSKY Terrain Print

Build a relief map from an existing ClearSKY order or local GeoTIFFs. The DEM
sets the relief. Choose a date range for a median color composite, print dates
individually, or use the mean for a series with stable light and colors.
Each puzzle tile is a separate printable model with its own four-color palette.

## Setup

Install Python 3.11 or newer and [Rust](https://rust-lang.org/install/). On
Windows, use the Rust MSVC toolchain with the C++ build tools. Open a terminal
at the root of this repository and run:

~~~text
python -m pip install -r clearsky_print/requirements.txt
cargo build --locked --release -p toposaic-core --example clearsky_terrain
~~~

The command looks for the engine under `target/release/examples`. Use
`--engine` to point to a separate binary. The workflow uses the TopoSaic
Rust core directly and runs without the map server or desktop app.

### Try it without an account

~~~text
python clearsky_print/demo.py demo-data
python clearsky_print/terrain.py build --series demo-data/series.json --dem demo-data/dem.tif --out demo-print --samples 65 --min-dates 2 --tile-palette r01c01 "#3C597C,#658450,#A7875E,#BBC19A" --tile-palette r01c02 "#315780,#698956,#967C54,#CDDAB0"
~~~

Open `demo-print/tile-layout.png`, then open the two files listed in
`demo-print/PRINT.txt`. The demo generates its own rasters. It shows how the
same terrain crosses the seam and how the palettes can differ between tiles.
Add `--each-date` to also export an individual model for each input date.

## From a ClearSKY order

1. Set `CLEARSKY_API_KEY` in your terminal environment. Get the key from the
   [ClearSKY dashboard](https://docs.clearsky.vision/start-here/get-credentials-api-key/).
2. Run `python clearsky_print/terrain.py orders` and copy the UUID of an
   existing order. The command uses its imagery model and constellations.
3. Draw the print area with [ClearSKY Polygon Tools](https://polygon.clearsky.vision/)
   and export one WGS84 polygon as GeoJSON. The square tile grid encloses the
   polygon, so check that the existing order covers the full rectangle. You
   can also set `--center LONGITUDE LATITUDE --span-km WIDTH_PER_TILE`.
4. Discover dates, review estimates, then fetch the dates you want.

~~~text
python clearsky_print/terrain.py dates --order ORDER_UUID --aoi area.geojson --tiles 2 1 --from 2025-07-01 --to 2025-07-31
python clearsky_print/terrain.py fetch --order ORDER_UUID --aoi area.geojson --tiles 2 1 --date 2025-07-06 --date 2025-07-16 --out imagery
~~~

Add `--download` to the second command to fetch. This uses processing units
from your ClearSKY account. The tool checks full area/date availability,
uses the documented geometry-only estimate, and requests 10 m harmonized
bottom-of-atmosphere B4/B3/B2 GeoTIFFs from existing orders. You can use
`--from` and `--to` in place of individual dates in `fetch`. A repeat of the
same request reuses previously saved TIFFs.

Supply the required notice for your data with `--attribution` when fetching or
building. See [ClearSKY's attribution guidance](https://docs.clearsky.vision/reference/source-attribution-requirements/).

~~~text
python clearsky_print/terrain.py build --series imagery/series.json --dem local-dem.tif --out conference-print --width-mm 160 --relief-mm 22 --min-dates 2
~~~

For a dashboard time-series ZIP, extract the GeoTIFFs and supply each with
`--image DATE PATH` on the build command. In this mode also supply `--aoi` or
`--center` and `--span-km`, plus `--tiles` for multiple pieces. Use a DEM with
complete coverage and heights in metres. Select matching imagery export
settings across dates. The reader accepts RGB quicklooks and signed
reflectance rasters with three RGB bands. For other layouts, specify the
1-based indexes with `--rgb-bands R G B`. For unscaled integer reflectance it
assumes DN / 10,000. If a raster has an unapplied offset, supply it in DN
with `--dn-offset` (for example, `--dn-offset -1000`).

## Puzzle tiles and filament changes

- `--tiles 3 2` builds three columns by two rows. `r01c01` is the
  northwestern tile. Matching interlocks and shared elevation scaling let
  the pieces assemble. The default nominal fit clearance between pieces
  is 0.24 mm. Set `--clearance-mm` to suit your printer's tolerances, or
  choose `--edge straight` for square edges.
- The first run can suggest up to four colors per tile from the composite.
  Review `rgb-preview.png` beside `filament-preview.png`. Provide the colors of
  the spools you'll actually use with `--palette "#614938,#517752,#BBAA87,#3A6885"`,
  or override a tile with `--tile-palette r01c02 "#2E5373,#749758,#A87B4F,#CDD0B6"`.
  The first color also prints the base and areas without valid imagery.
  Each run saves `tile-palettes.json`. Edit it and pass it back with
  `--palettes tile-palettes.json` to revise several tiles together.
- `PRINT.txt` groups plates by their filament set. Print one group, change
  spools, and print the next group. Rebuild
  an individual piece with `--only-tile`, keeping the original data, layout,
  width, and clearance. Use a new output folder each time.
- Import `terrain-solid.3mf` as a model into
  [Flash Studio Desktop](https://www.flashforge.com/pages/flash-studio-desktop).
  Select **Flashforge Creator 5 Pro** and your actual nozzle and filaments.
  Match the imported colors to the four toolheads using `PRINT.txt`, then
  check the sliced preview and plate fit before sending the job. Each 3MF
  carries face colors and paint codes. Flash Studio supplies the machine
  profile. A single-color STL is also available.

## What the time series adds

The default median reduces the influence of an unusually bright or dark
date. Use a short window within one season if the print should show particular
conditions. The mean blends changes across dates and can look smoother.
`date-count.png` stores the number of usable dates per sample as pixel values.
Set `--min-dates 2` or higher if a composite should exclude areas with only
one input. The same palette is used for every date of each tile so changing
colors in a comparison come from changes in the input image.

Multiple dates improve the stability of the surface colors. The tool keeps
the spatial detail available in the input rasters. Evaluating multi-image
super-resolution would need an independent higher-resolution reference and
a test that the added features survive the print scale.

## Try the Lofoten example

This example downloads small public raster windows from three Sentinel-2
Level-2A scenes and a Copernicus GLO-30 DEM. It produces two pieces of the
Haukland area. The downloads use public AWS datasets and require no ClearSKY
account.

~~~text
python clearsky_print/demo_lofoten.py lofoten-data
python clearsky_print/terrain.py build --series lofoten-data/series.json --dem lofoten-data/dem.tif --out lofoten-print --name "Haukland, Lofoten" --min-dates 2
~~~

The images contain modified Copernicus Sentinel data 2025, distributed by
[Element 84 Earth Search](https://earth-search.aws.element84.com/v1/).
The derived elevation source and its required notice are recorded in the
DEM, print manifest, and print guide.

Both example 3MFs were sliced locally in Flash Studio Desktop 1.7.23 with
the Creator 5 Pro 0.4 mm nozzle profile. The resulting G-code used all four
toolheads for each tile. Physical fit still needs a print test.

## Credits

Terrain geometry, puzzle interlocks, satellite palette quantization, STL
and 3MF export build on **TopoSaic by Yann Ramin and contributors**.
This downstream workflow adds ClearSKY retrieval and compositing, fitted
interlocking tiles that sample the DEM across seams, and an export style
that carries color without printer presets. The base revision is
[`fb365e0`](https://github.com/theatrus/toposaic/commit/fb365e0d12cfd99483d0d0cfe37077295d555b68).
See the upstream [LICENSE](../LICENSE), [NOTICE](../NOTICE), and
[third-party notices](../THIRD_PARTY_NOTICES.md). The source code is Apache
2.0. Imagery and elevation retain their own terms and attribution, which can
be included in the generated print manifest using `--attribution` and
`--dem-attribution`.
