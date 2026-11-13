// Desktop window for Fahis. Starts the Python backend, which owns the adapter, and passes the page's requests
// to it through a private pipe. There's no web server and no open network port.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::os::windows::process::CommandExt;
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};
use tokio::sync::oneshot;

const REQUEST_KINDS: [&str; 3] = ["state", "i18n", "api"];
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
// Where the source checkout is, for running without an installer. The installed app carries its own backend.
const SOURCE_ROOT: &str = concat!(env!("CARGO_MANIFEST_DIR"), "\\..");

#[derive(Default)]
struct Backend {
    stdin: Mutex<Option<ChildStdin>>,
    child: Mutex<Option<Child>>,
    waiting: Mutex<HashMap<u64, oneshot::Sender<Value>>>,
    next: AtomicU64,
    quitting: AtomicBool,
}

fn arabic() -> bool {
    sys_locale::get_locale().map_or(false, |locale| locale.starts_with("ar"))
}

fn fail(app: &AppHandle, english: &str, arabic_text: &str) {
    let text = if arabic() { arabic_text } else { english };
    app.dialog().message(text).title(if arabic() { "فاحص" } else { "Fahis" }).kind(MessageDialogKind::Error).blocking_show();
    app.exit(1);
}

/// Only the app's own page may ask, and only for the three kinds of request the backend answers.
fn own_page(url: &tauri::Url) -> bool {
    url.scheme() == "tauri" || url.host_str() == Some("tauri.localhost")
}

#[tauri::command]
async fn fahis_request(kind: String, payload: Value, webview: tauri::Webview, backend: tauri::State<'_, Arc<Backend>>) -> Result<Value, String> {
    let from_page = webview.url().map(|url| own_page(&url)).unwrap_or(false);
    if !from_page || !REQUEST_KINDS.contains(&kind.as_str()) {
        return Err("request refused".into());
    }
    let id = backend.next.fetch_add(1, Ordering::SeqCst) + 1;
    let (sender, answer) = oneshot::channel();
    backend.waiting.lock().unwrap().insert(id, sender);
    {
        let mut stdin = backend.stdin.lock().unwrap();
        let line = json!({"id": id, "kind": kind, "payload": payload}).to_string();
        let sent = match stdin.as_mut() {
            Some(pipe) => writeln!(pipe, "{line}").and_then(|_| pipe.flush()).is_ok(),
            None => false,
        };
        if !sent {
            backend.waiting.lock().unwrap().remove(&id);
            return Err("backend stopped".into());
        }
    }
    match tokio::time::timeout(REQUEST_TIMEOUT, answer).await {
        Ok(Ok(reply)) => Ok(json!({"status": reply["status"], "data": reply["data"]})),
        Ok(Err(_)) => Err("backend stopped".into()),
        Err(_) => {
            backend.waiting.lock().unwrap().remove(&id);
            Err("backend did not answer".into())
        }
    }
}

fn backend_command(app: &AppHandle, demo: bool) -> Result<Command, (&'static str, &'static str)> {
    let mut args = vec!["app".to_string()];
    if demo {
        args.push("--demo".into());
    }
    // Test builds always run the source code. Tauri copies the bundled backend next to them too, but that copy is
    // only as new as the last installer build, so running it would test old code.
    let bundled = app.path().resource_dir().ok().map(|folder| folder.join("backend").join("fahis-backend.exe"));
    if let Some(exe) = bundled.filter(|exe| exe.exists() && !cfg!(debug_assertions)) {
        // The installed app saves in the user's own folder rather than beside its program files.
        let data = app.path().app_data_dir().map(|folder| folder.join("data")).unwrap_or_else(|_| PathBuf::from("."));
        std::fs::create_dir_all(&data).ok();
        let mut command = Command::new(exe);
        command.args(&args).current_dir(&data).env("FAHIS_DATA_DIR", &data);
        return Ok(command);
    }
    let root = PathBuf::from(SOURCE_ROOT);
    let python = root.join(".venv").join("Scripts").join("python.exe");
    if !python.exists() {
        return Err(("Python setup is missing. Follow the Setup steps in the README first.",
                    "إعداد Python غير موجود. اتبع خطوات التثبيت في ملف README أولًا."));
    }
    let mut command = Command::new(python);
    command.arg("obd_tool.py").args(&args).current_dir(root);
    Ok(command)
}

