"""Area selection, GeoTIFF alignment and temporal compositing."""

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re
import warnings

import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform


@dataclass(frozen=True)
class Area:
    lon: float
    lat: float
    span_km: float
    columns: int
    rows: int
    projection: int | None = None

    def __post_init__(self):
        if not (-180 < self.lon < 180 and -80 <= self.lat <= 84):
            raise ValueError("Choose a longitude between -180 and 180 and a UTM latitude (-80 to 84).")
        if not (0.25 <= self.span_km <= 250 and 1 <= self.columns <= 8 and 1 <= self.rows <= 8):
            raise ValueError("Use a tile span of 0.25–250 km and 1–8 rows and columns.")

    @classmethod
    def from_geojson(cls, path, columns, rows):
        geometry = json.loads(path.read_text(encoding="utf-8-sig"))
        if geometry["type"] == "Feature":
            geometry = geometry["geometry"]
        if geometry["type"] == "FeatureCollection" and len(geometry["features"]) == 1:
            geometry = geometry["features"][0]["geometry"]
        if geometry["type"] != "Polygon":
            raise ValueError("Export one WGS84 Polygon from ClearSKY Polygon Tools.")
        vertices = geometry["coordinates"][0]
        lons, lats = ([point[axis] for point in vertices] for axis in (0, 1))
        if max(lons) - min(lons) >= 180 or len(vertices) < 4:
            raise ValueError("Use a non-empty polygon that does not cross the antimeridian.")
        lon = (max(lons) + min(lons)) / 2
        lat = (max(lats) + min(lats)) / 2
        origin = cls(lon, lat, .25, columns, rows)
        x, y = transform(4326, origin.epsg, lons, lats)
        center_lon, center_lat = transform(origin.epsg, 4326, [(min(x) + max(x)) / 2], [(min(y) + max(y)) / 2])
        span = max((max(x) - min(x)) / columns, (max(y) - min(y)) / rows) / 1000
        # Keep the projection used to compute the enclosure even if its midpoint
        # is on the other side of a UTM zone boundary.
        return cls(center_lon[0], center_lat[0], max(.25, span), columns, rows, origin.epsg)

    @property
    def epsg(self):
        return self.projection or ((32600 if self.lat >= 0 else 32700) + int((self.lon + 180) / 6) + 1)

    @property
    def bounds(self):
        x, y = transform(4326, self.epsg, [self.lon], [self.lat])
        dx = self.span_km * 500 * self.columns
        dy = self.span_km * 500 * self.rows
        return x[0] - dx, y[0] - dy, x[0] + dx, y[0] + dy

    def wkt(self):
        west, south, east, north = self.bounds
        corners = [(west, south), (east, south), (east, north), (west, north)]
        edges = [
            (x1 + (x2 - x1) * step / 16, y1 + (y2 - y1) * step / 16)
            for (x1, y1), (x2, y2) in zip(corners, corners[1:] + corners[:1])
            for step in range(16)
        ]
        lon, lat = transform(self.epsg, 4326, *zip(*(edges + edges[:1])))
        if max(lon) - min(lon) > 180:
            raise ValueError("This release supports areas within one side of the antimeridian.")
        return "POLYGON((" + ",".join(f"{x:.9f} {y:.9f}" for x, y in zip(lon, lat)) + "))"

    def grid(self, samples):
        spacing = self.span_km * 1000 / (samples - 1)
        west, _, _, north = self.bounds
        # The outer pixel centers are the tile corners. Neighbors share samples.
        return {
            "crs": self.epsg,
            "transform": from_origin(west - spacing / 2, north + spacing / 2, spacing, spacing),
            "width": self.columns * (samples - 1) + 1,
            "height": self.rows * (samples - 1) + 1,
            "dtype": "float32",
            "nodata": np.nan,
        }

    def tile_center(self, column, row):
        west, _, _, north = self.bounds
        step = self.span_km * 1000
        x, y = transform(self.epsg, 4326, [west + (column + .5) * step], [north - (row + .5) * step])
        return x[0], y[0]


def _read_aligned(source, bands, grid, default_nodata=None):
    if not source.crs:
        raise ValueError(f"{Path(source.name).name} is missing a coordinate reference system.")
    options = {"src_nodata": default_nodata} if source.nodata is None and default_nodata is not None else {}
    with WarpedVRT(source, **grid, **options, resampling=Resampling.bilinear) as aligned:
        if ColorInterp.alpha in aligned.colorinterp:
            alpha = aligned.colorinterp.index(ColorInterp.alpha) + 1
            return np.where(aligned.read(alpha) > 0, aligned.read(bands), np.nan)
        return aligned.read(bands, masked=True).filled(np.nan)


