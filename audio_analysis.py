"""Tempo (BPM) and musical key estimation from audio: a whole song (YouTube audio) or a 30-second clip (Deezer preview).

Pure numpy for the analysis; miniaudio decodes MP3 bytes and PyAV decodes the YouTube audio files.
- Key: the reference tuning is measured first (many recordings are not exactly at A=440 Hz), then spectral peaks
  are folded into a 12-note chroma that also credits the fundamentals of harmonics. The chroma is matched against
  four classic key profiles (Temperley, Krumhansl, Shaath, EDM) and the song is also cut into ~12 s segments that
  each vote. Confidence comes from the margin between the two best keys and from how many segments agree.
- BPM: spectral-flux onset envelope, autocorrelation summed over several beat multiples on a 0.1 BPM grid,
  weighted towards common tempos. Confidence comes from ~20 s segments agreeing, and drops when the half or double
  tempo is almost as plausible (then reported as alt_bpm).
These are estimates: no algorithm is right 100% of the time, which is why each result carries a confidence.
"""
import miniaudio
import numpy as np
SR = 22050
NOTES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
PROFILES = {
    "temperley": ([5, 2, 3.5, 2, 4.5, 4, 2, 4.5, 2, 3.5, 1.5, 4], [5, 2, 3.5, 4.5, 2, 4, 2, 4.5, 3.5, 2, 1.5, 4]),
    "krumhansl": ([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88],
                  [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]),
    "shaath": ([6.6, 2.0, 3.5, 2.3, 4.6, 4.0, 2.5, 5.2, 2.4, 3.7, 2.3, 3.4],
               [6.5, 2.7, 3.5, 5.4, 2.6, 3.5, 2.5, 5.2, 4.0, 2.7, 4.3, 3.2]),
    "edma": ([.165, .047, .083, .067, .100, .093, .053, .132, .052, .074, .052, .079],
             [.172, .040, .076, .129, .052, .080, .042, .132, .093, .048, .075, .061]),
}
# Camelot wheel (handy for DJs): index = pitch class of the tonic.
CAMELOT_MAJOR = ["8B", "3B", "10B", "5B", "12B", "7B", "2B", "9B", "4B", "11B", "6B", "1B"]
CAMELOT_MINOR = ["5A", "12A", "7A", "2A", "9A", "4A", "11A", "6A", "1A", "8A", "3A", "10A"]
N_FFT, HOP = 8192, 4096
FMIN, FMAX = 80, 4000

def _frames_mag(y):
    if len(y) < N_FFT: y = np.pad(y, (0, N_FFT - len(y)))
    win = np.hanning(N_FFT).astype(np.float32)
    starts = range(0, len(y) - N_FFT + 1, HOP)
    out = []
    for i in range(0, len(starts), 128):  # chunks to limit memory on full songs
        idx = list(starts)[i:i + 128]
        fr = np.stack([y[s:s + N_FFT] for s in idx]) * win
        out.append(np.abs(np.fft.rfft(fr, axis=1)).astype(np.float32))
    return np.concatenate(out)

def _peaks(mag):
    """Spectral peaks per frame: (frame index, frequency Hz, magnitude), with parabolic interpolation."""
    freqs_per_bin = SR / N_FFT
    lo, hi = int(FMIN / freqs_per_bin), int(FMAX / freqs_per_bin)
    m = mag[:, lo - 1:hi + 1]
    c = m[:, 1:-1]
    is_peak = (c > m[:, :-2]) & (c >= m[:, 2:]) & (c > 0.01 * mag.max(axis=1, keepdims=True))  # within 40 dB of frame max
    f_idx, b_idx = np.nonzero(is_peak)
    a, b, g = (np.log(m[f_idx, b_idx] + 1e-9), np.log(m[f_idx, b_idx + 1] + 1e-9), np.log(m[f_idx, b_idx + 2] + 1e-9))
    den = a - 2 * b + g
    off = np.where(den != 0, 0.5 * (a - g) / np.where(den == 0, 1, den), 0)
    freq = (lo + b_idx + off) * freqs_per_bin
    amp = np.exp(b - 0.25 * (a - g) * off)
    return f_idx, freq, amp