fn start_backend(app: &AppHandle, backend: Arc<Backend>, demo: bool) -> Result<(), (&'static str, &'static str)> {
    let mut command = backend_command(app, demo)?;
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
        .map_err(|_| ("The Fahis backend could not start.", "تعذّر تشغيل الخدمة الخلفية لبرنامج فاحص."))?;
    let stdout = child.stdout.take().expect("piped");
    *backend.stdin.lock().unwrap() = child.stdin.take();
    *backend.child.lock().unwrap() = Some(child);
    let handle = app.clone();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            // A line that isn't a reply can't be matched to a request, so its timeout handles it.
            let Ok(reply) = serde_json::from_str::<Value>(&line) else { continue };
            let Some(id) = reply["id"].as_u64() else { continue };
            if let Some(sender) = backend.waiting.lock().unwrap().remove(&id) {
                let _ = sender.send(reply);
            }
        }
        backend.waiting.lock().unwrap().clear(); // waiting requests now fail with "backend stopped"
        *backend.stdin.lock().unwrap() = None;
        if !backend.quitting.load(Ordering::SeqCst) {
            fail(&handle, "The Fahis backend stopped unexpectedly.", "توقفت الخدمة الخلفية لبرنامج فاحص بشكل غير متوقع.");
        }
    });
    Ok(())
}

fn stop_backend(backend: &Backend) {
    backend.quitting.store(true, Ordering::SeqCst);
    // The backend disconnects from the adapter when its pipe closes.
    backend.stdin.lock().unwrap().take();
    if let Some(mut child) = backend.child.lock().unwrap().take() {
        std::thread::sleep(Duration::from_millis(300));
        let _ = child.kill();
    }
}

fn main() {
    let demo = std::env::args().any(|arg| arg == "--demo");
    let backend = Arc::new(Backend::default());
    let app = tauri::Builder::default()
        // The adapter serves one program at a time, so a second copy could never connect.
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(backend.clone())
        .invoke_handler(tauri::generate_handler![fahis_request])
        .setup(move |app| {
            let handle = app.handle().clone();
            if let Err((english, arabic_text)) = start_backend(&handle, backend.clone(), demo) {
                fail(&handle, english, arabic_text);
                return Ok(());
            }
            let mut window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()));
            // Test builds can open a remote-debugging port, so the page can be checked the same way as before.
            // Release builds never do, as the port would let other programs on the laptop drive the window.
            if cfg!(debug_assertions) {
                if let Ok(port) = std::env::var("FAHIS_DEBUG_PORT") {
                    if port.chars().all(|c| c.is_ascii_digit()) {
                        window = window.additional_browser_args(&format!("--remote-debugging-port={port}"));
                    }
                }
            }
            window
                .title("Fahis")
                .inner_size(1200.0, 820.0)
                .min_inner_size(380.0, 500.0)
                .background_color(tauri::window::Color(0x10, 0x14, 0x18, 0xff))
                // Only the app's own page loads in the window. Web links open in the normal browser.
                .on_navigation(|url| {
                    if own_page(url) {
                        return true;
                    }
                    if url.scheme() == "https" {
                        let _ = tauri_plugin_opener::open_url(url.as_str(), None::<&str>);
                    }
                    false
                })
                .build()?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Fahis could not start its window");
    let state = app.state::<Arc<Backend>>().inner().clone();
    app.run(move |_, event| {
        if let RunEvent::Exit = event {
            stop_backend(&state);
        }
    });
}
