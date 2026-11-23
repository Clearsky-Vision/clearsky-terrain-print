"""Prepare ClearSKY imagery and terrain for a Flashforge Creator 5 Pro."""

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import date
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile
from xml.etree import ElementTree

import numpy as np
from PIL import Image, ImageDraw
from rasterio.errors import RasterioError

from clearsky_api import Client, discover_dates, order_settings, prepare_downloads
from rasters import Area, as_engine_pixels, composite, display_rgb, read_dem, read_rgb, source_metadata


ENGINE = Path(__file__).resolve().parents[1] / "target" / "release" / "examples" / (
    "clearsky_terrain.exe" if os.name == "nt" else "clearsky_terrain"
)
PRINTER = "Flashforge Creator 5 Pro"
MAX_DATES = 32


class MeshCheckError(ValueError):
    """A generated mesh failed a closed-edge check."""


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def area_from_args(args):
    if args.aoi:
        if args.center or args.span_km is not None:
            raise ValueError("Use --aoi or --center/--span-km.")
        return Area.from_geojson(args.aoi, *(args.tiles or (1, 1)))
    if not args.center or args.span_km is None:
        raise ValueError("Set --center LONGITUDE LATITUDE and --span-km PER_TILE.")
    return Area(*args.center, args.span_km, *(args.tiles or (1, 1)))


def requested_dates(args, client, area, model, constellations):
    if getattr(args, "date", None):
        if args.start or args.end:
            raise ValueError("Choose --date entries or a --from/--to window.")
        return sorted(set(args.date))
    if not args.start or not args.end or args.start > args.end:
        raise ValueError("Set a valid --from and --to date range.")
    return sorted(set(discover_dates(
        client.dates(area, args.start, args.end), model, constellations, args.start, args.end,
    )))


def run_api(args):
    client = Client()
    if args.command == "orders":
        for order in client.orders(args.order):
            print(json.dumps({key: order.get(key) for key in (
                "TaskOrderGuid", "Name", "From", "To", "Model", "SatelliteConstellations", "OrderingProcessStatus",
            )}))
        return

    area = area_from_args(args)
    model, constellations = order_settings(client, args.order)
    dates = requested_dates(args, client, area, model, constellations)
    if args.command == "dates":
        for day in dates:
            print(day)
        return
    if not dates or len(dates) > MAX_DATES:
        raise ValueError(f"Choose 1–{MAX_DATES} available dates for one print run. Use 'dates' to list candidates.")

    folder = args.out.resolve()
    series_path = folder / "series.json"
    series = {
        "provider": "ClearSKY Vision",
        "area": asdict(area),
        "model": model,
        "constellations": constellations,
        "attribution": args.attribution,
        "images": {str(day): f"{day}.tif" for day in dates},
    }
    if folder.exists() and (not series_path.exists() or json.loads(series_path.read_text("utf-8")) != series):
        raise ValueError("The download folder already belongs to another request. Pick a new --out folder.")
    missing = [day for day in dates if not (folder / series["images"][str(day)]).is_file()]
    planned = list(prepare_downloads(client, area, model, constellations, missing))
    for day, _, estimate in planned:
        print(json.dumps({"date": str(day), "estimate": estimate}))
    if not args.download:
        print(f"{len(planned)} dates to download, {len(dates) - len(missing)} already saved. Add --download to fetch.")
        return
    folder.mkdir(parents=True, exist_ok=True)
    if not series_path.exists():
        write_json(series_path, series)
    for day, payload, _ in planned:
        path = folder / series["images"][str(day)]
        client.download(payload, path)
        print(f"Saved {path}")
    print(f"Build with --series {series_path}")


def parse_palette(value):
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(color, str) for color in items):
        raise ValueError("A palette must be a list of one to four hex colors.")
    colors = tuple(color.strip().upper() for color in items)
    if not (1 <= len(set(colors)) == len(colors) <= 4) or any(
        not re.fullmatch(r"#[0-9A-F]{6}", color) for color in colors
    ):
        raise ValueError("Use one to four distinct hex colors, for example '#624F40,#697A4B,#D8CBB1,#446D85'.")
    return colors


