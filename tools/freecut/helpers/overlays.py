#!/usr/bin/env python3
"""
overlays.py — 在 ai-edit 口播 composition 疊「現成的疊加效果」(HyperFrames registry 6 個 block 改成直式繁中版)。

由專案的 creative.py 在寫出 index.html 前呼叫。有轉場的話**先加轉場、再加疊加**(這樣疊加會自己避開轉場):

    import sys; sys.path.insert(0, str(KIT / "tools" / "freecut" / "helpers"))
    from transitions import apply_transitions
    from overlays import apply_overlays
    import sfx_cues
    s, rep1, c1 = apply_transitions(s, [...])                      # 沒有轉場就跳過
    s, rep2, c2 = apply_overlays(s, [
        dict(at=0.4,  recipe="notification", app="訊息", title="小美", text="老師,我睫毛又掉了😭"),
        dict(at=23.7, recipe="flash"),                               # at = 閃那一下(揭曉那格)
        dict(at=19.3, recipe="freeze", variant="comedy"),           # at = 定格那格
        dict(at=8.0,  recipe="light-leak", dur=2.5),
        dict(at=2.8,  recipe="camcorder", dur=3.6),
        dict(at=29.6, recipe="ig-follow"),                           # 帳號/名稱/頭像從 我的剪輯偏好.md 讀
    ], base_dir=".")
    print(*rep1, *rep2, sep="\\n")                                   # 紀錄一定要讀
    for w in sfx_cues.save("sfx_cues.json", "轉場", c1): print(w)
    for w in sfx_cues.save("sfx_cues.json", "疊加卡片", c2): print(w)
    open("index.html", "w", encoding="utf-8").write(s)

每個 spec:
  at       開始秒數(成品時間軸)。flash = 閃那一下;freeze = 定格那一格
  recipe   配方(python3 overlays.py recipes 看全部)
  dur      停留秒數(不給用配方預設)。蓋到片尾會自動截短、不做退場
  sfx      不給 = 配方預設音效;False = 不要音效;或 SFX 表的名字(例 "record-scratch")
  其他     配方自己的參數(見 recipes 表 / tools/特效工具箱.md 第 10 節)

圖層:疊加 z-index 11-12 = 蓋過特效層(6-7),在轉場舞台(15)跟字幕(20)底下。字幕永遠在最上面。
時間:每個疊加是一條自己的 GSAP 子時間軸,用一句 `tl.add(ovN, 秒數)` 掛上去,外面包一個 class="clip" 的 div。
     → transitions.py 的碰撞檢查把它當一個「特效進場」,要 SHIFT 時整個一起搬,不會拆散。
     → 已經加過轉場:這裡自己先把落在轉場區間的疊加搬到轉場結束 +0.1 秒(整個搬,長度不變)。
模板:tools/overlays/<配方>.html(CSS + HTML + JS 三段,{{參數}} 由這支填)。
"""
from __future__ import annotations

import html as _html
import json
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIT = HERE.parents[2]
TPL_DIR = KIT / "tools" / "overlays"
PREFS = KIT / "我的剪輯偏好.md"
sys.path.insert(0, str(HERE))
import sfx_cues  # noqa: E402

OWNER = "疊加卡片"
GAP = 0.1
TRACK0 = 100            # 特效層 10-19、轉場 40+,疊加 100+
# 中文字型:lint 要求每個字型都有 @font-face。local() = 用電腦內建的(Mac 蘋方 / Windows 微軟正黑體),
# 都載不到時退到工具包安裝時就有的思源宋體。模板一律寫 font-family:"OvSans",...;等寬數字用 "OvMono"。
BASE_CSS = """      /* ==== OVERLAYS 共用字型(overlays.py)==== */
      @font-face { font-family:"OvSans"; src: local("PingFangTC-Regular"), local("PingFang TC"), local("MicrosoftJhengHeiRegular"), local("Microsoft JhengHei"); font-weight:400; }
      @font-face { font-family:"OvSans"; src: local("PingFangTC-Semibold"), local("MicrosoftJhengHeiBold"), local("Microsoft JhengHei Bold"); font-weight:700; }
      @font-face { font-family:"OvMono"; src: local("Menlo-Bold"), local("Menlo"), local("Consolas-Bold"), local("Consolas Bold"), local("Consolas"); font-weight:700; }"""

