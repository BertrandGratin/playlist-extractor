"""Playlist Extractor: lists the title and link of every video in a YouTube playlist, with music info."""
import re
import socket
import sys
import threading
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, request, send_file
from yt_dlp import YoutubeDL

from enrich import enrich, session
from updater import VERSION, update_if_needed

app = Flask(__name__)
# Folder holding the bundled files (different when the app is packaged by PyInstaller).
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))


def free_port(preferred=8765):
    """A free port (5000 is taken by AirPlay on Macs)."""
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue


@app.get("/")
def index():
    return send_file(BASE / "index.html")


@app.post("/api/playlist")
def playlist():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url:
        return jsonify(error="Paste a YouTube playlist link."), 400

    opts = {"extract_flat": "in_playlist", "quiet": True, "skip_download": True, "noplaylist": False}
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # yt-dlp raises many kinds of errors
        msg = str(exc).replace("ERROR: ", "")
        return jsonify(error=f"Couldn't read this playlist: {msg}"), 400

    entries = info.get("entries") or []
    videos = []
    for e in entries:
        if not e:
            continue
        vid = e.get("id")
        title = e.get("title") or "(untitled)"
        if title in ("[Private video]", "[Deleted video]"):
            continue
        videos.append({
            "title": title,
            "url": f"https://www.youtube.com/watch?v={vid}" if vid else e.get("url"),
            "channel": e.get("channel") or e.get("uploader") or "",
            "duration": e.get("duration"),
        })

    if not videos and info.get("id") and not entries:
        return jsonify(error="This link doesn't look like a playlist."), 400

    # Playlist images (YouTube thumbnails), largest first, one per size.
    thumbs, sizes = [], set()
    for t in sorted(info.get("thumbnails") or [], key=lambda t: -(t.get("width") or 0)):
        if t.get("url") and t.get("width") not in sizes:
            sizes.add(t.get("width"))
            thumbs.append(t["url"])

    return jsonify(title=info.get("title") or "Playlist", count=len(videos), videos=videos,
                   owner=info.get("uploader") or info.get("channel") or "",
                   description=info.get("description") or "", thumbnails=thumbs[:4])


@app.get("/api/version")
def version():
    return jsonify(version=VERSION)


@app.post("/api/enrich")
def enrich_video():
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(enrich(body.get("title", ""), body.get("channel", ""), body.get("url", "")))
    except Exception as exc:
        return jsonify(found=False, error=str(exc))


IMAGE_HOSTS = ("dzcdn.net", "deezer.com", "theaudiodb.com", "wikimedia.org", "coverartarchive.org", "archive.org",
               "genius.com", "ytimg.com", "ggpht.com", "googleusercontent.com")


@app.get("/api/image")
def image():
    """Downloads an image (cover, artist photo) with a clean file name."""
    url = request.args.get("url", "")
    host = urlparse(url).hostname or ""
    if not any(host == h or host.endswith("." + h) for h in IMAGE_HOSTS):
        return "Host not allowed", 400
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
    except requests.RequestException as exc:
        return f"Download failed: {exc}", 502
    ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
    ext = {"image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(ctype, "jpg")
    name = re.sub(r'[\\/:*?"<>|]+', "_", request.args.get("name") or "image").strip() or "image"
    resp = Response(r.content, mimetype=ctype)
    resp.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{requests.utils.quote(name)}.{ext}"
    return resp


if __name__ == "__main__":
    update_if_needed()
    PORT = free_port()
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    print(f"\n  Playlist Extractor {VERSION} running at http://127.0.0.1:{PORT}")
    print("  Keep this window open while you use the app; close it to quit.\n")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
