"""Windows launcher for preparing terrain and downloading existing ClearSKY imagery."""

import json
from dataclasses import asdict
import os
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
import sys
from threading import Thread
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk
import webbrowser

from rasters import Area, dated_images
from terrain import ENGINE, write_json


CLI = Path(__file__).with_name("terrain.py")
PACKAGED_ENGINE = CLI.parent.parent / "bin" / "clearsky_terrain.exe"
FUJI = CLI.parent.parent / "examples" / "mount-fuji"
POLYGON_TOOLS = "https://polygon.clearsky.vision/"
DASHBOARD = "https://dashboard.clearsky.vision/"


class TerrainPrint:
    def __init__(self, root):
        self.root = root
        root.title("ClearSKY Terrain Print")
        root.geometry("830x740")
        root.minsize(690, 640)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        self.events = Queue()
        self.running = False
        self.actions = []
        self.last_output = None

        top = ttk.Frame(root, padding=(18, 16, 18, 10))
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="ClearSKY Terrain Print", font=("Segoe UI", 17, "bold")).pack(anchor="w")
        ttk.Label(top, text="Prepare terrain tiles for a Flashforge Creator 5 Pro.").pack(anchor="w")

        self.tabs = ttk.Notebook(root)
        self.tabs.grid(row=1, column=0, sticky="nsew", padx=16)
        self.build_tab = ttk.Frame(self.tabs, padding=12)
        self.fetch_tab = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(self.build_tab, text="Build print files")
        self.tabs.add(self.fetch_tab, text="ClearSKY order")
        for tab in (self.build_tab, self.fetch_tab):
            tab.columnconfigure(1, weight=1)
        self.build_controls()
        self.fetch_controls()

        details = ttk.Frame(root, padding=(18, 8, 18, 14))
        details.grid(row=2, column=0, sticky="ew")
        details.columnconfigure(0, weight=1)
        self.status = tk.StringVar(value="Ready")
        ttk.Label(details, textvariable=self.status).grid(row=0, column=0, sticky="w")
        self.log = tk.Text(details, height=7, wrap="word", state="disabled", font=("Consolas", 9))
        self.log.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(75, self.poll)

    def entry(self, parent, row, label, initial="", *, browse=None, secret=False):
        variable = tk.StringVar(value=initial)
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
        ttk.Entry(parent, textvariable=variable, show="*" if secret else "").grid(
            row=row, column=1, sticky="ew", pady=4
        )
        if browse:
            ttk.Button(parent, text="Browse…", command=lambda: self.browse(variable, browse)).grid(
                row=row, column=2, padx=(6, 0)
            )
        return variable

    def browse(self, field, kind):
        if kind == "output":
            chosen = filedialog.asksaveasfilename(
                parent=self.root, title="Name the output folder", initialfile="terrain-print"
            )
        else:
            chosen = filedialog.askopenfilename(parent=self.root, filetypes=kind)
        if chosen:
            field.set(chosen)

    def action(self, parent, title, command):
        button = ttk.Button(parent, text=title, command=command)
        button.pack(side="left", padx=(0, 8))
        self.actions.append(button)

    def build_controls(self):
        tab = self.build_tab
        self.series = self.entry(tab, 0, "Imagery series", browse=[("Series JSON", "*.json")])
        self.dem = self.entry(tab, 1, "Elevation GeoTIFF", browse=[("GeoTIFF", "*.tif *.tiff")])
        self.output = self.entry(tab, 2, "New print folder", browse="output")
        self.width = self.entry(tab, 3, "Tile width (mm)", "90")
        self.relief = self.entry(tab, 4, "Relief height (mm)", "18")
        self.gap = self.entry(tab, 5, "Joint clearance (mm)", "0.30")
        self.palette = self.entry(tab, 6, "Shared colors")
        ttk.Button(tab, text="Pick color…", command=self.add_color).grid(row=6, column=2, padx=(6, 0))
        self.palettes = self.entry(tab, 7, "Per-tile palette file", browse=[("Palette JSON", "*.json")])
        ttk.Label(tab, text="Leave colors blank for suggestions. Reuse tile-palettes.json to customize each tile.",
                  wraplength=540).grid(row=8, column=1, sticky="w", pady=(0, 6))
        self.settings = ttk.Frame(tab)
        self.settings.grid(row=9, column=1, sticky="w", pady=3)
        self.composite = tk.StringVar(value="median")
        ttk.Label(self.settings, text="Composite").pack(side="left", padx=(0, 6))
        ttk.Combobox(self.settings, values=("median", "mean"), textvariable=self.composite,
                     state="readonly", width=9).pack(side="left", padx=(0, 16))
        self.each = tk.BooleanVar()
        ttk.Checkbutton(self.settings, text="Also export each date", variable=self.each).pack(side="left", padx=(0, 12))
        self.minimum = tk.StringVar(value="1")
        ttk.Label(self.settings, text="Min. dates").pack(side="left", padx=(0, 4))
        ttk.Entry(self.settings, textvariable=self.minimum, width=3).pack(side="left")

        buttons = ttk.Frame(tab)
        buttons.grid(row=10, column=1, sticky="w", pady=(10, 0))
        self.action(buttons, "Build models", self.build)
        self.open_button = ttk.Button(buttons, text="Open print folder", state="disabled", command=self.reveal)
        self.open_button.pack(side="left")
        ttk.Button(buttons, text="Import TIFFs…", command=self.import_tiffs).pack(side="left", padx=(8, 0))
        if (FUJI / "series.json").is_file():
            ttk.Button(buttons, text="Load Fuji example", command=self.load_example).pack(side="left", padx=(8, 0))

    def fetch_controls(self):
        tab = self.fetch_tab
        self.key = self.entry(tab, 0, "API key", os.environ.get("CLEARSKY_API_KEY", ""), secret=True)
        ttk.Button(tab, text="Dashboard", command=lambda: webbrowser.open(DASHBOARD)).grid(
            row=0, column=2, padx=(6, 0)
        )
        self.order = self.entry(tab, 1, "Order UUID")
        self.area = self.entry(tab, 2, "Area GeoJSON", browse=[("GeoJSON", "*.geojson *.json")])
        self.grid = self.entry(tab, 3, "Columns × rows", "2 x 2")
        self.start = self.entry(tab, 4, "From date (YYYY-MM-DD)")
        self.end = self.entry(tab, 5, "To date (YYYY-MM-DD)")
        self.downloads = self.entry(tab, 6, "Imagery folder", browse="output")
        self.attribution = self.entry(tab, 7, "Order source credit")

        self.dates = tk.Listbox(tab, selectmode=tk.EXTENDED, exportselection=False, height=6)
        ttk.Label(tab, text="Dates to download").grid(row=8, column=0, sticky="nw", pady=6)
        self.dates.grid(row=8, column=1, sticky="ew", pady=6)
        ttk.Button(tab, text="Draw area", command=lambda: webbrowser.open(POLYGON_TOOLS)).grid(
            row=8, column=2, sticky="n", padx=(6, 0), pady=6
        )
        buttons = ttk.Frame(tab)
        buttons.grid(row=9, column=1, sticky="w", pady=(5, 0))
        self.action(buttons, "Find dates", self.find_dates)
        self.action(buttons, "Estimate and fetch", self.fetch)

    def add_color(self):
        colors = [color.strip() for color in self.palette.get().split(",") if color.strip()]
        if len(colors) >= 4:
            messagebox.showinfo("Four toolheads", "Clear a color in the Shared colors field before adding another.")
            return
        _, chosen = colorchooser.askcolor(parent=self.root)
        if chosen:
            self.palette.set(",".join(colors + [chosen.upper()]))

    def load_example(self):
        self.series.set(str(FUJI / "series.json"))
        self.dem.set(str(FUJI / "dem.tif"))
        self.width.set("90")
        self.relief.set("18")
        self.gap.set("0.30")
        self.minimum.set("2")
        self.palette.set("#19251F,#4D6245,#785C46,#E9E5D6")
        self.palettes.set("")
        self.status.set("Fuji example loaded. Choose a new print folder and click Build models.")

    def import_tiffs(self):
        paths = filedialog.askopenfilenames(
            parent=self.root, title="Select the dated RGB GeoTIFFs", filetypes=[("GeoTIFF", "*.tif *.tiff")]
        )
        if not paths:
            return
        outline = filedialog.askopenfilename(
            parent=self.root, title="Choose the print area exported from Polygon Tools",
            filetypes=[("GeoJSON", "*.geojson *.json")],
        )
        if not outline:
            return
        grid = simpledialog.askstring("Tile grid", "Columns × rows", initialvalue="2 x 2", parent=self.root)
        if not grid:
            return
        try:
            match = re.fullmatch(r"(\d+)\s*[x×]\s*(\d+)", grid.strip())
            if not match:
                raise ValueError("Enter the grid as 2 x 2 or 1 x 1.")
            area = Area.from_geojson(Path(outline), *map(int, match.groups()))
            images = dated_images(paths)
        except (ValueError, OSError, TypeError, KeyError) as error:
            messagebox.showerror("Check the GeoTIFFs", str(error))
            return
        save = filedialog.asksaveasfilename(
            parent=self.root, title="Save the local series", initialfile="series.json", defaultextension=".json",
            filetypes=[("Series JSON", "*.json")],
        )
        if save:
            try:
                write_json(Path(save), {
                    "provider": "Local imagery", "images": images, "area": asdict(area),
                    "model": None, "constellations": None, "attribution": None,
                })
            except OSError as error:
                messagebox.showerror("Could not save series", str(error))
                return
            self.series.set(save)
            self.status.set("Imagery series ready. Choose the elevation GeoTIFF and build.")

    def reveal(self):
        if self.last_output:
            os.startfile(self.last_output)

    def close(self):
        if self.running:
            messagebox.showinfo("Working", "Wait for the current operation to finish before closing.")
        else:
            self.root.destroy()

    def execute(self, args, label, on_success, *, key=None):
        if self.running:
            return
        env = os.environ.copy()
        if key:
            env["CLEARSKY_API_KEY"] = key
        env["PYTHONIOENCODING"] = "utf-8"
        self.running = True
        self.status.set(label)
        for action in self.actions:
            action.state(["disabled"])
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

        def worker():
            try:
                captured = []
                with subprocess.Popen([sys.executable, "-u", str(CLI), *args], env=env, text=True,
                                      encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW) as proc:
                    for line in proc.stdout:
                        captured.append(line)
                        self.events.put(("line", line))
                    status = proc.wait()
                self.events.put(("done", (status, captured, on_success)))
            except OSError as error:
                self.events.put(("done", (1, [str(error)], on_success)))

        Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "line":
                    self.log.configure(state="normal")
                    self.log.insert("end", value)
                    self.log.see("end")
                    self.log.configure(state="disabled")
                else:
                    status, lines, on_success = value
                    self.running = False
                    for action in self.actions:
                        action.state(["!disabled"])
                    self.status.set("Finished" if status == 0 else "Check the details below")
                    if status == 0:
                        try:
                            on_success(lines)
                        except (ValueError, KeyError, TypeError) as error:
                            self.status.set("Could not read the result")
                            messagebox.showerror("Could not read the result", str(error))
                    else:
                        messagebox.showerror("Could not finish", "".join(lines[-7:]).strip() or f"Exited with code {status}.")
        except Empty:
            pass
        self.root.after(75, self.poll)

    def area_args(self):
        key, order, area = (field.get().strip() for field in (self.key, self.order, self.area))
        dimensions = re.fullmatch(r"(\d+)\s*[x×]\s*(\d+)", self.grid.get().strip())
        if not key or not order or not area or not Path(area).is_file() or not dimensions:
            raise ValueError("Provide the API key, an order UUID, a GeoJSON area and a grid like 2 x 2.")
        return key, ["--order", order, "--aoi", area, "--tiles", *dimensions.groups()]

    def find_dates(self):
        try:
            key, args = self.area_args()
            start, end = self.start.get().strip(), self.end.get().strip()
            if not start or not end:
                raise ValueError("Enter both ends of the date window.")
        except ValueError as error:
            messagebox.showerror("Check the order", str(error))
            return

        def found(lines):
            self.dates.delete(0, "end")
            for day in (line.strip() for line in lines):
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                    self.dates.insert("end", day)
            self.status.set(f"{self.dates.size()} candidate dates. Select the dates you want.")

        self.execute(["dates", *args, "--from", start, "--to", end], "Checking available dates…", found, key=key)

    def fetch(self):
        try:
            key, args = self.area_args()
            folder = self.downloads.get().strip()
            days = [self.dates.get(index) for index in self.dates.curselection()]
            if not folder or not days:
                raise ValueError("Select one or more dates and choose an imagery folder.")
        except ValueError as error:
            messagebox.showerror("Check the download", str(error))
            return
        command = ["fetch", *args, "--out", folder, "--attribution", self.attribution.get().strip()]
        for day in days:
            command.extend(("--date", day))

        def loaded(_):
            self.series.set(str(Path(folder) / "series.json"))
            self.tabs.select(self.build_tab)
            self.status.set("Imagery ready. Choose a DEM and build the print files.")

        def approve(lines):
            quotes = [json.loads(line) for line in lines if line.lstrip().startswith("{")]
            if not quotes:
                loaded([])
                return
            summary = "\n".join(f"{quote['date']}   {quote['estimate'].get('ProcessingUnitEstimate', '?')} units"
                                for quote in quotes)
            if messagebox.askyesno("Download imagery", f"Estimated processing units by date\n\n{summary}\n\nFetch these files?"):
                self.execute([*command, "--download"], "Downloading ClearSKY imagery…", loaded, key=key)

        self.execute(command, "Checking availability and estimating the download…", approve, key=key)

    def build(self):
        series, dem, output = (field.get().strip() for field in (self.series, self.dem, self.output))
        if not series or not dem or not output or not Path(series).is_file() or not Path(dem).is_file():
            messagebox.showerror("Check the print inputs", "Choose a series.json file, a DEM GeoTIFF and a new print folder.")
            return
        engine = PACKAGED_ENGINE if PACKAGED_ENGINE.is_file() else ENGINE
        command = [
            "build", "--series", series, "--dem", dem, "--out", output, "--engine", str(engine),
            "--width-mm", self.width.get(), "--relief-mm", self.relief.get(), "--clearance-mm", self.gap.get(),
            "--base-mm", "2.8", "--composite", self.composite.get(), "--min-dates", self.minimum.get(),
        ]
        for flag, value in (("--palette", self.palette.get().strip()), ("--palettes", self.palettes.get().strip())):
            if value:
                command.extend((flag, value))
        if self.each.get():
            command.append("--each-date")

        def done(_):
            self.last_output = Path(output).resolve()
            self.open_button.state(["!disabled"])
            self.status.set("Print files ready. Open the output folder for the 3MF files and filament guide.")

        self.execute(command, "Building terrain models…", done)


if __name__ == "__main__":
    window = tk.Tk()
    app = TerrainPrint(window)
    window.mainloop()
