"""Minecraft Sound Studio.

Turns a YouTube link into a trimmed .ogg sound event inside a Minecraft
resource pack (folder or .zip) or a mod project's resources.

Runs a small server on 127.0.0.1 and shows the UI in its own app window
(Microsoft Edge in --app mode). Uses only the standard library plus the
yt-dlp and ffmpeg executables found on PATH.
"""

import array
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOST = "127.0.0.1"
PORT = 8777
ORIGIN = f"http://{HOST}:{PORT}"
BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MCSoundStudio"
CACHE_DIR = DATA_DIR / "cache"
CONFIG_FILE = DATA_DIR / "config.json"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
                 "music.youtube.com", "youtu.be", "www.youtu.be"}
SOUND_NAME_RE = re.compile(r"^[a-z0-9_\-]+(\.[a-z0-9_\-]+)*$")
NAMESPACE_RE = re.compile(r"^[a-z0-9_.\-]+$")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{6,20}$")
AUDIO_EXTS = (".webm", ".m4a", ".opus", ".ogg", ".mp3", ".mp4", ".aac", ".wav")
PEAKS_PER_SECOND = 100
PEAK_RATE = 8000  # samples/s used for waveform analysis
HEARTBEAT_TIMEOUT = 150  # seconds without the window before the app quits

PACK_MCMETA = {
    "pack": {
        "description": "",
        "pack_format": 15,
        "min_format": 15,
        "max_format": 999,
        "supported_formats": [15, 64],
    }
}

jobs = {}
jobs_lock = threading.Lock()
write_lock = threading.Lock()
dialog_lock = threading.Lock()
last_heartbeat = [time.time()]


class UserError(Exception):
    """An error whose message is shown to the user as-is."""


def tool(name):
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"{name} no se encontró en el PATH. Instálalo con: winget install {name}")
    return path


YT_DLP = tool("yt-dlp")
FFMPEG = tool("ffmpeg")


# ---------------------------------------------------------------- config

def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")


def remember_target(path, namespace=None):
    cfg = load_config()
    recents = [r for r in cfg.get("recents", []) if r != path]
    cfg["recents"] = [path] + recents[:9]
    cfg["last_target"] = path
    if namespace:
        cfg.setdefault("namespaces", {})[path] = namespace
    save_config(cfg)


# ---------------------------------------------------------------- youtube

