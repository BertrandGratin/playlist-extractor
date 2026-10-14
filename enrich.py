"""Enrichissement d'une vidéo : album, cover, artistes, date de sortie, label, pays, genres, images.

Sources gratuites sans clé API :
- Deezer        : morceau, album, cover, date de sortie, label, genres, photo d'artiste
- MusicBrainz   : pays d'origine de l'artiste (+ genres de secours, lien Wikidata)
- TheAudioDB    : photos supplémentaires de l'artiste (clé publique de test "123")
- Wikidata      : photo Wikimedia Commons en secours
"""
import re
import threading
import time
from difflib import SequenceMatcher
from functools import lru_cache
from urllib.parse import quote

import requests

UA = "PlaylistExtractor/1.0 (projet perso local)"
session = requests.Session()
session.headers["User-Agent"] = UA
TIMEOUT = 12

# MusicBrainz impose 1 requête par seconde.
_mb_lock = threading.Lock()
_mb_last = [0.0]

NOISE = re.compile(
    r"\s*[\(\[][^\)\]]*(official|officiel|clip|video|vidéo|audio|lyrics?|paroles|visuali[sz]er|"
    r"hd|hq|4k|remaster(ed)?|live|explicit|color coded)[^\)\]]*[\)\]]",
    re.I,
)
FEAT = re.compile(r"\s+(feat\.?|ft\.?|featuring|avec|x|&|,)\s+.*$", re.I)


def _get(url, **params):
    try:
        r = session.get(url, params=params or None, timeout=TIMEOUT)
        if r.ok:
            return r.json()
    except (requests.RequestException, ValueError):
        pass
    return None


def _mb(path, **params):
    with _mb_lock:
        wait = 1.05 - (time.time() - _mb_last[0])
        if wait > 0:
            time.sleep(wait)
        _mb_last[0] = time.time()
        return _get(f"https://musicbrainz.org/ws/2/{path}", fmt="json", **params)