def _tuning(freq, amp):
    """Reference offset in semitones (e.g. +0.2 = tuned 20 cents sharp), from the weighted circular mean of peak deviations."""
    semis = 12 * np.log2(freq / 440)
    ang = 2 * np.pi * semis
    z = np.sum(amp * np.exp(1j * ang))
    return float(np.angle(z) / (2 * np.pi)) if abs(z) > 0 else 0.0

def _chroma_frames(n_frames, f_idx, freq, amp, tuning, harmonics=4):
    """HPCP-like chroma: each peak votes for the pitch class of its fundamental candidates, cosine-windowed."""
    chroma = np.zeros((n_frames, 12))
    for h in range(1, harmonics + 1):
        f0 = freq / h
        ok = f0 > 50
        semis = 12 * np.log2(f0[ok] / 440) - tuning + 69
        near = np.round(semis); d = semis - near
        w = np.where(np.abs(d) < 0.5, np.cos(np.pi * d) ** 2, 0) * (amp[ok] ** 2) * (0.6 ** (h - 1))
        np.add.at(chroma, (f_idx[ok], near.astype(int) % 12), w)
    mx = chroma.max(axis=1, keepdims=True)
    return np.divide(chroma, mx, out=np.zeros_like(chroma), where=mx > 0)

def _key_scores(chroma):
    """24 scores (12 major then 12 minor), z-normalised correlations averaged over profiles."""
    total = np.zeros(24)
    for maj, mnr in PROFILES.values():
        s = np.array([np.corrcoef(chroma, np.roll(p, t))[0, 1] for p in (maj, mnr) for t in range(12)])
        total += (s - s.mean()) / (s.std() + 1e-9)
    return total / len(PROFILES)

def estimate_key(y):
    mag = _frames_mag(y)
    f_idx, freq, amp = _peaks(mag)
    if len(freq) == 0: return None
    tuning = _tuning(freq, amp)
    ch = _chroma_frames(len(mag), f_idx, freq, amp, tuning)
    if not ch.any(): return None
    glob = _key_scores(ch.sum(axis=0))
    # Segment vote (~12 s windows).
    seg = max(1, int(12 * SR / HOP))
    votes = np.zeros(24); nseg = 0
    for s in range(0, len(ch), seg):
        part = ch[s:s + seg].sum(axis=0)
        if part.any() and len(ch[s:s + seg]) >= seg // 2:
            votes[np.argmax(_key_scores(part))] += 1; nseg += 1
    order = np.argsort(glob)[::-1]
    best, second = order[0], order[1]
    margin = glob[best] - glob[second]
    agree = votes[best] / nseg if nseg else 0
    tonic, mode = int(best % 12), "major" if best < 12 else "minor"
    conf = min(1.0, 0.5 * agree + margin / 0.6 * 0.5)
    level = "high" if conf >= .75 else "medium" if conf >= .5 else "low"
    return {"key": f"{NOTES[tonic]} {mode}", "camelot": camelot(tonic, mode), "confidence": level, "confidence_pct": round(100 * conf),
            "tuning_cents": round(100 * tuning)}

# ---------------- tempo ----------------
B_HOP, B_FFT = 256, 1024
FPS = SR / B_HOP

def onset_envelope(y):
    win = np.hanning(B_FFT).astype(np.float32)
    n = 1 + max(0, len(y) - B_FFT) // B_HOP
    flux = np.zeros(n); prev = None
    for i in range(0, n, 2048):
        idx = np.arange(i, min(n, i + 2048)) * B_HOP
        fr = np.stack([y[s:s + B_FFT] if s + B_FFT <= len(y) else np.pad(y[s:], (0, s + B_FFT - len(y))) for s in idx]) * win
        lm = np.log1p(100 * np.abs(np.fft.rfft(fr, axis=1)))
        if prev is not None: lm = np.vstack([prev, lm])
        d = np.maximum(np.diff(lm, axis=0), 0).sum(axis=1)
        flux[i + (0 if prev is not None else 1): i + (0 if prev is not None else 1) + len(d)] = d
        prev = lm[-1:]
    w = 16
    flux = np.maximum(flux - np.convolve(flux, np.ones(w) / w, mode="same"), 0)
    return flux

