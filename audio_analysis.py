"""Tempo (BPM) and musical key estimation from a short audio clip (e.g. Deezer's 30-second preview).

Pure numpy, no heavy audio library: miniaudio decodes the MP3, then
- BPM: autocorrelation of a spectral-flux onset envelope, weighted towards common tempos;
- key: chroma profile matched against Temperley's major/minor key profiles.
These are estimates: tempo can occasionally come out at half or double the real value.
"""
import miniaudio
import numpy as np

SR = 22050
NOTES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
MAJOR = np.array([5, 2, 3.5, 2, 4.5, 4, 2, 4.5, 2, 3.5, 1.5, 4])
MINOR = np.array([5, 2, 3.5, 4.5, 2, 4, 2, 4.5, 3.5, 2, 1.5, 4])
# Camelot wheel (handy for DJs): index = pitch class of the tonic.
CAMELOT_MAJOR = ["8B", "3B", "10B", "5B", "12B", "7B", "2B", "9B", "4B", "11B", "6B", "1B"]
CAMELOT_MINOR = ["5A", "12A", "7A", "2A", "9A", "4A", "11A", "6A", "1A", "8A", "3A", "10A"]


def decode(data):
    snd = miniaudio.decode(data, output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1, sample_rate=SR)
    return np.asarray(snd.samples, dtype=np.float32)


def _stft_mag(y, n_fft, hop):
    if len(y) < n_fft:
        y = np.pad(y, (0, n_fft - len(y)))
    frames = np.lib.stride_tricks.sliding_window_view(y, n_fft)[::hop]
    return np.abs(np.fft.rfft(frames * np.hanning(n_fft), axis=1))


def estimate_bpm(y):
    hop, n_fft = 256, 1024
    mag = _stft_mag(y, n_fft, hop)
    logmag = np.log1p(100 * mag)
    flux = np.maximum(np.diff(logmag, axis=0), 0).sum(axis=1)
    # Remove the slow trend so only the beats remain.
    win = 16
    flux = flux - np.convolve(flux, np.ones(win) / win, mode="same")
    flux = np.maximum(flux, 0)
    if not flux.any():
        return None
    fps = SR / hop
    ac = np.correlate(flux, flux, mode="full")[len(flux) - 1:]
    lags = np.arange(len(ac))
    lo, hi = int(fps * 60 / 200), int(fps * 60 / 60)  # 60 to 200 BPM
    lags, ac = lags[lo:hi + 1], ac[lo:hi + 1]
    bpms = 60 * fps / lags
    prior = np.exp(-0.5 * (np.log2(bpms / 120) / 0.9) ** 2)  # favour tempos around 120 BPM
    score = ac * prior
    i = int(np.argmax(score))
    # Parabolic refinement of the best lag for sub-frame precision.
    if 0 < i < len(score) - 1:
        a, b, c = score[i - 1], score[i], score[i + 1]
        denom = a - 2 * b + c
        offset = 0.5 * (a - c) / denom if denom else 0
    else:
        offset = 0
    return round(60 * fps / (lags[i] + offset))


def estimate_key(y):
    n_fft, hop = 8192, 2048
    mag = _stft_mag(y, n_fft, hop)
    freqs = np.fft.rfftfreq(n_fft, 1 / SR)
    keep = (freqs > 60) & (freqs < 2000)
    pcs = (np.round(12 * np.log2(freqs[keep] / 440) + 69).astype(int)) % 12
    energy = np.sqrt(mag[:, keep]).sum(axis=0)
    chroma = np.bincount(pcs, weights=energy, minlength=12)
    if not chroma.any():
        return None
    best = None
    for tonic in range(12):
        for mode, profile in (("major", MAJOR), ("minor", MINOR)):
            r = np.corrcoef(chroma, np.roll(profile, tonic))[0, 1]
            if best is None or r > best[0]:
                best = (r, tonic, mode)
    _, tonic, mode = best
    return {
        "key": f"{NOTES[tonic]} {mode}",
        "camelot": (CAMELOT_MAJOR if mode == "major" else CAMELOT_MINOR)[tonic],
    }


def analyse(data):
    """Returns {"bpm", "key", "camelot"} for MP3 bytes, or None if the audio cannot be read."""
    try:
        y = decode(data)
    except Exception:
        return None
    if len(y) < SR * 5:
        return None
    out = {"bpm": estimate_bpm(y)}
    out.update(estimate_key(y) or {})
    return out
