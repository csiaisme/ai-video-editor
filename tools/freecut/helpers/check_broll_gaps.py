#!/usr/bin/env python3
"""check_broll_gaps.py — render 前掃 composition,抓「兩段 b-roll 中間露出一小截 a-roll」。

為什麼需要:b-roll 照字幕句的起訖排,而句跟句之間本來就有換氣空隙,
兩段 b-roll 中間就會露出 0.1-0.3 秒的 a-roll,人臉「閃一下」
(學員回報 2026-09-08:三處全是使用者自己抓到的)。這是純算術,render 前掃一次就好。

用法:
    python3 check_broll_gaps.py <composition 資料夾或 index.html> [--min 0.6]

掃的是 index.html 裡有 data-start + data-duration 的 <video> / <img>(a-roll 除外)。
有碎縫就列出「前一段 b-roll 的 data-duration 改成多少剛好補滿」,exit 1;沒有就 exit 0。
不自動改檔:延長前要確認那段 b-roll 素材本身夠長(不夠長會停格),這步交給 AI 判斷。
動態 b-roll(HTML + GSAP 的 <div>)不在掃描範圍,自己對一下時間。
"""
from __future__ import annotations

import argparse
import sys
from html.parser import HTMLParser
from pathlib import Path


class _Clips(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.clips: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ("video", "img"):
            return
        a = {k: (v or "") for k, v in attrs}
        if a.get("id", "").startswith("a-roll") or "data-tx-aroll" in a:   # 轉場用的主畫面副本不是 b-roll
            return
        if "data-start" not in a or "data-duration" not in a:
            return
        try:
            start = float(a["data-start"])
            dur = float(a["data-duration"])
        except ValueError:
            print(f"  ? 看不懂時間,略過:<{tag} id={a.get('id', '?')}> "
                  f"data-start={a['data-start']!r} data-duration={a['data-duration']!r}")
            return
        self.clips.append({"id": a.get("id") or a.get("src") or tag,
                           "start": start, "end": start + dur, "dur": dur})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument("--min", type=float, default=0.6, help="小於這個秒數的 a-roll 空隙算碎縫(預設 0.6)")
    args = ap.parse_args()

    html = args.path / "index.html" if args.path.is_dir() else args.path
    if not html.exists():
        print(f"[X] 找不到 {html}")
        return 2
    p = _Clips()
    p.feed(html.read_text(encoding="utf-8"))
    clips = sorted(p.clips, key=lambda c: c["start"])
    if len(clips) < 2:
        print(f"[OK] b-roll {len(clips)} 段,沒有相鄰的可以檢查")
        return 0

    # 重疊的 b-roll 先併成一段,再看段跟段之間的縫
    bad = []
    cur = clips[0]
    cur_end = cur["end"]
    for nxt in clips[1:]:
        gap = nxt["start"] - cur_end
        if 0 < gap < args.min:
            bad.append((cur, nxt, gap))
        if nxt["end"] >= cur_end:
            cur, cur_end = nxt, nxt["end"]

    print(f"b-roll {len(clips)} 段:")
    for c in clips:
        print(f"  {c['start']:7.2f}-{c['end']:7.2f}  {c['id']}")
    if not bad:
        print(f"[OK] 相鄰 b-roll 之間沒有小於 {args.min} 秒的 a-roll 碎縫")
        return 0
    print(f"\n[!] {len(bad)} 處碎縫 — 人臉會閃一下:")
    for prev, nxt, gap in bad:
        fix = nxt["start"] - prev["start"]
        print(f"  {prev['end']:.2f}-{nxt['start']:.2f}(露 {gap:.2f} 秒)  {prev['id']} → {nxt['id']}")
        print(f"      補法:{prev['id']} 的 data-duration {prev['dur']:.2f} → {fix:.2f}"
              f"(先確認素材夠長,不夠就改讓 {nxt['id']} 提早開始)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
