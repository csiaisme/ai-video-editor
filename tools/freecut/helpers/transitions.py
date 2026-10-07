#!/usr/bin/env python3
"""
transitions.py — 在 ai-edit 口播 composition 加「轉場」(換段落、進出 b-roll、進出字卡)。

由專案的 creative.py 在寫出 index.html 前呼叫(一支片有幾個轉場就一次給,碰撞要一起算):

    import sys; sys.path.insert(0, str(KIT / "tools" / "freecut" / "helpers"))
    from transitions import apply_transitions
    import sfx_cues
    s, report, cues = apply_transitions(s, [
        dict(at=2.75,  recipe="card-flip",  into="card:重點一|水貂毛是什麼？", hold=1.4, back="iris-circle"),
        dict(at=10.48, recipe="push-slide", into="broll:broll-claude-chat.mp4", hold=1.3, back="glitch"),
        dict(at=23.65, recipe="shutter"),                   # into="video" = 同一支口播換段落
    ])
    print(*report, sep="\\n")                               # 碰撞處理紀錄,一定要讀
    for w in sfx_cues.save("sfx_cues.json", "轉場", cues): print(w)   # 跟兩行字幕的音效同一份
    open("index.html", "w", encoding="utf-8").write(s)

  at        轉場開始秒數(成品時間軸)。換段落就用 edl 的剪接點
  recipe    進場配方(python3 transitions.py recipes 看全部)
  into      video / broll:<專案裡的檔名> / card:<小標|大字>
  hold      b-roll / 字卡停留秒數(不含兩段轉場)
  back      轉回口播用的配方(broll、card 必填)
  dur / back_dur   改轉場長度(不給就用配方預設;配方時間照比例縮放)

三種用法(into):
  video          同一支口播換段落(a)。場景 A = 剪接點前最後一格的「近乎定格」副本,
                 場景 B = 同一支檔的即時副本(時間不動),#a-roll 本身完全不碰。
  broll:<檔名>   進 b-roll、停 hold 秒、再用 back 轉回來(b)。b-roll 檔先從素材庫複製進 captions/。
  card:<文字>    進全螢幕字卡、停 hold 秒、再轉回來(c)。「小標|大字」用 | 分開。

圖層:轉場舞台 z-index 15 = 蓋過特效層(6-7),在字幕(20)底下。字幕永遠在最上面、不動。
跨過轉場的常駐特效(計分板)會在 T-0.15 淡出、轉場(或 b-roll/字卡)結束後淡回。

碰撞規則(#3,自動):特效「進場」落在任何轉場忙碌區間 [T, T+D] → 往後推到 T+D+0.1。
  - 剪接點上的推鏡(stepZoom)直接拿掉(轉場取代它);跨過 T 的 #a-roll 運鏡改成在 T 前做完。
  - 落在 b-roll/字卡停留區間的進場:不動,但列 HIDDEN 警告(被蓋住,自己決定要不要搬)。
  - 解析不了的語句(forEach 之類)裡有數字落在區間 → 列 MANUAL,check 模式會失敗。

指令列:
  python3 transitions.py check index.html        列出還沒處理的碰撞;有就 exit 1
  python3 transitions.py recipes                 列出配方表
"""
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sfx_cues  # noqa: E402  音效路徑 / cue 格式跟兩行字幕共用

W, H = 1080, 1920       # apply_transitions 會照 index.html 的 data-width/height 改
GAP = 0.1
STAGE_Z = 15            # > 特效層 6-7, < 字幕 20
FADE_OUT, FADE_IN = 0.15, 0.2
DARK, YELLOW, CREAM, RED = "#14161C", "#FFD400", "#F5F1E8", "#E5484D"

# SFX:音量 = 10^((-26 - I)/20),I 是 2026-10-06 用 ebur128 量的 Integrated LUFS;
#      peak = 檔案裡最大聲那一下的秒數(對齊用:音效從 at - peak 開始,峰值落在畫面變化那格)
SFX = {
    "woosh-1":        ("3-轉場/woosh-1.mp3",        0.51, 0.13),
    "woosh-2":        ("3-轉場/woosh-2.mp3",        0.43, 0.36),
    "woop":           ("3-轉場/woop.mp3",           0.43, 0.35),
    "camera-shutter": ("3-轉場/camera-shutter.mp3", 0.86, 0.09),
    "glitch":         ("2-介面科技/glitch.mp3",     0.41, 0.49),
    "deep-woosh":     ("3-轉場/deep-woosh.mp3",     0.32, 0.51),
    "paper-slide":    ("3-轉場/paper-slide.mp3",    0.85, 0.27),
}
# 長尾巴的檔只用前幾秒(sfx_cues mix 會在結尾淡出)。deep-woosh 整檔 3.25 秒,配 0.4 秒的 blur-ripple 會拖一大段
SFX_TRIM = {"deep-woosh": 1.0}


