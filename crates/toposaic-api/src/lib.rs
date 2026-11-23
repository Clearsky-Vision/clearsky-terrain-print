use std::{
    collections::HashMap,
    path::PathBuf,
    sync::{Arc, Mutex as StdMutex, atomic::AtomicBool},
    time::{Duration, Instant},
};

use anyhow::{Context, Result};
use axum::{Json, Router, http::StatusCode, routing::get};
use reqwest::Client;
use rusqlite::Connection;
use serde::Serialize;
use tokio::{net::TcpListener, sync::Mutex as AsyncMutex};
use tower_http::trace::TraceLayer;
use tracing::info;

mod cache;
mod clearsky;
mod database;
mod datum;
mod elevation;
mod geo;
mod geocoding;
mod globe;
mod grid;
mod http;
mod imagery;
mod jobs;
mod osm;
mod settings;
mod setups;
mod sources;
mod surface;

/// Internal hooks for the `manifold_report_real` diagnostics example only;
/// not a stable API.
#[doc(hidden)]
pub mod diagnostics {
    pub use crate::cache::root as map_cache_root;
    pub use crate::elevation::fetch_height_field_with_progress;
    pub use crate::surface::{apply_marine_water, fetch_surface_field};
}

use database::migrate;
use geocoding::search_places;
pub(crate) use jobs::Job;
pub(crate) use setups::{SavedSetup, SetupVersion};

#[derive(Clone)]
struct AppState {
    db: Arc<StdMutex<Connection>>,
    jobs_dir: Arc<PathBuf>,
    map_cache_dir: Arc<PathBuf>,
    geocoder: Client,
    geocoder_base_url: Arc<String>,
    last_geocode_request: Arc<AsyncMutex<Instant>>,
    active_jobs: Arc<StdMutex<HashMap<String, Arc<AtomicBool>>>>,
    /// The live preview is replaceable work: a newer request cancels the
    /// older one instead of leaving a queue of OSM fetches behind it.
    active_preview: Arc<StdMutex<Option<ActivePreview>>>,
    clearsky: Option<clearsky::Bridge>,
    studio_dir: Option<PathBuf>,
}

#[derive(Clone)]
struct ActivePreview {
    id: String,
    cancellation: Arc<AtomicBool>,
}

#[derive(Debug, Serialize)]
struct Health {
    status: &'static str,
    storage: &'static str,
}

#[derive(Debug, Serialize)]
struct ApiError {
    error: String,
}

pub async fn run() -> Result<()> {
    run_with(settings::data_dir(), settings::bind_address()).await
}

pub async fn run_with(data_dir: PathBuf, address: String) -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| {
                "toposaic_api=info,toposaic_core::geometry=info,tower_http=info".into()
            }),
        )
        .try_init()
        .ok();

    let jobs_dir = data_dir.join("jobs");
    let map_cache_dir = cache::root()?;
    std::fs::create_dir_all(&jobs_dir)
        .with_context(|| format!("create jobs directory {}", jobs_dir.display()))?;
    std::fs::create_dir_all(&map_cache_dir)
        .with_context(|| format!("create map cache directory {}", map_cache_dir.display()))?;
    let connection = Connection::open(data_dir.join("toposaic.sqlite3"))?;
    migrate(&connection)?;
    let geocoder = http::async_client(Duration::from_secs(12))?;

    let state = AppState {
        db: Arc::new(StdMutex::new(connection)),
        jobs_dir: Arc::new(jobs_dir),
        map_cache_dir: Arc::new(map_cache_dir.clone()),
        geocoder,
        geocoder_base_url: Arc::new(settings::geocoder_base_url()),
        last_geocode_request: Arc::new(AsyncMutex::new(Instant::now() - Duration::from_secs(1))),
        active_jobs: Arc::new(StdMutex::new(HashMap::new())),
        active_preview: Arc::new(StdMutex::new(None)),
        clearsky: clearsky::Bridge::from_env(&data_dir),
        studio_dir: std::env::var_os("TOPOSAIC_STUDIO_DIR").map(PathBuf::from),
    };

    let app = Router::new()
        .route("/", get(studio_page))
        .route("/assets/{file}", get(studio_asset))
        .route("/api/health", get(health))
        .route("/api/clearsky/status", get(clearsky::status))
        .route(
            "/api/clearsky/bridge/{action}",
            axum::routing::post(clearsky::run),
        )
        .route("/api/clearsky/jobs", axum::routing::post(clearsky::new_job))
        .route("/api/clearsky/jobs/{id}", get(clearsky::job))
        .route(
            "/api/clearsky/jobs/{id}/preview/{tile}/{date}",
            get(clearsky::preview),
        )
        .route("/api/clearsky/jobs/{id}/download", get(clearsky::download))
        .route("/api/cache", get(cache::cache_summary))
        .route("/api/cache/clear", axum::routing::post(cache::clear_cache))
        .route("/api/places", get(search_places))
        .route("/api/preview", axum::routing::post(jobs::create_preview))
        .route(
            "/api/preview/{id}",
            axum::routing::delete(jobs::cancel_preview),
        )
        .route("/api/jobs", get(jobs::list_jobs).post(jobs::create_job))
        .route(
            "/api/jobs/{id}",
            get(jobs::get_job).delete(jobs::cancel_job),
        )
        .route("/api/jobs/{id}/downloads/{name}", get(jobs::download))
        .route("/api/jobs/{id}/sources", get(jobs::source_summary))
        .route(
            "/api/jobs/{id}/sources/build",
            axum::routing::post(jobs::build_sources),
        )
        .route(
            "/api/sources/import",
            axum::routing::post(jobs::import_sources)
                // The handler streams the upload to a temporary file and
                // enforces its own cap; axum's 2 MB default would refuse
                // every real bundle before the handler ever saw it.
                .layer(axum::extract::DefaultBodyLimit::disable()),
        )
        .route(
            "/api/setups",
            get(setups::list_setups).post(setups::save_setup),
        )
        .route(
            "/api/setups/{id}",
            axum::routing::delete(setups::delete_setup).patch(setups::rename_setup),
        )
        .route("/api/setups/{id}/versions", get(setups::list_versions))
        .route(
            "/api/setups/{id}/versions/{version_id}/restore",
            axum::routing::post(setups::restore_version),
        )
        .layer(http::cors_layer(settings::allowed_origins()))
        .layer(TraceLayer::new_for_http())
        .with_state(state);

    let listener = TcpListener::bind(&address).await?;
    info!(
        %address,
        data_dir = %data_dir.display(),
        map_cache_dir = %map_cache_dir.display(),
        "terrain api ready"
    );
    axum::serve(listener, app).await?;
    Ok(())
}

