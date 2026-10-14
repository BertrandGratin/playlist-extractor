"""Enriches a video with music info: album, cover, artists, release date, label, country, genres,
images, BPM and key.

Free sources, no API key needed:
- Deezer        : track, album, cover, release date, label, genres, artist photo, BPM, audio preview
- MusicBrainz   : artist's country of origin, album and artist genres, Wikidata link
- Genius        : artist photo and description (the website's public API, no key)
- TheAudioDB    : more artist photos, genres and styles (public test key "123")
- Wikidata / Wikipedia / Commons : more photos, fallback description
"""
import re
import threading
import time
from difflib import SequenceMatcher
from functools import lru_cache
from urllib.parse import quote

import requests

from audio_analysis import analyse

UA = "PlaylistExtractor/1.0 (projet perso local)"
session = requests.Session()
session.headers["User-Agent"] = UA
session.headers["Accept-Language"] = "en"  # genres and labels in English
TIMEOUT = 12

# MusicBrainz allows 1 request per second.
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
    """Guesses (artist, title) from the YouTube title and channel name."""
    clean = NOISE.sub("", title).strip()
    clean = re.sub(r"\s*\|.*$", "", clean).strip()  # "Title | Album" etc.
    if channel.endswith(" - Topic"):  # auto-generated "Artist - Topic" channels
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
    """Among the best matches, prefers the original album (not a compilation), the oldest one."""
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
    """Photos from a Wikimedia Commons category (often several concert photos, portraits…)."""
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
    """Merges genres from several sources; a genre cited by more sources ranks higher."""
    for n in names:
        n = (n or "").strip()
        if not n or len(n) > 40:
            continue
        key = re.sub(r"\band\b|[\s\-_&/'.]+", "", n.lower())
        g = bucket.setdefault(key, {"name": n, "sources": [], "score": 0})
        if source not in g["sources"]:
            g["sources"].append(source)
            g["score"] += 1


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _get_browser(url, **params):
    """Request with a browser User-Agent (Genius rejects generic bots)."""
    try:
        r = session.get(url, params=params or None, timeout=TIMEOUT,
                        headers={"User-Agent": BROWSER_UA, "Accept": "application/json"})
        if r.ok:
            return r.json()
    except (requests.RequestException, ValueError):
        pass
    return None


def genius_artist(name):
    """An artist's Genius photo(s), description and page."""
    data = _get_browser("https://genius.com/api/search/artist", q=name) or {}
    hits = []
    for section in (data.get("response") or {}).get("sections") or []:
        hits += [h.get("result") or {} for h in section.get("hits") or []]
    hit = next((h for h in hits if h.get("id") and _sim(h.get("name", ""), name) > 0.85), None)
    if not hit:
        return None
    full = ((_get_browser(f"https://genius.com/api/artists/{hit['id']}", text_format="plain") or {})
            .get("response") or {}).get("artist") or hit
    desc = ((full.get("description") or {}).get("plain") or "").strip()
    images = [u for u in (full.get("image_url"), full.get("header_image_url"))
              if u and "default_avatar" not in u and "default_cover" not in u]
    return {"images": images, "description": "" if desc == "?" else desc, "url": full.get("url") or hit.get("url")}


