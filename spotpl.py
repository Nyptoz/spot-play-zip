#!/usr/bin/env python3
"""
spotpl - one Spotify link in, one ZIP out.

Usage:
    python spotpl.py                 # open the local web UI (double-click friendly)
    python spotpl.py <spotify-url>   # command line: downloads and writes downloads/<name>.zip
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

APP_NAME = "spotpl"
HOST = "127.0.0.1"
DEFAULT_PORT = 8710
AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav", ".aac", ".webm"}
VALID_FORMATS = ("mp3", "flac", "opus", "m4a", "ogg", "wav")

ROOT = Path(__file__).resolve().parent
DOWNLOADS = ROOT / "downloads"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
SPOTIFY_URL_RE = re.compile(
    r"open\.spotify\.com/(?P<kind>playlist|album|track|episode|show|artist)/(?P<id>[A-Za-z0-9]{22})"
)
SPOTIFY_URI_RE = re.compile(r"^spotify:(?P<kind>playlist|album|track|episode|show|artist):(?P<id>[A-Za-z0-9]{22})$")


# --------------------------------------------------------------------------- utils


def setup_console() -> None:
    """Track names contain accents/emoji; never let a Windows console codepage kill the job."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def log_line(msg: str) -> None:
    try:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
    except Exception:
        pass


def clean_ansi(text: str) -> str:
    return ANSI_RE.sub("", text).strip()


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def slugify(name: str, fallback: str = "spotify-playlist") -> str:
    slug = re.sub(r"[^\w\s-]", "", name, flags=re.UNICODE).strip()
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-").lower()
    return slug[:80] or fallback


def parse_link(url: str) -> tuple[str, str]:
    """Return (kind, id) for a Spotify playlist/album/track link, URI or bare id."""
    url = (url or "").strip().strip("<>\"'")
    if not url:
        raise ValueError("Paste a Spotify link first.")

    uri = SPOTIFY_URI_RE.match(url)
    if uri:
        return uri.group("kind"), uri.group("id")

    if re.fullmatch(r"[A-Za-z0-9]{22}", url):
        return "playlist", url

    match = SPOTIFY_URL_RE.search(urlparse(url).path if "://" in url else url)
    if not match:
        match = SPOTIFY_URL_RE.search(url)
    if not match:
        raise ValueError(
            "That doesn't look like a Spotify link. "
            "Copy one from the app: Share -> Copy link to playlist."
        )
    return match.group("kind"), match.group("id")


def canonical_url(kind: str, ident: str) -> str:
    return f"https://open.spotify.com/{kind}/{ident}"


def fetch_title(kind: str, ident: str) -> str:
    """Playlist name via Spotify's public oEmbed endpoint (no login needed)."""
    url = canonical_url(kind, ident)
    try:
        req = Request(
            f"https://open.spotify.com/oembed?url={url}",
            headers={"User-Agent": f"{APP_NAME}/1.0"},
        )
        with urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        title = (data.get("title") or "").strip()
        if title:
            return title
    except Exception:
        pass
    return f"{kind}-{ident}"


def unique_path(folder: Path, stem: str, suffix: str) -> Path:
    candidate = folder / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = folder / f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


# --------------------------------------------------------------------------- job model


class Job:
    def __init__(self, url: str, audio_format: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.audio_format = audio_format
        self.created = datetime.now()
        self.state = "queued"  # queued | working | zipping | done | error
        self.logs: list[str] = []
        self.tracks = 0
        self.name = ""
        self.zip_path: Path | None = None
        self.error = ""
        self.lock = threading.Lock()

    def say(self, msg: str) -> None:
        msg = clean_ansi(msg)
        if not msg:
            return
        with self.lock:
            self.logs.append(msg)
            if len(self.logs) > 400:
                del self.logs[:-400]
        log_line(msg)

    def payload(self) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "url": self.url,
                "state": self.state,
                "logs": list(self.logs),
                "tracks": self.tracks,
                "name": self.name,
                "ready": self.zip_path is not None,
                "filename": self.zip_path.name if self.zip_path else None,
                "error": self.error,
            }


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def active_job() -> Job | None:
    with JOBS_LOCK:
        for job in reversed(list(JOBS.values())):
            if job.state in ("queued", "working", "zipping"):
                return job
    return None


# --------------------------------------------------------------------------- core download


def spotdl_command(job: Job, kind: str) -> list[str]:
    """spotdl writes into its own working directory, so we run it with cwd=temp dir.

    (--output is a *file name template* in spotdl v4, not a destination folder.)
    """
    cmd = [
        sys.executable,
        "-m",
        "spotdl",
        "download",
        job.url,
        "--format",
        job.audio_format,
        "--print-errors",
        "--log-level",
        "INFO",
    ]
    if kind == "playlist":
        # Keep playlist order in the ZIP: 01 - Artist - Title.mp3
        cmd += ["--output", "{list-position} - {artist} - {title}"]
    if not has_ffmpeg():
        # spotdl can fetch its own ffmpeg on first run (keeps setup to one click).
        cmd.append("--download-ffmpeg")
    return cmd


