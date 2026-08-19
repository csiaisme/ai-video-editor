#!/usr/bin/env python3
"""
edl_to_captions.py — map a word-level transcript through an EDL onto the output
timeline and group the surviving words into caption lines.

This replaces the ad-hoc remapping the AI used to rewrite every session. It does
the deterministic plumbing; the AI (or user) then only does the judgment work:
fixing mishears, adjusting breaks, picking highlight keywords.

What it does:
  1. Keeps only words whose start falls inside an EDL range (minus a 30ms edge
     guard that matches render.py's audio fades — boundary false-starts drop out)
  2. Maps each kept word to output-timeline seconds (monotonic; word ends are
     clamped to the next word's start so cuts can't create overlaps)
  3. Merges consecutive Latin fragments Whisper split ("any"+"ways" → "anyways")
  4. Applies an optional mishear-fix dictionary (see --fixes)
  5. Groups into caption lines: break on a real pause in the ORIGINAL audio
     (default >= 0.30s) or when a line reaches max display width; absorbs
     orphan fragments (<= 2 CJK chars) into the previous line

Output: captions.json — [{"start": s, "end": s, "text": "..."}, ...]
        Times are OUTPUT-timeline seconds, ready for data-start/data-duration.

The fixes file (--fixes) is a JSON object {"wrong": "right", ...}. Multi-token
errors are matched on the joined line text after grouping, single tokens at the
word level. Keep a per-user dictionary in 我的剪輯偏好.md and pass it here.

Usage:
  python3 edl_to_captions.py <transcript.json> <edl.json> [-o captions.json]
      [--fixes fixes.json] [--gap 0.30] [--max-width 26]

  --max-width counts CJK chars as 2, ASCII as 1 (26 ≈ 13 個中文字).

After every EDL change, RE-RUN this from scratch. Never arithmetic-shift old
caption times — that is exactly the bug this script exists to prevent.
"""
import argparse, json, re, sys
from pathlib import Path

EDGE_GUARD = 0.03   # matches render.py's 30ms audio fades

ASCII_RE = re.compile(r"[A-Za-z]+$")


def display_width(s):
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


def load_words(transcript_path):
    data = json.loads(Path(transcript_path).read_text(encoding="utf-8"))
    words = data["words"] if isinstance(data, dict) else data
    out = []
    for w in words:
        t = (w.get("word", "") or w.get("text", "")).strip()
        if t:
            out.append({"text": t, "start": w["start"], "end": w["end"]})
    return out


def map_to_output(words, ranges):
    offsets, cum = [], 0.0
    for r in ranges:
        offsets.append(cum)
        cum += r["end"] - r["start"]

    kept = []
    for w in words:
        for r, off in zip(ranges, offsets):
            if r["start"] - 0.001 <= w["start"] < r["end"] - EDGE_GUARD:
                kept.append({
                    "os": round(off + (w["start"] - r["start"]), 3),
                    "text": w["text"],
                    "s": w["start"], "e": w["end"],
                })
                break
    kept.sort(key=lambda k: k["os"])

    # monotonic clamp: a word may not extend past the next word's output start
    for i, k in enumerate(kept):
        natural = k["os"] + (k["e"] - k["s"])
        k["oe"] = min(natural, kept[i + 1]["os"]) if i + 1 < len(kept) else natural
    return kept


def merge_latin(kept, max_gap=0.12):
    merged = []
    for k in kept:
        if (merged and ASCII_RE.match(k["text"]) and ASCII_RE.match(merged[-1]["text"])
                and (k["s"] - merged[-1]["e"]) < max_gap):
            merged[-1]["text"] += k["text"]
            merged[-1]["e"] = k["e"]
            merged[-1]["oe"] = k["oe"]
        else:
            merged.append(dict(k))
    return merged


def group_lines(words, gap_break, max_width):
    lines, cur = [], []
    for w in words:
        if cur:
            gap = w["s"] - cur[-1]["e"]          # pause in ORIGINAL audio
            width = sum(display_width(x["text"]) for x in cur)
            if (width >= max_width * 0.6 and gap >= gap_break) or width >= max_width:
                lines.append(cur)
                cur = []
        cur.append(w)
    if cur:
        lines.append(cur)

    # absorb orphans (tiny fragments) into the previous line
    i = 1
    while i < len(lines):
        w = sum(display_width(x["text"]) for x in lines[i])
        close = (lines[i][0]["s"] - lines[i - 1][-1]["e"]) < 0.6
        if w <= 4 and close:
            lines[i - 1] += lines[i]
            del lines[i]
        else:
            i += 1
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript")
    ap.add_argument("edl")
    ap.add_argument("-o", "--output", default="captions.json")
    ap.add_argument("--fixes", help="JSON dict of mishear fixes {wrong: right}")
    ap.add_argument("--gap", type=float, default=0.30,
                    help="original-audio pause (s) that allows a line break")
    ap.add_argument("--max-width", type=int, default=26,
                    help="max display width per line (CJK=2, ASCII=1)")
    args = ap.parse_args()

    ranges = json.loads(Path(args.edl).read_text(encoding="utf-8"))["ranges"]
    fixes = json.loads(Path(args.fixes).read_text(encoding="utf-8")) if args.fixes else {}

    words = merge_latin(map_to_output(load_words(args.transcript), ranges))

    # single-token fixes before grouping
    for w in words:
        w["text"] = fixes.get(w["text"], w["text"])

    lines = group_lines(words, args.gap, args.max_width)

    caps = []
    for ln in lines:
        text = "".join(x["text"] for x in ln)
        for wrong, right in fixes.items():      # multi-token fixes on joined text
            text = text.replace(wrong, right)
        caps.append({
            "start": round(ln[0]["os"], 2),
            "end": round(ln[-1]["oe"], 2),
            "text": text,
        })

    overlaps = [i for i in range(1, len(caps))
                if caps[i]["start"] < caps[i - 1]["end"] - 0.001]
    if overlaps:
        sys.exit(f"BUG: overlapping captions at indexes {overlaps} — report this")

    Path(args.output).write_text(
        json.dumps(caps, ensure_ascii=False, indent=1), encoding="utf-8")
    for i, c in enumerate(caps):
        print(f"{i:3} {c['start']:7.2f}-{c['end']:7.2f}  {c['text']}")
    print(f"\n{len(caps)} lines → {args.output}  (no overlaps)")


if __name__ == "__main__":
    main()