def _comb_strength(env):
    """Tempo strength over a fine BPM grid: autocorrelation summed at multiples of the beat period."""
    env = env - env.mean()
    n = len(env); L = 1 << int(np.ceil(np.log2(2 * n)))
    f = np.fft.rfft(env, L); ac = np.fft.irfft(f * np.conj(f), L)[:n]
    if ac[0] <= 0: return None, None
    ac = ac / ac[0]
    bpms = np.arange(50, 220.01, 0.1)
    lag = 60 * FPS / bpms
    s = np.zeros_like(bpms)
    for k, wk in ((1, 1), (2, .6), (3, .4), (4, .3)):
        lk = lag * k; ok = lk < n - 1
        s[ok] += wk * np.interp(lk[ok], np.arange(n), ac)
    return bpms, s

def _prior(b): return np.exp(-0.5 * (np.log2(b / 120) / 0.8) ** 2)

def estimate_bpm(y):
    env = onset_envelope(y)
    if not env.any(): return None
    bpms, s = _comb_strength(env)
    if bpms is None: return None
    score = np.maximum(s, 0) * _prior(bpms)
    i = int(np.argmax(score)); bpm = bpms[i]
    # Strength of the half / double alternative (octave ambiguity).
    def at(b): return float(np.interp(b, bpms, score)) if 50 <= b <= 220 else 0.0
    alt_b = max((bpm * 2, bpm / 2), key=at); octave_ratio = at(alt_b) / score[i]
    # Segment agreement (~20 s windows).
    seg = int(20 * FPS); agree = n = 0
    for st in range(0, len(env) - seg // 2, seg):
        part = env[st:st + seg]
        if len(part) < seg // 2: continue
        b2, s2 = _comb_strength(part)
        if b2 is None: continue
        bb = b2[np.argmax(np.maximum(s2, 0) * _prior(b2))]
        n += 1; agree += any(abs(bb - bpm * k) <= 0.03 * bpm * k for k in (1, .5, 2))
    agree = agree / n if n else 0
    conf = agree * (1 - max(0, octave_ratio - 0.5))
    level = "high" if conf >= .8 and octave_ratio < .65 else "medium" if conf >= .5 else "low"
    out = {"bpm": int(round(bpm)), "confidence": level, "confidence_pct": round(100 * conf), }
    if octave_ratio >= .65: out["alt_bpm"] = int(round(alt_b))
    return out


def camelot(tonic, mode):
    return (CAMELOT_MAJOR if mode == "major" else CAMELOT_MINOR)[tonic]


def decode(data):
    snd = miniaudio.decode(data, output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1, sample_rate=SR)
    return np.asarray(snd.samples, dtype=np.float32)


def decode_file(path, max_seconds=900):
    """Decodes any audio file (YouTube opus/m4a…) to mono float32 at SR, with PyAV."""
    import av
    out, total = [], 0
    with av.open(str(path)) as container:
        resampler = av.AudioResampler(format="flt", layout="mono", rate=SR)
        for frame in container.decode(audio=0):
            for f in resampler.resample(frame):
                a = f.to_ndarray().reshape(-1); out.append(a); total += len(a)
            if total > max_seconds * SR:
                break
        for f in resampler.resample(None):
            out.append(f.to_ndarray().reshape(-1))
    return np.concatenate(out).astype(np.float32) if out else np.zeros(0, np.float32)


def analyse_samples(y):
    """{"bpm", "bpm_confidence", "key", "camelot", "key_confidence", …} or None if the audio is too short."""
    if y is None or len(y) < SR * 5:
        return None
    out = {}
    b = estimate_bpm(y)
    if b:
        out.update(bpm=b["bpm"], bpm_confidence=b["confidence"], bpm_confidence_pct=b["confidence_pct"])
        if b.get("alt_bpm"):
            out["alt_bpm"] = b["alt_bpm"]
    k = estimate_key(y)
    if k:
        out.update(key=k["key"], camelot=k["camelot"], key_confidence=k["confidence"],
                   key_confidence_pct=k["confidence_pct"], tuning_cents=k["tuning_cents"])
    return out


def analyse(data):
    """Same as analyse_samples, for MP3 bytes."""
    try:
        y = decode(data)
    except Exception:
        return None
    return analyse_samples(y)
