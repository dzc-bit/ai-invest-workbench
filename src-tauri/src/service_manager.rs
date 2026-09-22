use serde::Deserialize;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
#[cfg(windows)]
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};
use std::{env, fs};

use sha2::{Digest, Sha256};
use tauri::{AppHandle, Manager};

use crate::python_runtime::{backend_dir, project_root, python_command};

#[derive(Clone, serde::Serialize)]
pub struct DataServiceStatus {
    pub running: bool,
    pub port: u16,
    pub base_url: String,
    pub cache_dir: String,
    pub message: String,
}

struct ManagedService {
    child: Child,
    port: u16,
    cache_dir: String,
    executable_path: Option<PathBuf>,
    executable_sha256: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ServiceHealthPayload {
    cache_path: String,
    port: Option<u16>,
    process_id: Option<u32>,
    executable_path: Option<String>,
    executable_sha256: Option<String>,
}

#[derive(Clone, Debug, Deserialize, serde::Serialize)]
struct ServiceLockPayload {
    port: u16,
    cache_dir: String,
    process_id: Option<u32>,
    executable_path: Option<String>,
    executable_sha256: Option<String>,
}

#[derive(Default)]
pub struct DataServiceManager {
    service: Option<ManagedService>,
}

pub fn build_service_args(port: u16, cache_dir: &str) -> Vec<String> {
    vec![
        "-m".to_string(),
        "astock_backtester.service".to_string(),
        "--host".to_string(),
        "127.0.0.1".to_string(),
        "--port".to_string(),
        port.to_string(),
        "--cache-dir".to_string(),
        cache_dir.to_string(),
    ]
}

#[cfg(test)]
pub fn health_request(port: u16) -> String {
    format!("GET /ping HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n")
}

pub fn service_health_request(port: u16) -> String {
    format!("GET /identity HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n")
}

fn canonical_or_original(path: &str) -> PathBuf {
    fs::canonicalize(path).unwrap_or_else(|_| PathBuf::from(path))
}

fn paths_match(left: &str, right: &str) -> bool {
    let left = canonical_or_original(left);
    let right = canonical_or_original(right);
    left.to_string_lossy().eq_ignore_ascii_case(&right.to_string_lossy())
}

fn cached_service_matches(existing_cache_dir: &str, requested_cache_dir: &str) -> bool {
    paths_match(existing_cache_dir, requested_cache_dir)
}

fn http_json_body(raw: &str) -> Option<&str> {
    raw.split("\r\n\r\n").nth(1)
}

fn file_sha256(path: &Path) -> Result<String, String> {
    let mut file = fs::File::open(path).map_err(|err| format!("failed to open executable for hashing: {err}"))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 1024 * 64];
    loop {
        let bytes = file
            .read(&mut buffer)
            .map_err(|err| format!("failed to read executable for hashing: {err}"))?;
        if bytes == 0 {
            break;
        }
        hasher.update(&buffer[..bytes]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn validate_service_health(
    raw: &str,
    port: u16,
    cache_dir: &str,
    process_id: Option<u32>,
    expected_executable_path: Option<&Path>,
    expected_executable_sha256: Option<&str>,
) -> Result<(), String> {
    if !raw.contains("200 OK") {
        return Err("localhost data service health check did not return 200 OK".to_string());
    }
    let body = http_json_body(raw).ok_or_else(|| "localhost data service health response was empty".to_string())?;
    let health: ServiceHealthPayload =
        serde_json::from_str(body).map_err(|err| format!("localhost data service health json was invalid: {err}"))?;
    if health.port != Some(port) {
        return Err(format!(
            "localhost data service health port mismatch: expected {port}, got {:?}",
            health.port
        ));
    }
    if !paths_match(&health.cache_path, cache_dir) {
        return Err(format!(
            "localhost data service cache mismatch: expected {}, got {}",
            cache_dir, health.cache_path
        ));
    }
    let executable_identity_checked =
        expected_executable_path.is_some() && expected_executable_sha256.is_some();
    if let Some(expected) = process_id.filter(|_| !executable_identity_checked) {
        let actual = health
            .process_id
            .ok_or_else(|| "localhost data service health did not include process_id".to_string())?;
        if actual != expected {
            return Err(format!("localhost data service pid mismatch: expected {expected}, got {actual}"));
        }
    }
    if let Some(expected_path) = expected_executable_path {
        let actual_path = health
            .executable_path
            .as_deref()
            .ok_or_else(|| "localhost data service health did not include executable_path".to_string())?;
        if !paths_match(actual_path, &expected_path.to_string_lossy()) {
            return Err(format!(
                "localhost data service executable mismatch: expected {}, got {}",
                expected_path.display(),
                actual_path
            ));
        }
    }
    if let Some(expected_sha256) = expected_executable_sha256 {
        let actual_sha256 = health
            .executable_sha256
            .as_deref()
            .ok_or_else(|| "localhost data service health did not include executable_sha256".to_string())?;
        if !actual_sha256.eq_ignore_ascii_case(expected_sha256) {
            return Err(format!(
                "localhost data service executable hash mismatch: expected {}, got {}",
                expected_sha256, actual_sha256
            ));
        }
    }
    Ok(())
}

fn service_lock_path(cache_dir: &str) -> PathBuf {
    Path::new(cache_dir).join("astock-data-service.lock.json")
}

fn try_create_service_lock(path: &Path, payload: &ServiceLockPayload) -> Result<bool, String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|err| format!("failed to create service lock dir: {err}"))?;
    }
    let encoded = serde_json::to_vec_pretty(payload).map_err(|err| format!("failed to encode service lock: {err}"))?;
    match fs::OpenOptions::new().write(true).create_new(true).open(path) {
        Ok(mut file) => {
            file.write_all(&encoded)
                .map_err(|err| format!("failed to write service lock: {err}"))?;
            Ok(true)
        }
        Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => Ok(false),
        Err(err) => Err(format!("failed to create service lock: {err}")),
    }
}

