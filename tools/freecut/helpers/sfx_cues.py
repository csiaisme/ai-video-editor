#!/usr/bin/env python3
"""
sfx_cues.py — 音效時間表(sfx_cues.json)+ 第 7 步混音。兩行字幕跟轉場都把音效寫進同一份。

為什麼需要這支:兩行字幕(gen_captions)跟轉場(transitions.py)各自會配音效,
原型時期兩邊格式不一樣,第 7 步要手抄兩份時間表進 ffmpeg,容易漏、容易抄錯秒數。
現在統一成一份 `工作檔/captions/sfx_cues.json`,每個工具只改「自己那幾筆」(from 欄位),
重跑 gen_captions 不會洗掉轉場的音效,反過來也一樣。

一筆 cue 長這樣:
    {"at": 14.89, "sfx": "4-電影感/hit.mp3", "file": "<完整路徑>", "volume": 0.075,
     "trim": 0.6, "from": "兩行字幕", "note": "sub-8 stamp 滋生細菌"}
  at      音效從第幾秒開始放(成品時間軸)
  trim    只用前幾秒(可省略 = 整個檔)。hit 這種有長尾巴的檔一定要剪
  from    誰寫的:兩行字幕 / 轉場 / 手動(AI 自己加的其他音效就寫「手動」)

用法:
    python3 sfx_cues.py list  <sfx_cues.json>        列出全部 + 檢查(同一個音效 15 秒內重複、總數)
    python3 sfx_cues.py mix   <render_final.mp4> <sfx_cues.json> <成品.mp4> [--bgm 檔 --bgm-vol 0.2 --duck-db 4.5]
        一條 ffmpeg 把音效 + BGM 混進去(影片流複製不重壓),做法同 SKILL.md 第 7 步
        (amix normalize=0 → alimiter level=disabled),混完印峰值。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIT = HERE.parents[2]                                   # tools/freecut/helpers → KIT
SFX_ROOT = KIT / "素材庫" / "預設包" / "音效"
SAME_SFX_GAP = 15.0                                     # 同一個音效 15 秒內不重複(兩行字幕 Round 2 規則)
DUCK_DB = 4.5                                           # 音效響的時候 BGM 往下壓幾 dB(來源建議 3-6dB;Jake 2026-10-07 同意加)
CAP_HINT = "3-6"                                        # 對照表的每支音效點數(Jake 2026-10-07 定:只配在看得到的動作上)


def make_cue(sfx: str, at: float, volume: float, owner: str, note: str = "", trim: float | None = None) -> dict:
    """sfx = 預設包/音效 底下的相對路徑(例 "3-轉場/woosh-1.mp3"),或任何檔案的完整路徑。"""
    p = Path(sfx)
    f = p if p.is_absolute() else SFX_ROOT / p
    c = {"at": round(max(0.0, float(at)), 3), "sfx": sfx, "file": str(f), "volume": volume}
    if trim:
        c["trim"] = trim
    c["from"] = owner
    if note:
        c["note"] = note
    return c


def load(path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def check(cues: list[dict]) -> list[str]:
    """回傳提醒(不擋):同一個音效 15 秒內重複、總數超過建議。"""
    out, last = [], {}
    for c in sorted(cues, key=lambda c: c["at"]):
        k = Path(c.get("sfx") or c["file"]).name
        if k in last and c["at"] - last[k]["at"] < SAME_SFX_GAP:
            out.append(f"⚠ 同一個音效 {k} 在 {last[k]['at']}s 跟 {c['at']}s 重複(< {SAME_SFX_GAP:.0f} 秒)"
                       f":{last[k].get('note', '')} / {c.get('note', '')}")
        last[k] = c
    if len(cues) > int(CAP_HINT.split("-")[1]):
        out.append(f"⚠ 整支 {len(cues)} 個音效點(對照表建議 {CAP_HINT} 個;兩行字幕、轉場的音效都算在內)。"
                   f"多的可以在 captions.json 寫 \"line2_sfx\": false,或刪掉 sfx_cues.json 裡那筆")
    return out


def save(path, owner: str, cues: list[dict]) -> list[str]:
    """把 owner 這個工具的 cue 換成新的一批,別人的保留。回傳 check() 的提醒。
    沒有任何 cue 而且檔案本來不存在 → 不建檔。"""
    p = Path(path)
    keep = [c for c in load(p) if c.get("from") != owner]
    allc = sorted(keep + list(cues), key=lambda c: c["at"])
    if allc or p.exists():
        p.write_text(json.dumps(allc, ensure_ascii=False, indent=1), encoding="utf-8")
    return check(allc)


def _resolve(c: dict) -> str:
    f = Path(c["file"])
    if not f.exists() and c.get("sfx"):              # 工具包搬過位置:用相對路徑重找
        f2 = SFX_ROOT / c["sfx"]
        if f2.exists():
            f = f2
    if not f.exists():
        raise FileNotFoundError(f"音效檔不存在:{c['file']}")
    return str(f)


def _duration(p) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                         capture_output=True, text=True).stdout.strip()
    return float(out)


def _duck_expr(cues: list[dict], bgm_vol: float, duck_db: float) -> str:
    """BGM 的音量算式:每個音效響的那段(提早 0.05 秒、0.08 秒壓下去,結束後 0.3 秒回來)乘上 -duck_db。
    重疊的音效相乘,所以兩個疊在一起會壓更多(本來就不該疊在同一拍)。"""
    if duck_db <= 0 or not cues:
        return f"{bgm_vol}"
    d = 10 ** (-duck_db / 20)
    parts = []
    for c in cues:
        ln = c.get("trim") or min(_duration(_resolve(c)), 1.2)
        a, b = c["at"] - 0.05, c["at"] + ln
        parts.append(f"(1-{1 - d:.3f}*clip((t-{a:.3f})/0.08,0,1)*clip(({b + 0.3:.3f}-t)/0.3,0,1))")
    return f"{bgm_vol}*" + "*".join(parts)


def mix_args(render_mp4, cues: list[dict], out_mp4, bgm=None, bgm_vol: float = 0.2, duration: float | None = None,
             duck_db: float = DUCK_DB) -> list[str]:
    """回傳 ffmpeg 參數 list(跨平台直接丟 subprocess,不經過 shell 引號)。"""
    args = ["ffmpeg", "-y", "-i", str(render_mp4)]
    fl, labels = ["[0:a]aformat=channel_layouts=stereo:sample_rates=48000[v0]"], ["[v0]"]
    for k, c in enumerate(cues, 1):
        args += ["-i", _resolve(c)]
        ms = int(round(c["at"] * 1000))
        tr = c.get("trim")
        cut = f"atrim=0:{tr},afade=t=out:st={max(0.0, tr - 0.12):.2f}:d=0.12," if tr else ""
        fl.append(f"[{k}:a]aformat=channel_layouts=stereo:sample_rates=48000,{cut}"
                  f"volume={c['volume']},adelay={ms}|{ms}[s{k}]")
        labels.append(f"[s{k}]")
    if bgm:
        k = len(cues) + 1
        dur = duration or _duration(render_mp4)
        args += ["-stream_loop", "-1", "-i", str(bgm)]
        fl.append(f"[{k}:a]aformat=channel_layouts=stereo:sample_rates=48000,atrim=0:{dur:.3f},"
                  f"afade=t=in:st=0:d=0.9,afade=t=out:st={max(0.0, dur - 1.4):.2f}:d=1.4,"
                  f"volume='{_duck_expr(cues, bgm_vol, duck_db)}':eval=frame[bgm]")
        labels.append("[bgm]")
    fl.append(f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0[mix];"
              f"[mix]alimiter=limit=0.89:level=disabled[aout]")
    return args + ["-filter_complex", ";".join(fl), "-map", "0:v", "-map", "[aout]",
                   "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out_mp4)]


def _peak(p) -> str:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(p), "-af", "volumedetect", "-vn", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"max_volume:\s*(-?[\d.]+) dB", r.stderr)
    return m.group(1) if m else "?"


def main() -> int:
    ap = argparse.ArgumentParser(description="sfx_cues.json:列出 / 混音")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("list")
    a1.add_argument("cues")
    a2 = sub.add_parser("mix")
    a2.add_argument("render")
    a2.add_argument("cues")
    a2.add_argument("out")
    a2.add_argument("--bgm")
    a2.add_argument("--bgm-vol", type=float, default=0.2)
    a2.add_argument("--duck-db", type=float, default=DUCK_DB, help=f"音效響時 BGM 壓幾 dB(預設 {DUCK_DB},0 = 不壓)")
    a = ap.parse_args()

    cues = load(a.cues)
    if a.cmd == "list":
        for c in cues:
            tr = f" 剪 {c['trim']}s" if c.get("trim") else ""
            print(f"{c['at']:7.2f}s  {c.get('sfx', c['file'])}  x{c['volume']}{tr}  [{c.get('from', '?')}] {c.get('note', '')}")
        for w in check(cues):
            print(w)
        print(f"共 {len(cues)} 個音效點")
        return 0

    if not shutil.which("ffmpeg"):
        sys.exit("找不到 ffmpeg(先 source tools/env.sh)")
    for w in check(cues):
        print(w)
    args = mix_args(a.render, cues, a.out, bgm=a.bgm, bgm_vol=a.bgm_vol, duck_db=a.duck_db)
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-2000:])
        return r.returncode
    print(f"done: {a.out}({len(cues)} 個音效{' + BGM' if a.bgm else ''},長度 {_duration(a.out):.2f}s,"
          f"峰值 {_peak(a.out)} dB — 要 < -1)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
