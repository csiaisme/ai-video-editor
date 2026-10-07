"""gen_captions.py — turn a captions JSON into a lint-safe HyperFrames index.html.

Why this exists: writing the subtitle composition by hand re-discovers the same
traps every time (video needs data-start; subtitles need a high z-index or they
render BEHIND the video; each timed element needs class="clip" + data-* + a
unique id; the font needs an @font-face or it silently falls back to 黑體).
This generator bakes all of those in. The engineering that must not break is
fixed; the creative — phrasing, which words to highlight, and whatever effects /
sprites / b-roll you add — stays free.

Input: a captions JSON, a list of
    {"start": float, "end": float, "text": str, "hl": "substring"?}
`hl` is optional; that substring is painted yellow (colour only, same font).

字幕第二段特效(特效層 G 才加,都寫在 captions.json,重產字幕不會洗掉):
    "line2": "滋生細菌"   兩行字幕:text 的「結尾」那段變成第二行,講到才進場(第一行 = 前面那段)。
                          有 line2 的句子不再塗第一行的黃字。line2 不是 text 的結尾 → 印 ⚠、照一行出。
    "enter": stamp|slide|drop|type|pop   第二行怎麼進場(可省略 = 自動輪替)
    "line2_at": 秒        手動指定第二行進場(可省略)
    "line2_sfx": false    這句不配音效
    "kw_fx": "marker"     螢光筆:講到 hl 那個詞時,黃色螢光筆從左刷過去。沒寫 = 照舊整句靜態塗黃
    "kw_at": 秒           手動指定螢光筆時間(可省略)
    "kw_sfx": false       這句螢光筆不配提示音(預設配相機對焦「嗶」)
  進場時間照「字的位置」算:edl_to_captions 寫的 char_times(每個字的秒數)→ 那個字的時間;
  沒有 char_times、或字改過長度對不上 → 退回比例估算(句首 + 句長 × 前面字數 / 總字數)。
  第二行的音效寫進 sfx_cues.json(跟 transitions.py 同一份),第 7 步 sfx_cues.py mix 一次混。
  --l2-style soft(預設,黃字黑框 84px)/ mid(96px 描邊)/ heavy(120px 粗描邊):使用者說「第二行大一點、強一點」就調這個。

The emitted index.html has clearly-marked CREATIVE LAYER / CREATIVE TIMELINE
slots. Add title cards, pixel sprites, b-roll cutaways, camera moves there, then
`npx hyperframes lint` (should pass clean) and render.

Usage:
    python helpers/gen_captions.py captions.json \
        --video preview_v7.mp4 --w 1080 --h 1920 --duration 84.63 \
        --font 宋體 -o index.html

Fonts (two presets — pick by 中文 name). Each lists BOTH platform families:
    宋體  → "Source Han Serif TC VF" (Mac 裝的 VF 變數字型) +
            "Source Han Serif TC"    (Windows 裝的靜態子集,family 沒 VF)
            兩個名字都列才能跨平台不出包;繁中一定要 TC。
    黑體  → "PingFang TC" (Mac) + "Microsoft JhengHei" (Windows 正黑體), weight 700
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sfx_cues  # noqa: E402

FONTS = {
    "宋體": {"families": ["Source Han Serif TC VF", "Source Han Serif TC"], "weight": 600},
    "黑體": {"families": ["PingFang TC", "Microsoft JhengHei"], "weight": 700},
}

# 字幕樣式(可選)。classic = 現行預設,不加 --style 就跟以前一模一樣。
# 其餘是給學員挑的變化款,全部純 CSS、直式原生、不需下載任何 block。
# hl 關鍵字:多數款塗黃,emphasis 款則把關鍵字放大。
STYLES = {
    "classic":  {"label": "經典款(黑框)",
                 "inner": "display:inline-block; padding:10px 26px; border-radius:14px; background:rgba(0,0,0,0.72); box-shadow:0 4px 18px rgba(0,0,0,0.35); -webkit-box-decoration-break:clone; box-decoration-break:clone;",
                 "kw": "color:#FFD400;"},
    "clean":    {"label": "乾淨無框粗體",
                 "inner": "display:inline-block; font-weight:800; text-shadow:0 3px 14px rgba(0,0,0,0.85),0 1px 2px rgba(0,0,0,0.9);",
                 "kw": "color:#FFD400;"},
    "outline":  {"label": "描邊款(TikTok)",
                 "inner": "display:inline-block; font-weight:800; -webkit-text-stroke:7px #000; paint-order:stroke fill;",
                 "kw": "color:#FFD400;"},
    "neon":     {"label": "霓虹發光",
                 "inner": "display:inline-block; font-weight:800; color:#eafcff; text-shadow:0 0 8px #00e5ff,0 0 22px #00b4ff,0 0 40px #0077ff;",
                 "kw": "color:#fff;"},
    "gradient": {"label": "漸層填色",
                 "inner": "display:inline-block; font-weight:900; background:linear-gradient(92deg,#ffd36e,#ff5e9c 55%,#8a6bff); -webkit-background-clip:text; background-clip:text; color:transparent; filter:drop-shadow(0 3px 10px rgba(0,0,0,0.6));",
                 "kw": "-webkit-text-fill-color:#fff;"},
    "emphasis": {"label": "重點放大(關鍵字放大)",
                 "inner": "display:inline-block; font-weight:700; text-shadow:0 3px 12px rgba(0,0,0,0.8);",
                 "kw": "font-size:1.4em; font-weight:900; color:#ffd400; vertical-align:-0.06em;"},
}


def hl_html(text: str, hl: str | None, hl_color: str | None = None) -> str:
    t = html.escape(text)
    if hl:
        h = html.escape(hl)
        if h in t:
            # hl_color 覆蓋樣式預設的塗黃色(例:講「藍色」就把那兩個字染藍)。
            style = f' style="color:{html.escape(hl_color)}"' if hl_color else ""
            t = t.replace(h, f'<span class="kw"{style}>{h}</span>', 1)
    return t


# ---- 字幕第二段特效:兩行字幕(line2)+ 螢光筆(kw_fx: marker)----
# 原型與比較:kw_d.py / kw_fx.py(2026-10-06,熊熊-02 實測)。Jake 選了兩行字幕 C 質感 + 溫和進場、螢光筆。
# 字級時間 → 進場秒數要不要加偏移:熊熊-02 6 句,跟「人聲 + 波形手對」的進場點比,
# EDL 對映的逐字稿平均晚 0.08 秒、重轉 preview 的逐字稿平均早 0.01 秒 → 不加。
WORD_LAG = 0.0
PUNCT = set("，。、！？：；「」『』（）—…,.!?:;\"' ")
HEAVY = '"CJKHeavy","Source Han Serif TC VF","Source Han Serif TC",sans-serif'
L2_TRACK = 30          # 第二行的時間軌(句子不重疊,一條軌就夠;避開特效層常用的 6-19、轉場的 40+)
ENTRANCES = ["slide", "drop", "type", "pop", "stamp"]   # 沒寫 enter 時的輪替順序(最中性的 slide 先)
# 進場 → (音效 預設包/音效/ 底下, volume, 比進場早/晚幾秒放, 只用前幾秒)
# volume 是量「音效發聲那 0.5 秒」比人聲平均低 ~11dB 調的。hit 有 4 秒長尾,整檔 LUFS 換算會太大聲(對照表的 0.27 剪短用要降)。
L2_SFX = {
    "stamp": ("4-電影感/hit.mp3", 0.075, -0.04, 0.6),
    "slide": ("3-轉場/woosh-1.mp3", 0.357, -0.05, 0.42),
    "drop":  ("3-轉場/woop.mp3", 0.26, -0.05, 0.4),
    "type":  ("2-介面科技/keyboard-typing.mp3", 1.44, 0.0, 0.5),
    "pop":   ("6-卡通復古/pop.mp3", 0.341, -0.18, 0.6),       # 檔案前面有 0.22 秒空白,提早放
}
# 第二行強度。soft = Jake 選的預設(C 質感:黃字、跟第一行同款黑框);mid / heavy = 使用者說「大一點、強一點」
L2_STYLES = {
    "soft":  {"cap": 84, "pad": 34, "css": "font-weight:700; color:#FFDD55; padding:6px 24px; border-radius:14px; "
                                           "background:rgba(0,0,0,0.72); box-shadow:0 4px 18px rgba(0,0,0,0.35);"},
    "mid":   {"cap": 96, "pad": 22, "css": "font-weight:800; color:#FFD400; -webkit-text-stroke:6px #111; "
                                           "paint-order:stroke fill; text-shadow:0 4px 12px rgba(0,0,0,.45);"},
    "heavy": {"cap": 120, "pad": 22, "css": "font-weight:900; color:#FFD400; -webkit-text-stroke:12px #111; "
                                            "paint-order:stroke fill; text-shadow:0 8px 22px rgba(0,0,0,.55);"},
}
KW_FX = ("marker",)
# 螢光筆的提示音:相機對焦「嗶」(Jake 2026-10-07 挑的,Pixabay)。嗶在檔案第 0.78 秒 → 提早 0.68 秒放,
# 嗶落在螢光筆刷到一半(T+0.1)。前面那段是鏡頭對焦的馬達聲,當前導。一樣算在整支音效額度裡。
MARKER_SFX = ("3-轉場/camera-focus.mp3", 0.6, -0.68)   # 0.327(LUFS 公式)Jake 聽太小聲 → 0.6(+5dB;嗶很短,LUFS 低估)


def _n_units(s: str) -> int:
    return sum(1 for ch in s if ch not in PUNCT and not ch.isspace())


def _onset(c: dict, idx: int) -> tuple[float, str]:
    """text 第 idx 個字開口的秒數。有 char_times(長度跟 text 一樣)就用字的時間,
    否則比例估算(工具箱第 4 點的規則:句首 + 句長 × 前面字數 / 總字數,提早 0.08)。"""
    s, e = float(c["start"]), float(c["end"])
    ct = c.get("char_times")
    if isinstance(ct, list) and len(ct) == len(c["text"]) and 0 <= idx < len(ct):
        try:
            t = float(ct[idx]) + WORD_LAG
            if s - 0.6 <= t <= e:
                return t, "字級時間"
        except (TypeError, ValueError):
            pass
    t = s + (e - s) * _n_units(c["text"][:idx]) / max(1, _n_units(c["text"])) - 0.08
    return t, "比例估算"


def plan_caption_fx(captions: list[dict]) -> tuple[dict, list[str]]:
    """決定每句的第二段特效。回傳 (plan, notes)。
    plan[i] = {"kind": "line2", "line1", "line2", "enter", "T", "src", "sfx"} 或 {"kind": "marker", "T", "src"}。
    規則(Round 2 實測):同一種進場不連續兩次;stamp 每 30 秒最多 1 次;一分鐘最多 4 個兩行字幕;
    同一個音效 15 秒內不重複。違反的「不連續 / stamp」自動改掉並印出來,密度只提醒(要刪哪句是內容判斷)。"""
    plan, notes = {}, []
    for i, c in enumerate(captions):
        text, s, e = c["text"], float(c["start"]), float(c["end"])
        l2 = c.get("line2")
        if l2:
            l2 = str(l2)
            line1 = text[: len(text) - len(l2)].rstrip() if text.endswith(l2) else ""
            if not text.endswith(l2) or _n_units(line1) < 1:
                notes.append(f"⚠ sub-{i} line2「{l2}」不是「{text}」的結尾(字改過?),這句照一行出")
            elif e - s < 0.6:
                notes.append(f"⚠ sub-{i} 句子只有 {e - s:.2f} 秒,兩段看不出來,這句照一行出")
            else:
                if "line2_at" in c:
                    t, src = float(c["line2_at"]), "手動"
                else:
                    t, src = _onset(c, len(text) - len(l2))
                T = round(min(max(t, s + 0.2), e - 0.3), 3)
                plan[i] = {"kind": "line2", "line1": line1, "line2": l2, "T": T, "src": src}
                if c.get("kw_fx"):
                    notes.append(f"  sub-{i} 有 line2,kw_fx 不用(第二行本身就是重點)")
                continue
        fx = c.get("kw_fx")
        if fx:
            hl = c.get("hl")
            if fx not in KW_FX:
                notes.append(f"⚠ sub-{i} kw_fx「{fx}」不認得(目前只有 marker),照舊靜態塗黃")
            elif not hl or hl not in text:
                notes.append(f"⚠ sub-{i} kw_fx 要搭 hl,而且 hl 要在句子裡,照一般字幕出")
            else:
                if "kw_at" in c:
                    t, src = float(c["kw_at"]), "手動"
                else:
                    t, src = _onset(c, text.index(hl))
                T = round(min(max(t, s + 0.15), max(s + 0.15, e - 0.25)), 3)
                plan[i] = {"kind": "marker", "T": T, "src": src}

    # 進場輪替 + 音效
    prev, stamps, last_sfx = None, [], {}
    l2s = sorted((p["T"], i) for i, p in plan.items() if p["kind"] == "line2")
    for T, i in l2s:
        c, p = captions[i], plan[i]

        def ok(k: str) -> bool:
            return k != prev and not (k == "stamp" and stamps and T - stamps[-1] < 30)

        want = c.get("enter")
        if want and want not in L2_SFX:
            notes.append(f"⚠ sub-{i} enter「{want}」不認得(stamp/slide/drop/type/pop),改自動")
            want = None
        if want and not ok(want):
            why = "跟上一句同一種進場" if want == prev else "30 秒內已經有一個 stamp"
            notes.append(f"⚠ sub-{i} enter「{want}」{why},改自動")
            want = None
        if not want:
            k0 = ENTRANCES.index(prev) + 1 if prev in ENTRANCES else 0
            want = next(ENTRANCES[(k0 + k) % len(ENTRANCES)] for k in range(len(ENTRANCES))
                        if ok(ENTRANCES[(k0 + k) % len(ENTRANCES)]))
        p["enter"], prev = want, want
        if want == "stamp":
            stamps.append(T)
        f, vol, off, trim = L2_SFX[want]
        if c.get("line2_sfx") is False:
            p["sfx"] = None
        elif f in last_sfx and T - last_sfx[f] < 15:
            p["sfx"] = None
            notes.append(f"  sub-{i} 音效 {f} 跟 {last_sfx[f]}s 那個太近(< 15 秒),這句不配音效")
        else:
            p["sfx"] = sfx_cues.make_cue(f, T + off, vol, "兩行字幕", note=f"sub-{i} {want} {p['line2']}", trim=trim)
            last_sfx[f] = T
    # 螢光筆提示音:同一個音效 15 秒內不重複;寫 "kw_sfx": false 就不配
    last_mk = None
    for T, i in sorted((p["T"], i) for i, p in plan.items() if p["kind"] == "marker"):
        p, f, vol, off = plan[i], *MARKER_SFX
        if captions[i].get("kw_sfx") is False:
            p["sfx"] = None
        elif last_mk is not None and T - last_mk < 15:
            p["sfx"] = None
            notes.append(f"  sub-{i} 螢光筆提示音跟 {last_mk}s 那個太近(< 15 秒),這句不配")
        else:
            p["sfx"] = sfx_cues.make_cue(f, max(0.0, T + off), vol, "螢光筆", note=f"sub-{i} 對焦嗶 {captions[i]['hl']}")
            last_mk = T
    for k, (T, i) in enumerate(l2s):
        n = sum(1 for T2, _ in l2s if T <= T2 < T + 60)
        if n > 4:
            notes.append(f"⚠ {T:.1f}s 起一分鐘內有 {n} 個兩行字幕(建議最多 4 個),挑掉比較不重要的")
            break
    return plan, notes


def _marker_html(text: str, hl: str, i: int) -> str:
    t, h = html.escape(text), html.escape(hl)
    chars = "".join(f'<span class="mkc">{html.escape(ch)}</span>' for ch in hl)
    mk = (f'<span class="kwmk" id="kwm-{i}"><span class="mkbar" id="mkb-{i}"></span>'
          f'<span class="mkt" id="mkt-{i}">{chars}</span></span>')
    return t.replace(h, mk, 1)


def _l2_js(kind: str, i: int, T: float, shift: int) -> str:
    t = f"#l2t-{i}"
    js = f"""
      // sub-{i} 兩行字幕 · {kind} @ {T}(第一行往上讓位)
      tl.fromTo("#sub-{i} .sub-inner", {{ y:0 }}, {{ y:-{shift}, duration:.2, ease:"power2.out" }}, {T});"""
    if kind == "drop":
        js += f"""
      tl.fromTo("{t}", {{ opacity:0, y:-50 }}, {{ opacity:1, y:0, duration:.4, ease:"back.out(1.4)" }}, {T});"""
    elif kind == "stamp":
        js += f"""
      tl.fromTo("{t}", {{ opacity:0 }}, {{ opacity:1, duration:.04, ease:"none" }}, {T});
      tl.fromTo("{t}", {{ scale:1.3, rotation:-3 }},
        {{ scale:1, rotation:0, duration:.2, ease:"power2.out", immediateRender:false }}, {T});"""
    elif kind == "slide":
        js += f"""
      tl.fromTo("{t}", {{ opacity:0, x:220 }}, {{ opacity:1, x:0, duration:.38, ease:"back.out(1.2)" }}, {T});"""
    elif kind == "type":
        js += f"""
      tl.fromTo("{t} .l2c", {{ opacity:0, y:12, scale:.85 }}, {{ opacity:1, y:0, scale:1, duration:.16, ease:"power2.out", stagger:.07 }}, {T});"""
    elif kind == "pop":
        js += f"""
      tl.fromTo("{t}", {{ opacity:0, scale:.6 }}, {{ opacity:1, scale:1, duration:.32, ease:"back.out(1.6)" }}, {T});"""
    return js


def _marker_js(i: int, T: float, n: int) -> str:
    return f"""
      // sub-{i} 螢光筆 @ {T}
      gsap.set("#mkb-{i}", {{ scaleX:0, skewX:-6, transformOrigin:"0% 50%" }});
      tl.fromTo("#mkb-{i}", {{ scaleX:0 }}, {{ scaleX:1, duration:.32, ease:"power2.out" }}, {T});
      tl.fromTo("#mkt-{i} .mkc", {{ color:"#ffffff" }}, {{ color:"#111111", duration:.06, stagger:{round(.3 / max(1, n), 3)} }}, {round(T + .03, 3)});
      tl.fromTo("#kwm-{i}", {{ scale:1 }}, {{ scale:1.1, duration:.13, yoyo:true, repeat:1, ease:"back.out(3)" }}, {round(T + .22, 3)});"""


def build(captions: list[dict], video: str, w: int, h: int,
          duration: float, font_key: str, style_key: str = "classic",
          font_size: int = 56, sub_bottom: str = "var(--safe-bottom)",
          l2_style: str = "soft", fx_out: dict | None = None) -> str:
    """fx_out(可選):傳一個 dict 進來,會填 {"plan", "notes", "cues"}(兩行字幕/螢光筆的決定跟音效)。"""
    f = FONTS[font_key]
    st = STYLES[style_key]
    st_inner, st_kw = st["inner"], st["kw"]
    # Instagram Reels 安全區。螢幕尺寸 / IG 版本會變動,用比例算不綁單一機型。
    # 數值對齊公開規格(1080x1920):上 ~220px、下 ~450px(UI 蓋住)、右 ~100px(按鈕欄)、左 ~50px。
    # 上=標題列 下=帳號+字幕+音軌+進度條 右=按鈕欄 左=留白。要微調就改這四個係數。
    sz_top, sz_bottom = round(h * 0.115), round(h * 0.235)

    # ★ 安全區只在「用預設值」時保護得到。使用者說「字幕再低一點」而你直接照做,
    # 字幕會安靜地跑進 IG 的介面區(帳號列/進度條會蓋住),而且**在電腦上預覽完全
    # 看不出來** — 要等發到 IG 才發現。所以覆蓋安全區時一定要出聲。
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)px\s*", sub_bottom or "")
    if m and float(m.group(1)) < sz_bottom:
        print(f"⚠ 字幕底部設在 {m.group(1)}px,低於 IG Reels 安全區下緣 {sz_bottom}px。\n"
              f"  發 IG Reels 的話,字幕可能被帳號名稱/字幕列/進度條蓋到"
              f"(電腦上預覽看不出來)。\n"
              f"  發別的平台通常沒差。要貼回安全區就用 --sub-bottom {sz_bottom}px。",
              file=sys.stderr)
    sz_left, sz_right = round(w * 0.05), round(w * 0.11)
    # 每個平台家族各一條 @font-face(宣告本身就能擋 renderer fallback 成通用字型),
    # font-family 全部列上 — 哪個平台裝了哪個,瀏覽器自己挑得到。
    font_faces = "\n".join(
        f'      @font-face {{ font-family:"{fam}"; src: local("{fam}"); }}'
        for fam in f["families"]
    )
    fam_stack = ", ".join(f'"{fam}"' for fam in f["families"])

    # ★ 長句自動縮字級。字幕是 nowrap + 置中,寬度超過字幕框(左右各讓出按鈕欄)時
    # 不會換行,而是往右溢出被切掉,而且整句看起來「偏一邊」(選段demo 2026-09-29:
    # 66px 的 14 字句要 924px,框只有 842px,Jake 審成品才抓到,五句都中)。
    # 所以逐句量:全形字/標點算 1 個字寬,ASCII 算 0.55,emphasis 款的關鍵字算 1.4,
    # 超出就只把那一句縮到剛好放得下。其他句維持使用者指定的字級。
    pad = re.search(r"padding:\s*\d+px\s+(\d+)px", st_inner)
    room = (w - 2 * round(w * 0.11)) - 2 * (int(pad.group(1)) if pad else 0) - 8
    kw_scale = 1.4 if "1.4em" in st_kw else 1.0

    def units(text: str, hl: str | None) -> float:
        u = sum(0.55 if ord(ch) < 0x2E80 else 1.0 for ch in text if not ch.isspace())
        u += 0.55 * sum(1 for ch in text if ch == " ")
        if hl and hl in text and kw_scale != 1.0:
            u += (kw_scale - 1.0) * sum(0.55 if ord(ch) < 0x2E80 else 1.0 for ch in hl)
        return u

    plan, notes = plan_caption_fx(captions)
    l2st = L2_STYLES[l2_style]
    subs, shrunk, l2_clips, fx_js, cues, mk_cues = [], [], [], [], [], []
    for i, c in enumerate(captions):
        dur = round(float(c["end"]) - float(c["start"]), 2)
        p = plan.get(i, {})
        text = p["line1"] if p.get("kind") == "line2" else c["text"]
        hl = None if p.get("kind") == "line2" else c.get("hl")   # 有第二行就不塗第一行(兩個黃搶戲)
        u = units(text, hl if p.get("kind") != "marker" else None) + (0.1 if p.get("kind") == "marker" else 0)
        size_attr = ""
        if u * font_size > room:
            fs = int(room / u * 10) / 10
            size_attr = f' style="font-size:{fs}px"'
            shrunk.append((i, text, fs))
        inner = _marker_html(text, hl, i) if p.get("kind") == "marker" else hl_html(text, hl, c.get("hl_color"))
        subs.append(
            f'      <div id="sub-{i}" class="clip sub"{size_attr} data-start="{c["start"]}" '
            f'data-duration="{dur}" data-track-index="5">'
            f'<span class="sub-inner">{inner}</span></div>'
        )
        if p.get("kind") == "line2":
            T, l2 = p["T"], p["line2"]
            fs2 = int(min(l2st["cap"], 800 / max(1, units(l2, None))))
            shift = int(fs2 * 1.2 + l2st["pad"])
            body = ("".join(f'<span class="l2c">{"&nbsp;" if ch == " " else html.escape(ch)}</span>' for ch in l2)
                    if p["enter"] == "type" else html.escape(l2))
            l2_clips.append(f'      <div id="l2-{i}" class="clip l2w" data-start="{T}" '
                            f'data-duration="{round(float(c["end"]) - T, 3)}" data-track-index="{L2_TRACK}">'
                            f'<span class="l2" id="l2t-{i}" style="font-size:{fs2}px">{body}</span></div>')
            fx_js.append(_l2_js(p["enter"], i, T, shift))
            if p["sfx"]:
                cues.append(p["sfx"])
        elif p.get("kind") == "marker":
            fx_js.append(_marker_js(i, p["T"], len(hl)))
            if p.get("sfx"):
                mk_cues.append(p["sfx"])
    subs_html = "\n".join(subs)
    if fx_out is not None:
        fx_out.update(plan=plan, notes=notes, cues=cues, mk_cues=mk_cues)

    used = {p["kind"] for p in plan.values()}
    fx_css = ""
    if "line2" in used:
        fx_css += f"""      /* ==== 兩行字幕(captions.json 的 line2,強度 {l2_style}):第一行 = 鋪陳,第二行 = 重點,講到才進場 ====
         第二行在第一行後面一層(z 19),整組底線不變、往上長。改內容請改 captions.json 重跑 gen_captions。 */
      @font-face {{ font-family:"CJKHeavy";
        src: local("FZLTTHB--B51-0"), local("Lantinghei TC Heavy"), local("PingFangTC-Semibold"),
             local("MicrosoftJhengHeiBold"), local("Microsoft JhengHei Bold"); font-weight:100 900; }}
      .l2w {{ position:absolute; left:0; right:0; bottom:{sub_bottom}; text-align:center; z-index:19; pointer-events:none; }}
      .l2 {{ display:inline-block; font-family:{HEAVY}; line-height:1.1; white-space:nowrap; {l2st["css"]} }}
      .l2 .l2c {{ display:inline-block; }}