def read_dem(path, grid):
    with rasterio.open(path) as source:
        data = _read_aligned(source, [1], grid)[0]
        data = data * source.scales[0] + source.offsets[0]
        if (source.units[0] or "").lower() in {"ft", "foot", "feet", "us survey foot"}:
            raise ValueError("DEM elevations must be in metres. Convert the vertical unit first.")
    if not np.isfinite(data).all():
        missing = 1 - np.isfinite(data).mean()
        raise ValueError(f"DEM is missing {missing:.1%} of the print area. Choose a smaller area or a complete DEM.")
    return data


def _rgb_bands(source, override):
    if override:
        if len(set(override)) != 3 or min(override) < 1 or max(override) > source.count:
            raise ValueError(f"RGB band indexes do not fit {Path(source.name).name}.")
        return override
    colors = source.colorinterp
    rgb = (ColorInterp.red, ColorInterp.green, ColorInterp.blue)
    if all(channel in colors for channel in rgb):
        return [colors.index(channel) + 1 for channel in rgb]
    for attribute in ("description", "name", "bandname"):
        descriptions = [
            value if attribute == "description" else
            {key.lower(): tag for key, tag in source.tags(index).items()}.get(attribute)
            for index, value in enumerate(source.descriptions, start=1)
        ]
        labels = [re.sub(r"^B0([234])$", r"B\1", (label or "").upper().strip()) for label in descriptions]
        if all(band in labels for band in ("B4", "B3", "B2")):
            return [labels.index(band) + 1 for band in ("B4", "B3", "B2")]
    if source.count == 3:
        return [1, 2, 3]
    raise ValueError(f"Cannot identify red, green and blue in {Path(source.name).name}. Use --rgb-bands.")


def read_rgb(path, grid, encoding, bands=None, dn_offset=0):
    with rasterio.open(path) as source:
        bands = _rgb_bands(source, bands)
        sample_type = source.dtypes[bands[0] - 1]
        sample_types = {source.dtypes[band - 1] for band in bands}
        if len(sample_types) != 1:
            raise ValueError("Choose three RGB bands with the same stored data type.")
        imagery = _read_aligned(source, bands, grid, -32768 if sample_type == "int16" else None)
        if encoding == "srgb" or (encoding == "auto" and sample_type == "uint8"):
            values = np.clip(imagery / 255, 0, 1)
            # Undo display gamma before combining dates. TopoSaic maps 0.30
            # reflectance to display white when it assigns printable colors.
            linear = np.where(values <= .04045, values / 12.92, ((values + .055) / 1.055) ** 2.4)
            imagery = linear * .30
        else:
            for channel, band in enumerate(bands):
                scale, offset = source.scales[band - 1], source.offsets[band - 1]
                if scale == 1 and np.issubdtype(sample_type, np.integer):
                    scale = .0001
                imagery[channel] = (imagery[channel] + dn_offset) * scale + offset
    valid = np.isfinite(imagery).all(axis=0)
    return np.where(valid, imagery, np.nan)


def source_metadata(path):
    with rasterio.open(path) as source:
        return {key: value for key, value in source.tags().items()
                if key.upper() in {"IMAGE_DATE", "MODEL_VERSION", "IMAGE_VERSION", "CONSTELLATIONS", "SOURCE"}
                or any(term in key.lower() for term in ("attrib", "license", "provenance"))}


def dated_images(paths):
    images = {}
    for path in map(Path, paths):
        tags = {key.upper(): value for key, value in source_metadata(path).items()}
        guess = re.search(r"(?:19|20)\d{2}-?\d{2}-?\d{2}", path.stem)
        value = tags.get("IMAGE_DATE") or (guess.group(0) if guess else None)
        if not value:
            raise ValueError(f"{path.name} needs an image date in its metadata or a YYYY-MM-DD filename.")
        day = date.fromisoformat(value).isoformat()
        if day in images:
            raise ValueError(f"Choose only one image for {day}.")
        images[day] = str(path.resolve())
    return images


def composite(images, method, min_dates):
    stack = np.stack(images)
    counts = np.isfinite(stack[:, 0]).sum(axis=0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        if method == "median":
            rgb = np.nanmedian(stack, axis=0, overwrite_input=True)
        else:
            rgb = np.nanmean(stack, axis=0)
    rgb[:, counts < min_dates] = np.nan
    return rgb, counts


def display_rgb(rgb):
    linear = np.clip(np.nan_to_num(rgb) / .30, 0, 1)
    gamma = np.where(linear <= .0031308, 12.92 * linear, 1.055 * linear ** (1 / 2.4) - .055)
    return np.moveaxis(np.rint(gamma * 255).astype("uint8"), 0, -1)


def as_engine_pixels(rgb):
    valid = np.isfinite(rgb).all(axis=0)
    pixels = np.rint(np.clip(np.nan_to_num(rgb), 0, 1) * 10_000).astype("uint16")
    return np.moveaxis(pixels[:, ::-1, :], 0, -1).reshape(-1, 3).tolist(), valid[::-1].ravel().tolist()
