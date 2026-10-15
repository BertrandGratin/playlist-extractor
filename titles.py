"""Cleans YouTube video titles into plain song names.

"Daft Punk - One More Time (Official Video) [HD]" -> ("One More Time", ["Official Video", "HD"])

- When the title has the form "Artist - Song", the artist and the dash are removed (unless the part after the
  dash is only a version, e.g. "Song - Radio Edit").
- Mentions that describe the video rather than the song (official video, clip officiel, visualizer, lyrics,
  audio, HD…, in several languages) are removed and returned separately as tags.
Parts that belong to the song itself, like "(feat. X)", "(Remix)", "(Live)" or "(Radio Edit)", are kept.
"""
import re

# Words that only describe the upload, in English, French, Spanish, Portuguese, Italian, German, Dutch…
_NOISE_WORDS = [
    r"official", r"officiel(?:le)?", r"oficial", r"ufficiale", r"offiziell(?:es)?", r"officieel", r"officiële",
    r"music\s*video", r"video\s*clip", r"videoclip", r"clip", r"vid[eé]o", r"v[ií]deo", r"m/?v",
    r"lyrics?", r"lyric\s*video", r"paroles?", r"letras?", r"testo", r"songtext",
    r"visuali[sz]er", r"visualizador", r"visual", r"audio", r"[aá]udio",
    r"hd", r"hq", r"4k", r"1080p", r"720p", r"full\s*hd",
    r"out\s*now", r"free\s*download", r"premiere", r"explicit", r"clean", r"color\s*coded",
    r"subtitulado", r"sous[-\s]titr[ée]s?", r"legendado", r"traduction", r"traducción", r"tradução", r"lyrics\s*\+\s*translation",
    r"teaser", r"trailer", r"en\s*vivo", r"ao\s*vivo",
]
# A bracketed group is noise when every word in it is a noise word (plus fillers like "the", "-", "&", years).
_FILLER = r"(?:the|a|an|de|du|la|le|el|der|die|das|und|and|y|e|et|with|avec|con|com|in|en|\d{4}|new|nouveau|nouvelle|nuevo|neuer?|[-–—&+/|,.:!])"
_NOISE_TOKEN = re.compile(rf"^(?:{'|'.join(_NOISE_WORDS)}|{_FILLER})$", re.I)
_TOKEN_SPLIT = re.compile(r"(m/v|music\s*video|video\s*clip|lyric\s*video|full\s*hd|out\s*now|free\s*download|color\s*coded|"
                          r"sous[-\s]titr[ée]s?|en\s*vivo|ao\s*vivo|[\w'’]+|[-–—&+/|,.:!])", re.I)

# Part after " - " that is a version of the song, not a song name.
_VERSION_WORD = re.compile(r"\b(?:remix|edit|version|mix|remaster(?:ed)?|live|acoustic|acoustique|instrumental|"
                           r"extended|radio|demo|session|rework|bootleg|cover|reprise|unplugged|bonus)\b", re.I)


def _is_version(text):
    """"Radio Edit", "Live", "Remastered 2009"…: short, no brackets, contains a version word."""
    return "(" not in text and "[" not in text and len(text.split()) <= 3 and bool(_VERSION_WORD.search(text))


_DASH = re.compile(r"\s+[-–—]\s+|\s+[-–—](?=\S)|(?<=\S)[-–—]\s+")
_GROUP = re.compile(r"\s*([\(\[\{【「『])([^\)\]\}】」』]*)([\)\]\}】」』])")


def _is_noise(text):
    tokens = _TOKEN_SPLIT.findall(text.strip())
    return bool(tokens) and any(not re.fullmatch(_FILLER, t, re.I) for t in tokens) and all(_NOISE_TOKEN.match(t) for t in tokens)


def _tag(text):
    t = re.sub(r"\s+", " ", text.strip(" -–—|:.,")).strip()
    return t[:1].upper() + t[1:] if t else ""


def clean_title(title):
    """Returns (song name, [video mentions removed]) for a YouTube title."""
    if not title:
        return title, []
    tags = []
    s = title

    # 1. Bracketed mentions: "(Official Video)", "[HD]", "【MV】"…
    def group(m):
        if _is_noise(m.group(2)):
            tags.append(_tag(m.group(2)))
            return ""
        return m.group(0)
    s = _GROUP.sub(group, s)

    # 2. Mentions after a separator: "Song | Official Video", "Song - Clip officiel", "Song // Lyrics".
    while True:
        m = re.search(r"\s*(?:\||//|\s[-–—•·]\s)\s*([^|]+?)\s*$", s)
        if not m or not _is_noise(m.group(1)):
            break
        tags.append(_tag(m.group(1)))
        s = s[:m.start()]
    # "Song | Album name" or "Song | Artist": keep only the song part.
    s = re.sub(r"\s*(?:\||//)\s*.*$", "", s)

    # 3. Trailing bare mentions: "Song Official Video", "Song (Lyrics)" already handled; "Song HD".
    m = re.search(r"\s+((?:official|officiel|oficial)\s+(?:music\s+)?(?:video|vid[eé]o|clip|audio|lyric\s+video|visuali[sz]er|m/?v)|m/?v|hd|hq|4k)\s*$", s, re.I)
    if m:
        tags.append(_tag(m.group(1)))
        s = s[:m.start()]

    # 4. "Artist - Song": drop the artist and the dash (not for "Song - Radio Edit").
    parts = _DASH.split(s.strip(), maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip() and not _is_version(parts[1].strip()):
        s = parts[1]

    s = re.sub(r"\s{2,}", " ", s).strip(" -–—|:•·\"“”'")
    if not s:  # the whole title was a mention: keep it as is
        return title.strip(), []
    seen, uniq = set(), []
    for t in tags:
        if t and t.lower() not in seen:
            seen.add(t.lower()); uniq.append(t)
    return s, uniq
