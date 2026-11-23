"""Read existing ClearSKY orders and download imagery for a print area."""

from datetime import date
import json
import os
from pathlib import Path
import shutil
from tempfile import NamedTemporaryFile
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID

import rasterio


BASE_URL = "https://api.clearsky.vision"
PROCESS = "/api/satelliteimages/process/composite"


class Client:
    def __init__(self):
        self.key = os.environ.get("CLEARSKY_API_KEY")
        if not self.key:
            raise ValueError("Set CLEARSKY_API_KEY before using ClearSKY commands.")

    def _request(self, path, body=None, *, retry=True):
        request = Request(
            BASE_URL + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"X-API-KEY": self.key, "Content-Type": "application/json"},
        )
        attempts = 3 if retry else 1
        for attempt in range(attempts):
            try:
                return urlopen(request, timeout=240)
            except HTTPError as error:
                with error:
                    if error.code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                        retry_after = error.headers.get("Retry-After", "")
                        time.sleep(min(int(retry_after), 30) if retry_after.isdigit() else 2 ** attempt)
                        continue
                    try:
                        payload = json.loads(error.read(64_000))
                    except (UnicodeDecodeError, ValueError):
                        payload = {}
                    detail = payload.get("Error") or payload.get("title") or error.reason
                    raise ValueError(f"ClearSKY returned HTTP {error.code} from {path}: {detail}") from error

    def json(self, path, body=None):
        with self._request(path, body) as response:
            value = json.load(response)
        if not isinstance(value, dict) or value.get("Succeeded") is False or "Data" not in value:
            raise ValueError(f"ClearSKY did not complete {path}: {value}")
        return value["Data"]

    def orders(self, order=None):
        if order:
            order = str(UUID(order))
            result = self.json("/api/tasking/orders/details", {"Guids": [order]})
        else:
            result = self.json("/api/tasking/orders?" + urlencode({"getExpired": "false"}))
        return result["TaskOrders"]

    def dates(self, area, start, end):
        return self.json("/api/satelliteimages/search/available", {
            "Wkt": area.wkt(),
            "From": f"{start}T00:00:00Z",
            "Until": f"{end}T23:59:59Z",
        })

    def download(self, payload, path):
        # The processing request can consume units. Do not blindly replay it.
        temp_path = None
        try:
            with self._request(PROCESS, payload, retry=False) as response, NamedTemporaryFile(
                dir=path.parent, suffix=".tif", delete=False,
            ) as staging:
                temp_path = Path(staging.name)
                shutil.copyfileobj(response, staging, length=1024 * 1024)
            with rasterio.open(temp_path) as source:
                if not source.crs or source.count != 3:
                    raise ValueError("ClearSKY did not return the requested three-band georeferenced TIFF.")
            temp_path.replace(path)
        finally:
            if temp_path:
                temp_path.unlink(missing_ok=True)


def order_settings(client, order):
    items = client.orders(order)
    if len(items) != 1:
        raise ValueError(f"Could not find order {order}. Check its ID and API key.")
    item = items[0]
    return item["Model"], item["SatelliteConstellations"]


def discover_dates(data, model, constellations, start, end):
    for group in data["ModelImageDates"]:
        if group["Model"] != model or set(group["SatelliteConstellations"]) != set(constellations):
            continue
        for segment in group["DatesByGeog"]:
            for value in segment["Dates"]:
                day = date.fromisoformat(value[:10])
                if start <= day <= end:
                    yield day


def prepare_downloads(client, area, model, constellations, dates):
    wkt = area.wkt()
    estimate = None
    for day in dates:
        payload = {
            "Wkt": wkt,
            "Date": f"{day}T00:00:00Z",
            "EpsgProjection": area.epsg,
            "Model": model,
            "SatelliteConstellations": constellations,
            "UtmDataSelectionMode": "combined_utm",
            "Bandnames": "B4,B3,B2",
            "DataType": "INT16",
            "Level": "BOA",
            "UseLegacySentinel2Values": True,
            "Resolution": 10,
            "FileType": "tif",
            "PixelSelectionMode": "intersect",
            "UtmGridForcePixelResolutionSize": True,
            "AllowPartialImage": False,
            "AutomaticOrderCreation": False,
        }
        eligibility = client.json(PROCESS + "/available", {
            key: payload[key] for key in (
                "Wkt", "Date", "EpsgProjection", "Model", "SatelliteConstellations",
                "UtmDataSelectionMode", "Bandnames",
            )
        })
        if not all(eligibility.get(key) is True for key in (
            "FullyAvailable", "OrdersCoverPolygon", "DataAvailableForUser",
        )):
            raise ValueError(f"{day} does not cover the print area: {eligibility}. Choose another date or reduce the area.")
        if estimate is None:
            estimate = client.json(PROCESS + "/estimate", {"Wkt": wkt})
        yield day, payload, estimate
