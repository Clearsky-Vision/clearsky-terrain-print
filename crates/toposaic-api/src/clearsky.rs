//! Local bridge for ClearSKY orders and the raster-to-print workflow.

use std::{
    env,
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};

use anyhow::{Context, Result, bail};
use axum::{
    Json,
    body::Body,
    extract::{Path as AxumPath, State},
    http::{HeaderMap, HeaderValue, StatusCode, Uri, header},
    response::{IntoResponse, Response},
};
use serde_json::{Value, json};
use tokio::{fs, io::AsyncWriteExt, process::Command, time::timeout};
use tokio_util::io::ReaderStream;
use uuid::Uuid;

use crate::{ApiError, AppState, api_error, canonical_uuid, internal_error};

type Failure = (StatusCode, Json<ApiError>);

#[derive(Clone)]
pub(crate) struct Bridge {
    python: PathBuf,
    script: PathBuf,
    engine: PathBuf,
    example: Option<PathBuf>,
    root: PathBuf,
}

impl Bridge {
    pub(crate) fn from_env(data: &Path) -> Option<Self> {
        Some(Self {
            python: env::var_os("TOPOSAIC_CLEARSKY_PYTHON")?.into(),
            script: env::var_os("TOPOSAIC_CLEARSKY_BRIDGE")?.into(),
            engine: env::var_os("TOPOSAIC_CLEARSKY_ENGINE")?.into(),
            example: env::var_os("TOPOSAIC_CLEARSKY_EXAMPLE").map(PathBuf::from),
            root: data.join("clearsky"),
        })
    }

    async fn call(&self, action: &str, mut request: Value, output: Option<&Path>) -> Result<Value> {
        // The key travels only in the child environment, never in an argument,
        // saved request, artefact, or log field.
        let key = request
            .as_object_mut()
            .context("expected a JSON request object")?
            .remove("apiKey")
            .and_then(|value| value.as_str().map(str::to_owned));
        let mut command = Command::new(&self.python);
        command
            .arg(&self.script)
            .arg(action)
            .env("PYTHONIOENCODING", "utf-8")
            .env("TOPOSAIC_CLEARSKY_ENGINE", &self.engine)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        if let Some(key) = key.filter(|value| !value.trim().is_empty()) {
            command.env("CLEARSKY_API_KEY", key);
        }
        if let Some(example) = &self.example {
            command.env("TOPOSAIC_CLEARSKY_EXAMPLE", example);
        }
        if let Some(output) = output {
            command.arg(output);
        }
        #[cfg(windows)]
        command.creation_flags(0x08000000);

        let result = timeout(Duration::from_secs(900), async {
            let mut child = command.spawn().context("start the ClearSKY helper")?;
            let mut stdin = child.stdin.take().context("open the ClearSKY input")?;
            stdin.write_all(&serde_json::to_vec(&request)?).await?;
            drop(stdin);
            child
                .wait_with_output()
                .await
                .context("wait for the ClearSKY helper")
        })
        .await
        .context("The ClearSKY operation timed out.")??;
        let response: Value = serde_json::from_slice(&result.stdout).context(
            "The ClearSKY helper returned an unreadable response. Check the Python environment.",
        )?;
        if let Some(message) = response.get("error").and_then(Value::as_str) {
            bail!("{message}");
        }
        if !result.status.success() {
            bail!("The ClearSKY helper exited before completing.");
        }
        Ok(response)
    }
}

fn local_origin(headers: &HeaderMap) -> bool {
    let Some(origin) = headers
        .get(header::ORIGIN)
        .and_then(|origin| origin.to_str().ok())
    else {
        return false;
    };
    if matches!(
        origin,
        "tauri://localhost" | "http://tauri.localhost" | "https://tauri.localhost"
    ) {
        return true;
    }
    origin.parse::<Uri>().ok().is_some_and(|uri| {
        uri.scheme_str() == Some("http")
            && matches!(uri.host(), Some("localhost" | "127.0.0.1" | "[::1]"))
    })
}

fn bridge(state: &AppState) -> Result<&Bridge, Failure> {
    state.clearsky.as_ref().ok_or_else(|| {
        api_error(
            StatusCode::SERVICE_UNAVAILABLE,
            "Open the studio with the ClearSKY launcher to enable this workflow.",
        )
    })
}

fn require_local(headers: &HeaderMap) -> Result<(), Failure> {
    if !local_origin(headers) {
        return Err(api_error(
            StatusCode::FORBIDDEN,
            "ClearSKY inputs are only accessible from the local studio.",
        ));
    }
    Ok(())
}

fn require_local_reader(headers: &HeaderMap) -> Result<(), Failure> {
    if headers.contains_key(header::ORIGIN) {
        require_local(headers)?;
    }
    Ok(())
}

pub(crate) async fn status(State(state): State<AppState>) -> Json<Value> {
    Json(json!({
        "available": state.clearsky.is_some(),
        "example": state.clearsky.as_ref().and_then(|bridge| bridge.example.as_ref()).is_some(),
    }))
}

