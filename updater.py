"""Mise à jour automatique de l'appli empaquetée, depuis les releases GitHub.

Au lancement, l'appli compare sa version à la dernière release publiée. Si une version plus
récente existe, elle télécharge le zip correspondant à l'ordinateur, remplace son propre
fichier et se relance. En cas de problème (pas d'internet, GitHub indisponible…), elle démarre
simplement la version actuelle.
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
        return "Extracteur-Playlist-Windows.zip"
    if sys.platform == "darwin":
        return "Extracteur-Playlist-Mac-AppleSilicon.zip" if platform.machine() == "arm64" else "Extracteur-Playlist-Mac-Intel.zip"
    return "Extracteur-Playlist-Linux.zip"


def _relaunch(exe):
    # Demande à PyInstaller de repartir de zéro dans le nouveau processus.
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    if sys.platform == "win32":
        subprocess.Popen([exe], env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
        sys.exit(0)
    os.execve(exe, [exe], env)


def cleanup_old():
    """Supprime l'ancien exécutable laissé par une mise à jour Windows précédente."""
    old = Path(sys.executable + ".old")
    if old.exists():
        try:
            old.unlink()
        except OSError:
            pass


def update_if_needed():
    """Met l'appli à jour puis la relance si une nouvelle version existe. Ne fait rien depuis le code source."""
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

        print(f"\n  Nouvelle version {latest} disponible (tu as {VERSION}). Mise à jour en cours…", flush=True)
        exe = Path(sys.executable)
        with tempfile.TemporaryDirectory() as tmp:
            zpath = Path(tmp) / "update.zip"
            with requests.get(asset["browser_download_url"], stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(zpath, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            with zipfile.ZipFile(zpath) as z:
                member = next(n for n in z.namelist() if Path(n).name.startswith("Extracteur-Playlist"))
                new = exe.with_name(exe.name + ".new")
                with z.open(member) as src, open(new, "wb") as dst:
                    dst.write(src.read())
        os.chmod(new, 0o755)
        if sys.platform == "win32":
            # Windows interdit d'écraser un .exe en cours d'exécution, mais autorise à le renommer.
            os.replace(exe, str(exe) + ".old")
            try:
                os.replace(new, exe)
            except OSError:
                os.replace(str(exe) + ".old", exe)  # on remet l'ancienne version en place
                raise
        else:
            os.replace(new, exe)
        print(f"  Mise à jour vers {latest} terminée, redémarrage…\n", flush=True)
        _relaunch(str(exe))
    except SystemExit:
        raise
    except Exception as exc:  # pas d'internet, GitHub indisponible, droits insuffisants…
        print(f"  (Vérification des mises à jour impossible : {exc})")