def wikipedia_summary(title, lang):
    d = _get(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/" + quote(title.replace(" ", "_"), safe="")) or {}
    if d.get("type") == "standard" and d.get("extract"):
        return {"text": d["extract"], "url": ((d.get("content_urls") or {}).get("desktop") or {}).get("page")}
    return None


def _mb_genres(entity):
    return [g["name"] for g in sorted((entity or {}).get("genres") or [], key=lambda g: -g.get("count", 0))]


@lru_cache(maxsize=1024)
def artist_info(name, deezer_picture=""):
    """An artist's country of origin, genres, descriptions and every photo found."""
    info = {"name": name, "country": None, "country_label": None, "images": [], "genres": [],
            "audiodb_genres": [], "descriptions": [], "links": {}}
    seen = set()

    def add_img(url, source):
        if url and url not in seen:
            seen.add(url)
            info["images"].append({"url": url, "source": source})

    genius = genius_artist(name)
    if genius:
        for url in genius["images"]:
            add_img(url, "Genius")
        if genius["description"]:
            info["descriptions"].append({"source": "Genius", "text": genius["description"], "url": genius["url"]})
        info["links"]["Genius"] = genius["url"]
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
            for c in claims.get("P18", []):  # main image
                add_img(_commons_url(c["mainsnak"]["datavalue"]["value"]), "Wikimedia")
            for c in claims.get("P373", [])[:1]:  # Commons category: more photos
                for url in _commons_category(c["mainsnak"]["datavalue"]["value"]):
                    add_img(url, "Wikimedia Commons")
            sitelinks = ((ent.get("entities") or {}).get(wikidata) or {}).get("sitelinks") or {}
            for lang in ("en", "fr"):
                title = (sitelinks.get(f"{lang}wiki") or {}).get("title")
                summary = title and wikipedia_summary(title, lang)
                if summary:
                    info["descriptions"].append({"source": f"Wikipedia ({lang.upper()})", **summary})
                    info["links"]["Wikipedia"] = info["links"].get("Wikipedia") or summary["url"]
                    break

    artist_genres = {}
    _add_genres(artist_genres, info["genres"], "MusicBrainz")
    _add_genres(artist_genres, info["audiodb_genres"], "TheAudioDB")
    info["all_genres"] = [g["name"] for g in sorted(artist_genres.values(), key=lambda g: -g["score"])][:8]
    return info


@lru_cache(maxsize=2048)
def mb_release_group_genres(artist, track, album_title):
    """MusicBrainz genres of the album (release group) containing this track."""
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


@lru_cache(maxsize=2048)
def track_audio_features(track_id):
    """BPM and key of a track, estimated from its 30-second Deezer preview (BPM from Deezer when it has one)."""
    t = deezer_track(track_id)
    out = {"bpm": None, "bpm_source": None, "key": None, "camelot": None}
    analysed = None
    if t.get("preview"):
        try:
            r = session.get(t["preview"], timeout=20)
            if r.ok:
                analysed = analyse(r.content)
        except requests.RequestException:
            pass
    if analysed:
        out.update(key=analysed.get("key"), camelot=analysed.get("camelot"))
        if analysed.get("bpm"):
            out.update(bpm=analysed["bpm"], bpm_source="estimated from the audio preview")
    if t.get("bpm"):
        out.update(bpm=round(t["bpm"]), bpm_source="Deezer")
    return out


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
    _add_genres(genres, audiodb_track_genres(main, track["title"]), "TheAudioDB (track)")
    album_genres = [{"name": g["name"], "sources": g["sources"]}
                    for g in sorted(genres.values(), key=lambda g: -g["score"])][:8]
    for a in artist_details:
        _add_genres(genres, a["genres"], f"MusicBrainz ({a['name']})")
        _add_genres(genres, a["audiodb_genres"], f"TheAudioDB ({a['name']})")
    ordered = sorted(genres.values(), key=lambda g: -g["score"])  # stable sort: ties keep source order
    genre_list = [{"name": g["name"], "sources": g["sources"]} for g in ordered[:12]]

    return {
        "found": True,
        "track": track.get("title"),
        "album": alb_info.get("title"),
        "album_id": alb_info.get("id") or track["album"].get("id"),
        "album_artist": (album.get("artist") or {}).get("name") or main,
        "album_genres": album_genres,
        "record_type": album.get("record_type"),
        "cover": alb_info.get("cover_xl") or alb_info.get("cover_big"),
        "release_date": album.get("release_date") or full_track.get("release_date"),
        "label": album.get("label"),
        "genres": genre_list,
        "artists": artist_details,
        "deezer_url": track.get("link"),
        **track_audio_features(track["id"]),
    }