def inputs(args):
    if args.series:
        if args.center or args.span_km is not None or args.aoi or args.tiles:
            raise ValueError("--series already defines the area and tile grid. Omit the area flags.")
        folder = args.series.resolve().parent
        series = json.loads(args.series.read_text("utf-8"))
        area = Area(**series["area"])
        files = {date.fromisoformat(day): folder / path for day, path in series["images"].items()}
        attribution = combine_credits(series.get("attribution"), args.attribution)
        return area, files, series["provider"], series["model"], series["constellations"], attribution
    files = {date.fromisoformat(day): Path(path) for day, path in args.image}
    if len(files) != len(args.image):
        raise ValueError("Supply one image per date.")
    return area_from_args(args), files, args.provider, None, None, args.attribution


def tile_name(column, row):
    return f"r{row + 1:02d}c{column + 1:02d}"


def tile_job(args, area, elevations, reference, images, column, row, colors, provider):
    samples = args.samples
    cells = samples - 1
    x, y = column * cells, row * cells
    ys, xs = slice(y, y + samples), slice(x, x + samples)
    pad = (cells + 4) // 5 if args.edge == "puzzle" else 0
    top, left = max(0, y - pad), max(0, x - pad)
    bottom = min(elevations.shape[0], y + samples + pad)
    right = min(elevations.shape[1], x + samples + pad)
    heights = elevations[top:bottom, left:right][::-1]
    window = [x - left, bottom - y - samples, x - left + cells, bottom - y - 1]
    datum = float(elevations.min())
    metres_per_mm = max((float(elevations.max()) - datum) / args.relief_mm, .1)
    lon, lat = area.tile_center(column, row)
    south_row = area.rows - row - 1
    spec = {
        "center_lon": lon,
        "center_lat": lat,
        "ground_span_km": area.span_km,
        "width_mm": args.width_mm,
        "base_mm": args.base_mm,
        "relief_mm": args.relief_mm,
        "elevation_datum_m": datum,
        "elevation_m_per_mm": metres_per_mm,
        "solid_model": True,
        "mesh_samples_across": max(256, cells),
        "overlay_samples_across": max(256, cells),
        "place_name": f"{args.name} / {tile_name(column, row)}",
        "adjacent_columns": area.columns,
        "adjacent_rows": area.rows,
        "adjacent_tile_column": column,
        "adjacent_tile_row": south_row,
        "puzzle_tile_column": column,
        "puzzle_tile_row": south_row,
        "adjacent_interlocks": args.edge == "puzzle",
        "solid_interlock_clearance_mm": args.clearance_mm if args.edge == "puzzle" else None,
        "despike_terrain": False,
    }
    pixels, valid = as_engine_pixels(reference[:, ys, xs])
    output_images = []
    for name, rgb in images.items():
        colors_in, mask = as_engine_pixels(rgb[:, ys, xs])
        output_images.append({"name": name, "pixels": colors_in, "valid": mask})
    return {
        "spec": spec,
        "samples": samples,
        "elevation_dimensions": [heights.shape[1], heights.shape[0]],
        "elevation_window": window,
        "elevation_source": args.dem.name,
        "imagery_source": provider,
        "elevations": heights.ravel().tolist(),
        "palette_reference": pixels,
        "reference_valid": valid,
        "palette": colors,
        "images": output_images,
    }, ys, xs


def model_properties(path):
    with ZipFile(path) as archive, archive.open("3D/3dmodel.model") as model_file:
        root = ElementTree.parse(model_file).getroot()
    for mesh in root.findall(".//{*}mesh"):
        edges = Counter()
        for face in mesh.findall("./{*}triangles/{*}triangle"):
            a, b, c = (int(face.attrib[key]) for key in ("v1", "v2", "v3"))
            edges.update(tuple(sorted(pair)) for pair in ((a, b), (b, c), (c, a)))
        if not edges or set(edges.values()) != {2}:
            raise MeshCheckError(f"{path.parent.parent.name}/{path.parent.name} failed the closed-mesh check.")
    colors = [entry.attrib["color"][:7].upper() for entry in root.findall(".//{*}colorgroup/{*}color")]
    vertices = root.findall(".//{*}vertex")
    xs = [float(v.attrib["x"]) for v in vertices]
    ys = [float(v.attrib["y"]) for v in vertices]
    zs = [float(v.attrib["z"]) for v in vertices]
    dimensions = [max(values) - min(values) for values in (xs, ys, zs)]
    if not (1 <= len(colors) <= 4) or max(dimensions) > 256:
        raise ValueError(f"{path.parent}: {len(colors)} colors and {dimensions} mm exceed the Creator 5 Pro export limits.")
    return {"colors_in_3mf_order": colors, "dimensions_mm": [round(m, 3) for m in dimensions]}