# ------------------------------------------------------------------ recipes
# 每個配方:fn(A, B, T, D, u) -> (extra_html, [js lines])
#   A = 出場場景 wrapper 選擇器, B = 進場場景 wrapper 選擇器, u = 這一段的唯一前綴
# 時間全部寫成 T + 比例*D,呼叫時改 D 會等比例縮放。
def _n(x):
    return f"{x:.3f}".rstrip("0").rstrip(".")


def _timed_div(id_, T, D, style, track, inner=""):
    return (f'<div id="{id_}" class="clip" data-start="{_n(T)}" data-duration="{_n(D)}" '
            f'data-track-index="{track}" style="{style}">{inner}</div>')


def r_push_slide(A, B, T, D, u):
    return "", [
        f'tl.set("{A}", {{ zIndex: 1 }}, {_n(T)}); tl.set("{B}", {{ zIndex: 2 }}, {_n(T)});',
        f'tl.fromTo("{A}", {{ x: 0 }}, {{ x: -{W}, duration: {_n(D)}, ease: "power3.inOut", immediateRender: false }}, {_n(T)});',
        f'tl.fromTo("{B}", {{ x: {W} }}, {{ x: 0, duration: {_n(D)}, ease: "power3.inOut", immediateRender: false }}, {_n(T)});',
    ]


def r_whip(A, B, T, D, u):
    return "", [
        f'tl.set("{A}", {{ zIndex: 1 }}, {_n(T)}); tl.set("{B}", {{ zIndex: 2 }}, {_n(T)});',
        f'tl.fromTo("{A}", {{ x: 0, filter: "blur(0px)" }}, {{ x: -{W}, filter: "blur(32px)", duration: {_n(D)}, ease: "power2.in", immediateRender: false }}, {_n(T)});',
        f'tl.fromTo("{B}", {{ x: {W}, filter: "blur(32px)" }}, {{ x: 0, filter: "blur(0px)", duration: {_n(D)}, ease: "power2.out", immediateRender: false }}, {_n(T)});',
        f'tl.set("{B}", {{ filter: "none" }}, {_n(T + D)});',
    ]


def r_iris_circle(A, B, T, D, u):
    return "", [
        f'tl.set("{A}", {{ zIndex: 1 }}, {_n(T)});',
        f'tl.set("{B}", {{ zIndex: 2, x: 0, clipPath: "circle(0% at 50% 45%)" }}, {_n(T)});',
        f'tl.to("{B}", {{ clipPath: "circle(80% at 50% 45%)", duration: {_n(D)}, ease: "power2.out" }}, {_n(T)});',
        f'tl.set("{B}", {{ clipPath: "none" }}, {_n(T + D)});',
    ]


def r_shutter(A, B, T, D, u):
    half = H // 2
    st = f"position:absolute; left:0; width:{W}px; height:{half}px; background:{DARK}; z-index:6;"
    html = (_timed_div(f"{u}-sh-top", T, D, st + f" top:0; border-bottom:8px solid {YELLOW};", 70)
            + _timed_div(f"{u}-sh-bot", T, D, st + f" top:{half}px; border-top:8px solid {YELLOW};", 71))
    c, o = 0.45 * D, 0.55 * D
    return html, [
        f'tl.set("{B}", {{ opacity: 0, x: 0, zIndex: 2 }}, {_n(T)}); tl.set("{A}", {{ zIndex: 1 }}, {_n(T)});',
        f'tl.fromTo("#{u}-sh-top", {{ y: -{half + 10} }}, {{ y: 0, duration: {_n(c)}, ease: "power3.in", immediateRender: false }}, {_n(T)});',
        f'tl.fromTo("#{u}-sh-bot", {{ y: {half + 10} }}, {{ y: 0, duration: {_n(c)}, ease: "power3.in", immediateRender: false }}, {_n(T)});',
        f'tl.set("{A}", {{ opacity: 0 }}, {_n(T + 0.5 * D)}); tl.set("{B}", {{ opacity: 1 }}, {_n(T + 0.5 * D)});',
        f'tl.to("#{u}-sh-top", {{ y: -{half + 10}, duration: {_n(D - o)}, ease: "power3.out" }}, {_n(T + o)});',
        f'tl.to("#{u}-sh-bot", {{ y: {half + 10}, duration: {_n(D - o)}, ease: "power3.out" }}, {_n(T + o)});',
    ]