# 音效:(預設包相對路徑, volume, 對齊用:音效從「事件那格 − 這個秒數」開始放)
# volume:camera-shutter / record-scratch 照對照表;notification-sound 用 ebur128 I=-31.1 換算再保守一點;
#         mouse-click 只有 0.2 秒、LUFS 量不準,用峰值(-13.6 dB)抓到跟 camera-shutter 差不多大聲。2026-10-07 量。
SFX = {
    "mouse-click":    ("2-介面科技/mouse-click.mp3",        1.8,  0.03),
    "notification":   ("2-介面科技/notification-sound.mp3", 1.5,  0.0),
    "camera-shutter": ("3-轉場/camera-shutter.mp3",         0.86, 0.09),
    "record-scratch": ("1-迷因梗/record-scratch.mp3",       0.5,  0.06),
    "swell":          ("4-電影感/remembering-woosh.mp3",    0.6,  0.24),   # 光暈選配:很輕的回憶感
}


def _n(x):
    return f"{x:.3f}".rstrip("0").rstrip(".")


def _esc(s):
    return _html.escape(str(s), quote=True)


def _load(name):
    t = (TPL_DIR / f"{name}.html").read_text(encoding="utf-8")
    get = lambda tag: (re.search(rf"<{tag}>(.*?)</{tag}>", t, re.S) or [None, ""])[1]
    return get("style"), get("template").strip(), get("script").strip()


def _fill(s, params):
    def rep(m):
        k = m.group(1)
        if k not in params:
            raise KeyError(f"模板少了參數 {{{{{k}}}}}")
        return str(params[k])
    return re.sub(r"\{\{(\w+)\}\}", rep, s)


def prefs_ig():
    """從 我的剪輯偏好.md 讀 IG 帳號 / 顯示名稱 / 頭像(「## 特效／品牌」底下那三行)。"""
    out = {}
    if PREFS.exists():
        t = PREFS.read_text(encoding="utf-8")
        for key, field in (("handle", "IG 帳號"), ("name", "IG 顯示名稱"), ("avatar", "IG 頭像")):
            m = re.search(rf"{field}\s*[:：]\s*(.+)", t)
            v = m.group(1).strip().strip("`") if m else ""
            if v and "尚未" not in v:
                out[key] = v
    return out


def _copy_in(path, base_dir, name):
    """把素材(頭像)複製進 composition 資料夾,回傳 index.html 裡用的相對檔名。"""
    p = Path(path)
    if not p.is_absolute():
        p = next((c for c in (Path(base_dir) / p, KIT / p) if c.exists()), KIT / p)
    if not p.exists():
        raise FileNotFoundError(f"找不到圖檔:{path}(放進 素材庫/圖片/,偏好檔寫相對 KIT 的路徑)")
    dst = Path(base_dir) / (name + p.suffix.lower())
    if p.resolve() != dst.resolve():
        shutil.copyfile(p, dst)
    return dst.name


def _cue(name, at, uid, recipe):
    if name is False or name is None:
        return []
    path, vol, off = SFX[name] if name in SFX else (name, 1.0, 0.0)
    return [sfx_cues.make_cue(path, at - off, vol, OWNER, note=f"{uid} {recipe}")]


# ------------------------------------------------------------------ recipes
# builder(spec, T, D, u, ctx) -> (params, wrapper_style, cues, notes)
#   params 填進模板;T/D = 成品開始秒數 / 長度;ctx = dict(base_dir, src, vdur, ends_at_video_end)

