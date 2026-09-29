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
  python3 edl_to_captions.py <preview 自己的逐字稿.json> --no-edl [-o captions.json]
      逐字稿是「重轉剪好的 preview」得到的,時間已經是輸出時間軸,不用 EDL 對映。
      Whisper 對原始逐字稿的字級時間常系統性偏早 0.3-0.4 秒,EDL 對映會把每段
      第一個字吃掉;重轉 preview 再用 --no-edl 就沒有這個問題(我的剪輯偏好.md)。

  斷行只落在「詞與詞之間」:Whisper 常把中文切成單字 token(「自」「然」),
  光看寬度和停頓會把詞切兩半。有裝 jieba 就先斷詞(繁轉簡斷完再對回原字),
  沒裝就退回舊邏輯並提醒(setup.sh 會裝)。

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


def word_boundaries(words):
    """回傳可以斷行的位置集合:i 在集合裡 = 可以在 words[i] 後面斷。
    用 jieba 斷詞;沒裝 jieba 就回 None(呼叫端退回舊邏輯)。"""
    try:
        import logging
        import jieba
        jieba.setLogLevel(logging.ERROR)
    except ImportError:
        print("  ⚠ 沒有 jieba,斷行可能把詞切兩半。跑一次 setup.sh 就會裝。", file=sys.stderr)
        return None
    text = "".join(w["text"] for w in words)
    seg_src = text
    try:
        import opencc   # jieba 的詞典是簡體,繁體先轉簡再斷,切點對回原字(t2s 逐字等長)
        conv = opencc.OpenCC("t2s").convert(text)
        if len(conv) == len(text):
            seg_src = conv
    except Exception:
        pass
    ends, pos = set(), 0
    for tok in jieba.cut(seg_src, HMM=True):
        pos += len(tok)
        ends.add(pos)
    allowed, pos = set(), 0
    for i, w in enumerate(words):
        pos += len(w["text"])
        if pos in ends:
            allowed.add(i)
    return allowed


# ★ 停頓要從「聲音」量,不能從 Whisper 的字級時間算。Whisper 的中文 token 時間是
# 一個接一個黏著的,停頓被算進前一個字的長度(我的剪輯偏好.md:「逐字稿看起來 0.5s
# 實際常常是 0.9s」),所以 w.s - prev.e 幾乎永遠是 0,「在停頓處斷行」從來沒觸發過。
# 選段demo 2026-09-29 實測:整支 25 行沒有一行是在停頓斷的,全是寬度撐滿才硬斷。
# 做法:解出音訊 → 20ms 音量 → 門檻取「底噪 + 35% × (人聲 - 底噪)」(每支錄音音量不同,
# 固定 dB 門檻不通:原檔要 -30dB、loudnorm 過的 preview 要 -18dB 才抓得到)→ 連續 0.12s
# 以上低於門檻 = 停頓。兩個字之間的停頓 = [前字開始, 後字開始] 裡停頓的總長。
PAUSES = None   # [(start, end), ...] 秒,跟逐字稿同一條時間軸


def load_pauses(audio_path, min_len=0.12):
    import subprocess
    try:
        import numpy as np
        raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(audio_path), "-vn", "-ac", "1",
                              "-ar", "16000", "-f", "s16le", "-"], capture_output=True, check=True).stdout
    except Exception as e:
        print(f"  ⚠ 讀不到音訊({e}),停頓改用逐字稿時間(不準)", file=sys.stderr)
        return None
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    hop = 320   # 20ms
    n = len(x) // hop
    if n < 10:
        return None
    rms = np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    floor, speech = np.percentile(db, 10), np.percentile(db, 70)
    thr = floor + 0.35 * (speech - floor)
    quiet = db < thr
    spans, i = [], 0
    while i < n:
        if quiet[i]:
            j = i
            while j < n and quiet[j]:
                j += 1
            if (j - i) * 0.02 >= min_len:
                spans.append((i * 0.02, j * 0.02))
            i = j
        else:
            i += 1
    print(f"  停頓偵測:底噪 {floor:.1f}dB / 人聲 {speech:.1f}dB → 門檻 {thr:.1f}dB,{len(spans)} 個停頓",
          file=sys.stderr)
    return spans


def pause_between(a, b):
    """a、b 是相鄰的兩個字;回傳它們之間真實的停頓秒數。"""
    if PAUSES is None:
        return max(0.0, b["s"] - a["e"])
    lo, hi = a["s"], b["s"]
    return sum(max(0.0, min(e, hi) - max(s, lo)) for s, e in PAUSES if s < hi and e > lo)


# 斷在這些字後面,下一行會以「的/了」開頭或上一行停在介詞上,讀起來很怪
BAD_END = set("的在把被很跟給對從向往比會要都就")
BAD_START = set("的了嗎呢吧")


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

    # Whisper 字級時間普遍比實際發聲早 0.2-0.5 秒。舊判斷「w.start 落在 range 內」
    # 會把剪點邊界的字整個丟掉:聲音在、字幕沒字、零警告,下游(verify_cut)也看不出來
    # — 十份學員回報同一個坑。改成「字的區間與 range 有重疊就保留」:取重疊最大的
    # range,對映時間 clamp 進 range,跨過剪點起點的字印一行讓 AI 看得到。
    kept = []
    for w in words:
        best = None
        for r, off in zip(ranges, offsets):
            lo = max(w["start"], r["start"])
            hi = min(w["end"], r["end"] - EDGE_GUARD)
            ov = hi - lo
            if ov > 0 and (best is None or ov > best[0]):
                best = (ov, r, off, lo, hi)
        if best is None:
            continue
        ov, r, off, lo, hi = best
        if w["start"] < r["start"] - 0.001:
            print(f"  邊界字保留:「{w['text']}」start {w['start']:.2f} 早於剪點 "
                  f"{r['start']:.2f}(Whisper 時間偏早),已對齊剪點", file=sys.stderr)
        kept.append({
            "os": round(off + (lo - r["start"]), 3),
            "ov": round(ov, 3),
            "text": w["text"],
            "s": w["start"], "e": w["end"],
        })
    kept.sort(key=lambda k: k["os"])

    # monotonic clamp: a word may not extend past the next word's output start
    for i, k in enumerate(kept):
        natural = k["os"] + k.pop("ov")   # clamped span = audible portion in this range
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


