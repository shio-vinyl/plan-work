#!/usr/bin/env bash
# 录音转文字（中文为主），适用于 Apple Silicon 的 Mac。
#
# 用法：transcribe.sh 录音文件 输出目录 [提示词]
#   提示词：项目名、地名、专业词，帮助识别，例如 "城市更新单元，保障性租赁住房，容积率"。
#
# 输出：输出目录下 转写_带时间.txt（每句前带 分:秒），以及 srt/vtt/json 等原始格式。
# 第一次运行会建虚拟环境、下载约 1.6GB 模型，放在 ${PLAN_WORK_ASR:-~/.cache/plan-work/asr}。
#
# 两个已知的坑，脚本已处理：
# - huggingface 在国内常连不上或下到一半断掉：模型从 ModelScope 下载，支持断点续传。
# - Whisper 依赖上文时会陷入重复循环（整段只剩一个词）：关闭 condition_on_previous_text。
set -euo pipefail

[ $# -ge 2 ] || { sed -n '2,13p' "$0"; exit 1; }
AUDIO=$1; OUT=$2; HINT=${3:-}
ASR=${PLAN_WORK_ASR:-$HOME/.cache/plan-work/asr}
MODEL=$ASR/model
BASE=https://modelscope.cn/models/mlx-community/whisper-large-v3-turbo/resolve/master
WEIGHTS_SIZE=1613977612

[ "$(uname -sm)" = "Darwin arm64" ] || { echo "只支持 Apple Silicon 的 Mac（用的是 mlx-whisper）。其他机器可改用 faster-whisper，参数同样关闭依赖上文。"; exit 1; }
command -v ffmpeg >/dev/null || { echo "缺 ffmpeg：brew install ffmpeg"; exit 1; }

mkdir -p "$ASR" "$MODEL" "$OUT"

if [ ! -x "$ASR/.venv/bin/mlx_whisper" ]; then
  echo "建虚拟环境并安装 mlx-whisper……"
  if command -v uv >/dev/null; then
    uv venv -q -p 3.11 "$ASR/.venv" && uv pip install -q -p "$ASR/.venv" mlx-whisper
  else
    python3 -m venv "$ASR/.venv" && "$ASR/.venv/bin/pip" install -q mlx-whisper
  fi
fi

size() { stat -f%z "$1" 2>/dev/null || echo 0; }
[ -s "$MODEL/config.json" ] || curl -sSL -m 60 -o "$MODEL/config.json" "$BASE/config.json"
if [ "$(size "$MODEL/weights.safetensors")" != "$WEIGHTS_SIZE" ]; then
  echo "下载模型（约 1.6GB，可断点续传）……"
  curl -sSL -C - --retry 5 -o "$MODEL/weights.safetensors" "$BASE/weights.safetensors" || true
  [ "$(size "$MODEL/weights.safetensors")" = "$WEIGHTS_SIZE" ] || { echo "模型没下完整，重跑本脚本会接着下。"; exit 1; }
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
ffmpeg -y -loglevel error -i "$AUDIO" -ar 16000 -ac 1 "$TMP/audio.wav"

PROMPT="以下是普通话的句子。${HINT}"
"$ASR/.venv/bin/mlx_whisper" "$TMP/audio.wav" --model "$MODEL" --language zh \
  --condition-on-previous-text False --initial-prompt "$PROMPT" \
  --output-format all --output-dir "$OUT" >/dev/null

awk -F'\t' 'NR>1 && $3!="" {printf "%d:%02d %s\n", int($1/60000), int($1/1000)%60, $3}' "$OUT/audio.tsv" > "$OUT/转写_带时间.txt"
for f in "$OUT"/audio.*; do mv "$f" "$OUT/转写.${f##*.}"; done

# Whisper 常混出繁体字，统一转成简体
"$ASR/.venv/bin/python" -c "import opencc" 2>/dev/null || {
  if command -v uv >/dev/null; then uv pip install -q -p "$ASR/.venv" opencc-python-reimplemented
  else "$ASR/.venv/bin/pip" install -q opencc-python-reimplemented; fi; }
"$ASR/.venv/bin/python" - "$OUT" <<'PY'
import sys, pathlib, opencc
cc = opencc.OpenCC("t2s")
for p in pathlib.Path(sys.argv[1]).glob("转写*"):
    if p.suffix in (".txt", ".srt", ".vtt", ".tsv"):
        p.write_text(cc.convert(p.read_text(encoding="utf-8")), encoding="utf-8")
PY

LINES=$(wc -l < "$OUT/转写_带时间.txt" | tr -d ' ')
TOP=$(cut -d' ' -f2- "$OUT/转写_带时间.txt" | sort | uniq -c | sort -rn | head -1)
echo "完成：$OUT/转写_带时间.txt，$LINES 句。出现最多的一句：$TOP"
echo "某一句重复几十次以上，说明出现了重复循环，需要检查。转写未校对，地名和单位常有同音错字。"