def b_ig(spec, T, D, u, ctx):
    p = {**prefs_ig(), **{k: spec[k] for k in ("handle", "name", "avatar") if spec.get(k)}}
    if not p.get("handle"):
        raise ValueError("ig-follow 需要 IG 帳號:先問學員一次(帳號、顯示名稱、頭像),記進 我的剪輯偏好.md 的「特效／品牌」"
                         "(格式見 tools/我的剪輯偏好.範本.md),或在 spec 給 handle=")
    handle = p["handle"] if p["handle"].startswith("@") else "@" + p["handle"]
    name = p.get("name") or handle[1:]
    if p.get("avatar"):
        av = f'<img class="ov-ig-av" src="{_esc(_copy_in(p["avatar"], ctx["base_dir"], "ov-avatar"))}" alt="">'
    else:                                   # 沒頭像:名稱第一個字的圓章(不用任何真人照片)
        av = f'<div class="ov-ig-av ov-ig-ini">{_esc(name[:1])}</div>'
    press = float(spec.get("press", min(1.1, max(0.6, D - 1.0))))
    sub = spec.get("subline")
    exit_js = "" if ctx["ends_at_video_end"] else \
        f'{u}.to("#{u}-card", {{ y: 160, opacity: 0, duration: 0.25, ease: "power3.in" }}, {_n(D - 0.3)});'
    tap = spec.get("tap", "hand-ripple")          # 學員 2026-10-07 選的;circle / hand / none 是替代款
    parts = {"circle": ["circle"], "hand": ["hand"], "hand-ripple": ["ripple", "hand"], "none": []}.get(tap)
    if parts is None:
        raise ValueError(f"tap 只能是 circle / hand / hand-ripple / none,不是 {tap}")
    src = (TPL_DIR / "ig-follow.html").read_text(encoding="utf-8")
    sec = lambda kind, n: re.search(rf'<tap-{kind} name="{n}">(.*?)</tap-{kind}>', src, re.S).group(1).strip()
    tap_html = "".join(sec("html", n) for n in parts)
    tap_js = "\n      ".join(sec("js", n) for n in parts)
    params = dict(tap_html=tap_html, tap_js=tap_js, avatar=av, name=_esc(name), handle=_esc(handle), press=_n(press), exit=exit_js,
                  subline=f'<div class="ov-ig-sub">{_esc(sub)}</div>' if sub else "")
    top = int(spec.get("top", 1130))
    notes = [] if T >= ctx["vdur"] - 6 else [f"WARN    ig-follow 放在 {T}s,不在片尾 — CTA 通常放最後 2-4 秒"]
    return params, f"top:{top}px", _cue(spec.get("sfx", "mouse-click"), T + press, u, "ig-follow"), notes, ""


def b_notif(spec, T, D, u, ctx):
    exit_js = "" if ctx["ends_at_video_end"] else \
        f'{u}.to("#{u}-card", {{ y: -140, opacity: 0, duration: 0.3, ease: "power3.in" }}, {_n(D - 0.35)});'
    params = dict(app=_esc(spec.get("app", "訊息")), title=_esc(spec.get("title", "")), text=_esc(spec.get("text", "")),
                  time=_esc(spec.get("time", "現在")), icon=_esc(spec.get("icon_color", "#34C759")), exit=exit_js)
    if not spec.get("title") and not spec.get("text"):
        raise ValueError("notification 至少要給 title(寄件人)或 text(內容)")
    top = int(spec.get("top", 245))
    return params, f"top:{top}px", _cue(spec.get("sfx", "notification"), T + 0.12, u, "notification"), [], ""


def b_flash(spec, T, D, u, ctx):
    s = float(spec.get("strength", 1.0))
    params = dict(hit="0.05", wash=_n(0.92 * s), core=_n(1.0 * s), sweep=_n(0.9 * s))
    return params, "mix-blend-mode:screen;", _cue(spec.get("sfx", "camera-shutter"), T + 0.05, u, "flash"), [], ""


def b_freeze(spec, T, D, u, ctx):
    if not shutil.which("ffmpeg"):
        raise RuntimeError("找不到 ffmpeg(先 source tools/env.sh)— freeze 要用它截定格那一格")
    still = f"ov-freeze-{_n(T).replace('.', '_')}.png"
    out = Path(ctx["base_dir"]) / still
    if not out.exists():
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{T + 0.02:.3f}", "-i", str(Path(ctx["base_dir"]) / ctx["src"]),
                            "-frames:v", "1", str(out)], capture_output=True, text=True)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"截定格失敗:{r.stderr[-300:]}")
    variant = spec.get("variant", "photo")
    params = dict(still=still, exit=_n(max(0.75, D - 0.3)), tilt=_n(float(spec.get("tilt", -2.5))))
    sfx = spec.get("sfx", "record-scratch" if variant == "comedy" else "camera-shutter")
    notes = [f"NOTE    freeze 截了 {still}(檔案不見的話重跑 creative.py 會再截)。定格時聲音照常播"]
    return params, "", _cue(sfx, T + 0.02, u, "freeze"), notes, ""