# 斷行偏好:語氣詞後面、連接詞前面,都是自然句界。
# 學員回報(七份):舊版只看寬度硬切,詞被切兩半(「拒/絕」)、跨句硬切,
# 幾乎每支片的字幕都要人工全部重排。寬度只當上限,句界優先。
BREAK_AFTER = set("了嗎吧呢啊喔嘛耶啦囉唷哦呀")
BREAK_BEFORE = ("但是", "但", "所以", "因為", "然後", "如果", "可是", "而且",
                "還有", "接下來", "結果", "其實", "後來")


def best_break(cur, max_width, ok=None):
    """寬度到上限要斷行時,回頭在這行裡挑「最像句界」的位置,不要在講到一半硬切。
    評分 = 原始音訊的停頓長度 + 語氣詞/連接詞加成;位置至少要過 40% 寬,行不會太短。
    ok(i) = False 的位置是詞中間,永遠不選。"""
    total, widths = 0, []
    for x in cur:
        total += display_width(x["text"])
        widths.append(total)
    best_i, best_score = None, 0.0
    for i in range(len(cur) - 1):
        if widths[i] < max_width * 0.4:
            continue
        if ok is not None and not ok(i):
            continue
        gap = pause_between(cur[i], cur[i + 1])   # 原始音訊裡真實的停頓
        score = min(gap, 1.0)
        if cur[i]["text"] and cur[i]["text"][-1] in BREAK_AFTER:
            score += 0.25
        if any(cur[i + 1]["text"].startswith(p) for p in BREAK_BEFORE):
            score += 0.15
        if ok is not None:
            score += 0.05   # 詞界本身就是合格的斷點
            if cur[i]["text"][-1:] in BAD_END or cur[i + 1]["text"][:1] in BAD_START:
                continue    # 「很｜自然」「講｜的時候」:停頓再長也不斷在這
        if score >= best_score:   # 同分取後面的(行比較滿)
            best_i, best_score = i, score
    return best_i if best_score > 0.02 else None


def group_lines(words, gap_break, max_width):
    allowed = word_boundaries(words)
    base = 0   # cur[0] 在 words 裡的索引
    lines, cur = [], []

    def ok_at(j):   # 可以在 cur[j] 後面斷嗎
        return allowed is None or (base + j) in allowed

    for w in words:
        if cur:
            gap = pause_between(cur[-1], w)      # 原始音訊裡真實的停頓
            width = sum(display_width(x["text"]) for x in cur)
            bad = cur[-1]["text"][-1:] in BAD_END or w["text"][:1] in BAD_START
            if width >= max_width * 0.6 and gap >= gap_break and ok_at(len(cur) - 1) and not bad:
                lines.append(cur)
                base += len(cur)
                cur = []
            elif width >= max_width:
                bi = best_break(cur, max_width, ok_at if allowed is not None else None)
                if bi is None and allowed is not None:
                    # 沒有好斷點:退而求其次,取這行最後一個詞界
                    bi = next((j for j in range(len(cur) - 2, -1, -1) if ok_at(j)
                               and cur[j]["text"][-1:] not in BAD_END
                               and cur[j + 1]["text"][:1] not in BAD_START), None)
                if bi is not None and bi < len(cur) - 1:
                    lines.append(cur[:bi + 1])
                    base += bi + 1
                    cur = cur[bi + 1:]
                elif ok_at(len(cur) - 1):
                    lines.append(cur)
                    base += len(cur)
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
    ap.add_argument("edl", nargs="?")
    ap.add_argument("--no-edl", action="store_true",
                    help="逐字稿已是輸出時間軸(重轉 preview 得到的),不做 EDL 對映")
    ap.add_argument("-o", "--output", default="captions.json")
    ap.add_argument("--fixes", help="JSON dict of mishear fixes {wrong: right}")
    ap.add_argument("--gap", type=float, default=0.30,
                    help="original-audio pause (s) that allows a line break")
    ap.add_argument("--max-width", type=int, default=26,
                    help="max display width per line (CJK=2, ASCII=1)")
    ap.add_argument("--audio", help="量停頓用的音訊/影片。預設:EDL 模式用 EDL 的來源影片,"
                                    "--no-edl 模式一定要給(就是那支 preview)")
    args = ap.parse_args()

    if args.no_edl:
        ranges = [{"start": 0.0, "end": 1e9}]
    elif args.edl:
        ranges = json.loads(Path(args.edl).read_text(encoding="utf-8"))["ranges"]
    else:
        sys.exit("要給 edl.json,或用 --no-edl(逐字稿是重轉 preview 得到的)")
    fixes = json.loads(Path(args.fixes).read_text(encoding="utf-8")) if args.fixes else {}

    global PAUSES
    audio = args.audio
    if not audio and not args.no_edl:
        srcs = json.loads(Path(args.edl).read_text(encoding="utf-8")).get("sources", {})
        if len(srcs) == 1:
            audio = next(iter(srcs.values()))
    if audio:
        PAUSES = load_pauses(audio)
    else:
        print("  ⚠ 沒給 --audio,停頓改用逐字稿時間(Whisper 中文幾乎量不到停頓,斷行會偏硬)",
              file=sys.stderr)

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
