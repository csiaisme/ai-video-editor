#!/usr/bin/env python3
"""
cutlist_to_edl.py — 把「長錄影選段」清單裡的某一支,變成這支影片的第一版剪點(edl.json)。

學員（或 Jake）用 student-cutlist / reel-cutlist 從長錄影挑好片段後,選段清單裡每一支
都已經寫了原片時間碼、跳剪順序、字幕逐句、開頭字卡。以前進剪輯要從頭轉稿、重新找剪點,
等於把選段做過的判斷再做一次(選段demo 2026-09-29 復盤)。這支工具把那份判斷直接接過來:

  1. 讀選段清單的 .html(跟 PDF 同一個資料夾、同檔名),找第 N 支的「位置」
     例:「跳剪四段：25:37 → 25:51 ／ 24:59 → 25:11 ／ …」→ 依順序取出每一段
  2. 從長錄影切出「涵蓋這幾段 ± 3 秒」的片段當這支的原始影片(自動轉正、縮到 1080 寬,
     4K 長錄影不用整支搬進來),存到專案資料夾
  3. 選段清單的時間碼只到「秒」,所以每個剪點在附近找真的停頓對齊(跟 edl_to_captions
     同一套:量音量、門檻取底噪與人聲之間),找不到就照清單的秒數並標出來
  4. 寫 工作檔/edl.json(時間都是新片段的秒數)+ 工作檔/選段資訊.md(標題、這支在講什麼、
     開頭字卡、字幕逐句、定格點、保留停頓、不可掃到…給剪接當腳本參考)

★ 這是「提案底稿」不是驗證過的剪點:接著照 SKILL 走 —— 轉逐字稿、xref 旗標全部查完、
  句內猶豫收短、render preview、verify_cut、整句讀回。選段清單的字幕逐句是「參考不是真相」,
  跟影片對不上一律以影片為準。

用法:
  python3 cutlist_to_edl.py <選段.html> <clip編號> <長錄影> <專案資料夾>
      [--pad 3] [--no-scale]

  <專案資料夾> = KIT/我的影片/<專案名>(會建 工作檔/)
  只有 PDF 沒有 .html:請學員在選段那個資料夾找同名的 .html(選段 skill 會一起產)。
"""
import argparse, html, json, re, subprocess, sys
from pathlib import Path

TC = r"(\d{1,2}:\d{2}(?::\d{2})?)"
RANGE_RE = re.compile(TC + r"\s*(?:→|->|－>|—>)\s*" + TC)


def tc2s(tc):
    parts = [int(p) for p in tc.split(":")]
    s = 0
    for p in parts:
        s = s * 60 + p
    return float(s)


