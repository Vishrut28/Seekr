"""Score the film. No sample library here, so every sound is built from
scratch — but built as an instrument, not as a test tone.

The first attempt was sine drones with a tremolo on them, which is exactly
what a signal generator sounds like. This is closer to the ambient beds used
under product films: struck tones with harmonics and a long decay, a soft
sub for a section change, and air rather than a held pitch.
"""
from __future__ import annotations

import json
import math
import pathlib
import struct
import wave

HERE = pathlib.Path(__file__).parent
AUDIO = HERE / "audio"
AUDIO.mkdir(exist_ok=True)
SR = 48000

# A pentatonic set: no semitone clashes, so any two notes can overlap safely.
SCALE = {"a2": 110.00, "c3": 130.81, "d3": 146.83, "e3": 164.81, "g3": 196.00,
         "a3": 220.00, "c4": 261.63, "d4": 293.66, "e4": 329.63, "g4": 392.00,
         "a4": 440.00, "c5": 523.25, "e5": 659.25}


def write(path: pathlib.Path, samples: list[float], peak: float = 0.9):
    top = max(1e-9, max(abs(s) for s in samples))
    gain = peak / top
    with wave.open(str(path), "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        frames = bytearray()
        for s in samples:
            v = int(max(-1.0, min(1.0, s * gain)) * 32000)
            frames += struct.pack("<hh", v, v)
        w.writeframes(bytes(frames))


def struck(freq: float, seconds: float, decay: float = 3.2,
           partials=(1, 2, 3, 4.2), weights=(1.0, 0.42, 0.18, 0.07)) -> list[float]:
    """A struck tone — harmonics fading at different rates, like something hit.

    A pure sine reads as electronic; the overtones are what make it sound as
    though an object made the sound.
    """
    n = int(SR * seconds)
    out = [0.0] * n
    for p, wgt in zip(partials, weights, strict=True):
        w = 2 * math.pi * freq * p
        d = decay * (1 + 0.55 * (p - 1))          # higher partials die sooner
        for i in range(n):
            t = i / SR
            out[i] += wgt * math.sin(w * t) * math.exp(-d * t)
    # a soft attack, so nothing clicks at the start
    a = int(SR * 0.006)
    for i in range(min(a, n)):
        out[i] *= i / a
    return out


# Four chords, each held about eight seconds. A film that sits on one pitch
# for a minute reads as a hold tone; a slow progression reads as music without
# ever asking for attention.
PROGRESSION = [
    ["a2", "e3", "a3", "c4"],     # i
    ["g3", "d4", "g4"],           # VII
    ["c3", "g3", "c4", "e4"],     # III
    ["d3", "a3", "d4", "g4"],     # iv
]


def progression(seconds: float) -> list[float]:
    """The harmonic bed: struck chords, overlapping, moving slowly."""
    n = int(SR * seconds)
    out = [0.0] * n
    hold = 8.4
    for i in range(int(seconds / hold) + 1):
        notes = PROGRESSION[i % len(PROGRESSION)]
        start = int(SR * i * hold)
        # long decay so each chord is still ringing under the next
        voice = chord(notes, min(seconds, hold * 1.9), decay=0.55)
        for k, v in enumerate(voice):
            if start + k < n:
                out[start + k] += v * 0.5
    return out


def bed(seconds: float) -> list[float]:
    """Air, not a chord: filtered noise that swells and recedes, with two very
    quiet low tones under it for weight."""
    n = int(SR * seconds)
    out = [0.0] * n
    # one-pole lowpassed noise, seeded so the film sounds the same every build
    rnd = 12345
    lp = 0.0
    for i in range(n):
        rnd = (1103515245 * rnd + 12345) % (1 << 31)
        white = (rnd / (1 << 30)) - 1.0
        lp += 0.0016 * (white - lp)               # very dark
        t = i / SR
        swell = 0.55 + 0.45 * math.sin(2 * math.pi * 0.035 * t)
        out[i] = lp * 46.0 * swell
    for f, amp in ((55.0, 0.05), (82.41, 0.035)):
        w = 2 * math.pi * f
        for i in range(n):
            t = i / SR
            out[i] += amp * math.sin(w * t) * (0.6 + 0.4 * math.sin(2 * math.pi * 0.021 * t))
    fade = int(SR * 2.5)
    for i in range(min(fade, n)):
        out[i] *= i / fade
        out[n - 1 - i] *= i / fade
    return out


def sub(seconds: float = 1.1) -> list[float]:
    """The weight under a section change: a low tone that drops in pitch."""
    n = int(SR * seconds)
    out = [0.0] * n
    phase = 0.0
    for i in range(n):
        t = i / SR
        f = 78.0 * math.exp(-1.6 * t) + 34.0
        phase += 2 * math.pi * f / SR
        out[i] = math.sin(phase) * math.exp(-2.6 * t)
    return out


def whoosh(seconds: float = 0.9) -> list[float]:
    """Air moving — the transition sound, made from noise sweeping darker."""
    n = int(SR * seconds)
    out = [0.0] * n
    rnd = 99991
    lp = 0.0
    for i in range(n):
        rnd = (1103515245 * rnd + 12345) % (1 << 31)
        white = (rnd / (1 << 30)) - 1.0
        t = i / SR
        cut = 0.02 * math.exp(-2.2 * t) + 0.0015
        lp += cut * (white - lp)
        env = math.sin(math.pi * min(1.0, t / seconds)) ** 1.6
        out[i] = lp * 26.0 * env
    return out


def chord(names: list[str], seconds: float, decay: float = 2.2) -> list[float]:
    n = int(SR * seconds)
    out = [0.0] * n
    for k, name in enumerate(names):
        voice = struck(SCALE[name], seconds, decay=decay)
        offset = int(SR * 0.055 * k)               # spread, so it is played not triggered
        for i in range(n - offset):
            out[i + offset] += voice[i] * (0.85 ** k)
    return out


if __name__ == "__main__":
    c = json.loads((HERE / "cues.json").read_text())
    seconds = min(c["length"], 60.0)
    air = bed(seconds + 1.0)
    harmony = progression(seconds + 1.0)
    mixed = [a * 0.55 + h for a, h in zip(air, harmony, strict=True)]
    fade = int(SR * 3.0)
    for i in range(min(fade, len(mixed))):
        mixed[i] *= i / fade
        mixed[len(mixed) - 1 - i] *= i / fade
    write(AUDIO / "bed.wav", mixed, peak=0.62)
    write(AUDIO / "mark_in.wav", chord(["a3", "e4", "a4"], 3.0), peak=0.55)
    write(AUDIO / "mark_soft.wav", chord(["d4", "g4"], 2.4, decay=2.8), peak=0.4)
    write(AUDIO / "sub.wav", sub(), peak=0.6)
    write(AUDIO / "whoosh.wav", whoosh(), peak=0.32)
    write(AUDIO / "land.wav", chord(["e4", "a4", "c5"], 2.2, decay=2.6), peak=0.42)
    write(AUDIO / "close.wav", chord(["a2", "e3", "a3", "c4", "e4"], 5.0, decay=1.1), peak=0.62)
    # the dossier appearing: a brighter, higher figure than a section change
    write(AUDIO / "reveal.wav", chord(["e4", "a4", "c5", "e5"], 3.4, decay=1.5), peak=0.5)
    print(f"scored {seconds:.1f}s: bed, two marks, sub, whoosh, land, close")
