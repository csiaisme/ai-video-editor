#!/usr/bin/env python3
"""產生「開始審片.html」— 免拖檔的審片頁。

把 KIT/審片.html 複製一份到專案的 審片區/,並把影片檔名 + 字幕 JSON 內嵌進去。
使用者雙擊 審片區/開始審片.html 就直接開審,不用再拖兩個檔案。
每輪出新 preview 後重跑一次(直接覆蓋舊的)。

用法:
    python3 make_review_page.py <審片區資料夾> <影片檔名> [字幕.json路徑] [--burned-in|--no-burned-in] [--no-open]

    <影片檔名>   審片區裡那支影片的「檔名」(相對路徑,不是完整路徑)
    [字幕路徑]   預設用 <審片區>/字幕.json
    --burned-in  這支影片「已經把字幕燒進畫面」(成品 render 完的那種)。
                 帶這個旗標時,審片頁不會再疊一層預覽字幕 — 否則畫面上會出現
                 兩層字幕互相重疊。預設會自動判斷:檔名以「成品」開頭就當作已燒進去。
                 --no-burned-in 可強制關掉自動判斷。
    --no-open    產生完不要幫使用者打開(預設會用瀏覽器直接打開這頁 —
                 學員回報 15 次找不到審片區/成品,AI 直接幫他開的那幾次全部一次解決)。

每輪產生時會寫入一個「輪次戳記」。審片頁看到新的戳記,就知道這是 AI 出的新一版,
會把上一輪殘留的註解清掉 — 使用者不用再一則一則點叉叉。
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

MARKER_START = "<!-- __審片AUTO__"
MARKER_END = "-->"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if len(args) < 2:
        print(__doc__)
        return 1
    review_dir = Path(args[0])
    video_name = args[1]
    subs_path = Path(args[2]) if len(args) > 2 else review_dir / "字幕.json"

    # 字幕已經燒進畫面的片子,審片頁不能再疊一層預覽字幕(會變兩層重疊)。
    if "--no-burned-in" in flags:
        burned_in = False
    elif "--burned-in" in flags:
        burned_in = True
    else:
        burned_in = Path(video_name).stem.startswith("成品")

    kit = Path(__file__).resolve().parents[3]   # helpers/ → freecut/ → tools/ → KIT
    template = kit / "審片.html"
    if not template.exists():
        print(f"[X] 找不到 {template}")
        return 1
    if not (review_dir / video_name).exists():
        print(f"[X] 審片區裡沒有 {video_name} — 先把影片複製進去再產生這頁")
        return 1

    # 影片一律要跟這頁同一層。以前用 ../工作檔/xxx.mp4 相對路徑省一份複製,
    # 但 Safari 擋 file:// 往上層抓檔 → 只有 Safari 的 Mac(原廠狀態很常見)
    # 頁面一片空白,學員以為自己弄壞了(實測回報)。
    # macOS 用 APFS clone(秒複製、不佔額外空間),失敗退 hard link,再退真複製。
    if "/" in video_name:
        src = (review_dir / video_name).resolve()
        dest = review_dir / src.name
        # 同名舊檔一定要換掉:以前只在「不存在」時才複製,重出新版後審片區永遠停在
        # 第一版,訊息卻照印「已放進」— 學員連續兩支片審到舊片(回報 2026-09-10 / 09-14)。
        # hard link 過來的(samefile)本來就會跟著來源變,不用動。
        if dest.exists() and not dest.samefile(src):
            dest.unlink()
        copied = not dest.exists()
        if copied:
            # Windows 沒有 cp,subprocess 會直接丟 FileNotFoundError —
            # 不接住的話底下的 hard link / 真複製 fallback 永遠輪不到(學員實測必炸)。
            try:
                cloned = subprocess.run(["cp", "-c", str(src), str(dest)],
                                        capture_output=True).returncode == 0
            except (FileNotFoundError, OSError):
                cloned = False
            if not cloned:
                try:
                    os.link(src, dest)
                except OSError:
                    shutil.copy2(src, dest)
        video_name = src.name
        if copied:
            print(f"(影片已放進審片區同層:{video_name} — Safari 不吃 ../ 相對路徑)")
        else:
            print(f"(審片區的 {video_name} 跟來源是同一個檔,不用重複製)")

    html = template.read_text(encoding="utf-8")
    start = html.find(MARKER_START)
    if start < 0:
        print("[X] 審片.html 裡找不到 __審片AUTO__ 標記(舊版?先更新工具包)")
        return 1
    end = html.index(MARKER_END, start) + len(MARKER_END)

    subs_txt = "null"
    if subs_path.exists():
        # 先 parse 再 dump:確認是合法 JSON,也把內容壓成一行安全內嵌
        subs_txt = json.dumps(json.loads(subs_path.read_text(encoding="utf-8")),
                              ensure_ascii=False)
    # 樣式側檔(gen_captions 產的):有它,審片頁的預覽字幕就跟成品同字型/字級/位置。
    # 沒有就維持通用樣式,審片頁會提示「預覽樣式非成品」。
    cap_style = None
    for cand in (review_dir / "樣式.json", subs_path.with_name("樣式.json")):
        if cand.exists():
            try:
                cap_style = json.loads(cand.read_text(encoding="utf-8"))
                break
            except (json.JSONDecodeError, OSError):
                pass

    # round:這一版的戳記。審片頁比對到不一樣,就把上一輪的舊註解清掉。
    meta = {"video": video_name, "round": time.strftime("%Y%m%d-%H%M%S"),
            "project": review_dir.resolve().parent.name,
            "burnedIn": burned_in, "capStyle": cap_style}
    inject = ("<script>window.__審片AUTO = "
              + json.dumps(meta, ensure_ascii=False)[:-1]
              + f', "subs": {subs_txt}}};</script>')

    out = review_dir / "開始審片.html"
    out.write_text(html[:start] + inject + html[end:], encoding="utf-8")
    n = "?" if subs_txt == "null" else len(json.loads(subs_txt))
    burned_note = ",字幕已燒進畫面(不疊預覽字幕)" if burned_in else ""
    print(f"[OK] 產生 {out}(影片 {video_name},字幕 {n} 句{burned_note})— 雙擊即審,免拖檔")
    if "--no-open" not in flags:
        from open_file import open_for_user   # 同資料夾的 helper
        open_for_user(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
