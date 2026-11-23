"""Regression tests for date compositing, API preflight and puzzle export."""

from collections import Counter
from datetime import date
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree
from zipfile import ZipFile

import numpy as np

from clearsky_api import PROCESS, prepare_downloads
from demo import generate
from rasters import Area, composite
from terrain import ENGINE, MeshCheckError, build_with_mesh_check


class TemporalTests(unittest.TestCase):
    def test_mesh_retry_preserves_an_explicit_grid_size(self):
        tries = []

        def generate(args):
            tries.append(args.samples)
            if len(tries) == 1:
                raise MeshCheckError("fixture mesh check")

        with patch("terrain.build", side_effect=generate), patch("builtins.print"):
            build_with_mesh_check(SimpleNamespace(samples=None, width_mm=90))
        self.assertEqual(tries, [65, 81])
        with patch("terrain.build", side_effect=MeshCheckError("fixture mesh check")) as call:
            with self.assertRaises(MeshCheckError):
                build_with_mesh_check(SimpleNamespace(samples=65, width_mm=90))
        self.assertEqual(call.call_count, 1)

    def test_median_and_mean_ignore_missing_dates(self):
        images = [np.full((3, 1, 2), value) for value in (.1, .2, .9)]
        images[0][:, 0, 1] = np.nan
        images[2][:, 0, 1] = np.nan
        middle, counts = composite(images, "median", 2)
        average, _ = composite(images, "mean", 2)
        np.testing.assert_allclose(middle[:, 0, 0], .2)
        np.testing.assert_allclose(average[:, 0, 0], .4)
        np.testing.assert_array_equal(counts, [[3, 1]])
        self.assertTrue(np.isnan(middle[:, 0, 1]).all())

    def test_geometry_only_estimate_and_paid_download_payload(self):
        calls = []

        class FakeClient:
            def json(self, path, body):
                calls.append((path, body))
                if path.endswith("/available"):
                    return dict.fromkeys(("FullyAvailable", "OrdersCoverPolygon", "DataAvailableForUser"), True)
                return {"ProcessingUnitEstimate": 20}

        area = Area(10.55, 56.23, 3, 2, 1)
        days = [date(2025, 6, 2), date(2025, 6, 12)]
        result = list(prepare_downloads(FakeClient(), area, "Stratus2", ["Sentinel2"], days))
        self.assertEqual([call[0] for call in calls], [PROCESS + "/available", PROCESS + "/estimate",
                                                       PROCESS + "/available"])
        self.assertEqual(calls[1][1], {"Wkt": area.wkt()})
        self.assertNotIn("DataType", calls[0][1])
        self.assertEqual(result[0][1]["Bandnames"], "B4,B3,B2")
        self.assertFalse(result[0][1]["AutomaticOrderCreation"])


def read_model(path):
    with ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("3D/3dmodel.model"))
        assert "Metadata/project_settings.config" not in archive.namelist()
    vertices = [tuple(float(v.attrib[axis]) for axis in ("x", "y", "z"))
                for v in root.findall(".//{*}vertex")]
    triangles = [node.attrib for node in root.findall(".//{*}triangle")]
    return vertices, triangles


class ExportTests(unittest.TestCase):
    @unittest.skipUnless(ENGINE.is_file(), "Build the Rust example for the full integration test")
    def test_two_puzzle_tiles_keep_distinct_palettes_and_form_closed_meshes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            data, prints = root / "data", root / "prints"
            generate(data)
            palettes = {
                "r01c01": "#3C597C,#658450,#A7875E,#BBC19A",
                "r01c02": "#315780,#698956,#967C54,#CDDAB0",
            }
            command = [
                sys.executable, str(Path(__file__).with_name("terrain.py")), "build",
                "--series", str(data / "series.json"), "--dem", str(data / "dem.tif"),
                "--out", str(prints), "--samples", "65", "--min-dates", "2",
            ]
            for name, colors in palettes.items():
                command.extend(("--tile-palette", name, colors))
            subprocess.run(command, check=True, capture_output=True)
            manifest = json.loads((prints / "print-manifest.json").read_text("utf-8"))
            self.assertEqual(manifest["tile_edges"], "puzzle")
            heights, limits = [], []
            for name, colors in palettes.items():
                folder = prints / name / "median"
                self.assertEqual(manifest["tiles"][name]["palette"], colors.split(","))
                self.assertEqual(len(manifest["tiles"][name]["prints"]["median"]["colors_in_3mf_order"]), 4)
                vertices, triangles = read_model(folder / "terrain-solid.3mf")
                self.assertTrue(all("paint_color" in face and "pid" in face for face in triangles))
                edges = Counter()
                for face in triangles:
                    a, b, c = (int(face[key]) for key in ("v1", "v2", "v3"))
                    edges.update(tuple(sorted(pair)) for pair in ((a, b), (b, c), (c, a)))
                self.assertEqual(set(edges.values()), {2})
                limits.append((min(v[0] for v in vertices), max(v[0] for v in vertices)))
                spec = json.loads((folder / "manifest.json").read_text("utf-8"))["spec"]
                heights.append((spec["elevation_datum_m"], spec["elevation_m_per_mm"]))
                self.assertTrue((folder / "filament-preview.png").is_file())
            self.assertEqual(heights[0], heights[1])
            self.assertTrue(limits[0][1] > 160 or limits[1][0] < 0, "One tile must have a tab on the seam")


if __name__ == "__main__":
    unittest.main()
