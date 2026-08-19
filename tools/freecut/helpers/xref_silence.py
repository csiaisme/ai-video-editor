#!/usr/bin/env python3
"""
xref_silence.py — cross-reference a word-level transcript against real audio silence.

Whisper collapses repeated words and merges trailing pauses into a word's end
time. This script finds those lies BEFORE the first cut by comparing each
transcript word span against ffmpeg silencedetect ground truth.

Flags:
  MERGE   — a single word token contains a real silence >= gap threshold
            (likely repeated words collapsed, or dead air stuck on the word end)
  LONG    — word duration is abnormally long for its character count
            (a short word holding a long timestamp = suspicious)
  FILLER  — a 1-2 char token whose ACTUAL sound is short and sits alone in silence
            (long silence both before and after). Whisper turns "um / 呃" into a real
            word — verified: a 0.28s filler at 12.87 came out as the word 你, and the
            speaker never said 你 at all. Nothing downstream can tell: it looks like a
            perfectly normal word in the transcript. Check these by ear before you cut,
            and never build a hook on one.
  SNAP    — a short loud transient (finger snap / clap / tap) sitting in a silence with
            no word on it. The default silence pass treats it as noise and drops it, so
            it is invisible everywhere else — and then a cut removes it and the user
            says "you deleted my finger snap". Effects that must land on such a sound
            take their timing from here, never from word timings.
  GAP     — a wide space BETWEEN two transcript words that is mostly NOT silence,
            i.e. there is audible speech the transcript has no word for. This is the
            classic Whisper blind spot: a half-said word / false start / repeat that
            Whisper swallowed. It never appears as a token, so nothing downstream sees
            it — only this cross-check against real audio does. Almost every "the cut
            keeps dropping the wrong syllable" case lives in a GAP region.

Usage: python3 xref_silence.py <video> <transcript.json> [--noise -30] [--gap 0.30]
"""
import argparse, json, subprocess, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sound_map import pcm, envelope, find_transients, WIN   # 同一個資料夾