def r_glitch(A, B, T, D, u):
    st = "position:absolute; inset:0; mix-blend-mode:screen; z-index:6; opacity:0;"
    html = (_timed_div(f"{u}-gr", T, D, st + " background:rgba(229,72,77,.45);", 70)
            + _timed_div(f"{u}-gb", T, D, st + " background:rgba(67,97,238,.45);", 71))
    f = D / 0.2
    js = [f'tl.set("{B}", {{ opacity: 0, x: 0, zIndex: 2 }}, {_n(T)}); tl.set("{A}", {{ zIndex: 1 }}, {_n(T)});']
    steps = [(0.00, 40, -8, -30, 12, -15), (0.03, -30, 15, 50, -10, 20), (0.06, 60, -20, -40, 8, -25), (0.09, -20, 5, 35, -15, None)]
    for dt, rx, ry, bx, by, ax in steps:
        t = _n(T + dt * f)
        js.append(f'tl.set("#{u}-gr", {{ opacity: 1, x: {rx}, y: {ry} }}, {t}); tl.set("#{u}-gb", {{ opacity: 1, x: {bx}, y: {by} }}, {t});')
        if ax is not None:
            js.append(f'tl.set("{A}", {{ x: {ax} }}, {t});')
    js += [f'tl.set("{A}", {{ opacity: 0 }}, {_n(T + 0.12 * f)}); tl.set("{B}", {{ opacity: 1 }}, {_n(T + 0.12 * f)});',
           f'tl.set(["#{u}-gr", "#{u}-gb"], {{ opacity: 0, x: 0, y: 0 }}, {_n(T + 0.15 * f)}); tl.set("{A}", {{ x: 0 }}, {_n(T + 0.15 * f)});']
    return html, js


def r_blur_ripple(A, B, T, D, u):
    f = D / 0.4
    seq = [(0.00, 30, 1.02, 0, 1), (0.04, -25, 0.98, 4, 1), (0.08, 20, 1.01, 6, 1), (0.12, -15, 0.99, 8, 1), (0.16, 10, 1, 10, 0)]
    js = [f'tl.set("{B}", {{ opacity: 0, x: 0, zIndex: 2 }}, {_n(T)}); tl.set("{A}", {{ zIndex: 1 }}, {_n(T)});']
    for dt, x, s, b, op in seq:
        js.append(f'tl.to("{A}", {{ x: {x}, scale: {s}, filter: "blur({b}px)", opacity: {op}, duration: {_n(0.04 * f)}, ease: "none" }}, {_n(T + dt * f)});')
    js += [f'tl.set("{B}", {{ opacity: 1 }}, {_n(T + 0.16 * f)});',
           f'tl.fromTo("{B}", {{ x: -15, scale: 1.02, filter: "blur(8px)" }}, {{ x: 0, scale: 1, filter: "blur(0px)", duration: {_n(0.2 * f)}, ease: "power2.out", immediateRender: false }}, {_n(T + 0.2 * f)});',
           f'tl.set("{B}", {{ filter: "none" }}, {_n(T + D)});']
    return "", js


def r_card_flip(A, B, T, D, u):
    return "", [
        f'tl.set("{A}", {{ zIndex: 1 }}, {_n(T)}); tl.set("{B}", {{ zIndex: 2, x: 0 }}, {_n(T)});',
        f'tl.fromTo("{A}", {{ rotationY: 0 }}, {{ rotationY: 180, duration: {_n(D)}, ease: "power2.inOut", immediateRender: false }}, {_n(T)});',
        f'tl.fromTo("{B}", {{ rotationY: -180 }}, {{ rotationY: 0, duration: {_n(D)}, ease: "power2.inOut", immediateRender: false }}, {_n(T)});',
    ]


def r_color_blocks(A, B, T, D, u):
    f = D / 0.6
    st = "position:absolute; inset:0;"
    html = (_timed_div(f"{u}-wa", T, D, st + f" background:{YELLOW}; z-index:7;", 70)
            + _timed_div(f"{u}-wb", T, D, st + f" background:{DARK}; z-index:6;", 71))
    js = [f'tl.set("{B}", {{ opacity: 0, x: 0, zIndex: 2 }}, {_n(T)}); tl.set("{A}", {{ zIndex: 1 }}, {_n(T)});',
          f'tl.fromTo("#{u}-wa", {{ x: -{W} }}, {{ x: 0, duration: {_n(0.25 * f)}, ease: "power3.inOut", immediateRender: false }}, {_n(T)});',
          f'tl.fromTo("#{u}-wb", {{ x: -{W} }}, {{ x: 0, duration: {_n(0.25 * f)}, ease: "power3.inOut", immediateRender: false }}, {_n(T + 0.06 * f)});',
          f'tl.set("{A}", {{ opacity: 0 }}, {_n(T + 0.2 * f)}); tl.set("{B}", {{ opacity: 1 }}, {_n(T + 0.2 * f)});',
          f'tl.to("#{u}-wa", {{ x: {W}, duration: {_n(0.25 * f)}, ease: "power3.inOut" }}, {_n(T + 0.28 * f)});',
          f'tl.to("#{u}-wb", {{ x: {W}, duration: {_n(0.25 * f)}, ease: "power3.inOut" }}, {_n(T + 0.34 * f)});']
    return html, js


