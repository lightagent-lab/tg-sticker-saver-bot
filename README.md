# Telegram 表情包保存机器人 / Telegram Sticker Saver Bot

关键词：**Telegram 表情包保存、TG 贴纸下载、sticker to PNG、sticker to GIF、贴纸包批量打包、tgs 转 GIF、sticker saver bot**。

一个自托管的 Telegram 机器人：把贴纸**转图片 / 转 GIF** 发回给你，并支持**整套表情包批量打包 ZIP**。

**在线示例机器人（可以直接用，也可以照着部署）：[@yctybqb_bot](https://t.me/yctybqb_bot)**
维护者：[@Jaychou_8899](https://t.me/Jaychou_8899) —— 感谢他提供示例部署与反馈。

> 本项目是通用开源工具，与 Telegram 官方无关。示例机器人是社区自建实例，不是官方客户端功能。

## 功能

- **静态贴纸（WebP）→ PNG**：图片预览 + PNG 原文件。
- **动态贴纸（TGS / Lottie）→ GIF**：动画预览 + GIF 原文件。
- **视频贴纸（WebM）→ GIF**：ffmpeg 调色板转换。
- **Telegram 动图 / GIF → GIF 原文件**。
- **批量收图**：`/batch` 连发最多 50 个，`/done` 打包成 ZIP。
- **整套保存**：粘贴 `t.me/addstickers/...` 链接，自动分批转换与打包。
- **失败补发**：ZIP 发送失败时保留，`/retryfiles` 只补发，不重新转换。
- **任务进度**：`/status` 显示处理数、交付数、当前阶段与更新时间。
- **每日统计**：每天固定时间把「使用人数 / 保存数量 / 失败数量」私聊发给管理员。
- **多语言**：`BOT_LANG=zh` 或 `en`。

## 命令

| 命令 | 说明 |
| --- | --- |
| `/start` `/help` | 欢迎语与使用说明 |
| `/batch` | 开始批量收图（最多 50 个） |
| `/done` | 打包并发送收集到的贴纸 |
| `/pack` | 说明如何复制表情包链接 |
| `/status` | 查看自己的任务进度 |
| `/cancel` | 取消待打包列表与未完成任务 |
| `/clear` | 丢弃待收图与任务历史 |
| `/retryfiles` | 补发保留的失败 ZIP |
| `/stats` | 仅管理员：查看当日统计 |

也可以直接发贴纸、动图或表情包链接，不需要命令。

## 快速开始

### 1. 创建机器人

在 Telegram 找 [@BotFather](https://t.me/BotFather)：

```
/newbot
```

按提示填名称和用户名，拿到 `BOT_TOKEN`。

### 2. 拿到你的数字 ID

给 [@userinfobot](https://t.me/userinfobot) 发任意消息，它回复的数字就是 `ADMIN_ID`。

### 3. 配置

```bash
git clone https://github.com/USER/REPO.git stickerbot
cd stickerbot
cp .env.example .env
```

编辑 `.env`：

```ini
BOT_TOKEN=123456:ABC-DEF...
ADMIN_ID=你的数字ID
BOT_NAME=表情包保存bot
ADMIN_USERNAME=你的用户名
BOT_LANG=zh
REPORT_HOUR=0
REPORT_MINUTE=5
```

### 4. 一键部署（systemd）

```bash
sudo bash deploy/install.sh
```

脚本会创建低权限系统用户、虚拟环境、下载静态 ffmpeg，并安装启动 `stickerbot.service`。
默认目录 `/opt/stickerbot`，可用环境变量覆盖：

```bash
sudo APP_DIR=/opt/mybot SERVICE_USER=mybot bash deploy/install.sh
```

### 5. 手动运行

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
# 需要 ffmpeg：放到 bin/ffmpeg 或写进 PATH
set -a && . ./.env && set +a
venv/bin/python bot.py
```

Windows 下 `resource` 模块不可用，转换子进程的资源限制会自动跳过，其余功能一致。

## 配置项

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `BOT_TOKEN` | 必填 | BotFather 给的 Token |
| `ADMIN_ID` | 必填 | 管理员数字 ID，接收日报 |
| `BOT_NAME` | `表情包保存bot` | 显示名称 |
| `ADMIN_USERNAME` | 空 | 不带 `@`，展示在欢迎语与 ZIP 说明 |
| `BOT_LANG` | `zh` | `zh` 或 `en` |
| `REPORT_HOUR` / `REPORT_MINUTE` | `0` / `5` | 每日统计时间（服务器本地时间） |

## 资源占用与限制

- 公共队列逐张转换（约 5 秒间隔），管理员任务可并行。
- GIF 默认 **15 fps、最长边 512**；不静默截断原动图，过大直接报错。
- 单文件下载上限 **20 MB**，转换超时 150 秒，单次上传上限 45 MB。
- 转换子进程：**650 MB 地址空间、48 MB 输出**（Linux）。
- 服务：**850 MB 内存上限、单核 80% CPU、Nice 10**。
- 缓存（转换结果 + 原文件）上限 **1 GB**，超出按最久未用优先删除；临时目录小时级清理。
- 每用户默认最多 10 个排队任务。

实测：空闲常驻内存约 15 MB。

## 数据与隐私

- 唯一持久化数据是 SQLite（`data/bot.sqlite3`）与转换缓存。
- 统计中的「使用人数」是当天提交转换的去重账号数；不保存聊天内容。
- 缓存按内容哈希命名，不含用户身份；`rm -rf data/cache/*` 可随时清空。
- `.env` 已被 `.gitignore` 排除，**不要提交真实 Token**。

## 常见问题

**保存下来的动图变成视频？**
Telegram 客户端会把 `sendAnimation` 预览存成视频。要真正的 GIF，请下载文件附件，或下载随附 ZIP 后解压。

**TGS 转换失败？**
部分复杂特效超出 lottie 渲染器能力。机器人会明确报错并计入失败数，不会发坏文件。

**能保证与官方客户端渲染完全一致吗？**
不能，复杂特效可能有差异。

## 开发

```bash
python -m unittest test_bot -q     # 使用临时数据库，不影响业务数据
```

CI 在 Python 3.8 / 3.10 / 3.12 上跑同一套测试。

## 许可

MIT，见 [LICENSE](LICENSE)。