def mean_volume(video):
    """This file's own average loudness (dBFS), via ffmpeg volumedetect.
    Used to set the GAP threshold RELATIVE to the recording, not a fixed dB —
    a fixed value floods a loud/noisy recording and misses a quiet one
    (verified: sonnet floor ~-56dB, demo ~-45dB; -40 is clean on one, floods
    the other). -vn: audio only, no video decode (fast even on 4K)."""
    out = subprocess.run(["ffmpeg", "-vn", "-i", video, "-af", "volumedetect",
                          "-f", "null", "-"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stderr
    m = re.search(r"mean_volume:\s*(-?[\d.]+)", out)
    return float(m.group(1)) if m else None

def silence_windows(video, noise_db, min_sil):
    # -vn: this is audio analysis; decoding 4K video frames just to scan the
    # audio track wastes minutes. Audio-only makes it fast even on 4K sources.
    cmd = ["ffmpeg", "-vn", "-i", video, "-af",
           f"silencedetect=noise={noise_db}dB:d={min_sil}", "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stderr
    starts = [float(m) for m in re.findall(r"silence_start: ([\d.]+)", out)]
    ends   = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", out)]
    return list(zip(starts, ends))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("transcript")
    ap.add_argument("--noise", default="-30", help="silence threshold dB (default -30)")
    ap.add_argument("--gap", type=float, default=0.30,
                    help="min silence inside a word to flag MERGE (default 0.30s)")
    ap.add_argument("--long", type=float, default=0.35,
                    help="seconds-per-character over which a word is flagged LONG (default 0.35)")
    ap.add_argument("--gap-span", type=float, default=0.30,
                    help="min space between two words to consider for a GAP flag (default 0.30s)")
    ap.add_argument("--gap-voice", type=float, default=0.20,
                    help="min non-silent (voiced) seconds inside that space to flag GAP (default 0.20s)")
    ap.add_argument("--gap-noise", default="auto",
                    help="silence threshold dB for GAP detection only. 'auto' (default) = this "
                         "file's mean_volume + --gap-offset, so it adapts to how loud/noisy the "
                         "recording is (a fixed dB floods a noisy file and misses a quiet one). "
                         "Pass a number (e.g. -35) to force an absolute threshold.")
    ap.add_argument("--filler-sound", type=float, default=0.35,
                    help="FILLER:實際發聲短於這個秒數才算(預設 0.35s)")
    ap.add_argument("--filler-before", type=float, default=0.25,
                    help="FILLER:發聲前要有這麼久的安靜(預設 0.25s)")
    ap.add_argument("--filler-after", type=float, default=0.35,
                    help="FILLER:發聲後要有這麼久的安靜(預設 0.35s)")
    ap.add_argument("--no-snap", action="store_true",
                    help="不要掃爆音(彈指/拍手)。預設會掃。")
    ap.add_argument("--gap-offset", type=float, default=6.0,
                    help="dB above this file's mean_volume for the 'auto' GAP threshold "
                         "(default 6). Calibrated on 2 clips (sonnet -41.3→-35.3, demo "
                         "-35.9→-29.9); widen if GAP over-flags, tighten if it misses.")
    a = ap.parse_args()

    words = [w for w in json.load(open(a.transcript, encoding="utf-8"))["words"] if w.get("type") == "word"]
    sils = silence_windows(a.video, a.noise, 0.10)  # detect gaps >=100ms, filter later
    # Separate, more sensitive silence pass for GAP: a swallowed half-word is often
    # quiet enough that the -30dB pass calls it silence. MERGE/LONG keep the -30 pass.
    if a.gap_noise == "auto":
        mv = mean_volume(a.video)
        gap_noise = round(mv + a.gap_offset, 1) if mv is not None else -35.0
        gap_src = f"auto (mean {mv:.1f}dB + {a.gap_offset:.0f})" if mv is not None else "auto→-35 (mean_volume unread)"
    else:
        gap_noise = float(a.gap_noise)
        gap_src = "forced"
    sils_gap = silence_windows(a.video, gap_noise, 0.05) if gap_noise != float(a.noise) else sils

    def _overlap(s, e, windows):
        cov = 0.0
        for ss, se in windows:
            lo, hi = max(s, ss), min(e, se)
            if hi > lo:
                cov += hi - lo
        return cov

    def voiced_spans(s0, e0, windows):
        """[s0,e0] 內扣掉安靜之後,實際有聲音的區段。"""
        cuts = sorted((max(s0, ss), min(e0, se)) for ss, se in windows
                      if min(e0, se) > max(s0, ss))
        out, cur = [], s0
        for ss, se in cuts:
            if ss > cur:
                out.append((cur, ss))
            cur = max(cur, se)
        if cur < e0:
            out.append((cur, e0))
        return out

    def silence_run(t, windows, back):
        """t 這個時間點往前(back=True)或往後,連著多久是安靜。"""
        for ss, se in windows:
            if back and abs(se - t) < 0.12:
                return se - ss
            if not back and abs(ss - t) < 0.12:
                return se - ss
        return 0.0

    flags = []
    # FILLER:1-2 字的 token,實際只發了一小段音,而且前後都是長靜音 →
    # 幾乎都是 um/呃 被 Whisper 當成一個字(實測「你」就是這樣來的)。
    for w in words:
        t = w["text"]
        nch = len(re.sub(r"[^\w]", "", t)) or 1
        if nch > 2:
            continue
        vs = voiced_spans(w["start"] - 0.10, w["end"] + 0.10, sils_gap)
        if not vs:
            continue
        v0, v1 = vs[0][0], vs[-1][1]
        total = sum(b - a2 for a2, b in vs)
        if total > a.filler_sound:
            continue
        before = silence_run(v0, sils_gap, True)
        after = silence_run(v1, sils_gap, False)
        if before >= a.filler_before and after >= a.filler_after:
            flags.append((w["start"], w["end"], t, "FILLER",
                          f"實際只發聲 {total:.2f}s({v0:.2f}-{v1:.2f}),"
                          f"前靜 {before:.2f}s / 後靜 {after:.2f}s — 可能是 um/呃 被聽成字"))

    # GAP: audible speech BETWEEN two transcript words with no word token for it.
    for w1, w2 in zip(words, words[1:]):
        gs, ge = w1["end"], w2["start"]
        span = ge - gs
        if span < a.gap_span:
            continue
        voiced = span - _overlap(gs, ge, sils_gap)
        if voiced >= a.gap_voice:
            flags.append((gs, ge, f"{w1['text']}⋯{w2['text']}", "GAP",
                          f"{span:.2f}s space, ~{voiced:.2f}s of it is voiced "
                          f"(sound with no word — likely a swallowed half/repeat)"))
    for w in words:
        s, e, t = w["start"], w["end"], w["text"]
        dur = e - s
        # MERGE: a real silence >= gap sits INSIDE this word's span
        inside = [(ss, se) for (ss, se) in sils
                  if se - ss >= a.gap and ss > s + 0.02 and se < e - 0.02]
        if inside:
            gaps = ", ".join(f"{ss:.2f}-{se:.2f}" for ss, se in inside)
            flags.append((s, e, t, "MERGE", f"{dur:.2f}s span, real silence(s) inside: {gaps}"))
            continue
        # LONG: suspiciously long for char count (CJK: 1 char ~= 1 syllable)
        nchars = len(re.sub(r"[^\w]", "", t)) or 1
        if dur / nchars > a.long and dur > 0.6:
            flags.append((s, e, t, "LONG", f"{dur:.2f}s for {nchars} char(s) = {dur/nchars:.2f}s/char"))

    if not a.no_snap:
        dur_total = max((w["end"] for w in words), default=0) + 2.0
        env = envelope(pcm(a.video, 0.0, dur_total))
        if env:
            q = sorted(env)
            floor_db = q[max(0, int(len(q) * 0.10) - 1)]
            loud = [v > floor_db + 8 for v in env]
            segs, i2, n2 = [], 0, len(loud)
            while i2 < n2:
                if not loud[i2]:
                    i2 += 1
                    continue
                j2 = i2
                while j2 < n2 and loud[j2]:
                    j2 += 1
                if (j2 - i2) * WIN >= 0.04:
                    segs.append([i2, j2])
                i2 = j2
            for t_s, rise, peak in find_transients(env, floor_db, 8.0, 0.05, 0.0, segs):
                # ★ 判準是「它周圍大部分是安靜」。兩個更直覺的判準都試過、都失敗:
                #   ① 用「有沒有字蓋住」→ Whisper 常把 token 拉長橫跨整段靜音
                #     (彈指就被「變」31.14-32.06 蓋住),要找的東西直接被濾掉。
                #   ② 用「落在某個靜音窗裡」→ 爆音自己會把靜音切成兩半
                #     (31.23-31.57 + 31.57-32.00),它剛好卡在兩窗中間的縫隙。
                if _overlap(t_s - 0.30, t_s + 0.30, sils) < 0.35:
                    continue
                flags.append((t_s, t_s + 0.03, "(無字)", "SNAP",
                              f"短促爆音,比附近背景高 {rise:.1f}dB — 彈指/拍手之類,"
                              f"逐字稿沒有它,預設靜音門檻也看不到"))

    flags.sort(key=lambda f: f[0])
    kinds = {k: sum(1 for f in flags if f[3] == k)
             for k in ("GAP", "FILLER", "SNAP")}
    print(f"words: {len(words)}  silence gaps>=100ms: {len(sils)}  "
          f"flags: {len(flags)} ("
          + ", ".join(f"{v} {k}" for k, v in kinds.items() if v) + ")")
    print(f"GAP threshold: {gap_noise}dB [{gap_src}]\n")
    if not flags:
        print("No transcript/audio disagreements. First cut can trust the transcript timing.")
        return
    print(f"{'time':>16}  {'flag':6} {'word':12} detail")
    print("-" * 82)
    for s, e, t, kind, detail in flags:
        print(f"{s:7.2f}-{e:6.2f}  {kind:6} {t:12} {detail}")
    if kinds["FILLER"]:
        print("\n→ FILLER: 這個 token 很可能不是字,是 um/呃。剪之前先聽,"
              "**絕對不要把鉤子或第一句建在它上面**(實測:一個 0.28s 的 um 被聽成「你」,"
              "整支影片開頭就卡著一個氣音)。確定是贅字就整個剪掉。")
    if kinds["SNAP"]:
        print("\n→ SNAP: 這裡有聲音但逐字稿沒有,而且是短促爆音(彈指/拍手/敲桌)。"
              "**剪的時候不要當成靜音修掉** — 使用者會發現「我的彈指不見了」。"
              "要對特效/音效就用這個秒數,不要用逐字稿推算。")
    print("\n→ MERGE / LONG: generate a zoomed waveform (timeline_view.py) + split_blobs "
          "and place cuts on the visible silence, NOT the word timing.")
    print("→ GAP: there is speech here with no transcript word. Pull the waveform and "
          "split_blobs to see what it is. If you cannot tell what was said, ASK the user "
          "'X.X-X.X has sound but no word — what did you say here?' — do NOT cut around it "
          "blind. This is exactly where 'the cut keeps eating the wrong syllable' comes from.")

if __name__ == "__main__":
    main()
