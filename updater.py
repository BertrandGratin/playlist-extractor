"""Automatic updates of the packaged app, from GitHub releases.

At launch, the app compares its version with the latest published release. If a newer one
exists, it downloads the zip for this computer, replaces its own file and restarts. If anything
goes wrong (no internet, GitHub unavailable…), it simply starts the current version.
"""
import os
import platform
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import requests

try:
    from _version import VERSION
except ImportError:
    VERSION = "dev"

REPO = "BertrandGratin/playlist-extractor"
LATEST_URL = os.environ.get("EXTRACTEUR_UPDATE_URL", f"https://api.github.com/repos/{REPO}/releases/latest")


def _parse(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def _asset_name():
    if sys.platform == "win32":
        return "Playlist-Extractor-Windows.zip"
    if sys.platform == "darwin":
        return "Playlist-Extractor-Mac-AppleSilicon.zip" if platform.machine() == "arm64" else "Playlist-Extractor-Mac-Intel.zip"
    return "Playlist-Extractor-Linux.zip"


def _relaunch(exe):
    # Tell PyInstaller to start from scratch in the new process.
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    if sys.platform == "win32":
        subprocess.Popen([exe], env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
        sys.exit(0)
    os.execve(exe, [exe], env)


def cleanup_old():
    """Deletes the old executable left behind by a previous Windows update."""
    old = Path(sys.executable + ".old")
    if old.exists():
        try:
            old.unlink()
        except OSError:
            pass


def update_if_needed():
    """Updates and restarts the app if a newer version exists. Does nothing when run from source."""
    if not getattr(sys, "frozen", False) or VERSION == "dev":
        return
    cleanup_old()
    try:
        release = requests.get(LATEST_URL, timeout=6, headers={"Accept": "application/vnd.github+json"}).json()
        latest = release.get("tag_name", "")
        if _parse(latest) <= _parse(VERSION):
            return
        asset = next((a for a in release.get("assets") or [] if a.get("name") == _asset_name()), None)
        if not asset:
            return

        print(f"\n  New version {latest} available (you have {VERSION}). Updating…", flush=True)
        exe = Path(sys.executable)
        with tempfile.TemporaryDirectory() as tmp:
            zpath = Path(tmp) / "update.zip"
            with requests.get(asset["browser_download_url"], stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(zpath, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            with zipfile.ZipFile(zpath) as z:
                member = next(n for n in z.namelist() if Path(n).name.startswith(("Playlist-Extractor", "Extracteur-Playlist")))
                new = exe.with_name(exe.name + ".new")
                with z.open(member) as src, open(new, "wb") as dst:
                    dst.write(src.read())
        os.chmod(new, 0o755)
        if sys.platform == "win32":
            # Windows won't let a running .exe be overwritten, but it can be renamed.
            os.replace(exe, str(exe) + ".old")
            try:
                os.replace(new, exe)
            except OSError:
                os.replace(str(exe) + ".old", exe)  # put the old version back
                raise
        else:
            os.replace(new, exe)
        print(f"  Updated to {latest}, restarting…\n", flush=True)
        _relaunch(str(exe))
    except SystemExit:
        raise
    except Exception as exc:  # no internet, GitHub unavailable, missing permissions…
        print(f"  (Couldn't check for updates: {exc})")
