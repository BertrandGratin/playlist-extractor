# Playlist Extractor

A small app that runs on your own computer: paste a YouTube playlist link and it lists the title and link of every video, with one-click copy for each title or link (or all of them at once).

It also looks up the music info for every track and groups everything that repeats at the top of the page:

- **Playlist**: its images, its owner and the number of videos.
- **Artists**: one card per artist, with every photo found (Genius, Deezer, TheAudioDB, Wikimedia) so you can pick and download your favourite, their description (Genius, with Wikipedia as a complement or fallback), country of origin, genres, and how many of their tracks are in the playlist.
- **Albums**: one card per album, with the cover (downloadable), release date, label and genres.
- **Tracks**: title and link (copyable), BPM and key, with shortcuts to the artist and album cards.

"Export all (.csv)" puts everything in a file that opens in Excel.

## For your friends: the ready-made app

Go to the **Releases** section of the GitHub repository (right-hand column), download the zip for your computer, unzip it and double-click `Playlist-Extractor`. Nothing to install. The first launch shows a warning because the app isn't signed: how to get past it is explained on the release page.

### Automatic updates

Every time it starts, the app checks GitHub for a newer version. If there is one, it downloads it, replaces itself and restarts (a few seconds). Nobody needs to go back to GitHub: you just publish a new version. Without internet, the app simply starts the current version. The version number is shown at the bottom of the page.

## Running from the source code (for development)

### Requirements

Python 3.9 or newer ([python.org/downloads](https://www.python.org/downloads/)). On Windows, tick "Add python.exe to PATH" during installation.

### Launching

- **Windows**: double-click `run-windows.bat`.
- **Mac**: double-click `run-mac.command` (the first time: right-click > Open if macOS blocks the file).

The first launch installs the dependencies into a `.venv` folder, then the page opens by itself at http://127.0.0.1:8765. To quit, close the terminal window (or press Ctrl+C).

By hand, if you prefer: `pip install -r requirements.txt` then `python app.py`.

## How to use it

1. Paste a playlist link (`https://www.youtube.com/playlist?list=...`, or a video link containing `&list=...`).
2. Click **Extract**.
3. The copy icon next to a title, link, artist, album, BPM or key copies it; **Copy all titles** / **Copy all links** at the top copy the whole list.
4. In an artist card, click a photo to feature it; on hover, ⤢ opens it full size and ⬇ downloads it.

Private or deleted videos are skipped. The playlist must be public or unlisted.

## Where the info comes from

Free sources, no account or API key: Genius (artist photos and description), Wikipedia (description), Deezer (album, cover, release date, label, genres, photo, BPM when known, 30-second audio preview), MusicBrainz (country of origin, album and artist genres), TheAudioDB (photos, genres and styles) and Wikimedia Commons (photos). BPM and key are cross-checked with [GetSongBPM](https://getsongbpm.com). Genres from every source are merged: those cited by several sources come first, and hovering over a genre shows where it came from.

**BPM and key** come from analysing the whole song (its YouTube audio; the 30-second Deezer preview is used if that fails). The analysis measures the recording's tuning, lets ~12-second segments of the song vote and combines four key models. The result is then cross-checked with Deezer's BPM and with [GetSongBPM](https://getsongbpm.com). Each value carries a confidence mark: **✓** when independent sources agree, a grey **?** when it is probably right but unconfirmed, an orange **?** when sources disagree. Hover over the value to see its sources and the other candidates. No method is 100% reliable: the usual pitfalls are half/double tempo and confusing a key with its relative major/minor. The key is also shown in Camelot notation for DJs.

The app guesses the artist and title from the YouTube title ("Artist - Title") or from an "Artist - Topic" channel; if the YouTube title is too unusual, the track shows "No info found". On long playlists the info arrives progressively (MusicBrainz allows one request per second, so allow a few seconds per track).

## Publishing a new version

Repository's **Actions** tab > *Build the app* > *Run workflow* > enter a version number (e.g. `v1.4.0`) > *Run workflow*. A few minutes later a new release appears with the Windows and Mac zips, and every installed app updates itself at its next launch. The number must be higher than the previous one (e.g. `v1.3.0` → `v1.3.1` or `v1.4.0`).

## If it stops working

YouTube changes regularly. The launch scripts update yt-dlp at every start; for the ready-made app, just publish a new version (see above), which will bundle the latest yt-dlp.
