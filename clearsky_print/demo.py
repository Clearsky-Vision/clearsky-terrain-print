"""Generate a small synthetic three-date example with two puzzle tiles."""

import argparse
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

from rasters import Area
from terrain import write_json
from dataclasses import asdict


def generate(out):
    out.mkdir(parents=True, exist_ok=False)
    area = Area(10.55, 56.23, 3, 2, 1)
    west, south, east, north = area.bounds
    step = 25
    width, height = round((east - west) / step) + 8, round((north - south) / step) + 8
    grid = {
        "driver": "GTiff", "width": width, "height": height, "crs": area.epsg,
        "transform": from_origin(west - 4 * step, north + 4 * step, step, step), "compress": "deflate",
    }
    y, x = np.meshgrid(np.linspace(1, -1, height), np.linspace(-2, 2, width), indexing="ij")
    elevation = 24 + 180 * np.exp(-((x - .10) ** 2 + (y + .12) ** 2) * 2.1)
    elevation += 65 * np.exp(-((x + 1.15) ** 2 * 3 + (y - .45) ** 2 * 5))
    elevation += 13 * np.sin(3 * x + 2 * y)
    with rasterio.open(out / "dem.tif", "w", **grid, count=1, dtype="float32") as dem:
        dem.write(elevation.astype("float32"), 1)
        dem.set_band_unit(1, "m")
    zones = np.select(
        [y < -.53 + .23 * np.sin(2 * x), elevation > 150, np.sin(4 * x) + np.cos(6 * y) > .4],
        [0, 1, 2], default=3,
    )
    dates = ["2025-06-02", "2025-06-12", "2025-06-22"]
    for index, day in enumerate(dates):
        colors = np.array([[240, 450, 730], [1650, 1290, 780], [840, 1270, 390], [390, 650, 240]])
        if index == 2:
            colors[2] = [1500, 1450, 480]
        rgb = np.moveaxis(colors[zones], -1, 0).astype("int16")
        if index == 0:
            rgb[:, 25:47, 70:100] = -32768
        if index == 1:
            rgb[:, 55:61, 119:126] = 2600
        with rasterio.open(out / f"{day}.tif", "w", **grid, count=3, dtype="int16", nodata=-32768) as image:
            image.write(rgb)
            image.descriptions = ("B04", "B03", "B02")
            image.update_tags(IMAGE_DATE=day.replace("-", ""), MODEL_VERSION="synthetic-demo")
    write_json(out / "series.json", {
        "provider": "Synthetic example",
        "area": asdict(area),
        "model": None,
        "constellations": None,
        "attribution": "Generated test data. No satellite observations were used.",
        "images": {day: f"{day}.tif" for day in dates},
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    generate(parser.parse_args().output)