def r_blinds(A, B, T, D, u):
    f = D / 0.8
    n, sh = 8, H // 8
    cols = [DARK, YELLOW, CREAM, RED] * 2
    html = "".join(_timed_div(f"{u}-bl{i}", T, D, f"position:absolute; left:0; top:{i * sh}px; width:{W}px; height:{sh}px; background:{cols[i]}; z-index:6;", 70 + i)
                   for i in range(n))
    js = [f'tl.set("{B}", {{ opacity: 0, x: 0, zIndex: 2 }}, {_n(T)}); tl.set("{A}", {{ zIndex: 1 }}, {_n(T)});']
    for i in range(n):
        js.append(f'tl.fromTo("#{u}-bl{i}", {{ x: -{W} }}, {{ x: 0, duration: {_n(0.2 * f)}, ease: "power3.inOut", immediateRender: false }}, {_n(T + i * 0.025 * f)});')
    js.append(f'tl.set("{A}", {{ opacity: 0 }}, {_n(T + 0.4 * f)}); tl.set("{B}", {{ opacity: 1 }}, {_n(T + 0.4 * f)});')
    for i in range(n):
        js.append(f'tl.to("#{u}-bl{i}", {{ x: {W}, duration: {_n(0.2 * f)}, ease: "power3.inOut" }}, {_n(T + (0.42 + i * 0.025) * f)});')
    return html, js


# name: (fn, 來源 showcase, 預設 D, 適合 a/b/c, 預設音效, 畫面變化在 D 的哪裡(對音效峰值), 需要黑底)
RECIPES = {
    "push-slide":   (r_push_slide,   "transitions-push(Push Slide)",            0.5,  "b c",   "woosh-1",        0.5,  False),
    "whip":         (r_whip,         "transitions-push(Push Slide + 動態模糊)", 0.35, "a b",   "woosh-2",        0.5,  True),
    "iris-circle":  (r_iris_circle,  "transitions-radial(Circle Iris)",         0.5,  "b c",   "woop",           0.3,  False),
    "shutter":      (r_shutter,      "transitions-mechanical(Shutter)",         0.5,  "a b c", "camera-shutter", 0.45, False),
    "glitch":       (r_glitch,       "transitions-distortion(Glitch)",          0.2,  "a b",   "glitch",         0.0,  False),
    "blur-ripple":  (r_blur_ripple,  "transitions-distortion(Ripple)",          0.4,  "a",     "deep-woosh",     0.4,  True),
    "card-flip":    (r_card_flip,    "transitions-3d(3D Card Flip)",            0.6,  "c",     "paper-slide",    0.5,  True),
    "color-blocks": (r_color_blocks, "transitions-cover(Staggered Blocks)",     0.6,  "a c",   "woosh-1",        0.35, False),
    "blinds":       (r_blinds,       "transitions-cover(Horizontal Blinds)",    0.8,  "a c",   "paper-slide",    0.5,  False),
}

# ------------------------------------------------------------------ building blocks
CSS = f"""
      /* ==== TRANSITIONS (transitions.py) ==== */
      @font-face {{ font-family:"TxHeavy";
        src: local("FZLTTHB--B51-0"), local("Lantinghei TC Heavy"), local("PingFangTC-Semibold"),
             local("MicrosoftJhengHeiBold"), local("Microsoft JhengHei Bold"); font-weight:100 900; }}
      .tx-stage {{ position:absolute; inset:0; z-index:{STAGE_Z}; pointer-events:none; overflow:hidden; perspective:1600px; }}
      .tx-scene {{ position:absolute; inset:0; backface-visibility:hidden; -webkit-backface-visibility:hidden; }}
      .tx-fill {{ position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }}
      .tx-card {{ position:absolute; inset:0; background:{DARK}; display:flex; flex-direction:column; align-items:center;
        justify-content:flex-start; padding-top:620px;
        font-family:"TxHeavy","Source Han Serif TC VF","Source Han Serif TC",sans-serif; font-weight:900; }}
      .tx-card .tg {{ font-size:56px; color:{DARK}; background:{YELLOW}; padding:10px 34px; border-radius:999px; white-space:nowrap; }}
      .tx-card .tt {{ margin-top:44px; color:{CREAM}; line-height:1.15; white-space:nowrap; }}
      .tx-card .ln {{ margin-top:40px; width:160px; height:10px; border-radius:5px; background:{YELLOW}; }}
"""


def _video(id_, src, start, dur, media_start, track, rate=None, aroll=True):
    # data-tx-aroll = 這是口播主畫面的副本(cleanup.sh 認這個標記,不會因為它把 preview 當素材留下來)
    r = f' data-playback-rate="{_n(rate)}"' if rate else ""
    a = " data-tx-aroll" if aroll else ""
    return (f'<video id="{id_}" class="clip tx-fill" src="{src}" muted playsinline data-start="{_n(start)}" '
            f'data-duration="{_n(dur)}" data-media-start="{_n(media_start)}"{r} data-track-index="{track}"{a}></video>')


def _card(id_, text, start, dur, track):
    tag, title = (text.split("|", 1) + [""])[:2] if "|" in text else ("", text)
    n = max(1, len(title.replace(" ", "")))
    fs = min(150, int(920 / n))                     # 字數算字級(特效工具箱第 5 條)
    tg = f'<div class="tg">{tag}</div>' if tag else ""
    return (f'<div id="{id_}" class="clip tx-card" data-start="{_n(start)}" data-duration="{_n(dur)}" data-track-index="{track}">'
            f'{tg}<div class="tt" style="font-size:{fs}px">{title}</div><div class="ln"></div></div>')


