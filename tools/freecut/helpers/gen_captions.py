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


def build(captions: list[dict], video: str, w: int, h: int,
          duration: float, font_key: str, style_key: str = "classic",
          font_size: int = 56, sub_bottom: str = "var(--safe-bottom)") -> str:
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

    subs = []
    for i, c in enumerate(captions):
        dur = round(float(c["end"]) - float(c["start"]), 2)
        subs.append(
            f'      <div id="sub-{i}" class="clip sub" data-start="{c["start"]}" '
            f'data-duration="{dur}" data-track-index="5">'
            f'<span class="sub-inner">{hl_html(c["text"], c.get("hl"), c.get("hl_color"))}</span></div>'
        )
    subs_html = "\n".join(subs)

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
{subs_html}
    </div>

    <script>
      window.__timelines = window.__timelines || {{}};
      const tl = gsap.timeline({{ paused: true }});
      gsap.set("#a-roll", {{ transformOrigin: "50% 42%" }});

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
    ap.add_argument("-o", "--out", type=Path, required=True)
    args = ap.parse_args()

    if not args.captions.exists():
        sys.exit(f"captions not found: {args.captions}")
    captions = json.loads(args.captions.read_text(encoding="utf-8"))
    if not isinstance(captions, list) or not captions:
        sys.exit("captions JSON must be a non-empty list of {start,end,text}")

    html_out = build(captions, args.video, args.w, args.h, args.duration, args.font, args.style,
                     args.font_size, args.sub_bottom)
    args.out.write_text(html_out, encoding="utf-8")
    print(f"wrote {args.out}  ({len(captions)} subtitles, font={args.font}, style={args.style})")
    print("next: add creative layers in the marked slots → npx hyperframes lint → render")


if __name__ == "__main__":
    main()
