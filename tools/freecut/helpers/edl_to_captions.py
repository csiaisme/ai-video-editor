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

Output: captions.json — [{"start": s, "end": s, "text": "...", "char_times": [...]}, ...]
        Times are OUTPUT-timeline seconds, ready for data-start/data-duration.
        char_times = 每個字(含標點、英文字間空格)開始的秒數,跟 text 一樣長。
        gen_captions 用它照「字的位置」算第二行 / 螢光筆什麼時候進場(不用文字比對,
        Whisper 聽錯字也對得到)。長度跟 text 對不上(改過字)就自動退回比例估算。

  斷句 / 改字之後要補回字級時間(加 line2 / kw_fx 前跑一次):
  python3 edl_to_captions.py <transcript.json> <edl.json> --attach captions.json [--fixes fixes.json]
      只重算每句的 char_times(逐字對齊,斷句、併句、改幾個字都對得上),
      start/end/text/hl/line2 一個都不動。改太多對不上的那句就不給,退回比例估算。

The fixes file (--fixes) is a JSON object {"wrong": "right", ...}. Multi-token
errors are matched on the joined line text after grouping, single tokens at the
word level. Keep a per-user dictionary in 我的剪輯偏好.md and pass it here.

Usage:
  python3 edl_to_captions.py <transcript.json> <edl.json> [-o captions.json]
      [--fixes fixes.json] [--gap 0.30] [--max-width 26]
  python3 edl_to_captions.py <preview 自己的逐字稿.json> [edl.json] --no-edl [-o captions.json]
      (--no-edl 時 edl.json 只拿來找剪接點當斷行參考,建議一定要給)
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

# ★ 英文(或其他拼音文字)影片走另一條路。Whisper 在英文模式給的是一個個完整的字,
# 不是中文模式那種「C」「la」「ude」碎片;中文的作法(字直接黏起來、ASCII 碎片合併、
# jieba 斷詞)套在英文上會變成 "SoClaudebannedmyaccountyesterday."(IMG_2135 實測)。
# LATIN = True:字之間加空格、不合併碎片、每個字界都能斷、句號問號優先斷行。
# 中文影片 LATIN = False,走原本的路,輸出一個字都不變。
LATIN = False
CJK_LANGS = {"zh", "ja", "ko", "yue", "chinese", "japanese", "korean", "cantonese"}
PUNCT_START = tuple(".,!?;:%)]}”’…")          # 這些開頭的 token 前面不加空格
BREAK_BEFORE_EN = {"and", "but", "so", "because", "if", "when", "then", "or"}


def is_latin_transcript(words, lang=None):
    """逐字稿有記語言就照記的;沒記(舊逐字稿)就看 token:六成以上是拼音字 = 英文模式。
    中文逐字稿裡夾的英文術語(Claude、MVP)只佔少數,不會被誤判。"""
    if lang:
        return lang.lower() not in CJK_LANGS
    toks = [w["text"] for w in words if w["text"].strip()]
    if not toks:
        return False
    latin = sum(1 for t in toks
                if not any(ord(c) >= 0x2E80 for c in t) and any(c.isalpha() for c in t))
    return latin / len(toks) >= 0.6


def join_tokens(texts):
    """中文:直接黏起來(原本的行為)。英文:字之間加空格,標點前面不加。"""
    if not LATIN:
        return "".join(texts)
    out = ""
    for t in texts:
        if out and not t.startswith(PUNCT_START):
            out += " "
        out += t
    return out


def join_timed(tokens):
    """join_tokens 的帶時間版:回傳 (text, 每個字的開始秒數)。一個 token 有好幾個字
    (「3000」「anyways」)就把它的 [os, oe] 平均分給每個字;英文字間的空格 = 下個字的時間。"""
    text, times = "", []
    for x in tokens:
        if LATIN and text and not x["text"].startswith(PUNCT_START):
            text += " "
            times.append(x["os"])
        n = len(x["text"])
        span = max(0.0, x["oe"] - x["os"])
        times += [x["os"] + span * k / n for k in range(n)]
        text += x["text"]
    return text, times


def replace_timed(text, times, pattern, repl):
    """re.sub 的帶時間版(fixes 用):被換掉的那段時間,平均分給換上去的字。"""
    out, out_t, pos = "", [], 0
    for m in pattern.finditer(text):
        if m.end() == m.start():
            continue
        out += text[pos:m.start()]
        out_t += times[pos:m.start()]
        t0, t1 = times[m.start()], times[m.end() - 1]
        n = len(repl)
        out_t += [t0 + (t1 - t0) * k / max(1, n - 1) for k in range(n)]
        out += repl
        pos = m.end()
    return out + text[pos:], out_t + times[pos:]