def _sfx_cue(recipe, T, D, uid):
    """音效從「畫面變化那格 − 音效檔峰值位置」開始放,最大聲那下落在新畫面第一格。"""
    name = RECIPES[recipe][4]
    path, vol, peak = SFX[name]
    return sfx_cues.make_cue(path, T + RECIPES[recipe][5] * D - peak, vol, "轉場", note=f"{uid} {recipe}",
                             trim=SFX_TRIM.get(name))


def _check_broll(path, need):
    """b-roll 不夠長會在轉場中間變黑:進 + 停 + 回 必須 ≤ b-roll 秒數。"""
    p = Path(BASE_DIR) / path
    if not p.exists():
        raise FileNotFoundError(f"b-roll 不在 composition 資料夾:{p}(先從素材庫複製進 captions/)")
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                         capture_output=True, text=True).stdout.strip()
    if out and float(out) + 1e-3 < need:
        raise ValueError(f"b-roll {path} 只有 {float(out):.2f}s,這段要 {need:.2f}s(縮短 hold)")


def _build_one(i, spec, src):
    """回傳 (stage_html, js_lines, legs, covered, cues)。legs = [(T, T+D)]"""
    u = spec.get("uid") or f"tx{i}"
    T = float(spec["at"])
    rec = spec.get("recipe", "push-slide")
    fn, _, D0, _, _, _, dark = RECIPES[rec]
    D = float(spec.get("dur") or D0)
    into = spec.get("into", "video")
    back = spec.get("back")
    hold = float(spec.get("hold") or 0)
    tr = 40 + i * 10
    A, M, C = f"#{u}-a", f"#{u}-m", f"#{u}-c"
    # sfx=False 進場不配音效;back_sfx=False 回程不配(整支音效額度不夠時用,高慧雯測試實測需要)
    parts, js, legs, covered, cues = [], [], [(T, T + D)], [], ([_sfx_cue(rec, T, D, u)] if spec.get("sfx", True) is not False else [])

    # 場景 A:剪接點前的「近乎定格」副本(rate 0.1 → D 秒只吃掉 0.1*D 秒的畫面,結束在 T 前 0.05 秒)
    ms = max(0.0, T - 0.05 - 0.1 * D)
    parts.append(f'<div id="{u}-a" class="tx-scene">{_video(f"{u}-va", src, T, D, ms, tr, 0.1)}</div>')

    if into == "video":
        parts.append(f'<div id="{u}-m" class="tx-scene">{_video(f"{u}-vm", src, T, D, T, tr + 1)}</div>')
        total_end = T + D
    else:
        if not back:
            raise ValueError("broll/card 需要 back=<recipe>(轉回口播)")
        Db = float(spec.get("back_dur") or RECIPES[back][2])
        T2 = T + D + hold
        span = D + hold + Db
        if into.startswith("broll:"):
            _check_broll(into[6:], span + float(spec.get("broll_start", 0)))
            parts.append(f'<div id="{u}-m" class="tx-scene">{_video(f"{u}-vm", into[6:], T, span, float(spec.get("broll_start", 0)), tr + 1, aroll=False)}</div>')
        elif into.startswith("card:"):
            parts.append(f'<div id="{u}-m" class="tx-scene">{_card(f"{u}-card", into[5:], T, span, tr + 1)}</div>')
        else:
            raise ValueError(f"不認得的 into: {into}")
        parts.append(f'<div id="{u}-c" class="tx-scene">{_video(f"{u}-vc", src, T2, Db, T2, tr + 2)}</div>')
        legs.append((T2, T2 + Db))
        covered.append((T + D, T2))
        if spec.get("back_sfx", True) is not False:
            cues.append(_sfx_cue(back, T2, Db, u))
        total_end = T2 + Db
        if RECIPES[back][6]:
            parts.insert(0, _timed_div(f"{u}-bk2", T2, Db, "position:absolute; inset:0; background:#000;", tr + 8))
        h2, j2 = RECIPES[back][0](M, C, T2, Db, f"{u}b")
        parts.append(h2)
        js += [f"// {u} 轉回口播:{back} @ {_n(T2)}"] + j2

    if dark:
        parts.insert(0, _timed_div(f"{u}-bk", T, D, "position:absolute; inset:0; background:#000;", tr + 9))
    h1, j1 = fn(A, M, T, D, u)
    parts.append(h1)
    js = [f"// {u} {into} 進場:{rec} @ {_n(T)}"] + j1 + js
    stage = (f'      <div id="{u}-stage" class="tx-stage" data-tx-begin="{u}">\n        '
             + "\n        ".join(p for p in parts if p) + "\n      </div>")
    return stage, js, legs, covered, cues, total_end


# ------------------------------------------------------------------ collision pass (#3)
NUM = r"(\d+(?:\.\d+)?)"


