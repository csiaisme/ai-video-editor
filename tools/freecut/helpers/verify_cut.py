"""verify_cut.py — machine seam-check for a rendered cut preview.

The problem it kills: when you drop NG takes / fumbles from an EDL, the join can
clip a word ("大概花了" → "太花了") or leak a word from a dropped section, or
crash two sentences together ("我自己" + "我大概"). Those are invisible in the
EDL and only surface when a human listens — which cost this project ~7 rounds.

What it does: re-transcribes the rendered preview and diffs it against the
*intended* script (the source-transcript words that fall inside the EDL ranges,
in output order). Whisper mishears words the same way in both, so small
substitutions cancel out; what survives the diff is STRUCTURAL — a clipped or
leaked or collided word right at a seam. Those get printed with an approximate
output timestamp so the agent can look before showing the human anything.

Usage:
    python helpers/verify_cut.py <preview.mp4> <source_transcript.json> <edl.json>
    python helpers/verify_cut.py <preview.mp4> <source_transcript.json> <edl.json> --fixes fixes.json

Exit code 0 = clean (no structural mismatch), 1 = mismatches found (see report).
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import tempfile
from pathlib import Path

# reuse the shared whisper path + audio extraction
from transcribe import extract_audio, transcribe_whisper

# keep letters/digits/CJK; drop spaces + punctuation so Whisper's punctuation
# noise doesn't create fake diffs.
_KEEP = re.compile(r"[0-9A-Za-z一-鿿]")


def _norm(s: str, fixes: dict[str, str] | None = None) -> str:
    if fixes:
        for bad, good in fixes.items():
            s = s.replace(bad, good)
    return "".join(ch for ch in s if _KEEP.match(ch)).lower()


def expected_from_edl(transcript: dict, edl: dict) -> str:
    """Concatenate source words that fall inside each EDL range, in the order the
    ranges appear (so reordered/kept-later takes are handled)."""
    words = [w for w in transcript.get("words", []) if w.get("type") != "spacing"]
    out = []
    for r in edl.get("ranges", []):
        s, e = float(r["start"]), float(r["end"])
        for w in words:
            mid = (float(w["start"]) + float(w["end"])) / 2.0
            if s <= mid <= e:
                out.append((w.get("text") or ""))
    return "".join(out)


def actual_from_preview(preview: Path, language: str | None) -> tuple[str, list]:
    """Transcribe the preview fresh (no cache) and return (joined_text, words)."""
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / "a.wav"
        extract_audio(preview, audio)
        payload = transcribe_whisper(audio, language=language)
    words = [w for w in payload.get("words", []) if w.get("type") != "spacing"]
    return "".join((w.get("text") or "") for w in words), words


def _char_time_map(words: list) -> list[float]:
    """One start-time per kept character of the joined actual text."""
    times: list[float] = []
    for w in words:
        for _ in (w.get("text") or ""):
            times.append(float(w["start"]))
    return times


def _fmt(t: float) -> str:
    t = max(0, int(t))
    return f"{t // 60}:{t % 60:02d}"


def verify(preview: Path, transcript_path: Path, edl_path: Path,
           fixes: dict[str, str] | None, language: str | None,
           min_block: int = 4) -> int:
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    edl = json.loads(edl_path.read_text(encoding="utf-8"))

    exp_raw = expected_from_edl(transcript, edl)
    act_raw, act_words = actual_from_preview(preview, language)

    exp = _norm(exp_raw, fixes)
    act = _norm(act_raw, fixes)

    # map each kept char of `act` back to a timestamp for reporting
    act_char_times: list[float] = []
    for w in act_words:
        for ch in (w.get("text") or ""):
            if _KEEP.match(ch):
                act_char_times.append(float(w["start"]))

    sm = difflib.SequenceMatcher(a=exp, b=act, autojunk=False)
    ratio = sm.ratio()
    mismatches = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        # only flag blocks big enough to be structural, not single-char mishears
        if max(i2 - i1, j2 - j1) < min_block:
            continue
        t = act_char_times[j1] if j1 < len(act_char_times) else (
            act_char_times[-1] if act_char_times else 0.0)
        mismatches.append({
            "time": t,
            "tag": tag,
            "expected": exp_raw_slice(exp_raw, exp, i1, i2),
            "actual": act_raw_slice(act_raw, act, j1, j2),
        })

    print(f"cut verify — overall similarity {ratio*100:.1f}%")
    print(f"  intended chars: {len(exp)}   heard chars: {len(act)}")
    if not mismatches:
        print("  ✓ no structural seam mismatch (small mishears ignored)")
        return 0
    print(f"  ✗ {len(mismatches)} region(s) to check by ear/waveform:\n")
    for m in mismatches:
        print(f"  ~{_fmt(m['time'])}  [{m['tag']}]")
        print(f"      intended: …{m['expected']}…")
        print(f"      heard:    …{m['actual']}…")
    print("\n  Note: Whisper mishears words (中英→綜音 etc.); judge by whether a")
    print("  word looks CLIPPED, LEAKED from a cut, or COLLIDED — not by exact chars.")
    return 1


# The normalized strings drop chars, so to show readable context we re-slice the
# raw strings proportionally. Approximate but good enough for a human pointer.
def exp_raw_slice(raw: str, norm: str, i1: int, i2: int) -> str:
    return _proportional(raw, norm, i1, i2)


def act_raw_slice(raw: str, norm: str, j1: int, j2: int) -> str:
    return _proportional(raw, norm, j1, j2)


def _proportional(raw: str, norm: str, a: int, b: int) -> str:
    if not norm:
        return ""
    ra = int(len(raw) * a / len(norm))
    rb = int(len(raw) * b / len(norm))
    lo = max(0, ra - 6)
    hi = min(len(raw), rb + 6)
    return raw[lo:hi]


def main() -> None:
    ap = argparse.ArgumentParser(description="Machine seam-check a rendered cut preview")
    ap.add_argument("preview", type=Path)
    ap.add_argument("transcript", type=Path, help="source (whole-video) transcript JSON")
    ap.add_argument("edl", type=Path)
    ap.add_argument("--fixes", type=Path, default=None, help="錯字字典 JSON to normalize both sides")
    ap.add_argument("--language", default="zh")
    ap.add_argument("--min-block", type=int, default=4,
                    help="min diverging chars to flag (smaller = pickier)")
    args = ap.parse_args()

    for p in (args.preview, args.transcript, args.edl):
        if not p.exists():
            sys.exit(f"not found: {p}")
    fixes = json.loads(args.fixes.read_text(encoding="utf-8")) if args.fixes and args.fixes.exists() else None

    rc = verify(args.preview, args.transcript, args.edl, fixes, args.language, args.min_block)
    sys.exit(rc)


if __name__ == "__main__":
    main()
