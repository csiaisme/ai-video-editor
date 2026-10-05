#!/usr/bin/env python3
"""
landscape_to_vertical.py — 橫式錄影(教室腳架、投影幕、螢幕錄影)排成直式底片。

為什麼有這支:選段清單很多來自橫式長錄影(講者在一邊、投影幕在另一邊、台下學員在畫面下緣)。
中央裁切會把講者或投影幕切掉,每次都要手算 ffmpeg crop/overlay(2026-10-05 教室錄影手刻了一輪)。
這支把那套做法固定下來:每一段時間指定「裁哪一塊、放在直式畫面的哪個高度、哪裡要模糊」,
其餘留純色底(上方留白給字卡/圖解,下方給字幕)。

兩個模式:

1. 量裁切框(先做這個) —— 從剪好的 preview 抽幾格,疊 100px 格線,拼成一張圖給 AI 看:
     python3 landscape_to_vertical.py measure <preview.mp4> --at 1,12,23,33 [-o 格線.jpg]
   用格線讀出「這段要裁的框」(x,y,w,h,原始像素)跟「要模糊的地方」(個股名、帳號、客人的臉)。
   ★ 腳架機位中途常常被碰到:每一段都要看,不要假設整支同一個框(2026-10-05 實測前 7.6 秒跟後面差 50px)。

2. 排版輸出:
     python3 landscape_to_vertical.py build <preview.mp4> <base.mp4> \\
         --part "0-7.61  crop=860:1020:0:60   y=330" \\
         --part "7.61-end crop=1000:760:800:0 y=470 blur=0:36:1000:152" \\
         [--bg 3A0F16] [--w 1080] [--h 1920]
   - 時間是 preview 的秒數(剪好的那支),end = 到片尾;各段要首尾相接、照順序
   - crop=w:h:x:y 是原始畫面上的框;會等比縮到直式寬度(1080),放在 y 那個高度
   - blur=x:y:w:h 座標相對於「裁出來的那塊」(不是原始畫面);可以寫好幾個 blur=
   - 音訊原封不動複製
   輸出的 base.mp4 拿去當 gen_captions 的 --video,特效層(creative.py)疊在上面。
   版面建議:上方 0~y 留給開場字卡/標題/圖解,字幕照偏好在下方 340px。
"""
import argparse, re, subprocess, sys


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(r.stderr[-2000:])
    return r.stdout


def duration(video):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video]).strip())


def measure(a):
    ts = [float(t) for t in a.at.split(",")]
    tiles = []
    for i, t in enumerate(ts):
        f = f"{a.out}.{i}.jpg"
        run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", a.video, "-frames:v", "1", "-vf",
             "drawgrid=w=100:h=100:t=2:c=yellow@0.7,scale=960:-2", f])
        tiles.append(f)
    ins = sum([["-i", f] for f in tiles], [])
    run(["ffmpeg", "-v", "error", "-y", *ins, "-filter_complex", f"vstack=inputs={len(tiles)}" if len(tiles) > 1 else "null", a.out])
    for f in tiles:
        subprocess.run(["rm", "-f", f])
    print(f"格線圖:{a.out}(由上往下 = --at 的順序 {a.at};每格 = 原始 100px;圖縮成 960 寬,讀到的數字 ×2 才是原始像素)")
    # 不用 drawtext 標秒數:工具包裝的 ffmpeg(static b6.1.1)沒有這個濾鏡


def parse_part(p, total):
    m = re.match(r"\s*([\d.]+)\s*-\s*([\d.]+|end)\s+(.*)", p)
    if not m:
        sys.exit(f"--part 格式不對:{p}")
    t0 = float(m.group(1)); t1 = total if m.group(2) == "end" else float(m.group(2))
    rest = m.group(3)
    crop = re.search(r"crop=(\d+):(\d+):(\d+):(\d+)", rest)
    y = re.search(r"\by=(\d+)", rest)
    if not crop or not y:
        sys.exit(f"--part 要有 crop=w:h:x:y 跟 y=:{p}")
    blurs = [tuple(map(int, b)) for b in re.findall(r"blur=(\d+):(\d+):(\d+):(\d+)", rest)]
    return t0, t1, tuple(map(int, crop.groups())), int(y.group(1)), blurs


