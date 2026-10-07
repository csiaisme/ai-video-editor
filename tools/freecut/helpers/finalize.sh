#!/usr/bin/env bash
# finalize.sh — mix BGM under a rendered video and produce the final deliverable.
#
# The last pipeline step, after hyperframes render. Video stream is COPIED
# (no re-encode, ~1s), so run this freely after every re-render.
#
# What it does:
#   - loops the BGM to cover the full video (no dead silence anywhere)
#   - fades BGM in (1.0s) and out (1.5s), volume default 0.18 (under the voice)
#   - mixes voice + BGM, limits peaks at 0.97 to prevent clipping
#
# Usage:
#   bash finalize.sh <video.mp4> <bgm.mp3> <output.mp4> [bgm_volume]
#
#   bgm_volume: 0.15-0.25 sits under speech (default 0.18)
#
# Remember: re-run this after EVERY re-render — the mix lives only in the
# output file, not in the hyperframes project.
set -euo pipefail

if [ $# -lt 3 ]; then
  grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -18
  exit 1
fi

VIDEO="$1"; BGM="$2"; OUT="$3"; VOL="${4:-0.18}"

# Windows(Git Bash):ffmpeg/ffprobe 是原生 exe,MSYS 路徑要先轉;有 cygpath 才轉,
# Mac/Linux 原樣通過。
if command -v cygpath >/dev/null 2>&1; then
  VIDEO="$(cygpath -m "$VIDEO")"; BGM="$(cygpath -m "$BGM")"; OUT="$(cygpath -m "$OUT")"
fi

DUR=$(ffprobe -v error -show_entries format=duration -of default=nokey=1:noprint_wrappers=1 "$VIDEO")
# 算 fade-out 起點用 awk,不用 python3 — 全新 Windows 的 python3 是 Microsoft Store
# 的 0-byte 空殼,執行失敗配上 set -e 整支中斷(學員實測)。awk 到處都有。
FADE_OUT_START=$(awk -v d="$DUR" 'BEGIN { s = d - 1.6; if (s < 0) s = 0; print s }')

# hyperframes render 出來的人聲比 preview 小一截(0.8.137 實測整支固定 -1.8dB)→ 量一次補回 -14 LUFS。
# 只補 -3~+6dB,超出就不動(代表素材本身有問題)。邏輯同 sfx_cues.py 的 voice_gain。
VOICE_I=$(ffmpeg -hide_banner -i "$VIDEO" -vn -af ebur128 -f null - 2>&1 | awk '/I:/ && /LUFS/ {v=$2} END {print v}')
VG=$(awk -v i="$VOICE_I" 'BEGIN { if (i == "" || i < -70) { print 0; exit } g = -14 - i; if (g < -3 || g > 6 || (g < 0.3 && g > -0.3)) g = 0; printf "%.1f", g }')
echo "人聲 ${VOICE_I:-?} LUFS → 補 ${VG}dB"

ffmpeg -y -i "$VIDEO" -stream_loop -1 -i "$BGM" -filter_complex "
[1:a]atrim=0:${DUR},afade=t=in:st=0:d=1.0,afade=t=out:st=${FADE_OUT_START}:d=1.5,volume=${VOL}[bgm];
[0:a]volume=${VG}dB[v0];[v0][bgm]amix=inputs=2:normalize=0[mix];[mix]alimiter=limit=0.87:level=disabled:latency=1,volume=0.96[aout]
" -map 0:v -map "[aout]" -c:v copy -c:a aac -b:a 192k "$OUT"

echo ""
echo "done: $OUT ($(ffprobe -v error -show_entries format=duration -of default=nokey=1:noprint_wrappers=1 "$OUT")s, bgm vol ${VOL})"
