#!/usr/bin/env bash
# 清理工具 — 只刪一個專案「工作檔/」裡的大媒體檔,保留「配方」文字檔。
#
# 為什麼這樣設計:
#   真正佔空間的是 mp4/mov/png/wav/mp3 跟 node_modules;
#   但 edl.json、transcripts/、captions/*.json/*.html 這些文字檔加起來通常 < 1MB,
#   卻是整個專案的「配方」— 留著它們,隔天回來只要重跑 render 就好,
#   不用重轉逐字稿、重 init、重對時間軸(那些才是燒時間跟 token 的地方)。
#
# 用法: bash tools/cleanup.sh <專案資料夾>
set -u

PROJ="${1:-}"
if [ -z "$PROJ" ] || [ ! -d "$PROJ" ]; then
  echo "用法: bash tools/cleanup.sh <專案資料夾>"
  echo "(專案資料夾裡面應該有 原始影片、成品.mp4、工作檔/)"
  exit 1
fi

WORK="$PROJ/工作檔"
REVIEW="$PROJ/審片區"
if [ ! -d "$WORK" ] && [ ! -d "$REVIEW" ]; then
  echo "這個專案沒有「工作檔/」或「審片區/」資料夾,沒東西要清。"
  exit 0
fi
SCAN_DIRS=()
[ -d "$WORK" ] && SCAN_DIRS+=("$WORK")
# 專案已完成(根目錄有 成品.mp4)時,審片區整個保留 — 裡面的最終影片 + 字幕.json
# 是使用者之後重看成品的窗口(拖進 審片.html),刪了他就沒得重看(實測回報)。
# 專案還沒完成時,審片區裡只是舊 preview,照舊清掉。
if [ -f "$PROJ/成品.mp4" ]; then
  [ -d "$REVIEW" ] && echo "(審片區/ 保留不清 — 專案已完成,那是重看成品用的視窗)"
else
  [ -d "$REVIEW" ] && SCAN_DIRS+=("$REVIEW")
fi
# macOS 內建 bash 3.2 在 set -u 下展開空陣列會直接報錯,先擋掉「沒東西要掃」的情況
if [ "${#SCAN_DIRS[@]}" -eq 0 ]; then
  echo "沒有要掃的資料夾,不用清。"
  exit 0
fi

# 會刪的:大媒體檔(可重生)+ 已知的大型可重生資料夾
#   注意:captions/index.html、captions/*.json 這些「配方」不在刪除範圍。
MEDIA_EXT=(mp4 mov m4v webm mkv wav mp3 m4a aac png jpg jpeg gif)
BIG_DIRS=(node_modules clips_preview __pycache__ chk .hyperframes)

# ★ 合成用到的素材不能刪:圖片、b-roll 影片、去背切出來的人(fg*.webm)、音檔。
# 它們被 index.html 直接引用,而且**不是配方能重生的** — 刪掉之後重跑 render 不會報錯,
# 只是畫面上那個元素安靜地消失,學員根本不會發現。
# (學員回報兩次:b-roll 影片被刪,因為這裡原本只保護圖片。)
# 唯一例外是主畫面 a-roll(id="a-roll" / "a-roll-audio",以及 transitions.py 做轉場用的
# 主畫面副本 data-tx-aroll):它很大,而且重跑 render.py 就會再生。
MEDIA_REF='png|jpg|jpeg|gif|svg|webp|mp4|mov|m4v|webm|mkv|wav|mp3|m4a|aac'
KEEP_RE=""
for html in $(find "${SCAN_DIRS[@]}" -type f -name "*.html" 2>/dev/null); do
  # 抓 src="..." / href="..." / url(...) 裡的本機素材檔名;a-roll 那兩行(跟轉場的副本)跳過
  refs="$(grep -vE 'id="a-roll(-audio)?"|data-tx-aroll' "$html" 2>/dev/null \
          | grep -oE "(src|href)=\"[^\"]+\.($MEDIA_REF)\"|url\(([\"']?)[^)\"']+\.($MEDIA_REF)" \
          | grep -oE "[^/\"'(=]+\.($MEDIA_REF)" || true)"
  for r in $refs; do
    KEEP_NAMES="${KEEP_NAMES:-}${KEEP_NAMES:+ }$r"
  done
