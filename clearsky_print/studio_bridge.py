"""Connect TopoSaic's local studio to the ClearSKY terrain pipeline."""

from argparse import Namespace
from dataclasses import asdict
from datetime import date
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from xml.etree import ElementTree
from zipfile import ZIP_DEFLATED, ZipFile

from clearsky_api import Client, discover_dates, order_settings, prepare_downloads
from rasters import Area, dated_images
from terrain import MAX_DATES, build_with_mesh_check, write_json


def select_file(request, _):
    import tkinter as tk
    from tkinter import filedialog

    filters = {
        "series": ("Imagery series", "*.json"),
        "dem": ("Elevation GeoTIFF", "*.tif *.tiff"),
        "palettes": ("Tile palettes", "*.json"),
    }
    label, pattern = filters[request["kind"]]
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return {"path": filedialog.askopenfilename(parent=root, title=f"Choose {label}", filetypes=[(label, pattern)])}
    finally:
        root.destroy()


def read_series(path):
    source = json.loads(path.read_text("utf-8-sig"))
    area = Area(**source["area"])
    images = {str(date.fromisoformat(day)): path.parent / value for day, value in source["images"].items()}
    if not 1 <= len(images) <= MAX_DATES or any(not value.is_file() for value in images.values()):
        raise ValueError("The series must refer to 1–32 dated GeoTIFFs that exist on this computer.")
    return source, area, images


def inspect_series(request, _):
    if request.get("example"):
        folder = Path(os.environ["TOPOSAIC_CLEARSKY_EXAMPLE"])
        path, dem = folder / "series.json", folder / "dem.tif"
    else:
        path, dem = Path(request["path"]), None
    source, area, images = read_series(path)
    return {
        "path": str(path), "dem": str(dem) if dem else None, "area": asdict(area),
        "dates": sorted(images), "provider": source["provider"], "model": source["model"],
    }


def import_images(request, output):
    import tkinter as tk
    from tkinter import filedialog

    area = Area(**request["area"])
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        paths = filedialog.askopenfilenames(parent=root, title="Choose the dated RGB GeoTIFFs",
                                           filetypes=[("GeoTIFF", "*.tif *.tiff")])
    finally:
        root.destroy()
    if not paths:
        return {"path": None}
    images = dated_images(paths)
    if len(images) > MAX_DATES:
        raise ValueError(f"Choose up to {MAX_DATES} dates.")
    output.mkdir(parents=True, exist_ok=False)
    path = output / "series.json"
    write_json(path, {"provider": "Local imagery", "area": asdict(area), "images": images,
                      "model": None, "constellations": None, "attribution": None})
    return inspect_series({"path": str(path)}, None)


def dates(request, _):
    area = Area(**request["area"])
    start, end = date.fromisoformat(request["start"]), date.fromisoformat(request["end"])
    if start > end:
        raise ValueError("The start date must be before the end date.")
    client = Client()
    model, constellations = order_settings(client, request["order"])
    result = discover_dates(client.dates(area, start, end), model, constellations, start, end)
    return {"dates": sorted(set(map(str, result))), "model": model}


def download(request, output):
    area = Area(**request["area"])
    days = sorted(set(map(date.fromisoformat, request["dates"])))
    if not 1 <= len(days) <= MAX_DATES:
        raise ValueError(f"Choose 1–{MAX_DATES} available dates.")
    client = Client()
    model, constellations = order_settings(client, request["order"])
    planned = list(prepare_downloads(client, area, model, constellations, days))
    if output is None:
        return {"estimates": [{"date": str(day), "units": estimate.get("ProcessingUnitEstimate")}
                              for day, _, estimate in planned]}
    output.mkdir(parents=True, exist_ok=False)
    images = {}
    for day, payload, _ in planned:
        name = f"{day}.tif"
        client.download(payload, output / name)
        images[str(day)] = name
    path = output / "series.json"
    write_json(path, {"provider": "ClearSKY Vision", "area": asdict(area), "model": model,
                      "constellations": constellations, "attribution": request.get("attribution"),
                      "images": images})
    return inspect_series({"path": str(path)}, None)