fn write_service_lock(path: &Path, payload: &ServiceLockPayload) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|err| format!("failed to create service lock dir: {err}"))?;
    }
    let encoded = serde_json::to_vec_pretty(payload).map_err(|err| format!("failed to encode service lock: {err}"))?;
    fs::write(path, encoded).map_err(|err| format!("failed to write service lock: {err}"))
}

fn read_service_lock(path: &Path) -> Result<ServiceLockPayload, String> {
    let raw = fs::read_to_string(path).map_err(|err| format!("failed to read service lock: {err}"))?;
    serde_json::from_str(&raw).map_err(|err| format!("service lock json was invalid: {err}"))
}

fn remove_service_lock(path: &Path) {
    let _ = fs::remove_file(path);
}

fn require_recreated_service_lock(created: bool) -> Result<(), String> {
    if created {
        return Ok(());
    }
    Err("another localhost data service is starting for this cache; retry shortly".to_string())
}

fn recreate_service_lock_after_timeout(path: &Path, payload: &ServiceLockPayload) -> Result<(), String> {
    remove_service_lock(path);
    require_recreated_service_lock(try_create_service_lock(path, payload)?)
}

fn locked_service_expected_identity<'a>(
    lock: &'a ServiceLockPayload,
    expected_executable_path: Option<&'a Path>,
    expected_executable_sha256: Option<&'a str>,
) -> (Option<&'a Path>, Option<&'a str>) {
    let executable_path = expected_executable_path.or_else(|| lock.executable_path.as_deref().map(Path::new));
    let executable_sha256 = expected_executable_sha256.or(lock.executable_sha256.as_deref());
    (executable_path, executable_sha256)
}

fn locked_service_matches_current_expectation(
    cache_dir: &str,
    lock: &ServiceLockPayload,
    expected_executable_path: Option<&Path>,
    expected_executable_sha256: Option<&str>,
) -> bool {
    let (executable_path, executable_sha256) =
        locked_service_expected_identity(lock, expected_executable_path, expected_executable_sha256);
    is_verified_healthy(lock.port, cache_dir, lock.process_id, executable_path, executable_sha256)
}