done
# 去重(同一張圖常被引用多次,例如字卡跟 sprite 都用 clawd.png)
KEEP_NAMES="$(printf '%s\n' ${KEEP_NAMES:-} | sort -u | tr '\n' ' ' | sed 's/ $//')"
for r in $KEEP_NAMES; do
  KEEP_RE="${KEEP_RE}${KEEP_RE:+|}$(printf '%s' "$r" | sed 's/[.[\*^$]/\\&/g')"
done
[ -n "$KEEP_RE" ] && echo "保留合成用到的素材(圖片、b-roll、去背檔;刪了 render 會靜默少東西): $KEEP_NAMES" && echo ""

echo "掃描: ${SCAN_DIRS[*]}"
echo ""

# 蒐集要刪的清單
TMP_LIST="$(mktemp)"
for dir in "${SCAN_DIRS[@]}"; do
  for ext in "${MEDIA_EXT[@]}"; do
    find "$dir" -type f -iname "*.${ext}" -print >> "$TMP_LIST" 2>/dev/null
  done
  for d in "${BIG_DIRS[@]}"; do
    find "$dir" -type d -name "$d" -print >> "$TMP_LIST" 2>/dev/null
  done
done

# 把「合成有引用到的素材」從刪除清單裡剔除
if [ -n "$KEEP_RE" ]; then
  KEPT="$(grep -cE "/($KEEP_RE)\$" "$TMP_LIST" 2>/dev/null || true)"
  grep -vE "/($KEEP_RE)\$" "$TMP_LIST" > "$TMP_LIST.f" 2>/dev/null && mv "$TMP_LIST.f" "$TMP_LIST"
  [ "${KEPT:-0}" -gt 0 ] && echo "(已從刪除清單剔除 ${KEPT} 個合成引用到的素材)"
fi

if [ ! -s "$TMP_LIST" ]; then
  echo "沒有找到可清理的媒體檔或大資料夾,工作檔已經很乾淨。"
  rm -f "$TMP_LIST"
  exit 0
fi

DEL_SIZE="$(du -sch $(cat "$TMP_LIST") 2>/dev/null | tail -1 | cut -f1)"
DEL_COUNT="$(wc -l < "$TMP_LIST" | tr -d ' ')"

echo "會刪掉(約 ${DEL_SIZE:-未知}, ${DEL_COUNT} 項)— 大媒體檔 + 可重生資料夾:"
sed 's#^#  - #' "$TMP_LIST" | head -20
[ "$DEL_COUNT" -gt 20 ] && echo "  … 還有 $((DEL_COUNT-20)) 項"
echo ""
echo "會保留(專案配方,重做只要重跑 render):"
echo "  - edl.json、transcripts/(逐字稿 + words.txt)"
echo "  - captions/index.html、captions/captions.json、fixes.json、build 設定"
echo "  - 專案筆記.md、任何 .md / .srt / .txt"
echo "  - 合成(index.html)引用到的素材(圖片、b-roll、去背檔)— 刪了 render 會靜默少東西"
echo "  - 原始影片、成品.mp4(在專案最上層,本來就不動)"
echo ""
printf "確定要刪嗎? 輸入 yes 確認: "
read -r ANS
if [ "$ANS" = "yes" ]; then
  while IFS= read -r p; do rm -rf "$p"; done < "$TMP_LIST"
  # hyperframes 的影格快取放在系統暫存區,不在專案裡 — 學員實測堆到 931MB
  # 沒人清(cleanup 只掃專案資料夾掃不到)。它是純快取,刪了頂多下次 render 慢一點。
  HF_CACHE_SIZE="$(du -sh "${TMPDIR:-/tmp}"/hyperframes-extract-cache* 2>/dev/null | awk '{s=$1} END {print s}')"
  if [ -n "${HF_CACHE_SIZE:-}" ]; then
    rm -rf "${TMPDIR:-/tmp}"/hyperframes-extract-cache* 2>/dev/null
    echo "(順手清掉 hyperframes 影格快取 ${HF_CACHE_SIZE} — 純快取,不影響任何專案)"
  fi
  echo "已清理。配方文字檔、原始影片、成品.mp4 都還在。"
  echo "下次回來:cd 進 captions/ 跑 npm install(如果刪了 node_modules)再 render 即可。"
else
  echo "取消,沒有刪任何東西。"
fi
rm -f "$TMP_LIST"
