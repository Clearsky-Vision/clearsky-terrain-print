"use client";

import { lazy, Suspense, useEffect, useState } from "react";
import { API_URL, jsonBody, requestJson, terrainApi } from "./api";
import { initialSpec } from "./config";
import type { GenerationSpec, PlaceResult, PreviewData } from "./contracts";
import { coordinateAtLocalOffset } from "./geo";
import { TerrainMap } from "./map";

const ReliefPreview = lazy(() => import("./preview").then(({ ReliefPreview }) => ({ default: ReliefPreview })));
const ROOT = "/api/clearsky";
const COLORS = ["#19251F", "#4D6245", "#785C46", "#E9E5D6"];
const FIRST = { row: 0, column: 0 };
const ignore = () => undefined;

type Area = { lon: number; lat: number; span_km: number; columns: number; rows: number; projection?: number | null };
type Series = { path: string; dem?: string | null; area: Area; dates: string[]; provider: string };
type Capability = { available: boolean; example: boolean };
type Estimate = { date: string; units: number | null };
type Manifest = {
  composite: string;
  nominal_tile_width_mm: number;
  base_height_mm: number;
  relief_height_mm: number;
  tiles: Record<string, { palette: string[]; prints: Record<string, object> }>;
};
type Job = { status: "running" | "complete" | "failed"; error?: string; manifest?: Manifest };

function call<T>(action: string, body: object) {
  return requestJson<T>(ROOT + "/bridge/" + action, jsonBody(body));
}

function tileNames({ rows, columns }: Area) {
  const format = (number: number) => String(number).padStart(2, "0");
  return Array.from({ length: rows * columns }, (_, index) =>
    "r" + format(Math.floor(index / columns) + 1) + "c" + format(index % columns + 1),
  );
}

