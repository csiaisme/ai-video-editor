#!/usr/bin/env python3
"""幫使用者把檔案打開(或在 Finder / 檔案總管裡選起來)— 不要叫新手自己去找資料夾。

用法:
    python3 open_file.py <檔案>            用預設程式打開(審片頁 → 瀏覽器)
    python3 open_file.py <檔案> --reveal   打開所在資料夾,並把檔案選起來(交付成品用)

為什麼要有這支:學員回報 15 次「審片區在哪」「我找不到資料夾」「成品在哪」
(Windows 8 次、Mac 7 次;Windows 的工具包裝在 C:\\Users\\<帳號>\\ 深處)。
AI 直接幫他開的那幾次(Mac `open`、Windows `Start-Process`)全部一次解決 —
2026-08-04 / 08-29 / 09-08 三份復盤都建議寫死,這支就是那個寫死。

Windows 的成品不要用「預設程式打開」:舊版 Windows Media Player 開不了 mp4,
學員連續兩次卡在「我打不開」(2026-08-11)。成品一律 --reveal(選起來),
再把影片丟進對話。

開不了(例如 Codex 沙盒擋住)時不會報錯,會印出完整路徑,讓 AI 改成
「把這個路徑貼給使用者 + 請他雙擊」;或用沙盒外權限重跑這支。
"""
import os
import subprocess
import sys
from pathlib import Path


def open_for_user(path, reveal=False):
    """打開檔案(reveal=True 改成在資料夾裡選起來)。成功回 True,失敗回 False 並印出路徑。"""
    p = str(Path(path).resolve())
    ok = False
    try:
        if sys.platform == "darwin":
            cmd = ["open", "-R", p] if reveal else ["open", p]
            ok = subprocess.run(cmd, capture_output=True, timeout=20).returncode == 0
        elif os.name == "nt":
            if reveal:
                # explorer 成功時 exit code 也常是 1,不能拿 returncode 判斷
                subprocess.run(f'explorer /select,"{p}"', timeout=20)
            else:
                os.startfile(p)   # 等同 PowerShell 的 Start-Process
            ok = True
        else:
            target = str(Path(p).parent) if reveal else p
            ok = subprocess.run(["xdg-open", target], capture_output=True, timeout=20).returncode == 0
    except Exception:
        ok = False
    what = "在資料夾裡選起來" if reveal else "打開"
    if ok:
        print(f"[已幫使用者{what}] {p}")
    else:
        print(f"[沒能自動{what}] {p}\n"
              "  → 在沙盒裡跑的話用沙盒外權限重跑一次;還是不行,就把上面完整路徑貼給使用者請他雙擊,"
              "不要只講「到某某資料夾找」。")
    return ok


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    if not Path(args[0]).exists():
        print(f"[X] 找不到 {args[0]}")
        return 1
    open_for_user(args[0], reveal="--reveal" in sys.argv[1:])
    return 0   # 開不了也不算失敗:路徑已印出,流程照走


if __name__ == "__main__":
    raise SystemExit(main())