def _split_statements(js):
    """在深度 0 的分號切語句;跳過字串跟註解。回傳 [(start, end, text)]"""
    out, depth, i, st, n = [], 0, 0, 0, len(js)
    while i < n:
        c = js[i]
        if c in "\"'`":
            q = c; i += 1
            while i < n and js[i] != q:
                i += 2 if js[i] == "\\" else 1
        elif js.startswith("//", i):
            j = js.find("\n", i); j = n if j < 0 else j
            if depth == 0 and not js[st:i].strip():
                st = j
            i = j
        elif js.startswith("/*", i):
            j = js.find("*/", i); j = n if j < 0 else j + 2
            if depth == 0 and not js[st:i].strip():
                st = j
            i = j - 1
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == ";" and depth == 0:
            out.append((st, i, js[st:i]))
            st = i + 1
        i += 1
    return out


def _sub_ids(html):
    ids = set()
    for m in re.finditer(r'<div id="(sub-\d+)"[^>]*>(.*?)</div>\s*\n', html, re.S):
        ids.update(re.findall(r'id="([^"]+)"', m.group(2)))
    return ids


def _classify(stmt, var_ids, sub_ids):
    s = stmt.strip()
    m = re.match(r"^(\w+)\(\s*(?:.*,\s*)?" + NUM + r"\s*\)$", s, re.S)
    helper = None
    if m and not s.startswith("tl."):
        helper = m.group(1)
    tm = re.search(r",\s*" + NUM + r"(\s*\+\s*[^,()]*?)?\s*\)\s*$", s) or (re.search(r"^\w+\(\s*" + NUM + r"\s*\)$", s) if helper else None)
    if "tl." not in s and not helper:
        return None
    if tm is None:
        return {"kind": "unparsed"}
    tgt = re.match(r"^(?:for\s*\(.*?\)\s*)?tl\.\w+\(\s*([\"'])(.*?)\1", s, re.S)
    tvar = re.match(r"^(?:for\s*\(.*?\)\s*)?tl\.\w+\(\s*([A-Za-z_]\w*)\s*,", s, re.S)
    target = tgt.group(2) if tgt else (tvar.group(1) if tvar else "")
    tid = target[1:] if target.startswith("#") and re.fullmatch(r"#[\w-]+", target) else var_ids.get(target, "")
    dur = re.search(r"duration:\s*" + NUM, s)
    last_obj = s[s.rfind("{"):] if "{" in s else ""
    if tid in sub_ids:
        kind = "subtitle"
    elif helper and "zoom" in helper.lower():
        kind = "camera-cut"
    elif target in ("AR", "#a-roll") or (helper and helper in ("shake",)):
        kind = "camera"
    elif (helper and "out" in helper.lower()) or re.search(r"opacity:\s*0\s*[,}]", last_obj) and not re.search(r"opacity:\s*1", last_obj):
        kind = "exit"
    else:
        kind = "entrance"
    return {"kind": kind, "num_span": tm.span(1), "time": float(tm.group(1)), "dur": float(dur.group(1)) if dur else 0.0,
            "target": target or helper, "text": s}


def _in(t, w):
    return w[0] - 1e-6 <= t < w[1] - 1e-6