pub(crate) async fn run(
    State(state): State<AppState>,
    AxumPath(action): AxumPath<String>,
    headers: HeaderMap,
    Json(request): Json<Value>,
) -> Result<Json<Value>, Failure> {
    require_local(&headers)?;
    let bridge = bridge(&state)?;
    let output = match action.as_str() {
        "pick" | "inspect" | "dates" | "estimate" => None,
        "fetch" | "import" => Some(bridge.root.join("imagery").join(Uuid::new_v4().to_string())),
        _ => {
            return Err(api_error(
                StatusCode::NOT_FOUND,
                "Unknown ClearSKY operation.",
            ));
        }
    };
    bridge
        .call(&action, request, output.as_deref())
        .await
        .map(Json)
        .map_err(|error| api_error(StatusCode::UNPROCESSABLE_ENTITY, error))
}

pub(crate) async fn new_job(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<Value>,
) -> Result<(StatusCode, Json<Value>), Failure> {
    require_local(&headers)?;
    let bridge = bridge(&state)?.clone();
    let id = Uuid::new_v4().to_string();
    let folder = bridge.root.join("jobs").join(&id);
    fs::create_dir_all(&folder).await.map_err(internal_error)?;
    tokio::spawn(async move {
        let result = match bridge
            .call("build", request, Some(&folder.join("prints")))
            .await
        {
            Ok(manifest) => json!({"status": "complete", "manifest": manifest}),
            Err(error) => json!({"status": "failed", "error": error.to_string()}),
        };
        let temporary = folder.join("result.tmp");
        if let Err(error) = async {
            fs::write(&temporary, result.to_string()).await?;
            fs::rename(temporary, folder.join("result.json")).await
        }
        .await
        {
            tracing::error!(%error, "could not save the ClearSKY job result");
        }
    });
    Ok((
        StatusCode::ACCEPTED,
        Json(json!({"id": id, "status": "running"})),
    ))
}

fn job_folder(state: &AppState, id: &str) -> Result<PathBuf, Failure> {
    let id = canonical_uuid(id)
        .ok_or_else(|| api_error(StatusCode::NOT_FOUND, "ClearSKY job not found."))?;
    Ok(bridge(state)?.root.join("jobs").join(id))
}

pub(crate) async fn job(
    State(state): State<AppState>,
    AxumPath(id): AxumPath<String>,
    headers: HeaderMap,
) -> Result<Json<Value>, Failure> {
    require_local_reader(&headers)?;
    let folder = job_folder(&state, &id)?;
    match fs::read(folder.join("result.json")).await {
        Ok(bytes) => serde_json::from_slice(&bytes)
            .map(Json)
            .map_err(internal_error),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound && folder.is_dir() => {
            Ok(Json(json!({"status": "running"})))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            Err(api_error(StatusCode::NOT_FOUND, "ClearSKY job not found."))
        }
        Err(error) => Err(internal_error(error)),
    }
}

pub(crate) async fn preview(
    State(state): State<AppState>,
    AxumPath((id, tile, date)): AxumPath<(String, String, String)>,
    headers: HeaderMap,
) -> Result<Response, Failure> {
    require_local_reader(&headers)?;
    let valid_tile = tile.len() == 6
        && tile.as_bytes()[0] == b'r'
        && tile.as_bytes()[3] == b'c'
        && tile
            .bytes()
            .enumerate()
            .all(|(i, byte)| i == 0 || i == 3 || byte.is_ascii_digit());
    let valid_date = matches!(date.as_str(), "median" | "mean")
        || chrono::NaiveDate::parse_from_str(&date, "%Y-%m-%d")
            .ok()
            .is_some_and(|value| value.to_string() == date);
    if !valid_tile || !valid_date {
        return Err(api_error(
            StatusCode::NOT_FOUND,
            "ClearSKY preview not found.",
        ));
    }
    let file = job_folder(&state, &id)?
        .join("prints")
        .join(tile)
        .join(date)
        .join("preview.json");
    send_file(file, "application/json", None).await
}

pub(crate) async fn download(
    State(state): State<AppState>,
    AxumPath(id): AxumPath<String>,
    headers: HeaderMap,
) -> Result<Response, Failure> {
    require_local_reader(&headers)?;
    let file = job_folder(&state, &id)?
        .join("prints")
        .join("print-kit.zip");
    send_file(
        file,
        "application/zip",
        Some("attachment; filename=\"clearsky-terrain-print.zip\""),
    )
    .await
}

pub(crate) async fn send_file(
    path: PathBuf,
    media: &'static str,
    disposition: Option<&'static str>,
) -> Result<Response, Failure> {
    let file = fs::File::open(path)
        .await
        .map_err(|_| api_error(StatusCode::NOT_FOUND, "File not found."))?;
    let length = file.metadata().await.map_err(internal_error)?.len();
    let mut response = Body::from_stream(ReaderStream::new(file)).into_response();
    let headers = response.headers_mut();
    headers.insert(header::CONTENT_TYPE, HeaderValue::from_static(media));
    headers.insert(header::CONTENT_LENGTH, HeaderValue::from(length));
    headers.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    if let Some(disposition) = disposition {
        headers.insert(
            header::CONTENT_DISPOSITION,
            HeaderValue::from_static(disposition),
        );
    }
    Ok(response)
}