def build_zip(job: Job, tracks: list[Path], folder_name: str) -> Path:
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    zip_path = unique_path(DOWNLOADS, slugify(folder_name), ".zip")
    total = max(len(tracks), 1)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        root = zipfile.ZipInfo(f"{folder_name}/")
        root.external_attr = 0o40775 << 16
        zf.writestr(root, "")

        manifest = [
            f"{job.name}",
            "",
            f"Source     : {job.url}",
            f"Tracks     : {len(tracks)}",
            f"Format     : {job.audio_format}",
            f"Downloaded : {datetime.now():%Y-%m-%d %H:%M}",
            f"Tool       : {APP_NAME}",
            "",
            "Downloaded for personal listening only. Support the artists.",
        ]
        zf.writestr(
            f"{folder_name}/_playlist.txt",
            "\n".join(manifest) + "\n",
        )

        for index, track in enumerate(tracks, start=1):
            zf.write(track, arcname=f"{folder_name}/{track.name}")
            pct = int(index / total * 100)
            job.say(f"  Zipping {pct:3d}%  ({index}/{len(tracks)})  {track.name}")

    return zip_path


def run_job(job: Job) -> None:
    tmp_dir = ROOT / ".spotpl_tmp" / job.id
    try:
        job.state = "working"
        kind, ident = parse_link(job.url)
        job.url = canonical_url(kind, ident)
        job.name = fetch_title(kind, ident)

        if kind not in ("playlist", "album", "track", "episode", "show"):
            raise ValueError(f"Sorry, {kind} links are not supported yet. Use a playlist link.")

        job.say(f"Playlist: {job.name}")
        job.say("Fetching tracks from Spotify ...")
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        cmd = spotdl_command(job, kind)
        creation_flags = 0
        if os.name == "nt":
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        proc = subprocess.Popen(
            cmd,
            cwd=str(tmp_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creation_flags,
        )

        assert proc.stdout is not None
        for raw in proc.stdout:
            for chunk in raw.replace("\r", "\n").split("\n"):
                line = clean_ansi(chunk)
                if not line:
                    continue
                if line.lower().startswith("downloaded ") or "[download]" in line.lower():
                    job.tracks += 1
                job.say(line)
        code = proc.wait()

        if code != 0:
            raise RuntimeError(f"spotdl stopped with error code {code} (see log above).")

        tracks = sorted(
            p for p in tmp_dir.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS
        )
        if not tracks:
            raise RuntimeError("No audio files were produced - nothing to zip.")

        job.tracks = len(tracks)
        job.state = "zipping"
        job.say(f"Got {len(tracks)} track(s). Building ZIP ...")

        folder_name = slugify(job.name, f"spotify-{ident}")
        job.zip_path = build_zip(job, tracks, folder_name)
        job.state = "done"
        size_mb = job.zip_path.stat().st_size / (1024 * 1024)
        job.say(f"Done -> downloads/{job.zip_path.name}  ({size_mb:.1f} MB)")

    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        job.state = "error"
        job.error = str(exc) or exc.__class__.__name__
        job.say(f"ERROR: {job.error}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def start_job(url: str, audio_format: str = "mp3") -> Job:
    if audio_format not in VALID_FORMATS:
        audio_format = "mp3"
    parse_link(url)  # fail fast before the UI shows "working"
    job = Job(url, audio_format)
    with JOBS_LOCK:
        JOBS[job.id] = job
        for key, old in list(JOBS.items()):
            if key != job.id and old.state in ("done", "error"):
                del JOBS[key]
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job


def zip_inventory() -> list[dict]:
    if not DOWNLOADS.exists():
        return []
    items = []
    for path in sorted(DOWNLOADS.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = path.stat()
        items.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            }
        )
    return items[:20]


# --------------------------------------------------------------------------- web UI

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>spotpl - playlist to ZIP</title>
<style>
  :root {
    --bg: #0d0f12; --card: #171a1f; --line: #262b33; --text: #eef1f5;
    --muted: #8b95a5; --green: #1ed760; --red: #ff5a5a; --amber: #ffb020;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  .wrap { max-width: 860px; margin: 0 auto; padding: 40px 20px 80px; }
  h1 { font-size: 30px; margin: 0 0 6px; letter-spacing: -.5px; }
  h1 span { color: var(--green); }
  .sub { color: var(--muted); margin: 0 0 28px; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 20px; }
  .row { display: flex; gap: 10px; flex-wrap: wrap; }
  input[type=url] { flex: 1 1 320px; padding: 14px 16px; border-radius: 10px; border: 1px solid var(--line);
    background: #0f1216; color: var(--text); font-size: 15px; min-width: 0; }
  input[type=url]:focus { outline: 2px solid var(--green); border-color: transparent; }
  select { padding: 14px 12px; border-radius: 10px; border: 1px solid var(--line); background: #0f1216; color: var(--text); }
  button { padding: 14px 22px; border-radius: 10px; border: 0; background: var(--green); color: #06240f;
    font-weight: 700; font-size: 15px; cursor: pointer; }
  button:disabled { opacity: .45; cursor: not-allowed; }
  button.ghost { background: #232830; color: var(--text); font-weight: 600; }
  .status { display: flex; align-items: center; gap: 10px; margin: 18px 0 0; color: var(--muted); font-size: 14px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--green); }
  .dot.idle { background: var(--muted); } .dot.err { background: var(--red); } .dot.run { animation: pulse 1s infinite; }
  @keyframes pulse { 50% { opacity: .25; } }
  #log { margin-top: 16px; background: #0a0c0f; border: 1px solid var(--line); border-radius: 12px;
    padding: 14px 16px; height: 300px; overflow-y: auto; white-space: pre-wrap; word-break: break-word;
    font: 12.5px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; color: #c6cedb; }
  #log .err { color: var(--red); } #log .ok { color: var(--green); }
  .files { margin-top: 28px; }
  .files h2 { font-size: 14px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); }
  .file { display: flex; align-items: center; gap: 12px; padding: 11px 14px; border: 1px solid var(--line);
    border-radius: 10px; margin-bottom: 8px; background: var(--card); }
  .file a { color: var(--text); text-decoration: none; font-weight: 600; flex: 1; word-break: break-all; }
  .file a:hover { color: var(--green); }
  .file .meta { color: var(--muted); font-size: 12.5px; white-space: nowrap; }
  .note { color: var(--muted); font-size: 12.5px; margin-top: 22px; }
  a.dl { display: inline-block; padding: 13px 20px; border-radius: 10px; background: var(--green);
    color: #06240f; font-weight: 700; text-decoration: none; }
</style>
</head>
<body>
<div class="wrap">
  <h1>spot<span>pl</span></h1>
  <p class="sub">Paste one Spotify playlist link, get one ZIP with every track. Nothing else to configure.</p>

  <div class="card">
    <div class="row">
      <input id="url" type="url" placeholder="https://open.spotify.com/playlist/..." autofocus>
      <select id="fmt" title="Audio format">
        <option value="mp3">mp3</option>
        <option value="flac">flac</option>
        <option value="opus">opus</option>
        <option value="m4a">m4a</option>
      </select>
      <button id="go">Get ZIP</button>
    </div>
    <div class="status"><span class="dot idle" id="dot"></span><span id="state">Ready</span></div>
    <div id="log">Waiting for a link...</div>
  </div>

  <div class="files" id="files"></div>
  <p class="note">For personal listening only - please support the artists you love.</p>
</div>

<script>
const $ = (id) => document.getElementById(id);
let jobId = null, timer = null;

function fmtSize(b) {
  if (b > 1048576) return (b / 1048576).toFixed(1) + ' MB';
  if (b > 1024) return (b / 1024).toFixed(0) + ' kB';
  return b + ' B';
}

async function start() {
  const url = $('url').value.trim();
  if (!url) return;
  $('go').disabled = true;
  $('log').textContent = '';
  $('dot').className = 'dot run';
  $('state').textContent = 'Starting...';
  const res = await fetch('/api/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, format: $('fmt').value })
  });
  const data = await res.json();
  if (!res.ok) {
    $('dot').className = 'dot err';
    $('state').textContent = 'Cannot start';
    $('log').innerHTML += '<span class="err">' + (data.error || 'Error') + '</span>\\n';
    $('go').disabled = false;
    return;
  }
  jobId = data.id;
  poll();
}

async function poll() {
  clearTimeout(timer);
  const res = await fetch('/api/status?id=' + jobId);
  const job = await res.json();
  const box = $('log');
  box.textContent = job.logs.join('\\n');
  box.scrollTop = box.scrollHeight;

  if (job.state === 'done') {
    $('dot').className = 'dot';
    $('state').innerHTML = '<b>ZIP ready</b> - ' + job.tracks + ' track(s) - <a class="dl" href="/api/download?id=' +
      job.id + '">Download ' + job.filename + '</a>';
    $('go').disabled = false;
    loadFiles();
    return;
  }
  if (job.state === 'error') {
    $('dot').className = 'dot err';
    $('state').textContent = 'Failed';
    $('go').disabled = false;
    return;
  }
  $('state').textContent = job.state === 'zipping'
    ? 'Zipping...'
    : 'Downloading - ' + job.tracks + ' track(s) done';
  timer = setTimeout(poll, 900);
}

async function loadFiles() {
  const res = await fetch('/api/files');
  const files = await res.json();
  const wrap = $('files');
  if (!files.length) { wrap.innerHTML = ''; return; }
  wrap.innerHTML = '<h2>Your ZIPs</h2>' + files.map(f =>
    '<div class="file"><a href="/download/' + encodeURIComponent(f.name) + '">' + f.name + '</a>' +
    '<span class="meta">' + fmtSize(f.size) + '</span></div>').join('');
}

$('go').addEventListener('click', start);
$('url').addEventListener('keydown', e => { if (e.key === 'Enter') start(); });
loadFiles();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "spotpl"

    def log_message(self, fmt: str, *args) -> None:  # quieter console
        pass

    # -- helpers
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, data: dict) -> None:
        self._send(code, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    # -- routes
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return

        if route == "/api/status":
            job = JOBS.get((query.get("id") or [""])[0])
            self._json(200, job.payload() if job else {"state": "error", "error": "Job not found"})
            return

        if route == "/api/files":
            self._json(200, zip_inventory())
            return

        if route == "/api/download":
            job = JOBS.get((query.get("id") or [""])[0])
            if not job or not job.zip_path or not job.zip_path.exists():
                self._json(404, {"error": "ZIP not ready"})
                return
            self._stream_zip(job.zip_path, as_attachment=True)
            return

        if route.startswith("/download/"):
            name = route[len("/download/") :]
            path = DOWNLOADS / name
            if not path.exists() or path.suffix.lower() != ".zip" or path.parent != DOWNLOADS:
                self._json(404, {"error": "Not found"})
                return
            self._stream_zip(path, as_attachment=True)
            return

        self._json(404, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/start":
            self._json(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"error": "Bad request"})
            return

        busy = active_job()
        if busy:
            self._json(409, {"error": "A download is already running. Wait for it to finish."})
            return

        try:
            job = start_job(data.get("url", ""), data.get("format", "mp3"))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, {"id": job.id})

    def _stream_zip(self, path: Path, as_attachment: bool) -> None:
        size = path.stat().st_size
        disposition = "attachment" if as_attachment else "inline"
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Content-Disposition",
            f'{disposition}; filename="{path.name}"; filename*=UTF-8\'\'{path.name}',
        )
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as fh:
            shutil.copyfileobj(fh, self.wfile, length=1024 * 512)


def cleanup_stale_temp() -> None:
    """Remove leftovers from a session that was killed mid-download."""
    tmp_root = ROOT / ".spotpl_tmp"
    if tmp_root.exists():
        shutil.rmtree(tmp_root, ignore_errors=True)


def serve(port: int, open_browser: bool) -> None:
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, port), Handler)
    url = f"http://{HOST}:{httpd.server_address[1]}"
    log_line(f"{APP_NAME} ready at {url}")
    log_line(f"ZIPs are saved in {DOWNLOADS}")
    if open_browser:
        threading.Timer(0.8, lambda: __import__("webbrowser").open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log_line("bye")


# --------------------------------------------------------------------------- cli


def preflight() -> None:
    try:
        import spotdl  # noqa: F401
    except ImportError:
        sys.stderr.write(
            "\nspotdl is not installed yet.\n"
            "  Windows  -> double-click  start.bat\n"
            "  mac/Linux -> run  ./start.sh\n\n"
        )
        raise SystemExit(1)
    if not has_ffmpeg():
        log_line("ffmpeg not found on PATH - spotpl will fetch it automatically on first run.")


def main() -> int:
    setup_console()
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="One Spotify playlist link in, one ZIP out.",
    )
    parser.add_argument("url", nargs="?", help="Spotify playlist link (omit to open the web UI)")
    parser.add_argument("--format", default="mp3", choices=VALID_FORMATS, help="audio format (default: mp3)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"web UI port (default: {DEFAULT_PORT})")
    parser.add_argument("--serve", action="store_true", help="force the web UI even with a URL")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    args = parser.parse_args()

    cleanup_stale_temp()

    if not args.url or args.serve:
        preflight()
        serve(args.port, open_browser=not args.no_browser)
        return 0

    preflight()
    job = start_job(args.url, args.format)
    while job.state in ("queued", "working", "zipping"):
        time.sleep(0.4)
    if job.state == "error":
        sys.stderr.write(f"\nFailed: {job.error}\n")
        return 1
    print(f"\nZIP: {job.zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