def validate_url(url):
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    if (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        return None
    return url


def guess_video_id(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.endswith("youtu.be"):
        vid = parsed.path.strip("/").split("/")[0]
    elif parsed.path.startswith(("/shorts/", "/live/", "/embed/")):
        vid = parsed.path.split("/")[2]
    else:
        vid = (parse_qs(parsed.query).get("v") or [""])[0]
    return vid if VIDEO_ID_RE.match(vid or "") else None


def cached_audio(video_id):
    for ext in AUDIO_EXTS:
        p = CACHE_DIR / f"{video_id}{ext}"
        if p.is_file():
            return p
    return None


def update_job(job_id, **fields):
    with jobs_lock:
        jobs[job_id].update(fields)


def compute_peaks(audio_path, duration):
    """Return base64 bytes, one 0-255 peak per 1/PEAKS_PER_SECOND s."""
    bucket = PEAK_RATE // PEAKS_PER_SECOND
    proc = subprocess.Popen(
        [FFMPEG, "-v", "error", "-i", str(audio_path), "-ac", "1", "-ar", str(PEAK_RATE),
         "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    out = bytearray()
    chunk_bytes = bucket * 2 * 4000
    leftover = b""
    while True:
        data = proc.stdout.read(chunk_bytes)
        if not data:
            break
        data = leftover + data
        usable = len(data) - (len(data) % (bucket * 2))
        leftover = data[usable:]
        samples = array.array("h")
        samples.frombytes(data[:usable])
        for i in range(0, len(samples), bucket):
            seg = samples[i:i + bucket]
            peak = max(max(seg), -min(seg))
            out.append(min(255, peak >> 7))
    proc.wait()
    return base64.b64encode(bytes(out)).decode("ascii")


def run_fetch(job_id, url):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        vid = guess_video_id(url)
        meta_file = CACHE_DIR / f"{vid}.json" if vid else None
        audio = cached_audio(vid) if vid else None
        if not (audio and meta_file and meta_file.is_file()):
            audio, meta_file = download_audio(job_id, url)
        meta = json.loads(meta_file.read_text("utf-8"))
        update_job(job_id, status="analyzing", percent=100.0, title=meta.get("title", ""))
        duration = float(meta.get("duration") or 0)
        peaks_file = CACHE_DIR / f"{meta['id']}.peaks"
        if peaks_file.is_file():
            peaks = peaks_file.read_text("ascii")
        else:
            peaks = compute_peaks(audio, duration)
            peaks_file.write_text(peaks, "ascii")
        if not duration:
            duration = len(base64.b64decode(peaks)) / PEAKS_PER_SECOND
        update_job(job_id, status="done", video={
            "id": meta["id"], "title": meta.get("title", ""), "uploader": meta.get("uploader", ""),
            "duration": duration, "peaks": peaks, "peaksPerSecond": PEAKS_PER_SECOND,
        })
    except UserError as exc:
        update_job(job_id, status="error", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - surface anything to the UI
        update_job(job_id, status="error", error=f"Error inesperado: {exc}")


def download_audio(job_id, url):
    meta_tmp = CACHE_DIR / f"meta-{job_id}.json"
    cmd = [
        YT_DLP, "--no-playlist", "--newline", "--progress", "--no-simulate",
        "-f", "bestaudio/best",
        "--progress-template", "download:PROGRESS|%(progress._percent_str)s",
        "--print-to-file", "after_move:%(.{id,title,duration,uploader})j", str(meta_tmp),
        "-o", str(CACHE_DIR / "%(id)s.%(ext)s"),
        "--", url,
    ]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env, creationflags=NO_WINDOW)
    last_error = ""
    for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").strip()
        if line.startswith("PROGRESS|"):
            try:
                update_job(job_id, status="downloading",
                           percent=float(line.split("|", 1)[1].strip().rstrip("%")))
            except ValueError:
                pass
        elif line.startswith("ERROR:"):
            last_error = line[6:].strip()
    proc.wait()
    if proc.returncode != 0 or not meta_tmp.is_file():
        raise UserError(last_error or f"yt-dlp terminó con código {proc.returncode}")
    meta = json.loads(meta_tmp.read_text("utf-8").strip().splitlines()[-1])
    meta_file = CACHE_DIR / f"{meta['id']}.json"
    meta_file.write_text(json.dumps(meta, ensure_ascii=False), "utf-8")
    meta_tmp.unlink(missing_ok=True)
    audio = cached_audio(meta["id"])
    if not audio:
        raise UserError("La descarga terminó pero no se encontró el audio.")
    return audio, meta_file


# ---------------------------------------------------------------- targets

class FolderTarget:
    def __init__(self, root):
        self.root = Path(root)

    def read(self, rel):
        p = self.root / rel
        return p.read_bytes() if p.is_file() else None

    def names(self):
        assets = self.root / "assets"
        if not assets.is_dir():
            return []
        return sorted(d.name for d in assets.iterdir() if d.is_dir())

    def apply(self, changes):
        for rel, data in changes.items():
            p = self.root / rel
            if data is None:
                if p.is_file():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_name(p.name + ".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, p)

    def folder_of(self, rel):
        return self.root / rel


class ZipTarget:
    def __init__(self, path):
        self.path = Path(path)
        with zipfile.ZipFile(self.path) as z:
            names = z.namelist()
        self.prefix = ""
        if "pack.mcmeta" not in names:
            metas = [n for n in names if n.endswith("/pack.mcmeta") and n.count("/") == 1]
            if metas:
                self.prefix = metas[0][: -len("pack.mcmeta")]

    def read(self, rel):
        with zipfile.ZipFile(self.path) as z:
            try:
                return z.read(self.prefix + rel)
            except KeyError:
                return None

    def names(self):
        with zipfile.ZipFile(self.path) as z:
            found = set()
            start = self.prefix + "assets/"
            for n in z.namelist():
                if n.startswith(start):
                    part = n[len(start):].split("/")[0]
                    if part and "/" in n[len(start):]:
                        found.add(part)
        return sorted(found)

    def apply(self, changes):
        full = {self.prefix + k: v for k, v in changes.items()}
        fd, tmp = tempfile.mkstemp(suffix=".zip", dir=self.path.parent)
        os.close(fd)
        try:
            with zipfile.ZipFile(self.path) as src, \
                    zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
                for info in src.infolist():
                    if info.filename in full:
                        continue
                    dst.writestr(info, src.read(info.filename))
                for name, data in full.items():
                    if data is not None:
                        # .ogg is already compressed; store it as-is.
                        method = zipfile.ZIP_STORED if name.endswith(".ogg") else zipfile.ZIP_DEFLATED
                        dst.writestr(name, data, compress_type=method)
            try:
                os.replace(tmp, self.path)
            except PermissionError:
                raise UserError("No se pudo escribir el .zip: está en uso. Cierra Minecraft "
                                "(o desactiva el pack) y vuelve a intentarlo.")
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def folder_of(self, rel):
        return self.path.parent


def mod_ids(project):
    ids = []
    for f in list(project.rglob("fabric.mod.json"))[:5] + list(project.rglob("quilt.mod.json"))[:5]:
        if "build" in f.parts:
            continue
        try:
            data = json.loads(f.read_text("utf-8"))
            mid = data.get("id") or data.get("quilt_loader", {}).get("id")
            if mid and "$" not in mid:
                ids.append(mid)
        except (OSError, ValueError):
            pass
    for pattern in ("mods.toml", "neoforge.mods.toml"):
        for f in list(project.rglob(pattern))[:5]:
            if "build" in f.parts:
                continue
            for m in re.finditer(r'modId\s*=\s*"([^"$]+)"', f.read_text("utf-8", "replace")):
                ids.append(m.group(1))
    gp = project / "gradle.properties"
    if gp.is_file():
        m = re.search(r"^\s*(?:mod_id|modid|archives_base_name)\s*=\s*([a-z0-9_.\-]+)\s*$",
                      gp.read_text("utf-8", "replace"), re.M)
        if m:
            ids.append(m.group(1))
    return list(dict.fromkeys(ids))


def resolve_target(path):
    """Return (target, info) for a pack folder, pack .zip or mod project."""
    p = Path(path)
    if not p.exists():
        raise UserError("La ruta ya no existe.")
    preferred_ns = None
    suggested = []
    if p.is_file():
        if p.suffix.lower() != ".zip":
            raise UserError("Elige una carpeta o un archivo .zip.")
        try:
            t = ZipTarget(p)
        except zipfile.BadZipFile:
            raise UserError("El .zip está dañado o no es un zip.")
        if t.read("pack.mcmeta") is None:
            raise UserError("Ese .zip no tiene pack.mcmeta: no parece un resource pack.")
        kind, name = "zip", p.stem
    elif (p / "pack.mcmeta").is_file():
        t, kind, name = FolderTarget(p), "pack", p.name
    else:
        roots = [p / "src" / "main" / "resources"]
        subs = sorted(p.glob("*/src/main/resources"), key=lambda r: r.parts[-4] != "common")
        roots += subs
        mod_root = next((r for r in roots if r.is_dir()), None)
        if mod_root:
            t, kind, name = FolderTarget(mod_root), "mod", p.name
            suggested = mod_ids(p)
        elif (p / "assets").is_dir():
            t, kind, name = FolderTarget(p), "resources", p.name
        elif p.name == "assets":
            t, kind, name = FolderTarget(p.parent), "resources", p.parent.name
        elif p.parent.name == "assets":
            t, kind, name = FolderTarget(p.parent.parent), "resources", p.parent.parent.name
            preferred_ns = p.name
        else:
            raise UserError("Esta carpeta no parece un resource pack (falta pack.mcmeta) "
                            "ni un proyecto de mod (falta src/main/resources).")
    namespaces = t.names()
    for s in suggested:
        if s not in namespaces:
            namespaces.append(s)
    saved = load_config().get("namespaces", {}).get(str(p))
    # Packs default to "minecraft" so /playsound <name> works without a
    # prefix, unless the pack already keeps its sounds in another namespace.
    with_sounds = [n for n in namespaces
                   if n not in ("minecraft", "realms") and t.read(f"assets/{n}/sounds.json") is not None]
    default_ns = (preferred_ns or saved
                  or next(iter(suggested), None)
                  or next(iter(with_sounds), None)
                  or "minecraft")
    info = {
        "path": str(p), "kind": kind, "name": name,
        "root": str(getattr(t, "root", p)),
        "namespaces": namespaces or ["minecraft"], "namespace": default_ns,
    }
    return t, info


def read_json(target, rel):
    raw = target.read(rel)
    if raw is None:
        return {}
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except ValueError:
        raise UserError(f"{rel} tiene JSON inválido; corrígelo antes de continuar.")


def dump_json(data):
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def list_sounds(target, namespace):
    sounds = read_json(target, f"assets/{namespace}/sounds.json")
    out = []
    for event, entry in sounds.items():
        if not isinstance(entry, dict):
            continue
        files = []
        for s in entry.get("sounds", []):
            name = s if isinstance(s, str) else s.get("name", "")
            stream = False if isinstance(s, str) else bool(s.get("stream"))
            if isinstance(s, dict) and s.get("type") == "event":
                continue
            files.append({"name": name, "stream": stream})
        out.append({"event": event, "files": files, "subtitle": entry.get("subtitle")})
    out.sort(key=lambda e: e["event"])
    return out


def sound_file_rel(sound_name, namespace):
    ns, _, path = sound_name.rpartition(":")
    return f"assets/{ns or namespace}/sounds/{path}.ogg"


def convert(video_id, start, end, stereo, fade_in, fade_out, volume):
    src = cached_audio(video_id)
    if not src:
        raise UserError("El audio ya no está en caché; vuelve a cargar el enlace.")
    dur = end - start
    if dur <= 0.05:
        raise UserError("El recorte es demasiado corto.")
    filters = []
    if abs(volume - 1.0) > 0.001:
        filters.append(f"volume={volume:.3f}")
    if fade_in > 0:
        filters.append(f"afade=t=in:st=0:d={min(fade_in, dur):.3f}")
    if fade_out > 0:
        fo = min(fade_out, dur)
        filters.append(f"afade=t=out:st={dur - fo:.3f}:d={fo:.3f}")
    fd, out = tempfile.mkstemp(suffix=".ogg")
    os.close(fd)
    cmd = [FFMPEG, "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
           "-i", str(src), "-vn", "-map_metadata", "-1", "-ac", "2" if stereo else "1",
           "-ar", "44100", "-c:a", "libvorbis", "-q:a", "6"]
    if filters:
        cmd += ["-af", ",".join(filters)]
    cmd.append(out)
    try:
        res = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)
        if res.returncode != 0:
            raise UserError("ffmpeg falló: " + res.stderr.decode("utf-8", "replace")[-400:])
        return Path(out).read_bytes()
    finally:
        os.unlink(out)


def add_sound(data):
    target, info = resolve_target(str(data.get("target", "")))
    ns = str(data.get("namespace", "")).strip().lower()
    name = str(data.get("name", "")).strip().lower()
    if not NAMESPACE_RE.match(ns):
        raise UserError("Namespace inválido: usa minúsculas, números, _ . -")
    if not SOUND_NAME_RE.match(name):
        raise UserError("Nombre inválido: usa minúsculas, números, _ - y puntos (ej. pantheon.fountain.1).")
    mode = data.get("mode", "new")  # new | replace | variant
    video_id = str(data.get("videoId", ""))
    if not VIDEO_ID_RE.match(video_id):
        raise UserError("Primero carga un audio.")
    ogg = convert(video_id, float(data["start"]), float(data["end"]), bool(data.get("stereo", True)),
                  float(data.get("fadeIn", 0)), float(data.get("fadeOut", 0)),
                  max(0.0, min(4.0, float(data.get("volume", 1)))))
    stream = bool(data.get("stream", True))
    subtitle = str(data.get("subtitle", "")).strip()

    with write_lock:
        sounds_rel = f"assets/{ns}/sounds.json"
        sounds = read_json(target, sounds_rel)
        existing = sounds.get(name)
        if existing and mode == "new":
            return {"exists": True}
        base = name.replace(".", "/")
        changes = {}
        entry = existing if (existing and mode == "variant") else {}
        if mode == "replace" and existing:
            for s in existing.get("sounds", []):
                sname = s if isinstance(s, str) else s.get("name", "")
                if sname and not (isinstance(s, dict) and s.get("type") == "event"):
                    changes[sound_file_rel(sname, ns)] = None
        path = base
        if mode == "variant" and existing:
            used = {sound_file_rel(s if isinstance(s, str) else s.get("name", ""), ns)
                    for s in existing.get("sounds", [])}
            n = 2
            while f"assets/{ns}/sounds/{path}.ogg" in used or target.read(f"assets/{ns}/sounds/{path}.ogg"):
                path = f"{base}_{n}"
                n += 1
        file_rel = f"assets/{ns}/sounds/{path}.ogg"
        changes[file_rel] = ogg
        sound = {"name": f"{ns}:{path}"}
        if stream:
            sound["stream"] = True
        entry = dict(entry)
        entry["sounds"] = list(entry.get("sounds", [])) + [sound]
        if subtitle:
            key = f"subtitles.{ns}.{name}"
            entry["subtitle"] = key
            lang_rel = f"assets/{ns}/lang/en_us.json"
            lang = read_json(target, lang_rel)
            lang[key] = subtitle
            changes[lang_rel] = dump_json(lang)
        elif mode == "replace":
            entry.pop("subtitle", None)
        sounds[name] = entry
        changes[sounds_rel] = dump_json(sounds)
        if info["kind"] in ("pack", "zip"):
            # Make sure the pack.mcmeta is there and readable.
            if target.read("pack.mcmeta") is None:
                changes["pack.mcmeta"] = dump_json(PACK_MCMETA)
        target.apply(changes)
    remember_target(info["path"], ns)
    return {
        "ok": True, "file": file_rel, "event": f"{ns}:{name}",
        "command": f"/playsound {ns}:{name} master @s",
        "size": len(ogg), "kind": info["kind"],
    }


def delete_sound(data):
    target, _ = resolve_target(str(data.get("target", "")))
    ns, name = str(data.get("namespace", "")), str(data.get("event", ""))
    with write_lock:
        sounds_rel = f"assets/{ns}/sounds.json"
        sounds = read_json(target, sounds_rel)
        entry = sounds.pop(name, None)
        if entry is None:
            raise UserError("Ese sonido ya no existe.")
        changes = {sounds_rel: dump_json(sounds)}
        still_used = set()
        for other in sounds.values():
            for s in other.get("sounds", []) if isinstance(other, dict) else []:
                still_used.add(sound_file_rel(s if isinstance(s, str) else s.get("name", ""), ns))
        for s in entry.get("sounds", []):
            if isinstance(s, dict) and s.get("type") == "event":
                continue
            rel = sound_file_rel(s if isinstance(s, str) else s.get("name", ""), ns)
            if rel not in still_used and rel.startswith("assets/"):
                changes[rel] = None
        sub = entry.get("subtitle")
        if sub:
            lang_rel = f"assets/{ns}/lang/en_us.json"
            lang = read_json(target, lang_rel)
            if sub in lang:
                lang.pop(sub)
                changes[lang_rel] = dump_json(lang)
        target.apply(changes)
    return {"ok": True}


def create_pack(data):
    parent = Path(str(data.get("dir", "")))
    name = re.sub(r'[<>:"/\\|?*]', "", str(data.get("name", "")).strip())
    if not parent.is_dir():
        raise UserError("Elige una carpeta válida donde crear el pack.")
    if not name:
        raise UserError("Ponle un nombre al pack.")
    dest = parent / name
    if dest.exists():
        raise UserError("Ya existe algo con ese nombre ahí.")
    dest.mkdir()
    meta = json.loads(json.dumps(PACK_MCMETA))
    meta["pack"]["description"] = str(data.get("description", "")).strip() or f"{name} — sonidos"
    (dest / "pack.mcmeta").write_bytes(dump_json(meta))
    ns = str(data.get("namespace", "")).strip().lower()
    if ns and NAMESPACE_RE.match(ns):
        (dest / "assets" / ns).mkdir(parents=True)
    remember_target(str(dest), ns or None)
    return str(dest)


def detected_packs():
    """Resource packs in known launcher instances."""
    appdata = Path(os.environ.get("APPDATA", ""))
    home = Path.home()
    sources = [("Minecraft", appdata / ".minecraft" / "resourcepacks")]
    for base in (appdata / "PrismLauncher" / "instances", appdata / "MultiMC" / "instances"):
        if base.is_dir():
            for inst in sorted(base.iterdir()):
                for sub in ("minecraft", ".minecraft"):
                    rp = inst / sub / "resourcepacks"
                    if rp.is_dir():
                        sources.append((inst.name, rp))
    for base in (home / "curseforge" / "minecraft" / "Instances",
                 appdata / "ModrinthApp" / "profiles", appdata / "com.modrinth.theseus" / "profiles"):
        if base.is_dir():
            for inst in sorted(base.iterdir()):
                rp = inst / "resourcepacks"
                if rp.is_dir():
                    sources.append((inst.name, rp))
    groups = []
    for label, rp in sources:
        if not rp.is_dir():
            continue
        packs = []
        for item in sorted(rp.iterdir(), key=lambda x: x.name.lower()):
            if item.is_dir() and (item / "pack.mcmeta").is_file():
                packs.append({"path": str(item), "name": item.name, "kind": "pack"})
            elif item.suffix.lower() == ".zip":
                packs.append({"path": str(item), "name": item.stem, "kind": "zip"})
        groups.append({"label": label, "dir": str(rp), "packs": packs})
    return groups


# ---------------------------------------------------------------- dialogs

def native_dialog(kind, initial=None):
    import tkinter as tk
    from tkinter import filedialog
    with dialog_lock:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        try:
            opts = {"parent": root}
            if initial and Path(initial).is_dir():
                opts["initialdir"] = initial
            if kind == "zip":
                path = filedialog.askopenfilename(
                    title="Elige un resource pack .zip",
                    filetypes=[("Resource pack", "*.zip")], **opts)
            elif kind == "parent":
                path = filedialog.askdirectory(title="¿Dónde crear el pack nuevo?", **opts)
            else:
                path = filedialog.askdirectory(
                    title="Elige la carpeta del resource pack o del proyecto del mod", **opts)
        finally:
            root.destroy()
    return str(Path(path)) if path else None


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data, ctype):
        """Serve bytes with HTTP Range support so <audio> can seek."""
        total = len(data)
        rng = self.headers.get("Range")
        start, end = 0, total - 1
        status = 200
        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        if m and total:
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else total - 1
            else:
                start = max(0, total - int(m.group(2) or 0))
            end = min(end, total - 1)
            status = 206
        chunk = data[start:end + 1]
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        self.wfile.write(chunk)

    def origin_ok(self):
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        allowed = (f"{HOST}:{PORT}", f"localhost:{PORT}")
        return host in allowed and (origin is None or origin in [f"http://{h}" for h in allowed])

    def do_GET(self):
        if not self.origin_ok():
            return self.send_json(403, {"error": "Forbidden"})
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        parts = url.path.strip("/").split("/")
        try:
            if url.path in ("/", "/index.html"):
                return self.send_bytes(INDEX_FILE.read_bytes(), "text/html; charset=utf-8")
            if parts[:2] == ["api", "jobs"] and len(parts) == 3:
                with jobs_lock:
                    job = dict(jobs.get(parts[2], {}))
                return self.send_json(200 if job else 404, job or {"error": "No encontrado"})
            if parts[:2] == ["api", "media"] and len(parts) == 3:
                audio = cached_audio(parts[2]) if VIDEO_ID_RE.match(parts[2]) else None
                if not audio:
                    return self.send_json(404, {"error": "No encontrado"})
                ctype = "audio/mp4" if audio.suffix in (".m4a", ".mp4") else "audio/webm"
                return self.send_bytes(audio.read_bytes(), ctype)
            if url.path == "/api/state":
                cfg = load_config()
                recents = []
                for path in cfg.get("recents", []):
                    if Path(path).exists():
                        recents.append({"path": path, "name": Path(path).stem if path.lower().endswith(".zip") else Path(path).name,
                                        "kind": "zip" if path.lower().endswith(".zip") else "folder"})
                return self.send_json(200, {"recents": recents, "detected": detected_packs(),
                                            "last": cfg.get("last_target")})
            if url.path == "/api/target":
                target, info = resolve_target(q.get("path", ""))
                ns = q.get("ns") or info["namespace"]
                info["namespace"] = ns
                info["sounds"] = list_sounds(target, ns) if NAMESPACE_RE.match(ns) else []
                return self.send_json(200, info)
            if url.path == "/api/sound":
                target, _ = resolve_target(q.get("path", ""))
                data = target.read(sound_file_rel(q.get("name", ""), q.get("ns", "minecraft")))
                if data is None:
                    return self.send_json(404, {"error": "Archivo no encontrado"})
                return self.send_bytes(data, "audio/ogg")
        except UserError as exc:
            return self.send_json(400, {"error": str(exc)})
        self.send_json(404, {"error": "No encontrado"})

    def do_POST(self):
        if urlparse(self.path).path == "/api/bye" and self.origin_ok():
            # Sent by navigator.sendBeacon when the window closes or reloads;
            # a reload heartbeats again within the remaining few seconds.
            last_heartbeat[0] = time.time() - HEARTBEAT_TIMEOUT + 8
            self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
            return self.send_json(200, {"ok": True})
        if not self.origin_ok() or self.headers.get("X-Studio") != "1":
            return self.send_json(403, {"error": "Forbidden"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(min(length, 100_000)) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self.send_json(400, {"error": "JSON inválido"})
        path = urlparse(self.path).path
        try:
            if path == "/api/heartbeat":
                last_heartbeat[0] = time.time()
                return self.send_json(200, {"ok": True})
            if path == "/api/fetch":
                url = validate_url(str(data.get("url", "")))
                if not url:
                    return self.send_json(400, {"error": "Pega un enlace de YouTube válido."})
                job_id = uuid.uuid4().hex
                with jobs_lock:
                    jobs[job_id] = {"id": job_id, "status": "starting", "percent": 0.0}
                threading.Thread(target=run_fetch, args=(job_id, url), daemon=True).start()
                return self.send_json(202, {"id": job_id})
            if path == "/api/pick":
                kind = data.get("kind", "folder")
                return self.send_json(200, {"path": native_dialog(kind, data.get("initial"))})
            if path == "/api/add":
                return self.send_json(200, add_sound(data))
            if path == "/api/delete":
                return self.send_json(200, delete_sound(data))
            if path == "/api/newpack":
                return self.send_json(200, {"path": create_pack(data)})
            if path == "/api/reveal":
                p = Path(str(data.get("path", "")))
                if p.exists():
                    if p.is_file():
                        subprocess.Popen(["explorer", "/select,", str(p)])
                    else:
                        os.startfile(str(p))
                return self.send_json(200, {"ok": True})
            if path == "/api/forget":
                cfg = load_config()
                cfg["recents"] = [r for r in cfg.get("recents", []) if r != data.get("path")]
                save_config(cfg)
                return self.send_json(200, {"ok": True})
        except UserError as exc:
            return self.send_json(400, {"error": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            return self.send_json(400, {"error": f"Datos inválidos: {exc}"})
        except OSError as exc:
            return self.send_json(500, {"error": f"No se pudo escribir: {exc}"})
        self.send_json(404, {"error": "No encontrado"})


# ---------------------------------------------------------------- launch

def find_edge():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            p = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if p.is_file():
                return str(p)
    return shutil.which("msedge")


def open_window():
    edge = find_edge()
    if not edge:
        import webbrowser
        webbrowser.open(ORIGIN)
        return None
    profile = DATA_DIR / "window"
    return subprocess.Popen([
        edge, f"--app={ORIGIN}", f"--user-data-dir={profile}",
        "--window-size=1240,860", "--no-first-run", "--no-default-browser-check",
        "--disable-features=Translate", "--autoplay-policy=no-user-gesture-required",
    ])


def already_running():
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST, PORT)) == 0


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if already_running():
        open_window()
        return
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if os.environ.get("STUDIO_NO_WINDOW") == "1":
        print(f"Sound Studio en {ORIGIN}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return
    open_window()
    # Quit once the window has been closed (no heartbeat for a while).
    # Background windows throttle timers, so allow a long gap; closing the
    # window sends /api/bye, which shortens the wait to a few seconds.
    last_heartbeat[0] = time.time() + 30  # grace period while the window opens
    while time.time() - last_heartbeat[0] < HEARTBEAT_TIMEOUT:
        time.sleep(2)
    server.shutdown()


if __name__ == "__main__":
    main()