def resolve_collisions(html, busy, covered, fix=True):
    """回傳 (html, report_lines, unresolved_count)。busy/covered = [(t0, t1)]。"""
    report, unresolved = [], 0
    # 從 CREATIVE TIMELINE 那段註解「結束後」開始掃(註解裡的範例 tl.to(..., 65.5) 不算);
    # gen_captions 的 CAPTION FX(兩行字幕/螢光筆)在它前面,屬於字幕,不推不動
    a = html.find("CREATIVE TIMELINE")
    a = html.find("*/", a) + 2 if a >= 0 else 0
    b = html.find("/* TX-BEGIN */")
    b = b if b > 0 else html.find('window.__timelines["main"] = tl;', a)
    js = html[a:b]
    var_ids = dict(re.findall(r'(?:const|let|var)\s+(\w+)\s*=\s*document\.getElementById\("([^"]+)"\)', js))
    var_ids.update({"AR": "a-roll"})
    sub_ids = _sub_ids(html)
    edits = []  # (abs_start, abs_end, replacement)

    def target_after(t):
        for _ in range(10):
            hit = [w for w in busy if _in(t, w)]
            if not hit:
                return t
            t = max(w[1] for w in hit) + GAP
        return t

    for st, en, text in _split_statements(js):
        info = _classify(text, var_ids, sub_ids)
        if not info:
            continue
        lead = len(text) - len(text.lstrip())
        if info["kind"] == "unparsed":
            nums = [float(x) for x in re.findall(r"(?<![\w.])(\d+\.\d+)", text)]
            bad = [x for x in nums if any(_in(x, w) for w in busy)]
            if bad:
                unresolved += 1
                report.append(f"MANUAL  看不懂這句的時間,但有數字 {bad} 落在轉場區間:{text.strip()[:90]}")
            continue
        t, k = info["time"], info["kind"]
        n0, n1 = info["num_span"]
        abs_num = (a + st + lead + n0, a + st + lead + n1)
        if k in ("subtitle", "exit"):
            continue
        hit = [w for w in busy if _in(t, w)]
        cov = [w for w in covered if _in(t, w)]
        if k == "camera-cut":
            if hit or cov or any(abs(t - w[0]) < 0.06 for w in busy):
                report.append(f"REMOVE  {info['text'][:60]}  (剪接點推鏡,轉場取代它)")
                edits.append((a + st + lead, a + en + 1, f"/* TX removed: {info['text'].strip()} */"))
            continue
        if k == "camera":
            end = t + info["dur"]
            for w in busy:
                if t < w[0] < end - 1e-6:                      # 跨過 T 的運鏡 → 在 T 前做完
                    nt = max(0.0, w[0] - info["dur"])
                    report.append(f"MOVE    #a-roll 運鏡 {t} → {_n(nt)}(在轉場 {_n(w[0])} 前做完)")
                    edits.append((*abs_num, _n(nt)))
                    break
            else:
                if hit or cov:
                    report.append(f"REMOVE  #a-roll 運鏡 @ {t}(落在轉場/被蓋住的時間,副本看不到)")
                    edits.append((a + st + lead, a + en + 1, f"/* TX removed: {info['text'].strip()} */"))
            continue
        if hit:
            nt = target_after(t)
            report.append(f"SHIFT   {info['target']} 進場 {t} → {_n(nt)}  (轉場 {_n(hit[0][0])}–{_n(hit[0][1])})")
            edits.append((*abs_num, _n(nt)))
        elif cov:
            report.append(f"HIDDEN  {info['target']} 進場 {t} 被 b-roll/字卡蓋住({_n(cov[0][0])}–{_n(cov[0][1])}),沒動,自己決定要不要搬")

    # HTML 特效層的 clip:data-start 落在忙碌區間 → 推後、結尾不變
    ha = html.find("CREATIVE LAYER:")
    hb = html.find("<!-- subtitles -->")
    for m in re.finditer(r'<(?:div|img|video)\b[^>]*\bid="([^"]+)"[^>]*>', html[ha:hb]):
        tag = m.group(0)
        if "data-tx-begin" in tag or re.match(r"tx\d", m.group(1)):
            continue
        ms_, md_ = re.search(r'data-start="' + NUM + '"', tag), re.search(r'data-duration="' + NUM + '"', tag)
        if not ms_ or not md_:
            continue
        s0, d0 = float(ms_.group(1)), float(md_.group(1))
        hit = [w for w in busy if _in(s0, w)]
        if hit:
            ns = target_after(s0)
            nd = max(0.1, s0 + d0 - ns)
            new = tag.replace(ms_.group(0), f'data-start="{_n(ns)}"').replace(md_.group(0), f'data-duration="{_n(nd)}"')
            report.append(f"SHIFT   clip #{m.group(1)} data-start {s0} → {_n(ns)}(結尾不變)")
            edits.append((ha + m.start(), ha + m.end(), new))
        elif any(_in(s0, w) for w in covered):
            report.append(f"HIDDEN  clip #{m.group(1)} 從 {s0} 開始,被 b-roll/字卡蓋住")

    if not fix:
        unresolved += sum(1 for r in report if r.startswith(("SHIFT", "REMOVE", "MOVE")))
        return html, report, unresolved
    for s0, e0, rep in sorted(edits, key=lambda e: -e[0]):
        html = html[:s0] + rep + html[e0:]
    return html, report, unresolved


def _creative_clips(html):
    ha, hb = html.find("CREATIVE LAYER:"), html.find("<!-- subtitles -->")
    out = []
    for m in re.finditer(r'<div\b[^>]*\bid="([^"]+)"[^>]*class="clip"[^>]*>', html[ha:hb]):
        tag = m.group(0)
        ms_, md_ = re.search(r'data-start="' + NUM + '"', tag), re.search(r'data-duration="' + NUM + '"', tag)
        if ms_ and md_ and not re.match(r"tx\d", m.group(1)):
            out.append((m.group(1), float(ms_.group(1)), float(ms_.group(1)) + float(md_.group(1))))
    return out


