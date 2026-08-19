#!/usr/bin/env python3
"""
split_blobs.py — inside a flagged/messy region, list every individual speech
blob (bounded by real silence on both sides), with exact timestamps.

This is the automated version of manually reading ffmpeg silencedetect output
by hand. Use it on any region xref_silence.py flags as MERGE, to enumerate
the actual repeats/words hiding inside instead of trusting Whisper's one
merged token.

Usage: python3 split_blobs.py <video> <start> <end> [--noise -30] [--min-sil 0.1]
"""
import argparse, subprocess, re

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("start", type=float)
    ap.add_argument("end", type=float)
    ap.add_argument("--noise", default="-30")
    ap.add_argument("--min-sil", type=float, default=0.10,
                    help="minimum silence duration to count as a real gap (default 0.10s)")
    a = ap.parse_args()

    dur = a.end - a.start
    cmd = ["ffmpeg", "-ss", str(a.start), "-i", a.video, "-t", str(dur),
           "-af", f"silencedetect=noise={a.noise}dB:d={a.min_sil}", "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stderr
    starts = [float(m) for m in re.findall(r"silence_start: ([\d.]+)", out)]
    ends   = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", out)]
    sils = sorted(zip(starts, ends))

    # build speech blobs = the gaps BETWEEN silences (plus lead-in/trail-out)
    blobs = []
    cursor = 0.0
    for ss, se in sils:
        if ss > cursor:
            blobs.append((cursor, ss))
        cursor = max(cursor, se)
    if cursor < dur:
        blobs.append((cursor, dur))
    blobs = [(s, e) for s, e in blobs if e - s > 0.03]  # drop noise-floor slivers

    print(f"region {a.start:.2f}-{a.end:.2f} ({dur:.2f}s) — {len(blobs)} speech blob(s), {len(sils)} silence gap(s)\n")
    for i, (bs, be) in enumerate(blobs):
        print(f"  blob {i+1}: {a.start+bs:7.2f} - {a.start+be:7.2f}  ({be-bs:.2f}s)")
    print(f"\n→ Each blob is one candidate word/utterance, bounded by real silence.")
    print(f"  Cross-check against the waveform view for content, or pick by position/duration.")

if __name__ == "__main__":
    main()