def build(a):
    total = duration(a.video)
    parts = [parse_part(p, total) for p in a.part]
    for (p0, p1, *_), (q0, *_r) in zip(parts, parts[1:]):
        if abs(p1 - q0) > 0.01:
            sys.exit(f"--part 要首尾相接:{p1} 之後接的是 {q0}")
    fc, outs = [], []
    fc.append(f"[0:v]split={len(parts)}" + "".join(f"[s{i}]" for i in range(len(parts))))
    for i, (t0, t1, (cw, ch, cx, cy), y, blurs) in enumerate(parts):
        fc.append(f"color=c=0x{a.bg}:s={a.w}x{a.h}:r=30[bg{i}]")
        chain = f"[s{i}]trim={t0}:{t1},setpts=PTS-STARTPTS,crop={cw}:{ch}:{cx}:{cy}"
        if blurs:
            fc.append(chain + f",split={len(blurs) + 1}[c{i}]" + "".join(f"[cb{i}_{k}]" for k in range(len(blurs))))
            cur = f"c{i}"
            for k, (bx, by, bw, bh) in enumerate(blurs):
                r = max(4, min(bw, bh) // 8)
                fc.append(f"[cb{i}_{k}]crop={bw}:{bh}:{bx}:{by},boxblur={r}:2[bb{i}_{k}]")
                fc.append(f"[{cur}][bb{i}_{k}]overlay={bx}:{by}[c{i}_{k}]")
                cur = f"c{i}_{k}"
            fc.append(f"[{cur}]scale={a.w}:-2[p{i}]")
        else:
            fc.append(chain + f",scale={a.w}:-2[p{i}]")
        fc.append(f"[bg{i}][p{i}]overlay=0:{y}:shortest=1[o{i}]")
        outs.append(f"[o{i}]")
    fc.append("".join(outs) + f"concat=n={len(parts)}:v=1:a=0,format=yuv420p[v]")
    run(["ffmpeg", "-v", "error", "-y", "-i", a.video, "-filter_complex", ";".join(fc), "-map", "[v]", "-map", "0:a?",
         "-c:v", "libx264", "-crf", "16", "-preset", "fast", "-c:a", "copy", a.out])
    got = duration(a.out)
    print(f"寫好:{a.out}({a.w}x{a.h},{got:.2f} 秒,原片 {total:.2f} 秒)")
    if abs(got - total) > 0.2:
        print("  ⚠ 長度跟原片差超過 0.2 秒,檢查 --part 有沒有蓋滿整支", file=sys.stderr)
    print("下一步:抽幾格看裁切跟模糊有沒有對 → 拿這支當 gen_captions --video → 特效層")


def main():
    ap = argparse.ArgumentParser(description="橫式錄影 → 直式底片")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("measure", help="抽格線圖量裁切框")
    m.add_argument("video"); m.add_argument("--at", required=True, help="秒數,逗號分隔")
    m.add_argument("-o", "--out", default="格線.jpg")
    b = sub.add_parser("build", help="排成直式")
    b.add_argument("video"); b.add_argument("out")
    b.add_argument("--part", action="append", required=True, help='"t0-t1 crop=w:h:x:y y=Y [blur=x:y:w:h ...]"')
    b.add_argument("--bg", default="3A0F16", help="底色(預設酒紅 3A0F16)")
    b.add_argument("--w", type=int, default=1080); b.add_argument("--h", type=int, default=1920)
    a = ap.parse_args()
    measure(a) if a.cmd == "measure" else build(a)


if __name__ == "__main__":
    main()