# ------------------------------------------------------------------ public API
def apply_transitions(html, specs, src=None, base_dir="."):
    """specs: [dict(at, recipe, into="video"|"broll:<檔>"|"card:<小標|大字>", hold, back, dur, back_dur, sfx, back_sfx)]
    sfx / back_sfx = False:進場 / 回程不配音效(整支音效額度不夠時)。
    src = 口播主畫面檔名(預設讀 #a-roll 的 src);base_dir = index.html 所在資料夾(找 b-roll 用)。
    回傳 (html, report_lines, sfx_cues)。report 第一段是忙碌區間。cues 用 sfx_cues.save(..., "轉場", cues) 存。"""
    global W, H, BASE_DIR
    if "TX-WINDOWS" in html:
        raise ValueError("這份 index.html 已經加過轉場了。從 base.tpl 重產(重跑 creative.py)再加,不要疊兩次")
    m = re.search(r'id="a-roll"[^>]*?\bsrc="([^"]+)"', html) or re.search(r'\bsrc="([^"]+)"[^>]*?id="a-roll"', html)
    src = src or (m.group(1) if m else "preview_v1.mp4")
    mw, mh = re.search(r'data-width="(\d+)"', html), re.search(r'data-height="(\d+)"', html)
    W, H = (int(mw.group(1)) if mw else 1080), (int(mh.group(1)) if mh else 1920)
    BASE_DIR = base_dir
    stages, js_all, busy, covered, cues, spans = [], [], [], [], [], []
    for i, spec in enumerate(sorted(specs, key=lambda s: s["at"]), 1):
        st, js, legs, cov, cu, end = _build_one(i, spec, src)
        stages.append(st); js_all += js; busy += legs; covered += cov; cues += cu
        spans.append((float(spec["at"]), end))

    html, report, unresolved = resolve_collisions(html, busy, covered, fix=True)
    for spec in specs:      # 換段落用 push / iris 會同時看到「兩個她」(實測),提醒換會遮住畫面的配方
        rec = spec.get("recipe", "push-slide")
        if spec.get("into", "video") == "video" and "a" not in RECIPES[rec][3]:
            report.append(f"WARN    {spec['at']} 換段落用 {rec} 會同時看到兩個人,建議 shutter / color-blocks / blinds")

    # 常駐特效:跨過轉場的特效層 clip → T-0.15 淡出,轉場/停留結束後淡回(z 15 舞台本來就會蓋住它們,這是讓它變柔)
    for t0, t1 in spans:
        for cid, s0, e0 in _creative_clips(html):
            if s0 < t0 - FADE_OUT and e0 > t1 + FADE_IN:
                js_all.append(f'tl.to("#{cid}", {{ opacity: 0, duration: {FADE_OUT} }}, {_n(t0 - FADE_OUT)});')
                js_all.append(f'tl.fromTo("#{cid}", {{ opacity: 0 }}, {{ opacity: 1, duration: {FADE_IN}, immediateRender: false }}, {_n(t1)});')
                report.append(f"FADE    常駐特效 #{cid}:{_n(t0 - FADE_OUT)} 淡出、{_n(t1)} 淡回")

    windows = json.dumps({"busy": [[round(a, 3), round(b, 3)] for a, b in busy],
                          "covered": [[round(a, 3), round(b, 3)] for a, b in covered]})
    html = html.replace("</style>", CSS + "    </style>", 1)
    html = html.replace("<!-- subtitles -->",
                        f"<!-- TX-BEGIN (transitions.py) TX-WINDOWS {windows} -->\n" + "\n".join(stages)
                        + "\n      <!-- TX-END -->\n      <!-- subtitles -->", 1)
    html = html.replace('window.__timelines["main"] = tl;',
                        "/* TX-BEGIN */\n      " + "\n      ".join(js_all) + "\n      /* TX-END */\n      window.__timelines[\"main\"] = tl;", 1)
    report.insert(0, "BUSY    " + ", ".join(f"{_n(a)}–{_n(b)}" for a, b in busy)
                  + ("   COVERED " + ", ".join(f"{_n(a)}–{_n(b)}" for a, b in covered) if covered else ""))
    if unresolved:
        report.append(f"!! {unresolved} 個要手動處理(MANUAL)")
    return html, report, cues


def add_transition(html, at, recipe="push-slide", into="video", hold=None, back=None, **kw):
    """單一轉場的便利版。一支片有多個轉場時請用 apply_transitions 一次給(碰撞要一起算)。"""
    return apply_transitions(html, [dict(at=at, recipe=recipe, into=into, hold=hold, back=back, **kw)])


def check(html):
    m = re.search(r"TX-WINDOWS (\{.*?\}) -->", html)
    if not m:
        print("沒有 TX-WINDOWS 標記(這個 index.html 沒用 transitions.py 加過轉場)")
        return 0
    w = json.loads(m.group(1))
    _, report, bad = resolve_collisions(html, [tuple(x) for x in w["busy"]], [tuple(x) for x in w["covered"]], fix=False)
    for r in report:
        print(r)
    print(f"{'FAIL' if bad else 'OK'}:{bad} 個碰撞沒處理")
    return 1 if bad else 0


def mix_command(render_mp4, cues, out_mp4, bgm=None, bgm_vol=0.2, duration=None):
    """第 7 步混音指令(給人看的字串)。實際混音用 `sfx_cues.py mix`,會把兩行字幕跟轉場的音效一起排。"""
    import shlex
    return shlex.join(sfx_cues.mix_args(render_mp4, cues, out_mp4, bgm=bgm, bgm_vol=bgm_vol, duration=duration))


BASE_DIR = "."

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "check":
        p = Path(sys.argv[2])
        sys.exit(check((p / "index.html" if p.is_dir() else p).read_text(encoding="utf-8")))
    if len(sys.argv) >= 2 and sys.argv[1] == "recipes":
        for k, v in RECIPES.items():
            print(f"{k:13s} D={v[2]:<5} 適合 {v[3]:6s} 音效 {v[4]:15s} 來源 {v[1]}")
        sys.exit(0)
    print(__doc__)