def layout(reference, area, dest):
    grid = Image.fromarray(display_rgb(reference))
    size = min(900, grid.width * 2) // area.columns
    grid = grid.resize((size * area.columns, size * area.rows), Image.Resampling.BILINEAR)
    pen = ImageDraw.Draw(grid)
    for row in range(area.rows):
        for column in range(area.columns):
            x, y = column * size, row * size
            pen.rectangle((x, y, x + size - 1, y + size - 1), outline="white", width=1)
            pen.text((x + 6, y + 6), tile_name(column, row), fill="white", stroke_width=2, stroke_fill="black")
    grid.save(dest)


def filament_preview(export):
    preview = json.loads((export / "preview.json").read_text("utf-8"))
    table = [preview["surface_palette"]["rock"]] * preview["ground_material_offset"]
    table.extend(preview["ground_palette"])
    rgb = np.array([list(bytes.fromhex(color[1:])) for color in table], dtype="uint8")
    indices = np.array(preview["surface_classes"]).reshape(preview["height"], preview["width"])
    Image.fromarray(rgb[indices[::-1]]).save(export / "filament-preview.png")


def combine_credits(*values):
    lines = (line.strip() for value in values if value for line in value.splitlines())
    return "\n".join(dict.fromkeys(line for line in lines if line)) or None


def build(args):
    area, files, provider, model, constellations, attribution = inputs(args)
    if not args.engine.is_file():
        raise ValueError("Build the TopoSaic print engine first or pass its path with --engine. See the README.")
    if not 1 <= args.min_dates <= len(files) <= MAX_DATES:
        raise ValueError(f"Use 1–{MAX_DATES} images and a --min-dates value within that count.")
    if not (33 <= args.samples <= 257 and 60 <= args.width_mm <= 215
            and 1 <= args.base_mm <= 20 and 1 <= args.relief_mm <= 80 and 0 <= args.clearance_mm <= 1):
        raise ValueError("Use 33–257 samples, 60–215 mm width, 1–20 mm base, 1–80 mm relief and 0–1 mm clearance.")
    grid = area.grid(args.samples)
    if grid["width"] * grid["height"] * len(files) > 12_000_000:
        raise ValueError("This stack is too large for one run. Reduce --samples, tile count, or date count.")
    tiles = {(column, row): tile_name(column, row) for row in range(area.rows) for column in range(area.columns)}
    saved = json.loads(args.palettes.read_text("utf-8")) if args.palettes else {}
    if not isinstance(saved, dict) or set(saved).difference(tiles.values()):
        raise ValueError(f"Palette file keys must be tile names: {', '.join(tiles.values())}.")
    overrides = {name: parse_palette(raw) for name, raw in saved.items()}
    seen = set()
    for name, raw in args.tile_palette:
        if name not in tiles.values() or name in seen:
            raise ValueError(f"Invalid or repeated tile name {name}. Choose from {', '.join(tiles.values())}.")
        seen.add(name)
        overrides[name] = parse_palette(raw)
    if args.only_tile and args.only_tile not in tiles.values():
        raise ValueError(f"Unknown tile {args.only_tile}.")
    palette = parse_palette(args.palette) if args.palette else None
    if args.out.exists():
        raise ValueError(f"{args.out} already exists. Use a new output path.")
    elevations = read_dem(args.dem, grid)
    series = {str(day): read_rgb(path, grid, args.encoding, args.rgb_bands, args.dn_offset)
              for day, path in sorted(files.items())}
    reference, count = composite(list(series.values()), args.composite, args.min_dates)
    images = {args.composite: reference}
    if args.each_date:
        images.update(series)
    if not np.isfinite(reference).any():
        raise ValueError("The composite has no usable pixels. Check the area, masks or minimum date count.")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".terrain-", dir=args.out.parent) as temp:
        temp = Path(temp)
        bundle = temp / "print"
        bundle.mkdir()
        layout(reference, area, bundle / "tile-layout.png")
        Image.fromarray(count.astype("uint8")).save(bundle / "date-count.png")
        dem_metadata = source_metadata(args.dem)
        dem_tags = {key.upper(): value for key, value in dem_metadata.items()}
        metadata = {str(day): tags for day, path in sorted(files.items()) if (tags := source_metadata(path))}
        imagery_credits = [value for tags in metadata.values() for key, value in tags.items()
                           if "attrib" in key.lower()]
        manifest = {
            "printer": PRINTER,
            "slicer": "Flash Studio Desktop",
            "area": asdict(area),
            "utm_epsg": area.epsg,
            "tile_edges": args.edge,
            "samples_per_tile_side": args.samples,
            "nominal_joint_clearance_mm": args.clearance_mm if args.edge == "puzzle" else None,
            "nominal_tile_width_mm": args.width_mm,
            "base_height_mm": args.base_mm,
            "relief_height_mm": args.relief_mm,
            "imagery_provider": provider,
            "model": model,
            "constellations": constellations,
            "imagery_attribution": combine_credits(*imagery_credits, attribution),
            "imagery_files": {str(day): path.name for day, path in sorted(files.items())},
            "dem": args.dem.name,
            "dem_attribution": combine_credits(dem_tags.get("ATTRIBUTION"), args.dem_attribution),
            "dem_distribution_notice": dem_tags.get("DISTRIBUTION_NOTICE"),
            "dem_license_url": dem_tags.get("LICENSE_URL"),
            "composite": args.composite,
            "minimum_dates": args.min_dates,
            "composite_coverage": float(np.isfinite(reference[0]).mean()),
            "toposaic": "https://github.com/theatrus/toposaic",
            "toposaic_base_revision": "fb365e0d12cfd99483d0d0cfe37077295d555b68",
            "tiles": {},
        }
        if metadata:
            manifest["imagery_source_metadata"] = metadata
        if dem_metadata:
            manifest["dem_source_metadata"] = dem_metadata

        for (column, row), name in tiles.items():
            if args.only_tile and args.only_tile != name:
                continue
            print(f"Building {name}", flush=True)
            job, ys, xs = tile_job(args, area, elevations, reference, images, column, row,
                                   overrides.get(name, palette), provider)
            job_path = temp / "job.json"
            write_json(job_path, job)
            result = subprocess.run([str(args.engine.resolve()), str(job_path), str(bundle / name)],
                                    check=True, stdout=subprocess.PIPE, text=True)
            tile = {"palette": json.loads(result.stdout), "prints": {}}
            for image_name, rgb in images.items():
                export = bundle / name / image_name
                Image.fromarray(display_rgb(rgb[:, ys, xs])).save(export / "rgb-preview.png")
                filament_preview(export)
                tile["prints"][image_name] = model_properties(export / "terrain-solid.3mf")
            manifest["tiles"][name] = tile
        write_json(bundle / "print-manifest.json", manifest)
        write_json(bundle / "tile-palettes.json", {name: tile["palette"] for name, tile in manifest["tiles"].items()})
        write_print_guide(bundle, manifest)
        bundle.rename(args.out)
    print(f"Print files are in {args.out}")