def build(request, output):
    source, _, images = read_series(Path(request["series"]))
    area = Area(**request["area"])
    palette = request.get("palette")
    dem = Path(request["dem"])
    if not dem.is_file():
        raise ValueError("Choose an elevation GeoTIFF that exists on this computer.")
    with TemporaryDirectory(prefix="inputs-", dir=output.parent) as temporary:
        folder = Path(temporary)
        series = folder / "series.json"
        picked = {str(date.fromisoformat(day)) for day in request["dates"]}
        if not picked or not picked.issubset(images):
            raise ValueError("Choose one or more dates from the loaded imagery.")
        write_json(series, {**source, "area": asdict(area),
                            "attribution": request.get("imageryAttribution") or source.get("attribution"),
                            "images": {day: str(images[day].resolve()) for day in sorted(picked)}})
        args = Namespace(
            series=series, center=None, span_km=None, aoi=None, tiles=None,
            image=None, provider=source["provider"], attribution=None, dem=dem,
            dem_attribution=request.get("demAttribution"), engine=Path(os.environ["TOPOSAIC_CLEARSKY_ENGINE"]),
            out=folder / "prints", name=request.get("name") or "Satellite terrain",
            encoding="auto", rgb_bands=None, dn_offset=0, composite=request["composite"],
            min_dates=request["minimumDates"], each_date=request["eachDate"],
            palette=palette, palettes=Path(request["paletteFile"]) if request.get("paletteFile") else None,
            tile_palette=[(key, ",".join(colors)) for key, colors in request.get("tilePalettes", {}).items()],
            only_tile=None, edge=request["edge"], clearance_mm=request["clearanceMm"],
            width_mm=request["widthMm"], base_mm=request["baseMm"], relief_mm=request["reliefMm"], samples=None,
        )
        # Keep stdout for the JSON reply. Successful CLI generation logs go to stderr.
        from contextlib import redirect_stdout
        with redirect_stdout(sys.stderr):
            build_with_mesh_check(args)
        manifest = json.loads((args.out / "print-manifest.json").read_text("utf-8"))
        for model in args.out.glob("r*/**/terrain-solid.3mf"):
            add_export_preview(model, manifest["samples_per_tile_side"])
        with ZipFile(args.out / "print-kit.zip", "w", ZIP_DEFLATED, compresslevel=5) as archive:
            for path in args.out.rglob("*"):
                if path.is_file() and path != args.out / "print-kit.zip" and path.name != "preview.json":
                    archive.write(path, path.relative_to(args.out))
        args.out.rename(output)
    return manifest


def add_export_preview(model, samples_across):
    """Use the exact file geometry and filament assignments in the 3D viewer."""
    path = model.with_name("preview.json")
    preview = json.loads(path.read_text("utf-8"))
    with ZipFile(model) as archive:
        document = ElementTree.fromstring(archive.read("3D/3dmodel.model"))

    color_group = document.find(".//{*}colorgroup")
    slots = [entry.attrib["color"][:7].upper() for entry in color_group]
    palette = preview["ground_palette"]
    for color in slots:
        if color not in palette:
            palette.append(color)
    indices = [palette.index(color) + preview["ground_material_offset"] for color in slots]

    mesh = document.find(".//{*}mesh")
    vertices = [[float(node.attrib[axis]) for axis in "xyz"]
                for node in mesh.findall("./{*}vertices/{*}vertex")]
    faces = mesh.findall("./{*}triangles/{*}triangle")
    preview["model_meshes"] = [{
        "kind": "terrain",
        "name": "Solid terrain",
        "vertices": vertices,
        "triangles": [[int(face.attrib[key]) for key in ("v1", "v2", "v3")] for face in faces],
        "materials": [indices[int(face.attrib["p1"])] for face in faces],
    }]
    axes = list(zip(*vertices))
    preview["model_bounds_mm"] = [min(axis) for axis in axes] + [max(axis) for axis in axes]
    preview["model_preview_scope"] = "model"
    preview["model_mesh_samples_across"] = samples_across
    write_json(path, preview)


def main():
    command = sys.argv[1]
    request = json.load(sys.stdin)
    output = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    actions = {"pick": select_file, "inspect": inspect_series, "import": import_images, "dates": dates,
               "estimate": download, "fetch": download, "build": build}
    try:
        result = actions[command](request, output)
    except Exception as error:
        # Raster and subprocess errors also cross the API boundary as JSON.
        print(json.dumps({"error": str(error)}))
        return 1
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
