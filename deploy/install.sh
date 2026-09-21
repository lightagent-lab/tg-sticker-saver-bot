#!/usr/bin/env bash
# 一键部署到 systemd 系统。以 root 运行：bash deploy/install.sh
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/stickerbot}
SERVICE_USER=${SERVICE_USER:-stickerbot}
REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
PYTHON=${PYTHON:-python3}

if [ "$(id -u)" != "0" ]; then
  echo "请用 root 运行：sudo bash deploy/install.sh" >&2
  exit 1
fi

if [ ! -f "$REPO_DIR/.env" ]; then
  echo "缺少 $REPO_DIR/.env，请先复制 .env.example 并填写 BOT_TOKEN 与 ADMIN_ID。" >&2
  exit 1
fi

id "$SERVICE_USER" >/dev/null 2>&1 || \
  useradd --system --home-dir "$APP_DIR" --shell /sbin/nologin "$SERVICE_USER"

mkdir -p "$APP_DIR/bin" "$APP_DIR/data"
cp "$REPO_DIR/bot.py" "$REPO_DIR/requirements.txt" "$APP_DIR/"
cp "$REPO_DIR/.env" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

if [ ! -x "$APP_DIR/venv/bin/python" ]; then
  "$PYTHON" -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --upgrade pip >/dev/null
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

if [ ! -x "$APP_DIR/bin/ffmpeg" ]; then
  echo "下载静态 ffmpeg 到 $APP_DIR/bin ..."
  curl -fL --retry 3 -o /tmp/ffmpeg.tar.xz \
    https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
  tar -xJf /tmp/ffmpeg.tar.xz -C "$APP_DIR/bin" --strip-components=1
  rm -f /tmp/ffmpeg.tar.xz
fi

sed "s#__APP_DIR__#$APP_DIR#g; s#__SERVICE_USER__#$SERVICE_USER#g" \
  "$REPO_DIR/deploy/stickerbot.service" > /etc/systemd/system/stickerbot.service

chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
systemctl daemon-reload
systemctl enable --now stickerbot.service
systemctl --no-pager status stickerbot.service || true
echo "完成。日志：journalctl -u stickerbot -f"