def b_leak(spec, T, D, u, ctx):
    s = float(spec.get("strength", 0.7))
    params = dict(D=_n(D), peak=_n(0.9 * s), mid=_n(0.46 * s))
    return params, "mix-blend-mode:screen;", _cue(spec.get("sfx", None), T + 0.35, u, "light-leak"), [], ""


def b_cam(spec, T, D, u, ctx):
    d = spec.get("date") or date.today().strftime("%Y.%m.%d")
    tm = spec.get("time", "")
    params = dict(D=_n(D), date=_esc(d), time=f'<span class="ov-cam-time">{_esc(tm)}</span>' if tm else "",
                  start=_n(float(spec.get("counter_start", 0))), mode=_esc(spec.get("mode", "SP · 9:16")),
                  bottom=int(spec.get("bottom", 470)))
    return params, "", _cue(spec.get("sfx", None), T, u, "camcorder"), [], ""


# name: (builder, registry 來源, 預設 dur, z, 什麼時候用, 預設音效)
RECIPES = {
    "ig-follow":    (b_ig,     "instagram-follow",           3.2, 12, "結尾 CTA「追蹤我」(帳號/名稱/頭像讀偏好檔)", "mouse-click(點下去那格)"),
    "notification": (b_notif,  "macos-notification",         2.6, 12, "講到「有人傳訊息/客人問」→ 左上掉下一則通知",  "notification"),
    "flash":        (b_flash,  "editorial-flash-overlay",    0.6, 12, "揭曉/拍照那一下的閃光(at = 閃的那格)",       "camera-shutter"),
    "freeze":       (b_freeze, "freeze-frame-dressing",      1.6, 11, "定格成一張照片(紙+膠帶+閃光);variant=comedy 配唱片刮停", "camera-shutter / record-scratch"),
    "light-leak":   (b_leak,   "organic-light-leak-overlay", 2.5, 12, "回憶/感性那段的暖色漏光",                   "無(sfx='swell' 可加很輕的)"),
    "camcorder":    (b_cam,    "camcorder-hud",              4.0, 12, "幕後/vlog 段落:REC 點、電池、日期、計時",     "無"),
}


def _tx_windows(html):
    m = re.search(r"TX-WINDOWS (\{.*?\}) -->", html)
    if not m:
        return [], []
    w = json.loads(m.group(1))
    return [tuple(x) for x in w["busy"]], [tuple(x) for x in w["covered"]]


def _in(t, w):
    return w[0] - 1e-6 <= t < w[1] - 1e-6


