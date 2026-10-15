"""Enriches a video with music info: album, cover, artists, release date, label, country, genres,
images, BPM and key.

Free sources, no API key needed:
- Deezer        : track, album, cover, release date, label, genres, artist photo, BPM, audio preview
- MusicBrainz   : artist's country of origin, album and artist genres, Wikidata link
- Genius        : artist photo and description (the website's public API, no key)
- TheAudioDB    : more artist photos, genres and styles (public test key "123")
- Wikidata / Wikipedia / Commons : more photos, fallback description
- YouTube       : the whole song's audio, analysed for BPM and key (falls back to the Deezer preview)
- GetSongBPM    : BPM and key cross-check (needs a free API key, added at build time)
"""
import html
import os
import re
import tempfile
import threading
import time
from difflib import SequenceMatcher
from functools import lru_cache
from urllib.parse import quote

import requests

import audio_analysis
from audio_analysis import NOTES, analyse, analyse_samples, camelot

try:
    from _secrets import GETSONGBPM_API_KEY
except ImportError:
    GETSONGBPM_API_KEY = ""
GETSONGBPM_API_KEY = os.environ.get("GETSONGBPM_API_KEY", GETSONGBPM_API_KEY)

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


# Titles that say nothing about the song: matching them on the title alone picks a random artist's "Untitled".
GENERIC_TITLE = re.compile(r"^(untitled( track)?|no title|sans titre|unknown|intro|outro|interlude|skit|demo|"
                           r"instrumental|(track|piste|titre) ?\d*|\d+)$", re.I)