"""
    if "marker" in used:
        fx_css += """      /* ==== 螢光筆(captions.json 的 kw_fx: marker):講到關鍵字時黃色螢光筆從左刷過去、字變黑 ==== */
      .kwmk { position:relative; display:inline-block; margin:0 .05em; }
      .kwmk .mkbar { position:absolute; z-index:1; left:-.04em; right:-.04em; top:.15em; bottom:.1em;
        background:#FFD400; border-radius:.1em .22em .12em .2em; }
      .kwmk .mkt { position:relative; z-index:2; color:#fff; }
      .kwmk .mkc { -webkit-text-fill-color:currentColor; }
"""
    fx_clips = ("\n" + "\n".join(l2_clips)) if l2_clips else ""
    fx_js_block = ("\n      /* ==== CAPTION FX:gen_captions 依 captions.json(line2 / kw_fx)產生,"
                   "要改請改 captions.json 重跑 ==== */" + "".join(fx_js) + "\n") if fx_js else ""
    if shrunk:
        print(f"長句縮字級({len(shrunk)} 句超過字幕框 {room}px,只縮這幾句):", file=sys.stderr)
        for i, t, fs in shrunk:
            print(f"  sub-{i}  {fs}px  {t}", file=sys.stderr)

    return f'''<!doctype html>
<html lang="zh-Hant">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={w}, height={h}" />
    <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
    <style>
      /* @font-face is REQUIRED even for a system font — the declaration alone
         stops the renderer falling back to a generic face. */
{font_faces}
      * {{ margin:0; padding:0; box-sizing:border-box; }}
      html, body {{ width:{w}px; height:{h}px; overflow:hidden; background:#000; }}
      #root {{
        position:absolute; inset:0;
        /* Instagram Reels 安全區(可調)。重要元素別進下 ~450px(帳號/字幕/進度條)
           與右 ~100px(按鈕欄);螢幕/版本會變,故取保守值。 */
        --safe-top:{sz_top}px; --safe-bottom:{sz_bottom}px;
        --safe-left:{sz_left}px; --safe-right:{sz_right}px;
      }}

      /* subtitles: white text on a black box. z-index MUST beat the a-roll
         (a-roll is z-index:1) or captions render behind the video and vanish.
         bottom 用安全區下緣,才不會被 IG 帳號/字幕/進度條蓋到;
         max-width 兩側各清出按鈕欄寬度(取較大的 right,置中對稱)。 */
      .sub {{
        position:absolute; left:50%; bottom:{sub_bottom}; transform:translateX(-50%);
        width:auto; max-width:calc(100% - 2*var(--safe-right)); text-align:center; z-index:20;
        font-family:{fam_stack},sans-serif; font-weight:{f["weight"]};
        font-size:{font_size}px; line-height:1.32; color:#fff; white-space:nowrap;
      }}
      /* 樣式 = {style_key}。sub-inner / kw 由 STYLES 決定;定位與安全區在 .sub。 */
      .sub-inner {{ {st_inner} }}
      .kw {{ {st_kw} }}   /* keyword highlight（樣式決定顏色/大小） */
{fx_css}
      /* ==== CREATIVE LAYER styles: add your title-card / sprite / b-roll / callout CSS here ==== */

    </style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-duration="{duration}"
         data-width="{w}" data-height="{h}">

      <!-- a-roll: data-start="0" is REQUIRED (untimed media diverges preview vs render) -->
      <video id="a-roll" class="clip" src="{video}" muted playsinline
             data-start="0" data-duration="{duration}" data-track-index="0"
             style="position:absolute; inset:0; width:100%; height:100%; object-fit:cover; z-index:1;"></video>
      <audio id="a-roll-audio" src="{video}" data-start="0" data-duration="{duration}"
             data-track-index="2" data-volume="1"></audio>

      <!-- ==== CREATIVE LAYER: title cards, pixel sprites, b-roll cutaways, callouts, montage ====
           Rules that keep lint + render happy:
             - every timed element: class="clip" + data-start + data-duration + data-track-index + a unique id
             - b-roll <video> is its OWN clip (never nested in a timed <div>); unique id; muted
             - overlays that must sit above the video need z-index > 1 (subtitles use 20; keep captions on top)
             - camera moves = GSAP transform on #a-roll (transform-origin ~ "50% 42%" for a centred face)
             - IG 安全區:字卡 / callout / logo / lower-third 一律放進安全區內,用
               var(--safe-top/bottom/left/right)。重要元素別進下 ~450px(帳號/字幕/進度條)與右 ~100px(按鈕欄)。
           Add elements here. -->

      <!-- 安全區參考框:要檢查位置時把下面這段的註解打開(render 前記得再註解回去)。
      <div class="clip" data-start="0" data-duration="{duration}" data-track-index="98"
           style="position:absolute; top:var(--safe-top); bottom:var(--safe-bottom);
                  left:var(--safe-left); right:var(--safe-right);
                  border:2px dashed rgba(0,255,0,.6); z-index:90; pointer-events:none;"></div>
      -->

      <!-- subtitles -->
{subs_html}{fx_clips}
    </div>

    <script>
      window.__timelines = window.__timelines || {{}};
      const tl = gsap.timeline({{ paused: true }});
      gsap.set("#a-roll", {{ transformOrigin: "50% 42%" }});
{fx_js_block}
      /* ==== CREATIVE TIMELINE: add GSAP tweens at absolute output seconds ====
         Only deterministic animation (no Math.random / Date.now / infinite repeat).
         Examples:
           tl.to("#a-roll", {{ scale:1.12, duration:0.28 }}, 65.5);   // punch-in
           tl.to("#a-roll", {{ scale:1.0,  duration:0.5  }}, 67.1);
      */

      window.__timelines["main"] = tl;
    </script>
  </body>
</html>
'''


def main() -> None:
    ap = argparse.ArgumentParser(description="captions.json → lint-safe HyperFrames index.html")
    ap.add_argument("captions", type=Path, help="captions JSON: [{start,end,text,hl?}, ...]")
    ap.add_argument("--video", required=True, help="a-roll filename (relative to index.html)")
    ap.add_argument("--w", type=int, default=1080)
    ap.add_argument("--h", type=int, default=1920)
    ap.add_argument("--duration", type=float, required=True, help="ffprobe duration of the a-roll")
    ap.add_argument("--font", choices=list(FONTS), default="宋體")
    ap.add_argument("--style", choices=list(STYLES), default="classic",
                    help="字幕樣式:classic(預設) / clean / outline / neon / gradient / emphasis")
    ap.add_argument("--font-size", type=int, default=56,
                    help="字幕字級 px(預設 56)。使用者說「字大一點」就調這個,不要手改 index.html —— "
                         "手改的會在下次重跑 gen_captions 時整個被蓋掉。")
    ap.add_argument("--sub-bottom", default="var(--safe-bottom)",
                    help="字幕離畫面底部多高,例如 340px(預設 var(--safe-bottom) = IG 安全區下緣 451px)。"
                         "調低於 450px 會進到 IG 介面區,發 Reels 可能被帳號列/進度條蓋到 —— 要跟使用者講。")
    ap.add_argument("--l2-style", choices=list(L2_STYLES), default="soft",
                    help="兩行字幕第二行的強度:soft(預設,84px 黃字黑框)/ mid(96px 描邊)/ heavy(120px 粗描邊)")
    ap.add_argument("--sfx-cues", type=Path, default=None,
                    help="第二行進場音效寫到哪(預設跟 index.html 同資料夾的 sfx_cues.json;"
                         "只改「兩行字幕」那幾筆,轉場的保留)")
    ap.add_argument("-o", "--out", type=Path, required=True)
    args = ap.parse_args()

    if not args.captions.exists():
        sys.exit(f"captions not found: {args.captions}")
    captions = json.loads(args.captions.read_text(encoding="utf-8"))
    if not isinstance(captions, list) or not captions:
        sys.exit("captions JSON must be a non-empty list of {start,end,text}")

    fx: dict = {}
    html_out = build(captions, args.video, args.w, args.h, args.duration, args.font, args.style,
                     args.font_size, args.sub_bottom, args.l2_style, fx_out=fx)
    args.out.write_text(html_out, encoding="utf-8")

    # 兩行字幕 / 螢光筆:印出每句的決定(時間是字級時間還是比例估算),音效寫進共用的 sfx_cues.json
    for i, p in sorted(fx["plan"].items()):
        c = captions[i]
        if p["kind"] == "line2":
            snd = Path(p["sfx"]["sfx"]).stem if p.get("sfx") else "無音效"
            print(f"兩行字幕 sub-{i}  {p['line1']} / {p['line2']}  {p['enter']} @ {p['T']}({p['src']},{snd})")
        else:
            snd = "對焦嗶" if p.get("sfx") else "無音效"
            print(f"螢光筆   sub-{i}  {c['text']} [{c['hl']}] @ {p['T']}({p['src']},{snd})")
    for n in fx["notes"]:
        print(n)
    cues_path = args.sfx_cues or args.out.with_name("sfx_cues.json")
    sfx_cues.save(cues_path, "螢光筆", fx["mk_cues"])
    for wmsg in sfx_cues.save(cues_path, "兩行字幕", fx["cues"]):
        print(wmsg)
    if fx["cues"] or fx["mk_cues"]:
        print(f"wrote {cues_path}({len(fx['cues'])} 個第二行音效、{len(fx['mk_cues'])} 個螢光筆提示音,"
              f"第 7 步 sfx_cues.py mix 會一起混)")

    # 樣式側檔:給審片頁用,讓預覽字幕跟成品同字型/字級/位置。
    # 學員回報(五份):審片頁用自己的通用樣式,使用者對「不存在的問題」下指令
    # (字級誤報跑出鏡、字型不對白繞兩輪)。把實際參數寫出來,審片頁直接吃。
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)px\s*", args.sub_bottom or "")
    bottom_px = float(m.group(1)) if m else round(args.h * 0.235)  # 預設 = IG 安全區下緣
    style_meta = {
        "font": args.font, "families": FONTS[args.font]["families"],
        "weight": FONTS[args.font]["weight"], "style": args.style,
        "font_size": args.font_size, "video_w": args.w, "video_h": args.h,
        "sub_bottom_px": bottom_px, "l2_style": args.l2_style,
    }
    args.out.with_name("樣式.json").write_text(
        json.dumps(style_meta, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"wrote {args.out}  ({len(captions)} subtitles, font={args.font}, style={args.style})")
    print(f"wrote {args.out.with_name('樣式.json')}(審片頁同步樣式用 — 跟 captions.json 一起複製進審片區)")
    print("next: add creative layers in the marked slots → npx hyperframes lint → render")


if __name__ == "__main__":
    main()