export function ClearSkyStudio({ spec, onBack }: { spec: GenerationSpec; onBack: () => void }) {
  const [area, setArea] = useState<Area>({
    lon: spec.center_lon, lat: spec.center_lat, span_km: spec.ground_span_km, columns: 2, rows: 2,
  });
  const [redraw, setRedraw] = useState(0);
  const [capability, setCapability] = useState<Capability | null>(null);
  const [mode, setMode] = useState<"local" | "order">("local");
  const [series, setSeries] = useState<Series | null>(null);
  const [seriesPath, setSeriesPath] = useState("");
  const [localCredit, setLocalCredit] = useState("");
  const [dem, setDem] = useState("");
  const [demAttribution, setDemAttribution] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [order, setOrder] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [orderCredit, setOrderCredit] = useState("");
  const [candidates, setCandidates] = useState<string[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [estimate, setEstimate] = useState<Estimate[] | null>(null);
  const [place, setPlace] = useState("");
  const [places, setPlaces] = useState<PlaceResult[]>([]);
  const [name, setName] = useState(spec.place_name);
  const [print, setPrint] = useState({ widthMm: 90, reliefMm: 18, clearanceMm: 0.30,
    edge: "puzzle", composite: "median", minimumDates: 1, eachDate: false });
  const [paletteMode, setPaletteMode] = useState("auto");
  const [shared, setShared] = useState(COLORS);
  const [tilePalettes, setTilePalettes] = useState<Record<string, string[]>>({});
  const [paletteTile, setPaletteTile] = useState("r01c01");
  const [active, setActive] = useState("");
  const [error, setError] = useState("");
  const [job, setJob] = useState<Job | null>(null);
  const [jobId, setJobId] = useState("");
  const [tile, setTile] = useState("r01c01");
  const [variant, setVariant] = useState("median");
  const [preview, setPreview] = useState<PreviewData | null>(null);
  const [previewError, setPreviewError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    requestJson<Capability>(ROOT + "/status", { signal: controller.signal }).then(setCapability)
      .catch(() => { if (!controller.signal.aborted) setCapability({ available: false, example: false }); });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!jobId || job?.status !== "running") return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const check = async () => {
      try {
        const next = await requestJson<Job>(ROOT + "/jobs/" + jobId, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (next.status === "running") {
          timer = setTimeout(check, 850);
          return;
        }
        if (next.manifest) {
          setTile(Object.keys(next.manifest.tiles)[0]);
          setVariant(next.manifest.composite);
        }
        setJob(next);
      } catch (problem) {
        if (!controller.signal.aborted) setJob({ status: "failed", error: String(problem) });
      }
    };
    void check();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [jobId, job?.status]);

  useEffect(() => {
    if (job?.status !== "complete" || !jobId || !tile || !variant) return;
    const controller = new AbortController();
    requestJson<PreviewData>([ROOT, "jobs", jobId, "preview", tile, variant].join("/"), { signal: controller.signal })
      .then((data) => { if (!controller.signal.aborted) setPreview(data); })
      .catch((problem) => { if (!controller.signal.aborted) setPreviewError(String(problem)); });
    return () => controller.abort();
  }, [job?.status, jobId, tile, variant]);

  const busy = Boolean(active) || job?.status === "running";
  const controlsReady = capability?.available && !busy;
  const tiles = tileNames(area);
  const activePalette = tiles.includes(paletteTile) ? paletteTile : tiles[0];
  const anchor = coordinateAtLocalOffset(area.lat, area.lon,
    -(area.columns - 1) * area.span_km / 2, (area.rows - 1) * area.span_km / 2, 0, area.lat);
  const mapSpec: GenerationSpec = {
    ...initialSpec, solid_model: true, center_lon: anchor.longitude, center_lat: anchor.latitude,
    adjacent_columns: area.columns, adjacent_rows: area.rows, ground_span_km: area.span_km,
    super_tile_anchor: "top_left", adjacent_interlocks: print.edge === "puzzle",
    width_mm: print.widthMm, relief_mm: print.reliefMm, base_mm: 2.8,
    color_output: { ...initialSpec.color_output, enabled: true },
  };
  const previewSpec: GenerationSpec = {
    ...mapSpec, adjacent_columns: 1, adjacent_rows: 1, adjacent_interlocks: false,
    width_mm: job?.manifest?.nominal_tile_width_mm ?? print.widthMm,
    relief_mm: job?.manifest?.relief_height_mm ?? print.reliefMm,
    base_mm: job?.manifest?.base_height_mm ?? 2.8,
  };

  function updateArea(next: Area, recall = false) {
    setArea(next);
    setEstimate(null);
    if (mode === "order" && !series) {
      setCandidates([]);
      setSelected([]);
    }
    if (recall) setRedraw((value) => value + 1);
  }

  function clearDates() {
    setCandidates([]);
    setSelected([]);
    setEstimate(null);
  }

  function moveArea(longitude: number, latitude: number, span_km = area.span_km) {
    if (busy) return;
    const center = coordinateAtLocalOffset(latitude, longitude, (area.columns - 1) * span_km / 2,
      -(area.rows - 1) * span_km / 2, 0, latitude);
    updateArea({ ...area, lon: center.longitude, lat: center.latitude, span_km, projection: null });
  }

  async function run(label: string, operation: () => Promise<void>) {
    setActive(label);
    setError("");
    try { await operation(); }
    catch (problem) { setError(problem instanceof Error ? problem.message : String(problem)); }
    finally { setActive(""); }
  }

  function load(data: Series, example = false) {
    setSeries(data);
    setSeriesPath(data.path);
    setCandidates(data.dates);
    setSelected(data.dates);
    setEstimate(null);
    setArea(data.area);
    setRedraw((value) => value + 1);
    setLocalCredit("");
    if (data.dem) setDem(data.dem);
    if (example) {
      setName("Mount Fuji");
      setMode("local");
      setPrint({ widthMm: 90, reliefMm: 18, clearanceMm: 0.30, edge: "puzzle", composite: "median", minimumDates: 2, eachDate: false });
      setPaletteMode("shared");
      setShared(COLORS);
      setTilePalettes({});
    }
  }

  function inspect(example = false) {
    void run("Reading imagery…", async () => load(
      await call<Series>("inspect", example ? { example } : { path: seriesPath }), example,
    ));
  }

  function browse(kind: "series" | "dem") {
    void run("Waiting for a file selection…", async () => {
      const { path } = await call<{ path: string }>("pick", { kind });
      if (!path) return;
      if (kind === "series") load(await call<Series>("inspect", { path }));
      else setDem(path);
    });
  }

  function changeMode(next: "local" | "order") {
    if (next === mode) return;
    setMode(next);
    setSeries(null);
    clearDates();
  }

  function toggleDate(date: string) {
    setSelected((existing) => existing.includes(date) ? existing.filter((value) => value !== date) : [...existing, date]);
    setEstimate(null);
  }

  const orderRequest = { apiKey, order: order.trim(), area, attribution: orderCredit.trim() || undefined };
  function findDates() {
    setSeries(null);
    clearDates();
    void run("Checking order dates…", async () => {
      const result = await call<{ dates: string[] }>("dates", { ...orderRequest, start, end });
      setCandidates(result.dates);
      if (!result.dates.length) setError("No matching dates for this selection.");
    });
  }

  function generate() {
    if (!series || !dem || busy) return;
    void run("Preparing the print job…", async () => {
      const response = await requestJson<{ id: string }>(ROOT + "/jobs", jsonBody({
        ...print, area, series: series.path, dem, name, demAttribution, dates: selected, baseMm: 2.8,
        imageryAttribution: mode === "local" ? localCredit.trim() : undefined,
        palette: paletteMode === "shared" ? shared.join(",") : undefined,
        tilePalettes: Object.fromEntries(Object.entries(tilePalettes).filter(([key]) => tiles.includes(key))),
      }));
      setPreview(null);
      setPreviewError("");
      setJobId(response.id);
      setJob({ status: "running" });
    });
  }

  function changePreview(nextTile: string, nextVariant: string) {
    if (nextTile === tile && nextVariant === variant) return;
    setPreview(null);
    setPreviewError("");
    setTile(nextTile);
    setVariant(nextVariant);
  }

  function colorFields(colors: string[], setColors: (colors: string[]) => void) {
    return colors.map((color, index) => (
      <input key={index} type="color" aria-label={"Tool " + (index + 1) + " color"} value={color}
        onChange={(event) => setColors(colors.map((value, i) => i === index ? event.target.value : value))} />
    ));
  }

  return (
    <main className="studio clearsky-studio">
      <header className="topbar">
        <div className="brand"><span className="brand-mark" aria-hidden="true" />
          <span>TopoSaic<small>ClearSKY terrain printing</small></span>
        </div>
        <div className="topbar-actions">
          <button type="button" className="setup-menu-button" onClick={onBack} disabled={busy}>Standard studio</button>
          <button type="submit" form="clearsky-controls" className="topbar-generate"
            disabled={!controlsReady || !series || !dem || selected.length < print.minimumDates}>
            {job?.status === "running" ? "Building…" : "Build print files"}
          </button>
        </div>
      </header>
      <div className="clearsky-workspace">
        <div className="clearsky-visuals">
          <TerrainMap spec={mapSpec} markerPlacementMode={null} onPlaceMarker={ignore} onOutlineChange={ignore}
            onCenterChange={moveArea} onSelectionChange={moveArea}
            onGroundSpanChange={(span_km) => { if (!busy) updateArea({ ...area, span_km }); }} recallCount={redraw} />
          <Suspense fallback={<div className="clearsky-preview-note">Loading 3D preview…</div>}>
            {job?.status === "complete" ? (
              <ReliefPreview spec={previewSpec} preview={preview} previewState={previewError ? "error" : preview ? "generated" : "loading"}
                progress={null} previewError={previewError || null} previewTile={FIRST}
                onPreviewTileChange={ignore} markerPlacementMode={null} onPlaceMarker={ignore} />
            ) : <div className="clearsky-preview-note">
              {job?.status === "running" ? "Preparing printable tiles and their 3D previews…" : "Build the selected area to preview the colored tiles."}
            </div>}
          </Suspense>
        </div>
        <form className="controls clearsky-controls" id="clearsky-controls" onSubmit={(event) => { event.preventDefault(); generate(); }}>
          {capability?.available === false && <p role="alert">Open this app with the ClearSKY studio launcher to enable raster processing.</p>}
          <div role="status" aria-live="polite">{active || (job?.status === "running" ? "Generating meshes and checking their edges…" : "")}</div>
          {(error || job?.error) && <p className="clearsky-error" role="alert">{error || job?.error}</p>}
          <fieldset disabled={busy}>
            <legend>Area</legend>
            <div className="clearsky-row">
              <label>Find a place<input value={place} onChange={(event) => setPlace(event.target.value)} /></label>
              <button type="button" disabled={!place.trim()} onClick={() => void run("Searching…", async () => setPlaces(await terrainApi.searchPlaces(place.trim())))}>Search</button>
            </div>
            {places.map((result) => (
              <button key={result.display_name + result.latitude + result.longitude} type="button" onClick={() => {
                updateArea({ ...area, lat: result.latitude, lon: result.longitude, projection: null }, true);
                setName(result.display_name);
                setPlaces([]);
              }}>{result.display_name}</button>
            ))}
            <div className="clearsky-grid">
              <label>Columns<input type="number" min="1" max="8" value={area.columns} onChange={(event) => updateArea({ ...area, columns: Math.max(1, Math.min(8, Math.trunc(event.target.valueAsNumber || 1))) })} /></label>
              <label>Rows<input type="number" min="1" max="8" value={area.rows} onChange={(event) => updateArea({ ...area, rows: Math.max(1, Math.min(8, Math.trunc(event.target.valueAsNumber || 1))) })} /></label>
              <label>Ground width per tile (km)<input type="number" min="0.25" max="250" step="any" value={area.span_km} onChange={(event) => updateArea({ ...area, span_km: Number(event.target.value) || 0.25 })} /></label>
            </div>
            <small>Use Draw area or Move area on the map. The build uses a north-up UTM grid.</small>
          </fieldset>
          <fieldset disabled={!controlsReady}>
            <legend>Imagery</legend>
            <div className="clearsky-row">
              <button type="button" aria-pressed={mode === "local"} onClick={() => changeMode("local")}>Local series</button>
              <button type="button" aria-pressed={mode === "order"} onClick={() => changeMode("order")}>ClearSKY order</button>
              {capability?.example && <button type="button" onClick={() => inspect(true)}>Load Fuji example</button>}
            </div>
            {mode === "local" ? <>
              <div className="clearsky-row">
                <label>Imagery series (.json)<input value={seriesPath} onChange={(event) => { setSeriesPath(event.target.value); setSeries(null); clearDates(); }} /></label>
                <button type="button" onClick={() => browse("series")}>Browse…</button>
                <button type="button" disabled={!seriesPath} onClick={() => inspect()}>Load</button>
                <button type="button" onClick={() => void run("Waiting for GeoTIFFs…", async () => {
                  const data = await call<Series | { path: null }>("import", { area });
                  if (data.path) load(data);
                })}>Import GeoTIFFs…</button>
              </div>
              <label>Imagery source credit<input value={localCredit} onChange={(event) => setLocalCredit(event.target.value)} /></label>
            </> : <>
              <div className="clearsky-grid">
                <label>API key<input type="password" autoComplete="off" value={apiKey} onChange={(event) => { setApiKey(event.target.value); if (!series) clearDates(); }} /></label>
                <label>Order UUID<input value={order} onChange={(event) => { setOrder(event.target.value); setSeries(null); clearDates(); }} /></label>
                <label>From<input type="date" value={start} onChange={(event) => { setStart(event.target.value); if (!series) clearDates(); }} /></label>
                <label>To<input type="date" value={end} onChange={(event) => { setEnd(event.target.value); if (!series) clearDates(); }} /></label>
              </div>
              <label>Source credit<input value={orderCredit} onChange={(event) => { setOrderCredit(event.target.value); setEstimate(null); }} /></label>
              <div className="clearsky-row">
                <button type="button" disabled={!apiKey || !order || !start || !end} onClick={findDates}>Find dates</button>
                <button type="button" disabled={!selected.length || Boolean(series) || !apiKey || !order}
                  onClick={() => void run("Estimating the download…", async () => setEstimate(
                    (await call<{ estimates: Estimate[] }>("estimate", { ...orderRequest, dates: selected })).estimates,
                  ))}>Estimate download</button>
              </div>
            </>}
            {series && <p className="clearsky-series">{series.provider} · {series.dates.length} dates loaded</p>}
            {candidates.length > 0 && <div className="clearsky-dates" aria-label="Available dates">
              {candidates.map((date) => <label key={date}><input type="checkbox" checked={selected.includes(date)} onChange={() => toggleDate(date)} /> {date}</label>)}
            </div>}
            {estimate && <section className="clearsky-estimate" aria-label="Download estimate">
              <ul>{estimate.map(({ date, units }) => <li key={date}>{date} · {units ?? "Unavailable"} processing units</li>)}</ul>
              <button type="button" onClick={() => void run("Downloading ClearSKY imagery…", async () =>
                load(await call<Series>("fetch", { ...orderRequest, dates: selected })))}>Confirm and download</button>
            </section>}
          </fieldset>
          <fieldset disabled={busy}>
            <legend>Print setup</legend>
            <div className="clearsky-row"><label>Elevation GeoTIFF<input value={dem} onChange={(event) => setDem(event.target.value)} /></label>
              <button type="button" disabled={!controlsReady} onClick={() => browse("dem")}>Browse…</button></div>
            <label>DEM source credit<input value={demAttribution} onChange={(event) => setDemAttribution(event.target.value)} /></label>
            <label>Print name<input value={name} onChange={(event) => setName(event.target.value)} /></label>
            <div className="clearsky-grid">
              <label>Tile width (mm)<input type="number" min="60" max="215" value={print.widthMm} onChange={(event) => setPrint({ ...print, widthMm: Number(event.target.value) })} /></label>
              <label>Relief (mm)<input type="number" min="1" max="80" value={print.reliefMm} onChange={(event) => setPrint({ ...print, reliefMm: Number(event.target.value) })} /></label>
              <label>Tile edges<select value={print.edge} onChange={(event) => setPrint({ ...print, edge: event.target.value })}><option value="puzzle">Puzzle</option><option value="straight">Straight</option></select></label>
              {print.edge === "puzzle" && <label>Joint clearance (mm)<input type="number" min="0" max="1" step="0.01" value={print.clearanceMm} onChange={(event) => setPrint({ ...print, clearanceMm: Number(event.target.value) })} /></label>}
              <label>Composite<select value={print.composite} onChange={(event) => setPrint({ ...print, composite: event.target.value })}><option value="median">Median</option><option value="mean">Mean</option></select></label>
              <label>Minimum dates<input type="number" min="1" max="32" value={print.minimumDates} onChange={(event) => setPrint({ ...print, minimumDates: Number(event.target.value) })} /></label>
            </div>
            <label className="clearsky-checkbox"><input type="checkbox" checked={print.eachDate} onChange={(event) => setPrint({ ...print, eachDate: event.target.checked })} /> Also export each date</label>
          </fieldset>
          <fieldset disabled={busy}>
            <legend>Filament colors</legend>
            <div className="clearsky-grid">
              <label>Default palette<select value={paletteMode} onChange={(event) => setPaletteMode(event.target.value)}>
                <option value="auto">Suggest per tile</option><option value="shared">Use one set of spools</option></select></label>
              <label>Customize a tile<select value={activePalette} onChange={(event) => setPaletteTile(event.target.value)}>
                {tiles.map((key) => <option key={key}>{key}</option>)}</select></label>
            </div>
            {paletteMode === "shared" && <div className="clearsky-swatches">{colorFields(shared, setShared)}</div>}
            <div className="clearsky-row">
              {tilePalettes[activePalette] ? <>
                <div className="clearsky-swatches">{colorFields(tilePalettes[activePalette], (colors) => setTilePalettes({ ...tilePalettes, [activePalette]: colors }))}</div>
                <button type="button" onClick={() => setTilePalettes(Object.fromEntries(Object.entries(tilePalettes).filter(([key]) => key !== activePalette)))}>Use default</button>
              </> : <button type="button" onClick={() => setTilePalettes({ ...tilePalettes, [activePalette]: shared })}>Set colors for {activePalette}</button>}
            </div>
          </fieldset>
          {job?.manifest && <fieldset>
            <legend>Output</legend>
            <div className="clearsky-grid">
              <label>Preview tile<select value={tile} onChange={(event) => changePreview(event.target.value, job.manifest!.composite)}>
                {Object.keys(job.manifest.tiles).map((key) => <option key={key}>{key}</option>)}</select></label>
              <label>Preview imagery<select value={variant} onChange={(event) => changePreview(tile, event.target.value)}>
                {Object.keys(job.manifest.tiles[tile]?.prints ?? {}).map((key) => <option key={key}>{key}</option>)}</select></label>
            </div>
            <a className="clearsky-download" href={[API_URL + ROOT, "jobs", jobId, "download"].join("/")}>Download print kit</a>
            <small>The ZIP includes the 3MFs, STL files, layout and filament guide.</small>
          </fieldset>}
        </form>
      </div>
    </main>
  );
}