def build_with_mesh_check(args):
    if args.samples is not None:
        build(args)
        return
    candidates = (65, 81, 49) if args.width_mm < 120 else (129, 97, 65)
    for samples in candidates:
        args.samples = samples
        try:
            build(args)
        except MeshCheckError as error:
            print(f"{error} Tried {samples} samples per side.", flush=True)
        else:
            return
    raise MeshCheckError("Could not export closed meshes. Try a different joint clearance or smaller area.")


def write_print_guide(bundle, manifest):
    lines = [
        f"Open each terrain-solid.3mf in Flash Studio Desktop with your {PRINTER} preset.",
        "Keep the Flashforge printer, nozzle and filament presets. Map the imported colors",
        "to the loaded toolheads and inspect the sliced color preview before sending.",
        "Each tile is a separate plate. The layout image reads west to east and north to south.",
        "Color order below follows the file. Use the hex colors to match toolheads in the slicer.",
        "",
    ]
    groups = {}
    for name, tile in manifest["tiles"].items():
        for image, properties in tile["prints"].items():
            colors = ", ".join(properties["colors_in_3mf_order"])
            groups.setdefault(tuple(tile["palette"]), []).append(f"{name}/{image}/terrain-solid.3mf  {colors}")
    for palette, plates in groups.items():
        lines.extend(["Filament set " + ", ".join(palette), *plates, ""])
    lines += ["", "Terrain meshing, puzzle seams and 3MF export by TopoSaic (Yann Ramin and contributors).",
              "https://github.com/theatrus/toposaic"]
    for key in ("imagery_attribution", "dem_attribution", "dem_distribution_notice", "dem_license_url"):
        if manifest.get(key):
            lines.append(manifest[key])
    lines.append("See print-manifest.json for raster details.")
    (bundle / "PRINT.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def area_flags(parser):
    parser.add_argument("--aoi", type=Path, help="WGS84 GeoJSON polygon. Its bounding tile grid is printed.")
    parser.add_argument("--center", nargs=2, type=float, metavar=("LON", "LAT"), help="Center of the full layout in WGS84.")
    parser.add_argument("--span-km", type=float, help="Ground width of one square tile.")
    parser.add_argument("--tiles", nargs=2, type=int, metavar=("COLS", "ROWS"), help="Number of tiles west–east and north–south.")


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    orders = commands.add_parser("orders", help="List existing ClearSKY orders.")
    orders.add_argument("--order", help="Show details for one order UUID.")
    for name in ("dates", "fetch"):
        api = commands.add_parser(name, help={"dates": "List candidate dates for an area.",
                                             "fetch": "Estimate or fetch imagery from existing orders."}[name])
        area_flags(api)
        api.add_argument("--order", required=True, help="Existing order UUID to select model and constellations.")
        api.add_argument("--from", dest="start", type=date.fromisoformat)
        api.add_argument("--to", dest="end", type=date.fromisoformat)
        if name == "fetch":
            api.add_argument("--date", action="append", type=date.fromisoformat, help="Repeat for individual dates.")
            api.add_argument("--out", type=Path, required=True)
            api.add_argument("--attribution", help="The source notice for the order, supplied by ClearSKY.")
            api.add_argument("--download", action="store_true", help="Execute downloads after the availability and cost checks.")

    print_cmd = commands.add_parser("build", help="Build STL and color 3MF files for each tile.")
    area_flags(print_cmd)
    imagery = print_cmd.add_mutually_exclusive_group(required=True)
    imagery.add_argument("--image", nargs=2, metavar=("DATE", "TIFF"), action="append", help="Repeat once per date.")
    imagery.add_argument("--series", type=Path, help="series.json saved by fetch.")
    print_cmd.add_argument("--dem", type=Path, required=True, help="Elevation GeoTIFF with heights in metres.")
    print_cmd.add_argument("--out", type=Path, required=True)
    print_cmd.add_argument("--name", default="Satellite terrain")
    print_cmd.add_argument("--provider", default="Local imagery")
    print_cmd.add_argument("--attribution")
    print_cmd.add_argument("--dem-attribution")
    print_cmd.add_argument("--encoding", choices=("auto", "reflectance", "srgb"), default="auto")
    print_cmd.add_argument("--rgb-bands", nargs=3, type=int, metavar=("R", "G", "B"))
    print_cmd.add_argument("--dn-offset", type=float, default=0, help="Unapplied offset in source digital numbers.")
    print_cmd.add_argument("--composite", choices=("median", "mean"), default="median")
    print_cmd.add_argument("--min-dates", type=int, default=1)
    print_cmd.add_argument("--each-date", action="store_true", help="Also print each date using the tile's composite palette.")
    print_cmd.add_argument("--palette", help="Default comma-separated hex colors. Quote the value.")
    print_cmd.add_argument("--palettes", type=Path, help="Reuse or edit a generated tile-palettes.json.")
    print_cmd.add_argument("--tile-palette", nargs=2, metavar=("TILE", "COLORS"), action="append", default=[])
    print_cmd.add_argument("--only-tile", help="Regenerate one tile using the layout's original geometry.")
    print_cmd.add_argument("--edge", choices=("puzzle", "straight"), default="puzzle")
    print_cmd.add_argument("--clearance-mm", type=float, default=.24, help="Total nominal clearance between puzzle pieces.")
    print_cmd.add_argument("--width-mm", type=float, default=160)
    print_cmd.add_argument("--base-mm", type=float, default=3.2)
    print_cmd.add_argument("--relief-mm", type=float, default=22)
    print_cmd.add_argument("--samples", type=int, help="Aligned samples per tile side. Default retries alternate sizes if needed.")
    print_cmd.add_argument("--engine", type=Path, default=ENGINE)
    args = parser.parse_args()
    try:
        if args.command == "build":
            build_with_mesh_check(args)
        else:
            run_api(args)
    except (ValueError, OSError, KeyError, RasterioError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    cli()
