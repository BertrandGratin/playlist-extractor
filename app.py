"""Extracteur de playlist YouTube : liste le titre et le lien de chaque vidéo."""
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

app = Flask(__name__)
# Dossier des fichiers fournis (différent quand l'appli est empaquetée par PyInstaller).
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))


def free_port(preferred=8765):
    """Port libre (le 5000 est pris par AirPlay sur Mac)."""
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
        return jsonify(error="Colle un lien de playlist YouTube."), 400

    opts = {"extract_flat": "in_playlist", "quiet": True, "skip_download": True, "noplaylist": False}
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # yt-dlp lève des erreurs variées
        msg = str(exc).replace("ERROR: ", "")
        return jsonify(error=f"Impossible de lire cette playlist : {msg}"), 400

    entries = info.get("entries") or []
    videos = []
    for e in entries:
        if not e:
            continue
        vid = e.get("id")
        title = e.get("title") or "(sans titre)"
        if title in ("[Private video]", "[Deleted video]"):
            continue
        videos.append({
            "title": title,
            "url": f"https://www.youtube.com/watch?v={vid}" if vid else e.get("url"),
            "channel": e.get("channel") or e.get("uploader") or "",
            "duration": e.get("duration"),
        })

    if not videos and info.get("id") and not entries:
        return jsonify(error="Ce lien ne ressemble pas à une playlist."), 400

    # Images de la playlist (miniatures YouTube), de la plus grande à la plus petite, sans doublon de taille.
    thumbs, sizes = [], set()
    for t in sorted(info.get("thumbnails") or [], key=lambda t: -(t.get("width") or 0)):
        if t.get("url") and t.get("width") not in sizes:
            sizes.add(t.get("width"))
            thumbs.append(t["url"])

    return jsonify(title=info.get("title") or "Playlist", count=len(videos), videos=videos,
                   owner=info.get("uploader") or info.get("channel") or "",
                   description=info.get("description") or "", thumbnails=thumbs[:4])


@app.post("/api/enrich")
def enrich_video():
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(enrich(body.get("title", ""), body.get("channel", "")))
    except Exception as exc:
        return jsonify(found=False, error=str(exc))


IMAGE_HOSTS = ("dzcdn.net", "deezer.com", "theaudiodb.com", "wikimedia.org", "coverartarchive.org", "archive.org",
               "genius.com", "ytimg.com", "ggpht.com", "googleusercontent.com")


@app.get("/api/image")
def image():
    """Télécharge une image (cover, photo d'artiste) avec un nom de fichier propre."""
    url = request.args.get("url", "")
    host = urlparse(url).hostname or ""
    if not any(host == h or host.endswith("." + h) for h in IMAGE_HOSTS):
        return "Hôte non autorisé", 400
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
    except requests.RequestException as exc:
        return f"Téléchargement impossible : {exc}", 502
    ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
    ext = {"image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(ctype, "jpg")
    name = re.sub(r'[\\/:*?"<>|]+', "_", request.args.get("name") or "image").strip() or "image"
    resp = Response(r.content, mimetype=ctype)
    resp.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{requests.utils.quote(name)}.{ext}"
    return resp


if __name__ == "__main__":
    PORT = free_port()
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    print(f"\n  Extracteur de playlist lancé sur http://127.0.0.1:{PORT}")
    print("  Laisse cette fenêtre ouverte pendant que tu utilises l'appli ; ferme-la pour arrêter.\n")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