async fn studio_page(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> Result<axum::response::Response, (StatusCode, Json<ApiError>)> {
    let directory = state
        .studio_dir
        .ok_or_else(|| api_error(StatusCode::NOT_FOUND, "Studio not bundled."))?;
    clearsky::send_file(
        directory.join("index.html"),
        "text/html; charset=utf-8",
        None,
    )
    .await
}

async fn studio_asset(
    axum::extract::State(state): axum::extract::State<AppState>,
    axum::extract::Path(file): axum::extract::Path<String>,
) -> Result<axum::response::Response, (StatusCode, Json<ApiError>)> {
    if !file
        .bytes()
        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
    {
        return Err(api_error(StatusCode::NOT_FOUND, "Asset not found."));
    }
    let directory = state
        .studio_dir
        .ok_or_else(|| api_error(StatusCode::NOT_FOUND, "Studio not bundled."))?;
    let media_type = match file.rsplit_once('.').map(|(_, extension)| extension) {
        Some("js") => "text/javascript; charset=utf-8",
        Some("css") => "text/css; charset=utf-8",
        Some("png") => "image/png",
        Some("svg") => "image/svg+xml",
        Some("woff2") => "font/woff2",
        _ => return Err(api_error(StatusCode::NOT_FOUND, "Asset not found.")),
    };
    clearsky::send_file(directory.join("assets").join(file), media_type, None).await
}

async fn health() -> Json<Health> {
    Json(Health {
        status: "ok",
        storage: "sqlite",
    })
}

#[cfg(test)]
pub(crate) fn test_state() -> AppState {
    let connection = Connection::open_in_memory().unwrap();
    migrate(&connection).unwrap();
    let data_dir = std::env::temp_dir().join(format!("toposaic-api-test-{}", std::process::id()));
    AppState {
        db: Arc::new(StdMutex::new(connection)),
        jobs_dir: Arc::new(data_dir.join("jobs")),
        map_cache_dir: Arc::new(data_dir.join("cache")),
        geocoder: Client::new(),
        geocoder_base_url: Arc::new("https://example.invalid".into()),
        last_geocode_request: Arc::new(AsyncMutex::new(Instant::now())),
        active_jobs: Arc::new(StdMutex::new(HashMap::new())),
        active_preview: Arc::new(StdMutex::new(None)),
        clearsky: None,
        studio_dir: None,
    }
}

fn api_error(status: StatusCode, message: impl ToString) -> (StatusCode, Json<ApiError>) {
    (
        status,
        Json(ApiError {
            error: message.to_string(),
        }),
    )
}

fn internal_error(error: impl std::fmt::Display) -> (StatusCode, Json<ApiError>) {
    api_error(StatusCode::INTERNAL_SERVER_ERROR, error.to_string())
}

/// Parses a client-supplied id into its canonical hyphenated UUID form.
/// Jobs and setups both key their rows (and, for jobs, their artifact
/// directories) by this form, so path escapes like `../data` parse to
/// `None` and map to a 404.
pub(crate) fn canonical_uuid(id: &str) -> Option<String> {
    uuid::Uuid::parse_str(id)
        .ok()
        .map(|value| value.hyphenated().to_string())
}