def parse_title_ex(title, channel=""):
    """Guesses (artist, title, artist_is_reliable) from the YouTube title and channel name.

    The artist is reliable when it comes from an "Artist - Topic" channel or an "Artist - Title" video title;
    a plain channel name is only a guess (it may be a label, a fan account or the person who uploaded it).
    """
    clean = NOISE.sub("", title).strip()
    clean = re.sub(r"\s*\|.*$", "", clean).strip()  # "Title | Album" etc.
    if channel.endswith(" - Topic"):  # auto-generated "Artist - Topic" channels
        return channel[:-8].strip(), clean, True
    parts = re.split(r"\s+[-–—]\s+", clean, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip().strip('"“”'), True
    artist = re.sub(r"(VEVO|Official|Officiel)$", "", channel, flags=re.I).strip()
    return artist, clean, False


def parse_title(title, channel=""):
    """Guesses (artist, title) from the YouTube title and channel name."""
    return parse_title_ex(title, channel)[:2]


def _search_deezer(artist, track, artist_reliable=True):
    """Deezer tracks matching the guessed artist and title, best first ([] when nothing matches well enough).

    A candidate must match the title, and the artist too whenever one is known: summing both scores used to let
    a perfect title with an unrelated artist through (e.g. "(untitled)" → Vast's "Untitled"). Searching on the
    title alone is only allowed when the artist is a mere guess and the title is distinctive.
    """
    main_artist = FEAT.sub("", artist).strip()
    short_track = FEAT.sub("", re.sub(r"\s*[\(\[].*?[\)\]]", "", track)).strip() or track.strip("()[] ")
    if not short_track:
        return []
    generic = bool(GENERIC_TITLE.match(short_track))
    artist_sim = lambda t: max(_sim(t["artist"]["name"], artist), _sim(t["artist"]["name"], main_artist)) if artist else 0
    queries = []
    if artist:
        queries.append((f'artist:"{artist}" track:"{short_track}"', True))
        if main_artist != artist:
            queries.append((f'artist:"{main_artist}" track:"{short_track}"', True))
        queries.append((f"{main_artist} {short_track}", True))
    if not generic and (not artist or not artist_reliable) and len(short_track) >= 4:
        queries.append((short_track, False))
    for q, need_artist in queries:
        data = (_get("https://api.deezer.com/search", q=q, limit=10) or {}).get("data") or []
        scored = []
        for t in data:
            ts = _sim(t["title"], short_track)
            if need_artist:
                a = artist_sim(t)
                if ts >= 0.6 and a >= 0.7:
                    scored.append((ts + a, t))
            elif ts >= 0.9:
                scored.append((ts, t))
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


# ---------- Shared helpers for Wikidata, Discogs and MusicBrainz links ----------
@lru_cache(maxsize=1024)
def wikidata_entity(qid):
    return ((_get("https://www.wikidata.org/wiki/Special:EntityData/" + qid + ".json") or {}).get("entities") or {}).get(qid) or {}


def _wd_values(ent, prop):
    out = []
    for c in (ent.get("claims") or {}).get(prop, []):
        v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if v is not None:
            out.append(v)
    return out


def _wd_names(qids):
    """English (else French) names of Wikidata items."""
    qids = [q for q in qids if q][:12]
    if not qids:
        return []
    d = _get("https://www.wikidata.org/w/api.php", action="wbgetentities", ids="|".join(qids), props="labels",
             languages="en|fr", format="json") or {}
    out = []
    for q in qids:
        labels = ((d.get("entities") or {}).get(q) or {}).get("labels") or {}
        name = (labels.get("en") or labels.get("fr") or {}).get("value")
        if name:
            out.append(name)
    return out


def _wd_year(ent, prop):
    for v in _wd_values(ent, prop):
        m = re.match(r"[+-]?(\d{4})", (v or {}).get("time", "")) if isinstance(v, dict) else None
        if m:
            return m.group(1)
    return None


def _wikipedia_descriptions(ent):
    """Wikipedia summaries (English and French) of a Wikidata item, plus the article's main image."""
    out, image = [], None
    sitelinks = ent.get("sitelinks") or {}
    for lang in ("en", "fr"):
        title = (sitelinks.get(f"{lang}wiki") or {}).get("title")
        d = title and _get(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/" + quote(title.replace(" ", "_"), safe=""))
        if d and d.get("type") == "standard" and d.get("extract"):
            out.append({"source": f"Wikipedia ({lang.upper()})", "text": d["extract"],
                        "url": ((d.get("content_urls") or {}).get("desktop") or {}).get("page")})
            image = image or (d.get("originalimage") or {}).get("source")
    return out, image


DISCOGS_MARKUP = [
    (re.compile(r"\[(?:a|l|m|r)=([^\]]+)\]"), r"\1"),       # [a=Artist Name] -> Artist Name
    (re.compile(r"\[(?:a|l|m|r)\d+\]"), ""),                # [a12345] (an id with no name)
    (re.compile(r"\[url=[^\]]*\]([^\[]*)\[/url\]"), r"\1"),
    (re.compile(r"\[/?(?:b|i|u)\]"), ""),
]


def _discogs_text(text):
    for rx, rep in DISCOGS_MARKUP:
        text = rx.sub(rep, text or "")
    return re.sub(r"\r\n?", "\n", text).strip()


@lru_cache(maxsize=1024)
def discogs(kind, did):
    """Discogs artist or label ("artists"/"labels", id): profile text, images, page (public API, no key)."""
    d = _get(f"https://api.discogs.com/{kind}/{did}") or {}
    if not d:
        return None
    return {
        "profile": _discogs_text(d.get("profile")),
        "images": [i.get("uri") for i in d.get("images") or [] if i.get("uri")],
        "url": d.get("uri"),
        "parent": (d.get("parent_label") or {}).get("name"),
        "sublabels": [x.get("name") for x in d.get("sublabels") or [] if x.get("name")][:12],
        "members": [x.get("name") for x in d.get("members") or [] if x.get("name")][:12],
    }


LINK_NAMES = [  # (part of the URL, name shown)
    ("bandcamp.com", "Bandcamp"), ("soundcloud.com", "SoundCloud"), ("discogs.com", "Discogs"),
    ("allmusic.com", "AllMusic"), ("last.fm", "Last.fm"), ("instagram.com", "Instagram"), ("twitter.com", "X"),
    ("x.com", "X"), ("facebook.com", "Facebook"), ("youtube.com", "YouTube"), ("tiktok.com", "TikTok"),
    ("open.spotify.com", "Spotify"), ("music.apple.com", "Apple Music"), ("rateyourmusic.com", "RateYourMusic"),
]


def _mb_links(relations):
    """Useful links from MusicBrainz relations: official site, Bandcamp, Discogs, socials…"""
    links, ids = {}, {}
    for r in relations or []:
        url = (r.get("url") or {}).get("resource") or ""
        if not url:
            continue
        if r.get("type") == "wikidata":
            ids["wikidata"] = url.rstrip("/").rsplit("/", 1)[-1]
            continue
        if r.get("type") == "official homepage":
            links.setdefault("Official site", url)
            continue
        m = re.search(r"discogs\.com/(artist|label)/(\d+)", url)
        if m:
            ids["discogs"] = m.group(2)
        for part, name in LINK_NAMES:
            if part in url:
                links.setdefault(name, url)
                break
    return links, ids


BANDCAMP_SEARCH = "https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic"


@lru_cache(maxsize=2048)
def bandcamp_search(text, kind):
    """Bandcamp's own search box: kind "b" for artists and labels, "a" for albums."""
    try:
        r = session.post(BANDCAMP_SEARCH, json={"search_text": text, "search_filter": kind, "full_page": False,
                                                "fan_id": None}, timeout=10)
        r.raise_for_status()
        return tuple((r.json().get("auto") or {}).get("results") or [])
    except (requests.RequestException, ValueError, AttributeError):
        return ()


def _bc_image(url):
    """Bandcamp image in large size (the search gives a small one)."""
    return re.sub(r"_\d+\.(jpg|png)$", r"_10.\1", url) if url else None


def bandcamp_page(name, label):
    """Bandcamp page of the artist (label=False) or label (label=True) with this exact name."""
    hits = [r for r in bandcamp_search(name, "b") if r.get("type") == "b" and _sim(r.get("name") or "", name) > 0.9
            and not (label and r.get("is_label") is False)]  # a label is never matched to a plain artist page
    hits.sort(key=lambda r: bool(r.get("is_label")) != label)  # an artist page first for an artist, a label page for a label
    for r in hits:
        url = (r.get("item_url_root") or r.get("item_url_path") or "").rstrip("/")
        if url.startswith("http"):
            return {"url": url, "image": _bc_image(r.get("img")), "location": r.get("location")}
    return None


@lru_cache(maxsize=2048)
def bandcamp_album(artist, title):
    """Bandcamp page of the album, when the artist or their label sells it there."""
    for query in (title, f"{artist} {title}"):
        for r in bandcamp_search(query, "a"):
            if (r.get("type") == "a" and _sim(r.get("name") or "", title) > 0.85
                    and _sim(r.get("band_name") or "", artist) > 0.8 and (r.get("item_url_path") or "").startswith("http")):
                return r["item_url_path"]
    return None


@lru_cache(maxsize=1024)
def bandcamp_bio(url):
    """The "about" text of a Bandcamp artist or label page."""
    try:
        page = session.get(url, timeout=10).text
    except requests.RequestException:
        return ""
    m = re.search(r'id="bio-text"[^>]*>(.*?)</p>', page, re.S)
    if not m:
        return ""
    text = re.sub(r'<span class="peekaboo-ellipsis".*?</span>|<span class="peekaboo-link".*', "", m.group(1), flags=re.S)  # "... more"
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _add_bandcamp(info, name, label, add_img):
    """Bandcamp link, picture and "about" text of an artist or label."""
    page = None
    if not info["links"].get("Bandcamp"):
        page = bandcamp_page(name, label)
        if page:
            info["links"]["Bandcamp"] = page["url"]
            add_img(page["image"], "Bandcamp")
    url = info["links"].get("Bandcamp") or ""
    if re.match(r"https?://[\w-]+\.bandcamp\.com", url):
        bio = bandcamp_bio(url.split(".com")[0] + ".com/")
        if bio:
            info["descriptions"].append({"source": "Bandcamp", "text": bio, "url": url})
    return page


def _year(date):
    return (date or "")[:4] or None


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

    if adb:
        for lang in ("EN", "FR"):
            bio = (adb.get(f"strBiography{lang}") or "").strip() if _sim(adb.get("strArtist") or "", name) > 0.85 else ""
            if bio:
                info["descriptions"].append({"source": f"TheAudioDB ({lang})", "text": bio,
                                             "url": f"https://www.theaudiodb.com/artist/{adb.get('idArtist')}"})
        for k, n in (("strWebsite", "Official site"), ("strFacebook", "Facebook"), ("strTwitter", "X")):
            u = (adb.get(k) or "").strip()
            if u:
                info["links"].setdefault(n, u if u.startswith("http") else "https://" + u)

    facts = []
    if info.get("mbid"):
        full = _mb(f"artist/{info['mbid']}", inc="url-rels+genres+artist-rels") or {}
        info["genres"] = _mb_genres(full)[:6]
        links, ids = _mb_links(full.get("relations"))
        for k, v in links.items():
            info["links"].setdefault(k, v)
        group = full.get("type") == "Group"
        life = full.get("life-span") or {}
        place = (full.get("begin-area") or {}).get("name") or (full.get("area") or {}).get("name")
        if life.get("begin"):
            facts.append({"label": "Formed" if group else "Born", "value": ", ".join(x for x in (_year(life["begin"]), place) if x)})
        if life.get("ended") and life.get("end"):
            facts.append({"label": "Split up" if group else "Died", "value": _year(life["end"])})
        members = [r["artist"]["name"] for r in full.get("relations") or []
                   if r.get("type") == "member of band" and r.get("direction") == "backward" and r.get("artist")]
        if members:
            facts.append({"label": "Members", "value": ", ".join(dict.fromkeys(members))})
        if full.get("disambiguation"):
            facts.append({"label": "Known as", "value": full["disambiguation"]})

        if ids.get("wikidata"):
            ent = wikidata_entity(ids["wikidata"])
            for v in _wd_values(ent, "P18"):  # main image
                add_img(_commons_url(v), "Wikimedia")
            for v in _wd_values(ent, "P373")[:1]:  # Commons category: more photos
                for url in _commons_category(v):
                    add_img(url, "Wikimedia Commons")
            descs, img = _wikipedia_descriptions(ent)
            add_img(img, "Wikipedia")
            for d in descs:
                info["descriptions"].append(d)
                info["links"].setdefault("Wikipedia", d["url"])
            for v in _wd_values(ent, "P3283")[:1]:  # Bandcamp profile id
                info["links"].setdefault("Bandcamp", f"https://{v}.bandcamp.com")
            labels = _wd_names([v.get("id") for v in _wd_values(ent, "P264") if isinstance(v, dict)])
            if labels:
                facts.append({"label": "Labels", "value": ", ".join(labels)})

        if ids.get("discogs"):
            dg = discogs("artists", ids["discogs"])
            if dg:
                if dg["profile"]:
                    info["descriptions"].append({"source": "Discogs", "text": dg["profile"], "url": dg["url"]})
                for u in dg["images"]:
                    add_img(u, "Discogs")
                if dg["members"] and not members:
                    facts.append({"label": "Members", "value": ", ".join(dg["members"])})
    info["facts"] = facts
    _add_bandcamp(info, name, False, add_img)

    order = ["Genius", "Wikipedia (EN)", "Wikipedia (FR)", "TheAudioDB (EN)", "TheAudioDB (FR)", "Discogs", "Bandcamp"]
    info["descriptions"].sort(key=lambda d: order.index(d["source"]) if d["source"] in order else 99)
    artist_genres = {}
    _add_genres(artist_genres, info["genres"], "MusicBrainz")
    _add_genres(artist_genres, info["audiodb_genres"], "TheAudioDB")
    info["all_genres"] = [g["name"] for g in sorted(artist_genres.values(), key=lambda g: -g["score"])][:8]
    return info


def _label_name(raw):
    """Deezer label strings: "Parlophone (France)", "Columbia/Sony Music", "XL Recordings Ltd" -> main name."""
    name = re.sub(r"^\s*(?:\([pc]\)|℗|©)?\s*\d{4}\s+", "", raw or "", flags=re.I)  # "(P) 2019 Label"
    name = re.split(r"\s*/\s*|\s+under exclusive licen[cs]e.*|\s*,\s*", name, flags=re.I)[0]
    name = re.sub(r"\s*\(.*?\)\s*", " ", name)
    name = re.sub(r"\b(ltd\.?|limited|inc\.?|llc|gmbh|s\.?a\.?s?\.?|b\.?v\.?)$", "", name.strip(), flags=re.I)
    return name.strip(" .-")


def _wd_search(name, hint):
    """Wikidata item for a name whose description mentions a word (e.g. "label"), or None."""
    d = _get("https://www.wikidata.org/w/api.php", action="wbsearchentities", search=name, language="en",
             format="json", limit=7) or {}
    for hit in d.get("search") or []:
        desc = (hit.get("description") or "").lower()
        if _sim(hit.get("label", ""), name) > 0.85 and any(h in desc for h in hint):
            return hit.get("id")
    return None


@lru_cache(maxsize=512)
def label_info(raw_name):
    """Everything found about a record label: description(s), logo and images, country, founding, founders, parent…"""
    name = _label_name(raw_name) or raw_name
    info = {"name": name, "raw": raw_name, "descriptions": [], "images": [], "facts": [], "links": {},
            "country": None, "type": None, "found": False}
    seen = set()

    def add_img(url, source):
        if url and url not in seen:
            seen.add(url)
            info["images"].append({"url": url, "source": source})

    def fact(label, value):
        if value and all(f["label"] != label for f in info["facts"]):
            info["facts"].append({"label": label, "value": value})

    ids = {}
    hits = (_mb("label/", query=f'label:"{name}"', limit=5) or {}).get("labels") or []
    hit = next((h for h in hits if _sim(h.get("name", ""), name) > 0.85), None)
    if hit:
        full = _mb(f"label/{hit['id']}", inc="url-rels+label-rels") or {}
        info["found"] = True
        info["name"] = full.get("name") or hit.get("name") or name
        info["country"] = full.get("country") or hit.get("country")
        info["type"] = full.get("type")
        life = full.get("life-span") or {}
        fact("Founded", _year(life.get("begin")))
        if life.get("ended"):
            fact("Closed", _year(life.get("end")) or "yes")
        fact("Based in", (full.get("area") or {}).get("name"))
        if full.get("label-code"):
            fact("Label code", f"LC {full['label-code']}")
        parents = [r["label"]["name"] for r in full.get("relations") or []
                   if r.get("type") in ("label ownership", "imprint") and r.get("direction") == "backward" and r.get("label")]
        fact("Part of", ", ".join(dict.fromkeys(parents)))
        imprints = [r["label"]["name"] for r in full.get("relations") or []
                    if r.get("type") in ("label ownership", "imprint") and r.get("direction") == "forward" and r.get("label")]
        fact("Sub-labels", ", ".join(list(dict.fromkeys(imprints))[:10]))
        links, ids = _mb_links(full.get("relations"))
        info["links"].update(links)
        info["links"]["MusicBrainz"] = f"https://musicbrainz.org/label/{hit['id']}"

    qid = ids.get("wikidata") or _wd_search(name, ("label", "record company", "music company", "maison de disques"))
    if qid:
        info["found"] = True
        ent = wikidata_entity(qid)
        for v in _wd_values(ent, "P154"):  # logo
            add_img(_commons_url(v, 800), "Wikimedia (logo)")
        for v in _wd_values(ent, "P18"):
            add_img(_commons_url(v), "Wikimedia")
        descs, img = _wikipedia_descriptions(ent)
        add_img(img, "Wikipedia")
        info["descriptions"] += descs
        if descs:
            info["links"].setdefault("Wikipedia", descs[0]["url"])
        fact("Founded", _wd_year(ent, "P571"))
        ref = lambda p: _wd_names([v.get("id") for v in _wd_values(ent, p) if isinstance(v, dict)])
        fact("Founded by", ", ".join(ref("P112")))
        fact("Part of", ", ".join(ref("P749")))
        fact("Based in", ", ".join(ref("P159")))
        fact("Distributed by", ", ".join(ref("P750")))
        for v in _wd_values(ent, "P856")[:1]:
            info["links"].setdefault("Official site", v)
        for v in _wd_values(ent, "P3283")[:1]:  # Bandcamp profile id
            info["links"].setdefault("Bandcamp", f"https://{v}.bandcamp.com")
        if not ids.get("discogs"):
            ids["discogs"] = next(iter(_wd_values(ent, "P1955")), None)  # Discogs label id

    if ids.get("discogs"):
        dg = discogs("labels", ids["discogs"])
        if dg:
            info["found"] = True
            if dg["profile"]:
                info["descriptions"].append({"source": "Discogs", "text": dg["profile"], "url": dg["url"]})
            for u in dg["images"]:
                add_img(u, "Discogs")
            fact("Part of", dg["parent"])
            fact("Sub-labels", ", ".join(dg["sublabels"]))
            info["links"].setdefault("Discogs", dg["url"])
    page = _add_bandcamp(info, info["name"], True, add_img)
    if page:
        info["found"] = True
        fact("Based in", page["location"])
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


class _SilentLogger:
    def debug(self, msg): pass
    warning = info = error = debug


@lru_cache(maxsize=512)
def youtube_audio_features(url):
    """BPM and key from the whole song: downloads the YouTube audio (no conversion) and analyses it."""
    if not url:
        return None
    import yt_dlp
    with tempfile.TemporaryDirectory() as tmp:
        opts = {
            "format": "bestaudio[filesize<40M]/bestaudio[filesize_approx<40M]/bestaudio/best[height<=480]/best",
            "outtmpl": os.path.join(tmp, "audio.%(ext)s"),
            "quiet": True, "no_warnings": True, "noplaylist": True, "noprogress": True, "logger": _SilentLogger(),
            "match_filter": yt_dlp.utils.match_filter_func("duration <? 900"),  # skip mixes over 15 min
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            files = [os.path.join(tmp, f) for f in os.listdir(tmp)]
            if not files:
                return None
            return analyse_samples(audio_analysis.decode_file(files[0]))
        except Exception:
            return None


def deezer_preview_features(track_id):
    t = deezer_track(track_id)
    if not t.get("preview"):
        return None
    try:
        r = session.get(t["preview"], timeout=20)
        return analyse(r.content) if r.ok else None
    except requests.RequestException:
        return None


_KEY_RE = re.compile(r"^\s*([A-Ga-g])\s*([#♯b♭]?)\s*(m|min|minor|maj|major)?\s*$")


def parse_key(text):
    """'F♯m', 'Bb', 'C# minor'… -> (pitch class, "major"/"minor") or None."""
    m = _KEY_RE.match(text or "")
    if not m:
        return None
    pc = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[m.group(1).upper()]
    pc += {"#": 1, "♯": 1, "b": -1, "♭": -1}.get(m.group(2), 0)
    mode = "minor" if (m.group(3) or "").lower() in ("m", "min", "minor") else "major"
    return pc % 12, mode


@lru_cache(maxsize=512)
def getsongbpm(artist, title):
    """{"bpm", "key"} from GetSongBPM, or None (no key configured, not found, offline…)."""
    if not GETSONGBPM_API_KEY:
        return None
    lookup = f"song:{title} artist:{artist}"
    for base in ("https://api.getsong.co", "https://api.getsongbpm.com"):
        try:
            r = session.get(f"{base}/search/", timeout=TIMEOUT,
                            params={"api_key": GETSONGBPM_API_KEY, "type": "both", "lookup": lookup})
            if not r.ok:
                continue
            results = (r.json() or {}).get("search")
        except (requests.RequestException, ValueError):
            continue
        if not isinstance(results, list):
            return None
        best = max(results, key=lambda x: _sim(x.get("title", ""), title) + _sim((x.get("artist") or {}).get("name", ""), artist),
                   default=None)
        if not best or _sim(best.get("title", ""), title) < 0.6 or _sim((best.get("artist") or {}).get("name", ""), artist) < 0.5:
            return None
        try:
            bpm = float(best.get("tempo") or 0) or None
        except ValueError:
            bpm = None
        return {"bpm": bpm, "key": parse_key(best.get("key_of")), "url": best.get("uri")}
    return None


def _same_tempo(a, b, tol=0.03):
    return abs(a - b) <= tol * b


def combine_audio(analysed, source, deezer_bpm=None, gsb=None):
    """Cross-checks the audio analysis with Deezer's and GetSongBPM's values.

    Agreement between independent sources raises the confidence to "high"; disagreement lowers it and the
    other candidates are listed (bpm_alternatives / key_alternatives) so they can be shown on hover.
    """
    a = analysed or {}
    out = {"bpm": None, "bpm_confidence": None, "bpm_sources": [], "bpm_alternatives": [],
           "key": None, "camelot": None, "key_confidence": None, "key_sources": [], "key_alternatives": [],
           "audio_source": source if analysed else None, "tuning_cents": a.get("tuning_cents")}

    # --- BPM ---
    refs = [(name, v) for name, v in (("Deezer", deezer_bpm), ("GetSongBPM", (gsb or {}).get("bpm"))) if v]
    if a.get("bpm"):
        bpm, conf, srcs = a["bpm"], a["bpm_confidence"], [f"audio analysis ({source})"]
        alt = a.get("alt_bpm")
        for name, v in refs:
            if _same_tempo(v, bpm):
                srcs.append(name)
            elif alt and _same_tempo(v, alt):  # the other source settles the half/double doubt
                bpm, alt = alt, None
                srcs = [f"audio analysis ({source})", name]
        agreeing = len(srcs) - 1
        disagreeing = [f"{round(v)} ({name})" for name, v in refs if name not in srcs]
        octave_only = all(_same_tempo(v, bpm * 2) or _same_tempo(v, bpm / 2) for name, v in refs if name not in srcs)
        if agreeing:
            conf = "high"
        elif disagreeing:  # half/double time is a matter of feel; any other mismatch is a real doubt
            conf = "medium" if octave_only and conf == "high" else "low"
        if alt and conf != "high":
            out["bpm_alternatives"].append(f"{alt} (half/double)")
        out["bpm_alternatives"] += disagreeing
        out.update(bpm=int(round(bpm)), bpm_confidence=conf, bpm_sources=srcs)
    elif refs:
        agree = len(refs) == 2 and _same_tempo(refs[0][1], refs[1][1])
        out.update(bpm=int(round(refs[0][1])), bpm_confidence="high" if agree else "medium",
                   bpm_sources=[n for n, _ in refs] if agree else [refs[0][0]],
                   bpm_alternatives=[] if agree or len(refs) < 2 else [f"{round(refs[1][1])} ({refs[1][0]})"])

    # --- Key ---
    ref_key = (gsb or {}).get("key")
    label = lambda k: f"{NOTES[k[0]]} {k[1]}"
    if a.get("key"):
        key, conf, srcs = a["key"], a["key_confidence"], [f"audio analysis ({source})"]
        if ref_key:
            if label(ref_key) == key:
                conf = "high"; srcs.append("GetSongBPM")
            elif conf == "high":
                conf = "medium"; out["key_alternatives"].append(f"{label(ref_key)} (GetSongBPM)")
            else:  # uncertain analysis and the database disagrees: show the database's key, flagged as doubtful
                out["key_alternatives"].append(f"{key} (audio analysis)")
                key, conf, srcs = label(ref_key), "low", ["GetSongBPM"]
        tonic, mode = NOTES.index(key.split()[0]), key.split()[1]
        out.update(key=key, camelot=camelot(tonic, mode), key_confidence=conf, key_sources=srcs)
    elif ref_key:
        out.update(key=label(ref_key), camelot=camelot(*ref_key), key_confidence="medium", key_sources=["GetSongBPM"])
    return out


def track_audio_features(track_id=None, video_url="", artist="", title=""):
    """BPM and key: whole-song analysis (YouTube audio, else the 30 s Deezer preview), cross-checked with
    Deezer's BPM and GetSongBPM."""
    analysed, source = youtube_audio_features(video_url), "whole song"
    if not analysed and track_id:
        analysed, source = deezer_preview_features(track_id), "30 s preview"
    deezer_bpm = (deezer_track(track_id).get("bpm") or None) if track_id else None
    gsb = getsongbpm(artist, title) if artist and title else None
    return combine_audio(analysed, source, deezer_bpm, gsb)


def enrich(title, channel="", video_url=""):
    artist_guess, track_guess, artist_reliable = parse_title_ex(title, channel)
    candidates = _search_deezer(artist_guess, track_guess, artist_reliable)
    if not candidates:
        return {"found": False, "guess": {"artist": artist_guess, "track": track_guess},
                **track_audio_features(None, video_url, artist_guess, track_guess)}

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
        "bandcamp_url": bandcamp_album((album.get("artist") or {}).get("name") or main, alb_info.get("title") or ""),
        **track_audio_features(track["id"], video_url, main, track.get("title") or track_guess),
    }