def s2tc(s):
    s = int(round(s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def strip(h):
    h = re.sub(r"<br\s*/?>", "\n", h)
    h = re.sub(r"</p>", "\n", h)
    return html.unescape(re.sub(r"<[^>]+>", "", h)).strip()


def parse_clip(doc, n):
    for sec in re.findall(r'<section class="clip">(.*?)</section>', doc, re.S):
        m = re.search(r"<h3>\s*(\d+)\.\s*(.*?)</h3>", sec, re.S)
        if not m or int(m.group(1)) != n:
            continue
        rows = {}
        for k, v in re.findall(r'<div class="k">(.*?)</div>\s*<div class="v[^"]*">(.*?)</div>\s*</div>', sec, re.S):
            rows[strip(k)] = v
        lines = re.findall(r'<div class="lines">(.*?)</div>', sec, re.S)
        hook = re.search(r'<div class="hook">(.*?)</div>', sec, re.S)
        return {"title": strip(m.group(2)), "rows": rows,
                "lines": strip(lines[0]) if lines else "",
                "hook": strip(hook.group(1)) if hook else ""}
    return None


def probe(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height:stream_side_data=rotation", "-of", "json", str(video)],
                         capture_output=True, text=True, check=True).stdout
    st = json.loads(out)["streams"][0]
    w, h = st["width"], st["height"]
    rot = next((abs(int(sd.get("rotation", 0))) for sd in st.get("side_data_list", []) if "rotation" in sd), 0)
    return (h, w) if rot in (90, 270) else (w, h)


def pauses(audio, min_len=0.12):
    """跟 edl_to_captions.load_pauses 同一套:20ms 音量,門檻 = 底噪 + 35% × (人聲 - 底噪)。"""
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(audio), "-vn", "-ac", "1", "-ar", "16000",
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    n = len(x) // 320
    db = 20 * np.log10(np.sqrt((x[: n * 320].reshape(n, 320) ** 2).mean(axis=1) + 1e-12))
    floor, speech = np.percentile(db, 10), np.percentile(db, 70)
    q = db < floor + 0.35 * (speech - floor)
    spans, i = [], 0
    while i < n:
        if q[i]:
            j = i
            while j < n and q[j]:
                j += 1
            if (j - i) * 0.02 >= min_len:
                spans.append((i * 0.02, j * 0.02))
            i = j
        else:
            i += 1
    return spans


def snap_start(t, spans):
    # 清單的起點是「那句字幕開始的那一秒」(無條件捨去),真的開口在 [t, t+1) 附近。
    # 找 [t-1.2, t+1.2] 裡最接近 t+0.5 的停頓結束點,起點放在開口前 0.05 秒。
    c = [e for s, e in spans if t - 1.2 <= e <= t + 1.2]
    return (max(0.0, min(c, key=lambda e: abs(e - (t + 0.5))) - 0.05), True) if c else (t, False)


def snap_end(t, spans):
    # 清單的終點是最後一句的「結束秒」,真正收音在 [t, t+1) 附近。
    # 找 [t-0.8, t+1.8] 裡最接近 t+0.5 的停頓開始點,收在字尾後 0.1 秒。
    c = [s for s, e in spans if t - 0.8 <= s <= t + 1.8]
    return (min(c, key=lambda s: abs(s - (t + 0.5))) + 0.1, True) if c else (t + 0.9, False)


def main():
    ap = argparse.ArgumentParser(description="選段清單的某一支 → 第一版 edl.json")
    ap.add_argument("cutlist", help="選段清單的 .html(跟 PDF 同資料夾同檔名)")
    ap.add_argument("clip", type=int, help="第幾支(清單上的編號)")
    ap.add_argument("video", help="長錄影原檔")
    ap.add_argument("project", help="專案資料夾(KIT/我的影片/<專案名>)")
    ap.add_argument("--pad", type=float, default=3.0, help="切片段時前後多留幾秒(預設 3)")
    ap.add_argument("--no-scale", action="store_true", help="保留原解析度(預設縮到短邊 1080)")
    args = ap.parse_args()

    src = Path(args.cutlist)
    if src.suffix.lower() == ".pdf":
        alt = src.with_suffix(".html")
        if not alt.exists():
            sys.exit(f"只有 PDF 讀不到時間碼。選段那個資料夾應該有同名的 .html:{alt.name}")
        src = alt
    clip = parse_clip(src.read_text(encoding="utf-8"), args.clip)
    if not clip:
        sys.exit(f"選段清單裡找不到第 {args.clip} 支")
    pos = strip(clip["rows"].get("位置", ""))
    ranges = [(tc2s(a), tc2s(b)) for a, b in RANGE_RE.findall(pos)]
    if not ranges:
        sys.exit(f"第 {args.clip} 支的「位置」沒有時間碼(可能是沒時間碼的逐字稿,要照原話定位):{pos}")
    bad = [(a, b) for a, b in ranges if b <= a]
    if bad:
        sys.exit(f"時間碼順序不對:{bad}")

    proj = Path(args.project)
    work = proj / "工作檔"
    work.mkdir(parents=True, exist_ok=True)
    t0 = max(0.0, min(a for a, _ in ranges) - args.pad)
    t1 = max(b for _, b in ranges) + args.pad
    h_, m_, s_ = int(t0) // 3600, int(t0) % 3600 // 60, int(t0) % 60
    stamp = f"{h_}時{m_:02d}分{s_:02d}秒" if h_ else f"{m_}分{s_:02d}秒"
    excerpt = proj / f"原始影片-{stamp}起.mp4"
    vf = []
    if not args.no_scale:
        w, h = probe(args.video)
        if min(w, h) > 1080:
            vf = ["-vf", "scale=1080:-2" if w < h else "scale=-2:1080"]
    print(f"切片段:原片 {s2tc(t0)} → {s2tc(t1)}({t1 - t0:.0f} 秒)→ {excerpt.name}")
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{t0:.3f}", "-to", f"{t1:.3f}", "-i",
                    str(args.video), *vf, "-c:v", "libx264", "-crf", "16", "-preset", "fast",
                    "-c:a", "aac", "-b:a", "192k", str(excerpt)], check=True)

    # 清單常把一段連續的話拆成兩列(例:「25:57 → 26:07 ／ 26:07 → 26:29」),
    # 前一段的終點 = 下一段的起點。分開對停頓會重疊、同一句播兩次 → 先併成一段。
    merged = []
    for a, b in ranges:
        if merged and 0 <= a - merged[-1][1] <= 1.0:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    if len(merged) < len(ranges):
        print(f"連續的段落已合併:{len(ranges)} 段 → {len(merged)} 段")
    ranges = merged

    spans = pauses(excerpt)
    edl_ranges, notes = [], []
    for k, (a, b) in enumerate(ranges, 1):
        s, ok_s = snap_start(a - t0, spans)
        e, ok_e = snap_end(b - t0, spans)
        flag = "" if ok_s and ok_e else "(附近沒有明顯停頓,照清單秒數,一定要查波形)"
        notes.append(f"{k}. 原片 {s2tc(a)}→{s2tc(b)} → 片段 {s:.2f}-{e:.2f}{flag}")
        edl_ranges.append({"source": "SRC", "start": round(s, 2), "end": round(e, 2),
                           "beat": f"段{k}", "quote": "", "reason": f"選段清單第 {args.clip} 支 位置第 {k} 段{flag}"})
    # 保險:同一條原片時間上,後一段不能吃進前一段(會重播)
    for i, r in enumerate(edl_ranges):
        for q in edl_ranges[:i]:
            if r["start"] < q["end"] and r["end"] > q["start"]:
                print(f"  ⚠ 第 {i + 1} 段跟前面重疊({r['start']}-{r['end']} vs {q['start']}-{q['end']}),"
                      f"查波形再修", file=sys.stderr)
    # 最後一段多留室內音,不然成品最後一個字會被切(我的剪輯偏好.md「結尾一定要留尾巴」)
    edl_ranges[-1]["end"] = round(edl_ranges[-1]["end"] + 0.6, 2)

    edl = {"version": 1, "sources": {"SRC": str(excerpt.resolve())}, "ranges": edl_ranges,
           "grade": "none", "overlays": [], "subtitles": None,
           "total_duration_s": round(sum(r["end"] - r["start"] for r in edl_ranges), 2),
           "from_cutlist": {"file": str(src.resolve()), "clip": args.clip, "offset_s": t0}}
    (work / "edl.json").write_text(json.dumps(edl, ensure_ascii=False, indent=1), encoding="utf-8")

    keep = ["這支在講什麼", "開頭備案", "開頭字卡", "定格點", "保留停頓", "看完會改變什麼",
            "批評別人的話", "不可掃到", "客戶同意"]
    md = [f"# 選段資訊：第 {args.clip} 支 {clip['title']}", "",
          f"- 來源清單:`{src}`",
          f"- 原片時間 = 這支片段的秒數 + {t0:.2f}", "",
          "## 位置（原片）→ 第一版剪點（片段秒數，已對停頓）", *notes, "",
          f"## 開頭鉤子\n{clip['hook']}", ""]
    for k in keep:
        if k in clip["rows"]:
            md += [f"## {k}", strip(clip["rows"][k]), ""]
    md += ["## 字幕逐句（參考，不是真相：跟影片對不上以影片為準）", clip["lines"], ""]
    (work / "選段資訊.md").write_text("\n".join(md), encoding="utf-8")

    print("\n".join(notes))
    print(f"\n寫好:{work / 'edl.json'}({len(edl_ranges)} 段,約 {edl['total_duration_s']:.0f} 秒)")
    print(f"      {work / '選段資訊.md'}(腳本參考:開頭字卡、字幕逐句、定格點、不可掃到)")
    print("下一步:轉逐字稿 → xref 旗標查完 → 句內停頓收短 → render preview → verify_cut")


if __name__ == "__main__":
    main()