def fix_pattern(wrong):
    if LATIN:   # 不分大小寫,而且要整個字對到("Boars" 不會吃掉 "Boarsmith" 的一半)
        return re.compile(r"(?<!\w)" + re.escape(wrong) + r"(?!\w)", re.IGNORECASE)
    return re.compile(re.escape(wrong))


def attach_char_times(caps, stream, stream_t):
    """把逐字稿的字級時間逐字對齊到「已經斷過句 / 改過字」的 captions 上。
    用整串文字做序列比對(不是一句一句找字串):斷句、併句、改幾個字、加標點都對得上,
    Whisper 聽錯的字也只是那幾個字沒對到,用前後的字內插。一句裡對到不到一半就不給。"""
    import difflib
    flat, owner = "", []
    for i, c in enumerate(caps):
        flat += c["text"]
        owner += [(i, k) for k in range(len(c["text"]))]
    got = [[None] * len(c["text"]) for c in caps]
    sm = difflib.SequenceMatcher(None, stream, flat, autojunk=False)
    for a, b, n in sm.get_matching_blocks():
        for k in range(n):
            i, j = owner[b + k]
            got[i][j] = stream_t[a + k]
    ok, bad = 0, []
    for i, c in enumerate(caps):
        t = got[i]
        real = [j for j, ch in enumerate(c["text"]) if not ch.isspace()]
        hit = [j for j in real if t[j] is not None]
        s, e = float(c["start"]), float(c["end"])
        mid = sorted(t[j] for j in hit)[len(hit) // 2] if hit else None
        if not real or len(hit) * 2 < len(real) or not (s - 0.6 <= mid <= e + 0.3):
            c.pop("char_times", None)
            bad.append(i)
            continue
        for j in range(len(t)):                 # 沒對到的字:前後內插(頭尾就貼最近的)
            if t[j] is None:
                lo = next((x for x in range(j - 1, -1, -1) if got[i][x] is not None), None)
                hi = next((x for x in range(j + 1, len(t)) if got[i][x] is not None), None)
                if lo is not None and hi is not None:
                    t[j] = t[lo] + (t[hi] - t[lo]) * (j - lo) / (hi - lo)
                else:
                    t[j] = t[lo] if lo is not None else t[hi]
        for j in range(1, len(t)):              # 時間只能往後走
            t[j] = max(t[j], t[j - 1])
        c["char_times"] = [round(x, 2) for x in t]
        ok += 1
    return ok, bad


def dump_captions(caps):
    """indent=1 跟以前一樣好讀,但 char_times 壓成一行(不然一個字一行,檔案長十倍)。"""
    s = json.dumps(caps, ensure_ascii=False, indent=1)
    return re.sub(r'"char_times": \[\s*([-0-9.,\s]*?)\s*\]',
                  lambda m: '"char_times": [' + ",".join(v.strip() for v in m.group(1).split(",")) + "]", s)


def display_width(s):
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


def line_width(tokens):
    """一行字幕的顯示寬度。英文模式要把字間空格算進去。"""
    return display_width(join_tokens([x["text"] for x in tokens]))


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
    # 中文 Whisper 把停頓算進「前一個字」的長度,所以量 [前字開始, 後字開始]。
    # 英文 Whisper 剛好相反:停頓被算進「後一個字」的開頭(IMG_2135 實測:"chat, ... I would"
    # 那 1.4 秒停頓落在 "I" 的 95.26-96.72 裡),照中文的窗會晚一個字 → 斷出單獨一行 "I"。
    # 英文改量 [前字結束, 後字結束],把被後字吞進去的停頓算回這兩個字之間。
    lo, hi = (a["e"], b["e"]) if LATIN else (a["s"], b["s"])
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

# 剪接點(EDL 每段的起點,輸出時間軸)。剪點幾乎都落在句界,是最可靠的斷行位置。
# 為什麼要它:有背景音樂的素材(直播、有墊樂的錄影)量不到停頓,舊版只能照寬度硬切,
# 斷出前一句尾巴接下一句開頭的跨句行(2026-10-05 直播素材,21 句全部手動重斷)。
SEAMS = []
# 不可切開的位置(words 的索引 i = 不能斷在 words[i] 後面):fixes 裡的多字詞(人名、課名),
# 例:兩個字的人名被 Whisper 拆成兩個 token,舊版把人名從中間斷成兩行。
NOBREAK = set()


def seam_between(a, b):
    """a 後面是不是剪接點(mark_seams 事先標好)。"""
    return bool(a.get("seam_after"))


def mark_seams(words):
    """每個剪接點只標一個斷點:最後一個「開頭不晚於剪點 + 0.05 秒」的字前面。
    Whisper 字級時間偏早 0.2-0.4 秒,用寬鬆的時間窗會一次命中好幾個字界、斷錯一個字
    (2026-10-05 實測把下一句的第一個詞斷進上一行);取「剪點前最後開頭的字」就落在剪點上。"""
    for t in SEAMS:
        cand = [i for i in range(len(words) - 1) if t - 0.6 <= words[i + 1]["os"] <= t + 0.05]
        if cand:
            words[cand[-1]]["seam_after"] = True


def protect_fix_words(words, fixes):
    """fixes 的鍵跟值(長度 >= 2)在字串裡出現的地方,裡面的字界都不准斷。"""
    joined, owner = "", []
    for i, w in enumerate(words):
        if LATIN and joined and not w["text"].startswith(PUNCT_START):
            joined += " "          # 英文模式字間有空格,fixes 的多字詞("for no reason")才對得到
            owner.append(i)
        joined += w["text"]
        owner += [i] * len(w["text"])
    hay = joined.lower() if LATIN else joined          # 英文不分大小寫(lower 不改長度,索引對得上)
    for term in {t for kv in fixes.items() for t in kv if len(t) >= 2}:
        needle = term.lower() if LATIN else term
        start = hay.find(needle)
        while start != -1:
            idx = owner[start:start + len(term)]
            for i in range(idx[0], idx[-1]):
                NOBREAK.add(i)
            start = hay.find(needle, start + 1)


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
        if seam_between(cur[i], cur[i + 1]):
            score += 0.6                           # 剪接點:幾乎一定是句界
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
        if (base + j) in NOBREAK:
            return False
        return allowed is None or (base + j) in allowed

    for w in words:
        if cur:
            gap = pause_between(cur[-1], w)      # 原始音訊裡真實的停頓
            width = sum(display_width(x["text"]) for x in cur)
            bad = cur[-1]["text"][-1:] in BAD_END or w["text"][:1] in BAD_START
            seam = seam_between(cur[-1], w)
            if ((width >= max_width * 0.6 and gap >= gap_break) or (seam and width >= max_width * 0.3)) \
                    and ok_at(len(cur) - 1) and not bad:
                lines.append(cur)
                base += len(cur)
                cur = []
            elif width >= max_width:
                bi = best_break(cur, max_width, ok_at if (allowed is not None or NOBREAK) else None)
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


# ---- 英文斷行(LATIN 模式才用;中文完全走上面的 group_lines)----
# 上面那套是為中文寫的:貪婪填滿、再回頭找停頓/語氣詞。套在英文上會斷出
# "means. And then I"、"to Claude. If"、行尾停在 "my"(2026-10-05 實測)。
# 英文最可靠的斷點是句號:Whisper 英文 token 自帶標點("yesterday.")。所以:
#   1. 先切成句子(句號問號驚嘆號、剪接點、≥0.5 秒的真實停頓)
#   2. 塞得下就一句一行;塞不下就把那一句切成「最平均、斷點最自然」的幾行:
#      優先切在逗號、and/but/so 前面、真實停頓;行尾不准停在 my / the / to 這種字。
BAD_END_EN = {"a", "an", "the", "to", "of", "my", "your", "our", "their", "his", "her",
              "its", "in", "on", "at", "for", "with", "from", "by", "and", "but", "or",
              "so", "i", "we", "you", "they", "he", "she", "that", "this", "if", "because"}


def _bare(t):
    return t.lower().strip(".,!?;:\"“”()")


def _split_sentence_latin(sent, max_width):
    """一句太長時,用動態規劃找總代價最低的切法。
    每多一行 +1;每行越空 +(空的比例)²;斷點加分:逗號 0.6、and/but/so 前 0.35、停頓最多 0.6;
    行尾是 my/the/to 這種字 -1.5(寧可多一行也不要)。fixes 的多字詞中間不能切。"""
    n = len(sent)
    if n <= 1 or line_width(sent) <= max_width:
        return [sent]

    def bonus(k):   # 在 sent[k] 後面斷
        a, b = sent[k], sent[k + 1]
        s = min(pause_between(a, b), 0.6)
        if a["text"][-1:] in ",;:":
            s += 0.6
        if _bare(b["text"]) in BREAK_BEFORE_EN:
            s += 0.35
        if _bare(a["text"]) in BAD_END_EN:
            s -= 1.5
        return s

    INF = float("inf")
    dp = [(0.0, -1)] + [(INF, -1)] * n        # dp[j] = (sent[:j] 的最低代價, 上一個斷點)
    for j in range(1, n + 1):
        for i in range(j - 1, -1, -1):
            w = line_width(sent[i:j])
            if w > max_width and j - i > 1:
                break                          # 再往前只會更寬
            if dp[i][0] == INF or (i > 0 and sent[i - 1].get("_i") in NOBREAK):
                continue
            slack = (max_width - min(w, max_width)) / max_width
            cost = dp[i][0] + 1.0 + slack * slack - (bonus(i - 1) if i > 0 else 0.0)
            if cost < dp[j][0]:
                dp[j] = (cost, i)
    if dp[n][0] == INF:                        # fixes 多字詞比一行還寬:整句一行,交給 gen_captions 縮字級
        return [sent]
    out, j = [], n
    while j > 0:
        i = dp[j][1]
        out.append(sent[i:j])
        j = i
    return out[::-1]


def group_lines_latin(words, gap_break, max_width):
    for k, w in enumerate(words):
        w["_i"] = k                            # 全域索引,NOBREAK 用
    sents, cur = [], []
    for k, w in enumerate(words):
        cur.append(w)
        nxt = words[k + 1] if k + 1 < len(words) else None
        if nxt is None:
            break
        # 不看剪接點:mark_seams 為了中文 Whisper 時間偏早,會標在剪點前一個字,英文會因此
        # 斷出 "know what that / means."。英文的句界本來就有標點,剪點加不了資訊。
        # 停頓斷句要這行已經有 4 成寬:不然 "So" / "Boris," 這種單字會自己一行閃一下就沒
        # (中文那套也有同樣的防呆,是 6 成)。句號問號照斷,不受這個限制。
        # 行尾也不准是 because / the / my(講話時停在這種字很正常,但讀起來像斷掉)
        long_enough = line_width(cur) >= max_width * 0.4 and _bare(w["text"]) not in BAD_END_EN
        if k not in NOBREAK and (w["text"][-1:] in ".?!"
                                 or (long_enough and pause_between(w, nxt) >= max(gap_break, 0.5))):
            sents.append(cur)
            cur = []
    if cur:
        sents.append(cur)
    lines = []
    for s in sents:
        lines.extend(_split_sentence_latin(s, max_width))
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
    ap.add_argument("--max-width", type=int, default=None,
                    help="max display width per line (CJK=2, ASCII=1)。預設 26;"
                         "有給 --font-size 就照字級算,直接給這個就以這個為準")
    ap.add_argument("--font-size", type=int, default=None,
                    help="填跟 gen_captions --font-size 一樣的數字。英文影片:一行的長度照字級算到剛好放得下;"
                         "中文影片不受影響,照舊固定 26")
    ap.add_argument("--video-width", type=int, default=1080, help="跟 gen_captions --w 一樣")
    ap.add_argument("--audio", help="量停頓用的音訊/影片。預設:EDL 模式用 EDL 的來源影片,"
                                    "--no-edl 模式一定要給(就是那支 preview)")
    ap.add_argument("--attach", metavar="CAPTIONS_JSON",
                    help="不重新斷句:只幫這份(斷過句/改過字的)captions.json 補回每句的 char_times,其他欄位不動")
    args = ap.parse_args()

    edl_ranges = json.loads(Path(args.edl).read_text(encoding="utf-8"))["ranges"] if args.edl else None
    if args.no_edl:
        ranges = [{"start": 0.0, "end": 1e9}]
    elif edl_ranges:
        ranges = edl_ranges
    else:
        sys.exit("要給 edl.json,或用 --no-edl(逐字稿是重轉 preview 得到的)")
    # --no-edl 也可以把 edl.json 一起給:不拿來對映時間,只拿剪接點當斷行參考
    if edl_ranges:
        cum = 0.0
        for r in edl_ranges[:-1]:
            cum += r["end"] - r["start"]
            SEAMS.append(round(cum, 3))
    elif args.no_edl:
        print("  ⚠ --no-edl 沒給 edl.json:不知道剪接點在哪,有背景音樂的素材斷行會跨句。"
              "建議:edl_to_captions.py <preview逐字稿> 工作檔/edl.json --no-edl --audio …", file=sys.stderr)
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

    global LATIN
    raw = load_words(args.transcript)
    tdata = json.loads(Path(args.transcript).read_text(encoding="utf-8"))
    LATIN = is_latin_transcript(raw, tdata.get("language") if isinstance(tdata, dict) else None)
    if LATIN:
        print("  英文模式:字間加空格、不合併英文碎片、句號問號優先斷行", file=sys.stderr)

    # ★ 一行多長要跟字級一起算。固定 26 是照 56px 抓的;字級 66 時同樣長度放不下,
    #   gen_captions 只好把那句縮小 — IMG_2135(66px)82 句裡 31 句被縮,大小忽大忽小。
    #   字幕框寬度用 gen_captions 的同一條公式(classic 款,左右各讓 26px padding),
    #   每個字的寬也照它的估法:全形字 = 1 個字級寬,英文字母約 0.55 個字級寬。
    #   只套英文:中文照舊固定 26(Jake 2026-10-05 決定。中文在 66px 換算是一行 11 字,縮字級的句子
    #   變少,但多出「趨勢線然後」這種短行 — 中文斷行品質卡在斷詞,不在寬度,先不動)。
    if args.max_width is None:
        if args.font_size and LATIN:
            W = args.video_width
            room = (W - 2 * round(W * 0.11)) - 2 * 26 - 8
            args.max_width = int(room / (0.55 * args.font_size))
            print(f"  一行長度照字級算:{args.font_size}px → --max-width {args.max_width}", file=sys.stderr)
        else:
            args.max_width = 26
    kept = map_to_output(raw, ranges)
    words = kept if LATIN else merge_latin(kept)   # 碎片合併只給中文模式(英文會把整句黏成一個字)

    # 英文不分大小寫:Whisper 大小寫很隨機(同一個詞這次 "Thron pick"、下次 "Thron Pick"),
    # fixes 只寫一種就要兩種都對到。中文模式照舊(字一模一樣才換)。
    fixes_ci = {k.lower(): v for k, v in fixes.items()} if LATIN else {}

    # single-token fixes before grouping
    for w in words:
        t = w["text"]
        if t in fixes:
            w["text"] = fixes[t]
        elif LATIN:   # 英文 token 自帶標點:"Boars," 也要對到 fixes 的 "Boars"
            core = t.rstrip(".,!?;:")
            if core.lower() in fixes_ci:
                w["text"] = fixes_ci[core.lower()] + t[len(core):]
    protect_fix_words(words, fixes)

    if args.attach:
        # 整串一起套 fixes(跨句的多字錯字也換得到),再逐字對齊到現有的句子上
        stream, stream_t = join_timed(words)
        for wrong, right in fixes.items():
            stream, stream_t = replace_timed(stream, stream_t, fix_pattern(wrong), right)
        path = Path(args.attach)
        caps = json.loads(path.read_text(encoding="utf-8"))
        ok, bad = attach_char_times(caps, stream, stream_t)
        path.write_text(dump_captions(caps), encoding="utf-8")
        print(f"補回字級時間:{len(caps)} 句裡 {ok} 句對得上 → {path}")
        for i in bad:
            print(f"  sub-{i} 「{caps[i]['text']}」對不上(改太多或時間不符),這句的第二行/螢光筆會用比例估算")
        return

    mark_seams(words)

    lines = (group_lines_latin if LATIN else group_lines)(words, args.gap, args.max_width)

    caps = []
    for ln in lines:
        text, times = join_timed(ln)
        for wrong, right in fixes.items():      # multi-token fixes on joined text(時間跟著字走)
            text, times = replace_timed(text, times, fix_pattern(wrong), right)
        caps.append({
            "start": round(ln[0]["os"], 2),
            "end": round(ln[-1]["oe"], 2),
            "text": text,
            "char_times": [round(t, 2) for t in times],
        })

    overlaps = [i for i in range(1, len(caps))
                if caps[i]["start"] < caps[i - 1]["end"] - 0.001]
    if overlaps:
        sys.exit(f"BUG: overlapping captions at indexes {overlaps} — report this")

    Path(args.output).write_text(dump_captions(caps), encoding="utf-8")
    for i, c in enumerate(caps):
        print(f"{i:3} {c['start']:7.2f}-{c['end']:7.2f}  {c['text']}")
    print(f"\n{len(caps)} lines → {args.output}  (no overlaps)")


if __name__ == "__main__":
    main()