# ------------------------------------------------------------------ public API
def apply_overlays(html, specs, base_dir=".", src=None):
    """specs: [dict(at, recipe, dur, sfx, **配方參數)]。base_dir = index.html 所在資料夾(頭像、定格圖放這)。
    回傳 (html, report_lines, sfx_cues)。cues 用 sfx_cues.save(..., "疊加卡片", cues) 存。"""
    if "OV-BEGIN" in html:
        raise ValueError("這份 index.html 已經加過疊加卡片了。從 base.tpl 重產(重跑 creative.py)再加,不要疊兩次")
    m = re.search(r'id="a-roll"[^>]*?\bsrc="([^"]+)"', html) or re.search(r'\bsrc="([^"]+)"[^>]*?id="a-roll"', html)
    src = src or (m.group(1) if m else "preview_v1.mp4")
    md = re.search(r'id="root"[^>]*?data-duration="([\d.]+)"', html) or re.search(r'data-duration="([\d.]+)"', html)
    vdur = float(md.group(1)) if md else 1e9
    busy, covered = _tx_windows(html)
    css_done, css, clips, js, report, cues, spans = set(), [BASE_CSS], [], [], [], [], []

    for i, spec in enumerate(sorted(specs, key=lambda s: s["at"]), 1):
        rec = spec.get("recipe")
        if rec not in RECIPES:
            raise ValueError(f"不認得的配方 {rec}(python3 overlays.py recipes)")
        fn, source, D0, z, _, _ = RECIPES[rec]
        u = f"ov{i}"
        T = float(spec["at"]) - (0.05 if rec == "flash" else 0.0)     # flash:at = 閃那格,clip 提早 0.05 開
        D = float(spec.get("dur") or D0)
        for _ in range(10):                                           # 已經有轉場:整個搬到轉場結束後
            hit = [w for w in busy if _in(T, w) or (rec == "flash" and _in(T + 0.05, w))]
            if not hit:
                break
            nt = max(w[1] for w in hit) + GAP
            report.append(f"SHIFT   {u} {rec} {_n(T)} → {_n(nt)}(落在轉場 {_n(hit[0][0])}–{_n(hit[0][1])},整個往後搬)")
            T = nt
        if any(_in(T, w) for w in covered):
            report.append(f"HIDDEN  {u} {rec} @ {_n(T)} 被 b-roll/字卡蓋住,自己決定要不要搬")
        span_hit = [w for w in busy if T < w[0] < T + D]
        if span_hit:
            report.append(f"NOTE    {u} {rec} 跨過轉場 {_n(span_hit[0][0])}(轉場那 0.5 秒會被蓋住,正常)")
        ends = T + D >= vdur - 0.05
        if T + D > vdur:
            D = max(0.3, vdur - T)
        ctx = dict(base_dir=base_dir, src=src, vdur=vdur, ends_at_video_end=ends)
        params, wstyle, cu, notes, _ = fn(spec, T, D, u, ctx)
        style, tpl, script = _load(rec)
        params.update(u=u, O=f"ov{i}")
        if rec not in css_done:
            css.append(f"      /* ==== OVERLAY {rec}(overlays.py,來源 registry {source})==== */\n      " + style.strip())
            css_done.add(rec)
        inner = _fill(_fill(tpl, params), params)            # 兩次:tap_html 裡面還有 {{u}}
        inner_style = wstyle if not wstyle.startswith("mix-blend") else ""
        wrap_extra = wstyle if wstyle.startswith("mix-blend") else ""
        clips.append(f'      <div id="{u}" class="clip" data-start="{_n(T)}" data-duration="{_n(D)}" data-track-index="{TRACK0 + i}" '
                     f'data-ov="{rec}" style="position:absolute; inset:0; pointer-events:none; z-index:{z}; {wrap_extra}">'
                     f'<div class="ov-{rec}" style="{inner_style}">{inner}</div></div>')
        body = _fill(_fill(script, params), params)
        js.append(f"// {u} {rec} @ {_n(T)}(疊加卡片 overlays.py)\n      const ov{i} = gsap.timeline();\n      {body}\n"
                  f"      tl.add(ov{i}, {_n(T)});")
        cues += cu
        report.append(f"ADD     {u} {rec} {_n(T)}–{_n(T + D)}" + (f"  音效 {Path(cu[0]['sfx']).stem} @ {cu[0]['at']}" if cu else ""))
        report += notes
        spans.append((T, T + D, rec, u))

    full = {"flash", "light-leak", "freeze"}                     # 全畫面類別疊在一起會糊
    for a in range(len(spans)):
        for b in range(a + 1, len(spans)):
            s1, s2 = spans[a], spans[b]
            if s1[0] < s2[1] and s2[0] < s1[1] and (s1[2] in full or s2[2] in full or s1[2] == s2[2]):
                report.append(f"WARN    {s1[3]} {s1[2]} 跟 {s2[3]} {s2[2]} 時間重疊,同一拍只放一個")

    html = html.replace("</style>", "\n".join(css) + "\n    </style>", 1)
    anchor = "<!-- TX-BEGIN" if "<!-- TX-BEGIN" in html else "<!-- subtitles -->"
    html = html.replace(anchor, "<!-- OV-BEGIN (overlays.py) -->\n" + "\n".join(clips) + "\n      <!-- OV-END -->\n      " + anchor, 1)
    janchor = "/* TX-BEGIN */" if "/* TX-BEGIN */" in html else 'window.__timelines["main"] = tl;'
    html = html.replace(janchor, "/* OV-BEGIN */\n      " + "\n      ".join(js) + "\n      /* OV-END */\n      " + janchor, 1)
    return html, report, cues


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "recipes":
        for k, v in RECIPES.items():
            print(f"{k:13s} D={v[2]:<4} 音效 {v[5]:28s} {v[4]}  (registry: {v[1]})")
        print("\n音效表(sfx= 可以填的名字):", ", ".join(SFX))
        sys.exit(0)
    print(__doc__)
