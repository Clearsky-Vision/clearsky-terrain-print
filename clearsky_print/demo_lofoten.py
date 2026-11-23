"""Prepare an open-data Lofoten example from public Sentinel-2 and DEM windows."""

import argparse
from dataclasses import asdict
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling
from rasterio.vrt import WarpedVRT

from rasters import Area
from terrain import write_json


SCENES = {
    "2025-07-06": "S2C_33WVR_20250706_0_L2A",
    "2025-07-13": "S2C_33WVR_20250713_0_L2A",
    "2025-07-16": "S2C_33WVR_20250716_1_L2A",
}
SENTINEL = "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/33/W/VR/2025/7"
DEM_TILE = "Copernicus_DSM_COG_10_N68_00_E013_00_DEM"
DEM = f"https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com/{DEM_TILE}/{DEM_TILE}.tif"
IMAGE_CREDIT = "Contains modified Copernicus Sentinel data 2025. COG distribution by Element 84 Earth Search."
DEM_CREDIT = (
    "Produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH "
    "2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved"
)


def read_window(url, grid, bands, resampling):
    with rasterio.open(url) as source, WarpedVRT(source, **grid, resampling=resampling) as cropped:
        return cropped.read(bands)


def generate(folder):
    folder.mkdir(parents=True, exist_ok=False)
    area = Area(13.54, 68.194, 2.4, 2, 1)
    grid = area.grid(241)
    heights = read_window(DEM, grid, 1, Resampling.bilinear)
    with rasterio.open(folder / "dem.tif", "w", driver="GTiff", count=1, compress="deflate", **grid) as out:
        out.write(heights, 1)
        out.set_band_unit(1, "m")
        out.update_tags(SOURCE=DEM, ATTRIBUTION=DEM_CREDIT)

    photos = {**grid, "dtype": "uint8", "nodata": None}
    for day, scene in SCENES.items():
        print(f"Reading {scene}", flush=True)
        prefix = f"{SENTINEL}/{scene}"
        rgb = read_window(f"{prefix}/TCI.tif", grid, [1, 2, 3], Resampling.bilinear)
        classification = read_window(f"{prefix}/SCL.tif", grid, 1, Resampling.nearest)
        valid = np.isin(classification, [2, 4, 5, 6, 7, 11]) & np.isfinite(rgb).all(axis=0)
        with rasterio.open(folder / f"{day}.tif", "w", driver="GTiff", count=4,
                           compress="deflate", **photos) as out:
            out.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha)
            out.write(np.where(valid, rgb, 0).astype("uint8"), indexes=[1, 2, 3])
            out.write(valid.astype("uint8") * 255, indexes=4)
            out.update_tags(IMAGE_DATE=day.replace("-", ""), ATTRIBUTION=IMAGE_CREDIT, SOURCE=scene)

    write_json(folder / "series.json", {
        "provider": "Copernicus Sentinel-2 L2A via Earth Search",
        "area": asdict(area), "model": None, "constellations": ["Sentinel2"],
        "attribution": IMAGE_CREDIT,
        "images": {day: f"{day}.tif" for day in SCENES},
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    generate(parser.parse_args().output)