def _sim(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def parse_title(title, channel=""):
    """Devine (artiste, titre) depuis le titre YouTube et la chaîne."""
    clean = NOISE.sub("", title).strip()
    clean = re.sub(r"\s*\|.*$", "", clean).strip()  # « Titre | Album » etc.
    if channel.endswith(" - Topic"):  # chaînes auto-générées « Artiste - Topic »
        return channel[:-8].strip(), clean
    parts = re.split(r"\s+[-–—]\s+", clean, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip().strip('"“”')
    artist = re.sub(r"(VEVO|Official|Officiel)$", "", channel, flags=re.I).strip()
    return artist, clean


def _search_deezer(artist, track):
    main_artist = FEAT.sub("", artist).strip()
    short_track = FEAT.sub("", re.sub(r"\s*[\(\[].*?[\)\]]", "", track)).strip() or track
    queries = []
    if artist:
        queries.append(f'artist:"{artist}" track:"{short_track}"')
        if main_artist != artist:
            queries.append(f'artist:"{main_artist}" track:"{short_track}"')
        queries.append(f"{main_artist} {short_track}")
    queries.append(short_track)
    for q in queries:
        data = (_get("https://api.deezer.com/search", q=q, limit=10) or {}).get("data") or []
        scored = []
        for t in data:
            score = _sim(t["title"], short_track) + (max(_sim(t["artist"]["name"], artist), _sim(t["artist"]["name"], main_artist)) if artist else 0)
            scored.append((score, t))
        scored = [s for s in scored if s[0] > (1.1 if artist else 0.6)]
        if scored:
            scored.sort(key=lambda s: -s[0])
            return [t for _, t in scored[:5]]
    return []


@lru_cache(maxsize=2048)
def deezer_album(album_id):
    return _get(f"https://api.deezer.com/album/{album_id}") or {}


@lru_cache(maxsize=2048)
def deezer_track(track_id):
    return _get(f"https://api.deezer.com/track/{track_id}") or {}


def _pick_album(candidates):
    """Parmi les meilleurs résultats, préfère l'album original (pas une compilation), le plus ancien."""
    best = None
    for t in candidates[:3]:
        alb = deezer_album(t["album"]["id"])
        if not alb:
            continue
        key = (alb.get("record_type") == "compile", alb.get("release_date") or "9999")
        if best is None or key < best[0]:
            best = (key, t, alb)
    if best:
        return best[1], best[2]
    t = candidates[0]
    return t, {}


def _commons_url(fname, width=1000):
    return "https://commons.wikimedia.org/wiki/Special:FilePath/" + quote(fname.replace(" ", "_")) + f"?width={width}"


def _commons_category(category, limit=10):
    """Photos d'une catégorie Wikimedia Commons (souvent plusieurs photos de concerts, portraits…)."""
    data = _get(
        "https://commons.wikimedia.org/w/api.php", action="query", format="json",
        generator="categorymembers", gcmtitle="Category:" + category, gcmtype="file", gcmlimit=30,
        prop="imageinfo", iiprop="url|mime", iiurlwidth=1000,
    ) or {}
    out = []
    for page in ((data.get("query") or {}).get("pages") or {}).values():
        ii = (page.get("imageinfo") or [{}])[0]
        if ii.get("mime") in ("image/jpeg", "image/png", "image/webp"):
            out.append(ii.get("thumburl") or ii.get("url"))
    return out[:limit]


def _add_genres(bucket, names, source):
    """Fusionne des genres de plusieurs sources ; un genre cité par plusieurs sources remonte en tête."""
    for n in names:
        n = (n or "").strip()
        if not n or len(n) > 40:
            continue
        key = re.sub(r"\band\b|[\s\-_&/'.]+", "", n.lower())
        g = bucket.setdefault(key, {"name": n, "sources": [], "score": 0})
        if source not in g["sources"]:
            g["sources"].append(source)
            g["score"] += 1


def _mb_genres(entity):
    return [g["name"] for g in sorted((entity or {}).get("genres") or [], key=lambda g: -g.get("count", 0))]


@lru_cache(maxsize=1024)
def artist_info(name, deezer_picture=""):
    """Pays d'origine, genres et toutes les photos trouvées d'un artiste."""
    info = {"name": name, "country": None, "country_label": None, "images": [], "genres": [], "audiodb_genres": []}
    seen = set()

    def add_img(url, source):
        if url and url not in seen:
            seen.add(url)
            info["images"].append({"url": url, "source": source})

    add_img(deezer_picture, "Deezer")

    mb = (_mb("artist/", query=f'artist:"{name}"', limit=3) or {}).get("artists") or []
    mb = [a for a in mb if _sim(a.get("name", ""), name) > 0.85]
    if mb:
        a = mb[0]
        info["country"] = a.get("country")
        area = a.get("begin-area") or a.get("area") or {}
        info["country_label"] = area.get("name")
        info["mbid"] = a.get("id")

    adb = ((_get("https://www.theaudiodb.com/api/v1/json/123/search.php", s=name) or {}).get("artists") or [None])[0]
    if adb and _sim(adb.get("strArtist") or "", name) > 0.85:
        for k in ("strArtistThumb", "strArtistFanart", "strArtistFanart2", "strArtistFanart3",
                  "strArtistFanart4", "strArtistWideThumb", "strArtistCutout"):
            add_img(adb.get(k), "TheAudioDB")
        info["audiodb_genres"] = [x for x in (adb.get("strGenre"), adb.get("strStyle")) if x]
        if not info["country_label"] and adb.get("strCountry"):
            info["country_label"] = adb["strCountry"]

    if info.get("mbid"):
        full = _mb(f"artist/{info['mbid']}", inc="url-rels+genres") or {}
        info["genres"] = _mb_genres(full)[:6]
        wikidata = next((r["url"]["resource"].rsplit("/", 1)[-1] for r in full.get("relations") or []
                         if r.get("type") == "wikidata"), None)
        if wikidata:
            ent = _get("https://www.wikidata.org/wiki/Special:EntityData/" + wikidata + ".json") or {}
            claims = ((ent.get("entities") or {}).get(wikidata) or {}).get("claims") or {}
            for c in claims.get("P18", []):  # image principale
                add_img(_commons_url(c["mainsnak"]["datavalue"]["value"]), "Wikimedia")
            for c in claims.get("P373", [])[:1]:  # catégorie Commons : autres photos
                for url in _commons_category(c["mainsnak"]["datavalue"]["value"]):
                    add_img(url, "Wikimedia Commons")
    return info


@lru_cache(maxsize=2048)
def mb_release_group_genres(artist, track, album_title):
    """Genres MusicBrainz de l'album (release group) contenant ce morceau."""
    recs = (_mb("recording/", query=f'recording:"{track}" AND artist:"{artist}"', limit=10) or {}).get("recordings") or []
    best = None
    for r in recs:
        for rel in r.get("releases") or []:
            rg = rel.get("release-group") or {}
            if not rg.get("id"):
                continue
            score = _sim(rel.get("title", ""), album_title or "") + (0.5 if not rg.get("secondary-types") else 0)
            if best is None or score > best[0]:
                best = (score, rg["id"])
    if not best:
        return []
    return _mb_genres(_mb(f"release-group/{best[1]}", inc="genres"))


@lru_cache(maxsize=2048)
def audiodb_track_genres(artist, track):
    t = ((_get("https://www.theaudiodb.com/api/v1/json/123/searchtrack.php", s=artist, t=track) or {}).get("track") or [None])[0]
    return [x for x in (t.get("strGenre"), t.get("strStyle")) if x] if t else []


def enrich(title, channel=""):
    artist_guess, track_guess = parse_title(title, channel)
    candidates = _search_deezer(artist_guess, track_guess)
    if not candidates:
        return {"found": False, "guess": {"artist": artist_guess, "track": track_guess}}

    track, album = _pick_album(candidates)
    full_track = deezer_track(track["id"])

    artists = []
    for c in full_track.get("contributors") or [track["artist"]]:
        if c["name"] not in [a["name"] for a in artists]:
            artists.append({"name": c["name"], "picture": c.get("picture_xl") or ""})
    artist_details = [artist_info(a["name"], a["picture"]) for a in artists[:4]]

    alb_info = album or track["album"]
    main = artists[0]["name"]
    genres = {}
    _add_genres(genres, mb_release_group_genres(main, track["title"], alb_info.get("title")), "MusicBrainz (album)")
    _add_genres(genres, [g["name"] for g in ((album.get("genres") or {}).get("data") or [])], "Deezer")
    _add_genres(genres, audiodb_track_genres(main, track["title"]), "TheAudioDB (morceau)")
    for a in artist_details:
        _add_genres(genres, a["genres"], f"MusicBrainz ({a['name']})")
        _add_genres(genres, a["audiodb_genres"], f"TheAudioDB ({a['name']})")
    ordered = sorted(genres.values(), key=lambda g: -g["score"])  # tri stable : à égalité, ordre des sources
    genre_list = [{"name": g["name"], "sources": g["sources"]} for g in ordered[:12]]

    return {
        "found": True,
        "track": track.get("title"),
        "album": alb_info.get("title"),
        "record_type": album.get("record_type"),
        "cover": alb_info.get("cover_xl") or alb_info.get("cover_big"),
        "release_date": album.get("release_date") or full_track.get("release_date"),
        "label": album.get("label"),
        "genres": genre_list,
        "artists": artist_details,
        "deezer_url": track.get("link"),
    }