fn wait_for_locked_service(
    cache_dir: &str,
    lock_path: &Path,
    deadline: Duration,
    expected_executable_path: Option<&Path>,
    expected_executable_sha256: Option<&str>,
) -> Option<ServiceLockPayload> {
    let expires_at = Instant::now() + deadline;
    while Instant::now() < expires_at {
        if let Ok(lock) = read_service_lock(lock_path) {
            if locked_service_matches_current_expectation(
                cache_dir,
                &lock,
                expected_executable_path,
                expected_executable_sha256,
            ) {
                return Some(lock);
            }
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    None
}

fn dedupe_paths(candidates: Vec<PathBuf>) -> Vec<PathBuf> {
    let mut deduped: Vec<PathBuf> = Vec::new();
    for candidate in candidates {
        let normalized = candidate.to_string_lossy().to_lowercase();
        if deduped
            .iter()
            .any(|existing| existing.to_string_lossy().to_lowercase() == normalized)
        {
            continue;
        }
        deduped.push(candidate);
    }
    deduped
}

fn workspace_cache_candidates_from_root(root: &Path, cache_dir: &str) -> Vec<PathBuf> {
    dedupe_paths(vec![
        root.join("运行产物").join("本地数据仓"),
        root.join(cache_dir),
    ])
}

fn runtime_workspace_root_from(start: &Path) -> Option<PathBuf> {
    for candidate in start.ancestors() {
        if candidate.file_name().and_then(|name| name.to_str()) == Some("运行产物") {
            if let Some(root) = candidate.parent() {
                return Some(root.to_path_buf());
            }
        }
    }
    None
}

fn runtime_data_candidates_from(start: &Path, cache_dir: &str) -> Vec<PathBuf> {
    runtime_workspace_root_from(start)
        .map(|root| workspace_cache_candidates_from_root(&root, cache_dir))
        .unwrap_or_default()
}

fn has_market_data(cache_dir: &Path) -> bool {
    let warehouse_root = cache_dir.join("warehouse").join("daily_bars");
    if let Ok(entries) = fs::read_dir(&warehouse_root) {
        for entry in entries.flatten() {
            let parquet = entry.path().join("daily_bars.parquet");
            if parquet.exists() && parquet.metadata().map(|meta| meta.len() > 0).unwrap_or(false) {
                return true;
            }
        }
    }

    let legacy_parquet = cache_dir.join("parquet").join("daily_bars.parquet");
    legacy_parquet.exists() && legacy_parquet.metadata().map(|meta| meta.len() > 0).unwrap_or(false)
}

fn choose_populated_cache_dir(preferred: &Path, candidates: &[PathBuf]) -> PathBuf {
    if has_market_data(preferred) {
        return preferred.to_path_buf();
    }

    for candidate in candidates {
        if candidate != preferred && has_market_data(candidate) {
            return candidate.clone();
        }
    }

    preferred.to_path_buf()
}

fn release_cache_candidates(cache_dir: &str) -> Vec<PathBuf> {
    let mut candidates: Vec<PathBuf> = Vec::new();

    if let Ok(root) = project_root() {
        candidates.extend(workspace_cache_candidates_from_root(&root, cache_dir));
    }

    if let Ok(current_dir) = env::current_dir() {
        candidates.extend(runtime_data_candidates_from(&current_dir, cache_dir));
        candidates.extend(workspace_cache_candidates_from_root(&current_dir, cache_dir));
    }

    if let Ok(exe_path) = env::current_exe() {
        if let Some(exe_dir) = exe_path.parent() {
            candidates.extend(runtime_data_candidates_from(exe_dir, cache_dir));
        }
    }

    dedupe_paths(candidates)
}

fn resolve_cache_dir(cache_dir: &str) -> Result<String, String> {
    let path = Path::new(cache_dir);
    if path.is_absolute() || cfg!(debug_assertions) {
        return Ok(cache_dir.to_string());
    }
    let candidates = release_cache_candidates(cache_dir);
    let preferred = candidates
        .first()
        .cloned()
        .ok_or_else(|| {
            format!(
                "workspace data dir was not found; expected 运行产物\\本地数据仓 or {cache_dir} under the project root (set ASTOCK_BACKTESTER_PROJECT_ROOT to override)"
            )
        })?;
    let selected = choose_populated_cache_dir(&preferred, &candidates);
    Ok(selected.to_string_lossy().to_string())
}

fn choose_port() -> Result<u16, String> {
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|err| format!("bind port failed: {err}"))?;
    let port = listener
        .local_addr()
        .map_err(|err| format!("read port failed: {err}"))?
        .port();
    drop(listener);
    Ok(port)
}

fn service_startup_timeout() -> Duration {
    Duration::from_secs(30)
}

fn service_probe_timeout() -> Duration {
    Duration::from_secs(2)
}

fn wait_for_verified_health_with_timeout(
    port: u16,
    cache_dir: &str,
    process_id: Option<u32>,
    expected_executable_path: Option<&Path>,
    expected_executable_sha256: Option<&str>,
    timeout: Duration,
) -> Result<(), String> {
    let deadline = Instant::now() + timeout;
    let mut last_error = format!("localhost data service did not become healthy on port {port}");
    while Instant::now() < deadline {
        if let Ok(mut stream) = TcpStream::connect(("127.0.0.1", port)) {
            let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
            let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
            let _ = stream.write_all(service_health_request(port).as_bytes());
            let mut raw = String::new();
            let _ = stream.read_to_string(&mut raw);
            match validate_service_health(
                &raw,
                port,
                cache_dir,
                process_id,
                expected_executable_path,
                expected_executable_sha256,
            ) {
                Ok(()) => return Ok(()),
                Err(err) => last_error = err,
            }
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    Err(last_error)
}

fn wait_for_verified_health(
    port: u16,
    cache_dir: &str,
    process_id: Option<u32>,
    expected_executable_path: Option<&Path>,
    expected_executable_sha256: Option<&str>,
) -> Result<(), String> {
    wait_for_verified_health_with_timeout(
        port,
        cache_dir,
        process_id,
        expected_executable_path,
        expected_executable_sha256,
        service_startup_timeout(),
    )
}

fn is_verified_healthy(
    port: u16,
    cache_dir: &str,
    process_id: Option<u32>,
    expected_executable_path: Option<&Path>,
    expected_executable_sha256: Option<&str>,
) -> bool {
    wait_for_verified_health_with_timeout(
        port,
        cache_dir,
        process_id,
        expected_executable_path,
        expected_executable_sha256,
        service_probe_timeout(),
    )
    .is_ok()
}

fn packaged_service_relative_path() -> PathBuf {
    PathBuf::from("bin").join("astock-data-service.exe")
}

#[cfg(windows)]
pub fn hidden_process_creation_flags() -> u32 {
    0x08000000
}

#[cfg(not(windows))]
pub fn hidden_process_creation_flags() -> u32 {
    0
}

fn packaged_service_path(app: &AppHandle) -> Result<PathBuf, String> {
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|err| format!("resource dir unavailable: {err}"))?;
    Ok(resource_dir.join(packaged_service_relative_path()))
}

fn should_use_packaged_service(packaged_service_exists: bool) -> bool {
    packaged_service_exists && !cfg!(debug_assertions)
}

fn stop_child_after_start_failure<T>(child: &mut Child, message: String) -> Result<T, String> {
    if child.try_wait().map_err(|err| format!("{message}; failed to inspect child: {err}"))?.is_none() {
        let _ = child.kill();
        let _ = child.wait();
    }
    Err(message)
}

impl DataServiceManager {
    pub fn ensure_running(&mut self, app: &AppHandle, cache_dir: &str) -> Result<DataServiceStatus, String> {
        let resolved_cache_dir = resolve_cache_dir(cache_dir)?;
        if let Some(existing) = self.service.as_mut() {
            let existing_pid = existing.child.id();
            if existing.child.try_wait().map_err(|err| err.to_string())?.is_none()
                && cached_service_matches(&existing.cache_dir, &resolved_cache_dir)
                && is_verified_healthy(
                    existing.port,
                    &resolved_cache_dir,
                    Some(existing_pid),
                    existing.executable_path.as_deref(),
                    existing.executable_sha256.as_deref(),
                )
            {
                return Ok(DataServiceStatus {
                    running: true,
                    port: existing.port,
                    base_url: format!("http://127.0.0.1:{}", existing.port),
                    cache_dir: existing.cache_dir.clone(),
                    message: "local data service already running".to_string(),
                });
            }
            let _ = existing.child.kill();
            self.service = None;
        }

        let port = choose_port()?;
        let packaged_service = packaged_service_path(app)?;
        let use_packaged_service = should_use_packaged_service(packaged_service.exists());
        let expected_executable_path = if use_packaged_service {
            Some(packaged_service.clone())
        } else {
            None
        };
        let expected_executable_sha256 = match expected_executable_path.as_deref() {
            Some(path) => Some(file_sha256(path)?),
            None => None,
        };
        let lock_path = service_lock_path(&resolved_cache_dir);
        if let Ok(lock) = read_service_lock(&lock_path) {
            if locked_service_matches_current_expectation(
                &resolved_cache_dir,
                &lock,
                expected_executable_path.as_deref(),
                expected_executable_sha256.as_deref(),
            ) {
                return Ok(DataServiceStatus {
                    running: true,
                    port: lock.port,
                    base_url: format!("http://127.0.0.1:{}", lock.port),
                    cache_dir: resolved_cache_dir,
                    message: "local data service already running for this cache".to_string(),
                });
            }
            remove_service_lock(&lock_path);
        }
        let pending_lock = ServiceLockPayload {
            port,
            cache_dir: resolved_cache_dir.clone(),
            process_id: None,
            executable_path: expected_executable_path
                .as_ref()
                .map(|path| path.to_string_lossy().to_string()),
            executable_sha256: expected_executable_sha256.clone(),
        };
        if !try_create_service_lock(&lock_path, &pending_lock)? {
            if let Some(lock) = wait_for_locked_service(
                &resolved_cache_dir,
                &lock_path,
                service_startup_timeout(),
                expected_executable_path.as_deref(),
                expected_executable_sha256.as_deref(),
            ) {
                return Ok(DataServiceStatus {
                    running: true,
                    port: lock.port,
                    base_url: format!("http://127.0.0.1:{}", lock.port),
                    cache_dir: resolved_cache_dir,
                    message: "local data service already running for this cache".to_string(),
                });
            }
            recreate_service_lock_after_timeout(&lock_path, &pending_lock)?;
        }
        let mut command = if should_use_packaged_service(packaged_service.exists()) {
            let mut packaged = Command::new(packaged_service);
            packaged.args(["--host", "127.0.0.1", "--port", &port.to_string(), "--cache-dir", &resolved_cache_dir]);
            packaged
        } else if cfg!(debug_assertions) {
            let root = project_root()?;
            let backend_path = backend_dir(&root);
            let mut python = python_command()?;
            python.args(build_service_args(port, &resolved_cache_dir));
            python.current_dir(&root);
            python.env("PYTHONPATH", backend_path);
            python
        } else {
            return Err("packaged localhost data service was not found".to_string());
        };
        command.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
        #[cfg(windows)]
        {
            command.creation_flags(hidden_process_creation_flags());
        }
        let mut child = command
            .spawn()
            .map_err(|err| format!("failed to start localhost data service: {err}"))?;
        if let Err(err) = wait_for_verified_health(
            port,
            &resolved_cache_dir,
            Some(child.id()),
            expected_executable_path.as_deref(),
            expected_executable_sha256.as_deref(),
        ) {
            remove_service_lock(&lock_path);
            return stop_child_after_start_failure(&mut child, err);
        }
        if let Some(status) = child
            .try_wait()
            .map_err(|err| format!("failed to inspect localhost data service after health check: {err}"))?
        {
            remove_service_lock(&lock_path);
            return Err(format!("localhost data service exited immediately after health check: {status}"));
        }
        write_service_lock(
            &lock_path,
            &ServiceLockPayload {
                port,
                cache_dir: resolved_cache_dir.clone(),
                process_id: Some(child.id()),
                executable_path: expected_executable_path
                    .as_ref()
                    .map(|path| path.to_string_lossy().to_string()),
                executable_sha256: expected_executable_sha256.clone(),
            },
        )?;
        self.service = Some(ManagedService {
            child,
            port,
            cache_dir: resolved_cache_dir.clone(),
            executable_path: expected_executable_path,
            executable_sha256: expected_executable_sha256,
        });
        Ok(DataServiceStatus {
            running: true,
            port,
            base_url: format!("http://127.0.0.1:{port}"),
            cache_dir: resolved_cache_dir,
            message: "local data service started".to_string(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::{
        build_service_args, cached_service_matches, choose_populated_cache_dir, health_request,
        file_sha256, packaged_service_relative_path, read_service_lock, service_health_request,
        hidden_process_creation_flags, locked_service_expected_identity, require_recreated_service_lock, service_lock_path, should_use_packaged_service,
        service_startup_timeout, stop_child_after_start_failure, try_create_service_lock, runtime_data_candidates_from,
        validate_service_health, workspace_cache_candidates_from_root, ServiceLockPayload,
    };
    use std::fs;
    use std::process::{Command, Stdio};
    use std::time::{SystemTime, UNIX_EPOCH};

    fn unique_temp_dir(name: &str) -> std::path::PathBuf {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system time should be valid")
            .as_nanos();
        std::env::temp_dir().join(format!("astock-backtester-{name}-{suffix}"))
    }

    #[test]
    fn health_request_targets_lightweight_ping_endpoint() {
        let raw = health_request(9123);
        assert!(raw.contains("GET /ping HTTP/1.1"));
        assert!(raw.contains("Host: 127.0.0.1:9123"));
    }

    #[test]
    fn service_health_request_targets_identity_endpoint() {
        let raw = service_health_request(9123);
        assert!(raw.contains("GET /identity HTTP/1.1"));
        assert!(raw.contains("Host: 127.0.0.1:9123"));
    }

    #[test]
    fn packaged_onefile_service_has_a_cold_start_budget() {
        assert!(
            service_startup_timeout() >= std::time::Duration::from_secs(20),
            "the packaged sidecar can spend more than 8 seconds unpacking before it serves /identity"
        );
    }

    #[test]
    fn cached_service_matches_requested_cache_case_insensitively() {
        let root = unique_temp_dir("cache-identity");
        let cache_a = root.join("CacheA");
        let cache_b = root.join("CacheB");
        fs::create_dir_all(&cache_a).expect("cache A should exist");
        fs::create_dir_all(&cache_b).expect("cache B should exist");
        let cache_a_lower = cache_a.to_string_lossy().to_ascii_lowercase();

        assert!(cached_service_matches(&cache_a.to_string_lossy(), &cache_a_lower));
        assert!(!cached_service_matches(&cache_a.to_string_lossy(), &cache_b.to_string_lossy()));

        fs::remove_dir_all(&root).expect("temp cache tree should be removed");
    }

    #[test]
    fn validate_service_health_accepts_matching_identity() {
        let root = unique_temp_dir("health-identity-ok");
        let cache = root.join("CacheA");
        fs::create_dir_all(&cache).expect("cache should exist");
        let cache_lower = cache.to_string_lossy().to_ascii_lowercase();
        let body = serde_json::json!({
            "ok": true,
            "cache_path": cache.to_string_lossy(),
            "port": 9123,
            "process_id": 4567,
            "coverage": []
        });
        let raw = format!("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{body}");

        let result = validate_service_health(&raw, 9123, &cache_lower, Some(4567), None, None);

        assert!(result.is_ok());

        fs::remove_dir_all(&root).expect("temp cache tree should be removed");
    }

    #[test]
    fn validate_service_health_rejects_mismatched_identity() {
        let root = unique_temp_dir("health-identity-mismatch");
        let cache_a = root.join("CacheA");
        let cache_b = root.join("CacheB");
        fs::create_dir_all(&cache_a).expect("cache A should exist");
        fs::create_dir_all(&cache_b).expect("cache B should exist");
        let body = serde_json::json!({
            "ok": true,
            "cache_path": cache_a.to_string_lossy(),
            "port": 9123,
            "process_id": 4567,
            "coverage": []
        });
        let raw = format!("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{body}");

        assert!(validate_service_health(&raw, 9000, &cache_a.to_string_lossy(), Some(4567), None, None)
            .unwrap_err()
            .contains("port mismatch"));
        assert!(validate_service_health(&raw, 9123, &cache_b.to_string_lossy(), Some(4567), None, None)
            .unwrap_err()
            .contains("cache mismatch"));
        assert!(validate_service_health(&raw, 9123, &cache_a.to_string_lossy(), Some(9999), None, None)
            .unwrap_err()
            .contains("pid mismatch"));

        fs::remove_dir_all(&root).expect("temp cache tree should be removed");
    }

    #[test]
    fn validate_service_health_allows_packaged_onefile_child_pid_when_executable_identity_matches() {
        let root = unique_temp_dir("health-onefile-child-pid");
        let cache = root.join("CacheA");
        let executable = root.join("astock-data-service.exe");
        fs::create_dir_all(&cache).expect("cache should exist");
        fs::write(&executable, b"packaged-sidecar").expect("executable should exist");
        let expected_hash = file_sha256(&executable).expect("executable hash should be available");
        let body = serde_json::json!({
            "ok": true,
            "cache_path": cache.to_string_lossy(),
            "port": 9123,
            "process_id": 9999,
            "executable_path": executable.to_string_lossy(),
            "executable_sha256": expected_hash,
            "coverage": []
        });
        let raw = format!("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{body}");

        let result = validate_service_health(
            &raw,
            9123,
            &cache.to_string_lossy(),
            Some(4567),
            Some(&executable),
            Some(&expected_hash),
        );

        assert!(result.is_ok());

        fs::remove_dir_all(&root).expect("temp cache tree should be removed");
    }

    #[test]
    fn validate_service_health_rejects_mismatched_executable_identity() {
        let root = unique_temp_dir("health-executable-mismatch");
        let cache = root.join("CacheA");
        let executable_a = root.join("astock-data-service-a.exe");
        let executable_b = root.join("astock-data-service-b.exe");
        fs::create_dir_all(&cache).expect("cache should exist");
        fs::write(&executable_a, b"sidecar-a").expect("executable A should exist");
        fs::write(&executable_b, b"sidecar-b").expect("executable B should exist");
        let expected_hash = file_sha256(&executable_a).expect("executable A hash should be available");
        let wrong_hash = file_sha256(&executable_b).expect("executable B hash should be available");
        let body = serde_json::json!({
            "ok": true,
            "cache_path": cache.to_string_lossy(),
            "port": 9123,
            "process_id": 4567,
            "executable_path": executable_a.to_string_lossy(),
            "executable_sha256": expected_hash,
            "coverage": []
        });
        let raw = format!("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{body}");

        assert!(validate_service_health(
            &raw,
            9123,
            &cache.to_string_lossy(),
            Some(4567),
            Some(&executable_b),
            Some(&expected_hash),
        )
        .unwrap_err()
        .contains("executable mismatch"));
        assert!(validate_service_health(
            &raw,
            9123,
            &cache.to_string_lossy(),
            Some(4567),
            Some(&executable_a),
            Some(&wrong_hash),
        )
        .unwrap_err()
        .contains("executable hash mismatch"));

        fs::remove_dir_all(&root).expect("temp cache tree should be removed");
    }

    #[test]
    fn service_lock_file_is_created_exclusively_inside_cache_dir() {
        let root = unique_temp_dir("service-lock");
        let cache = root.join("CacheA");
        fs::create_dir_all(&cache).expect("cache should exist");
        let lock_path = service_lock_path(&cache.to_string_lossy());
        let payload = ServiceLockPayload {
            port: 9123,
            cache_dir: cache.to_string_lossy().to_string(),
            process_id: Some(4567),
            executable_path: Some("C:\\service\\astock-data-service.exe".to_string()),
            executable_sha256: Some("abc123".to_string()),
        };

        assert!(try_create_service_lock(&lock_path, &payload).expect("lock should be created"));
        assert!(!try_create_service_lock(&lock_path, &payload).expect("second lock should not be created"));
        let parsed = read_service_lock(&lock_path).expect("lock should be readable");

        assert_eq!(lock_path.parent(), Some(cache.as_path()));
        assert_eq!(parsed.port, 9123);
        assert_eq!(parsed.cache_dir, cache.to_string_lossy());
        assert_eq!(parsed.process_id, Some(4567));

        fs::remove_dir_all(&root).expect("temp cache tree should be removed");
    }

    #[test]
    fn recreated_service_lock_must_be_owned_before_spawning() {
        let error = require_recreated_service_lock(false)
            .expect_err("should not continue without owning the recreated lock");

        assert!(error.contains("another localhost data service is starting"));
        assert!(require_recreated_service_lock(true).is_ok());
    }

    #[test]
    fn locked_service_expected_identity_prefers_current_packaged_identity() {
        let root = unique_temp_dir("service-lock-identity");
        let lock = ServiceLockPayload {
            port: 9123,
            cache_dir: "cache".to_string(),
            process_id: Some(4567),
            executable_path: Some("python.exe".to_string()),
            executable_sha256: Some("debug-python-hash".to_string()),
        };
        let packaged = root.join("bin").join("astock-data-service.exe");

        let (path, hash) = locked_service_expected_identity(&lock, Some(&packaged), Some("packaged-hash"));

        assert_eq!(path, Some(packaged.as_path()));
        assert_eq!(hash, Some("packaged-hash"));
    }

    #[test]
    fn build_service_args_uses_expected_host_port_and_cache_dir() {
        let args = build_service_args(9010, ".astock-cache");
        assert_eq!(
            args,
            vec![
                "-m".to_string(),
                "astock_backtester.service".to_string(),
                "--host".to_string(),
                "127.0.0.1".to_string(),
                "--port".to_string(),
                "9010".to_string(),
                "--cache-dir".to_string(),
                ".astock-cache".to_string(),
            ]
        );
    }

    #[test]
    fn workspace_cache_candidates_prefer_runtime_data_dir_before_cache_alias() {
        let root = unique_temp_dir("cache-candidates");
        let candidates = workspace_cache_candidates_from_root(&root, ".astock-cache");

        assert_eq!(
            candidates,
            vec![
                root.join("运行产物").join("本地数据仓"),
                root.join(".astock-cache"),
            ]
        );
    }

    #[test]
    fn runtime_data_candidates_find_workspace_data_dir_from_desktop_runtime_tree() {
        let root = unique_temp_dir("runtime-data-candidates");
        let start = root.join("运行产物").join("桌面软件").join("A股策略回测工作台");
        let candidates = runtime_data_candidates_from(&start, ".astock-cache");

        assert_eq!(
            candidates,
            vec![
                root.join("运行产物").join("本地数据仓"),
                root.join(".astock-cache"),
            ]
        );
    }

    #[test]
    fn packaged_service_relative_path_targets_bundled_sidecar() {
        let path = packaged_service_relative_path();
        assert_eq!(path, std::path::PathBuf::from("bin").join("astock-data-service.exe"));
    }

    #[test]
    fn windows_sidecar_processes_are_started_without_visible_console() {
        if cfg!(windows) {
            assert_eq!(hidden_process_creation_flags(), 0x08000000);
        } else {
            assert_eq!(hidden_process_creation_flags(), 0);
        }
    }

    #[test]
    fn service_spawn_applies_hidden_process_flags() {
        let source = include_str!("service_manager.rs");
        let needle = [".creation_flags", "(hidden_process_creation_flags())"].concat();

        assert!(
            source.contains(&needle),
            "data service sidecar spawn should suppress Windows console windows"
        );
    }

    #[test]
    fn service_spawn_discards_unread_stderr() {
        let source = include_str!("service_manager.rs");

        assert!(
            source.contains("command.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())"),
            "unread sidecar stderr pipes can fill and stall the local data service"
        );
    }

    #[test]
    fn packaged_service_preference_matches_build_mode() {
        assert_eq!(should_use_packaged_service(true), !cfg!(debug_assertions));
        assert!(!should_use_packaged_service(false));
    }

    #[test]
    fn cache_alias_is_used_only_when_preferred_cache_is_empty() {
        let root = unique_temp_dir("cache-pick-alias");
        let preferred = root.join("preferred");
        let cache_alias = root.join(".astock-cache");
        fs::create_dir_all(&preferred).expect("preferred cache dir should exist");
        let year_dir = cache_alias.join("warehouse").join("daily_bars").join("year=2026");
        fs::create_dir_all(&year_dir).expect("cache alias warehouse dir should exist");
        fs::write(year_dir.join("daily_bars.parquet"), b"alias-data").expect("cache alias parquet should exist");

        let selected = choose_populated_cache_dir(&preferred, &[preferred.clone(), cache_alias.clone()]);

        assert_eq!(selected, cache_alias);

        fs::remove_dir_all(&root).expect("temp cache tree should be removed");
    }

    #[test]
    fn release_cache_candidates_do_not_include_old_local_data_dir() {
        let root = unique_temp_dir("release-cache-candidates");
        let candidates = workspace_cache_candidates_from_root(&root, ".astock-cache");

        assert!(candidates.contains(&root.join("运行产物").join("本地数据仓")));
        assert!(candidates.contains(&root.join(".astock-cache")));
        assert!(!candidates.contains(&root.join("运行产物").join("本地数据")));
    }

    #[test]
    fn preferred_cache_stays_selected_when_it_already_has_data() {
        let root = unique_temp_dir("cache-pick-preferred");
        let preferred = root.join("preferred");
        let legacy = root.join("legacy");
        let preferred_year_dir = preferred.join("warehouse").join("daily_bars").join("year=2026");
        let legacy_year_dir = legacy.join("warehouse").join("daily_bars").join("year=2026");
        fs::create_dir_all(&preferred_year_dir).expect("preferred warehouse dir should exist");
        fs::create_dir_all(&legacy_year_dir).expect("legacy warehouse dir should exist");
        fs::write(preferred_year_dir.join("daily_bars.parquet"), b"preferred-data")
            .expect("preferred parquet should exist");
        fs::write(legacy_year_dir.join("daily_bars.parquet"), b"legacy-data").expect("legacy parquet should exist");

        let selected = choose_populated_cache_dir(&preferred, &[preferred.clone(), legacy.clone()]);

        assert_eq!(selected, preferred);

        fs::remove_dir_all(&root).expect("temp cache tree should be removed");
    }

    #[test]
    fn stop_child_after_start_failure_terminates_spawned_process() {
        let mut child = Command::new("cmd")
            .args(["/C", "ping -n 30 127.0.0.1 > nul"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("test child process should start");

        let child_id = child.id();
        let result: Result<(), String> = stop_child_after_start_failure(&mut child, "health failed".to_string());

        assert_eq!(result.unwrap_err(), "health failed");
        let status = Command::new("tasklist")
            .args(["/FI", &format!("PID eq {child_id}")])
            .output()
            .expect("tasklist should run");
        let raw = String::from_utf8_lossy(&status.stdout);
        assert!(!raw.contains(&child_id.to_string()));
    }
}
