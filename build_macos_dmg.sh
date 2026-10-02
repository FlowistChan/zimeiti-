#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
python3 -m pip install --upgrade pyinstaller openai-whisper tkinterdnd2

FFMPEG_PATH="$(command -v ffmpeg || true)"
if [ -z "$FFMPEG_PATH" ]; then
  echo "请先安装 FFmpeg，例如：brew install ffmpeg" >&2
  exit 1
fi

python3 -m PyInstaller --noconfirm --clean --windowed \
  --name "视频首句重命名" \
  --collect-all tkinterdnd2 \
  --collect-all whisper \
  --add-binary "$FFMPEG_PATH:." \
  video_first_sentence.py

mkdir -p dmg_contents
cp -R "dist/视频首句重命名.app" dmg_contents/
ln -s /Applications dmg_contents/Applications
hdiutil create -volname "视频首句重命名" -srcfolder dmg_contents \
  -ov -format UDZO "视频首句重命名-macOS.dmg"
rm -rf dmg_contents
echo "已生成：$(pwd)/视频首句重命名-macOS.dmg"
