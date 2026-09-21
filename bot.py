#!/usr/bin/env python3
"""Telegram 表情包保存机器人 / Telegram sticker saver bot.

静态贴纸转 PNG，TGS / WebM / GIF 动态贴纸转 GIF，支持批量打包 ZIP。
Python 3.6+，只依赖 Telegram Bot API。
"""
import datetime as dt
import gzip
import hashlib
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CACHE = DATA / "cache"
TEMP = DATA / "tmp"
DB = DATA / "bot.sqlite3"
for directory in (DATA, CACHE, TEMP):
    directory.mkdir(parents=True, exist_ok=True)
ADMIN = int(os.environ.get("ADMIN_ID", "0"))
TOKEN = os.environ.get("BOT_TOKEN", "")
BOT_NAME = os.environ.get("BOT_NAME", "表情包保存bot")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "").lstrip("@")
BOT_LANG = os.environ.get("BOT_LANG", "zh").lower()
# 可选开场白，留空则不显示。
BOT_TAGLINE = os.environ.get("BOT_TAGLINE", "又来收藏表情包啦？真拿你没办法喵。")
API = "https://api.telegram.org/bot" + TOKEN + "/"
FFMPEG = str(ROOT / "bin" / "ffmpeg")
if not os.path.exists(FFMPEG):
    # Fall back to PATH so the bot also works without the vendored binary.
    FFMPEG = shutil.which("ffmpeg") or FFMPEG
MAX_ITEMS = 50
MAX_DOWNLOAD = 20 * 1024 * 1024
MAX_UPLOAD = 45 * 1024 * 1024
ZIP_TARGET = 2 * 1024 * 1024
UPLOAD_LOCK = threading.Lock()
OUTBOX_LOCK = threading.Lock()
OUTBOX = DATA / "outbox"
OUTBOX.mkdir(parents=True, exist_ok=True)
REPORT_HOUR = int(os.environ.get("REPORT_HOUR", "0"))
REPORT_MINUTE = int(os.environ.get("REPORT_MINUTE", "5"))
NETWORK_ATTEMPTS = 4
JOB_CONTEXT = threading.local()
PUBLIC_NEXT_START = 0.0
SCHEDULER_LOCK = threading.Lock()
ACTIVE_JOBS = {}
KEYBOARD = {
    "keyboard": [
        ["📖 怎么用喵", "📦 批量收图", "🗂 整套搬走"],
        ["⏳ 排到我没", "✅ 打包带走", "❌ 不弄了喵"],
        ["🗑 清空列表"],
    ],
    "resize_keyboard": True,
}
TEXTS = {
    "zh": {
        "welcome": (
            "你好喵，我是{BOT_NAME} ✨\n\n"
            "{TAGLINE}{TAGLINE_BREAK}"
            "🖼 发静态表情：图片预览 + PNG 文件。\n"
            "🎞 发动态表情、视频贴纸或 GIF：动画预览 + GIF 文件。\n"
            "📦 点「批量收图」，连续发最多 50 个，最后点「打包带走」。\n"
            "🗑 点「清空列表」丢弃待收图和任务历史，重新开始。\n"
            "🗂 打开表情包详情 → 分享或复制链接 → 把完整链接发给我，自动分批打包。\n"
            "💾 保存 GIF 请下载文件附件；若预览保存成视频，请下载附带 ZIP 解压。\n"
            "⏳ 普通队列逐张处理，开始间隔至少 5 秒。\n"
            "\n{ADMIN_LINE}"
        ),
        "admin_line": "管理员：@{ADMIN_USERNAME}\n非官方粉丝自制机器人",
        "report_title": "{BOT_NAME} 的每日小账本喵 📊",
    },
    "en": {
        "welcome": (
            "Hi, I am {BOT_NAME} ✨\n\n"
            "{TAGLINE}{TAGLINE_BREAK}"
            "🖼 Static sticker -> PNG preview + PNG file.\n"
            "🎞 Animated sticker / video sticker / GIF -> GIF preview + GIF file.\n"
            "📦 Tap “Batch”, send up to 50 stickers, then tap “Pack & send”.\n"
            "🗑 Tap “Clear list” to drop your pending list and job history.\n"
            "🗂 Open a sticker set, share it, paste the full link here to save the whole set.\n"
            "💾 To keep a real GIF, download the file attachment; if a preview saves as "
            "video, download the attached ZIP instead.\n"
            "⏳ The public queue converts one item at a time.\n"
            "\n{ADMIN_LINE}"
        ),
        "admin_line": "Admin: @{ADMIN_USERNAME}\nUnofficial fan-made bot",
        "report_title": "{BOT_NAME} daily report",
    },
}


def text(key):
    block = TEXTS.get(BOT_LANG, TEXTS["zh"])
    tagline = BOT_TAGLINE.strip()
    return block[key].format(
        BOT_NAME=BOT_NAME,
        TAGLINE=tagline,
        TAGLINE_BREAK="\n\n" if tagline else "",
        ADMIN_USERNAME=ADMIN_USERNAME or "ADMIN",
        ADMIN_LINE=block["admin_line"].format(
            ADMIN_USERNAME=ADMIN_USERNAME or "ADMIN"))


WELCOME = text("welcome")
ALIASES = dict(zip(
    ["📖 怎么用喵", "📦 批量收图", "🗂 整套搬走",
     "⏳ 排到我没", "✅ 打包带走", "❌ 不弄了喵", "🗑 清空列表"],
    ["/help", "/batch", "/pack", "/status", "/done", "/cancel", "/clear"],
))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("sticker")


def now():
    return dt.datetime.utcnow() + dt.timedelta(hours=8)


def sql(query, args=(), one=False):
    with closing(sqlite3.connect(str(DB), timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(query, args)
        rows = cur.fetchall()
        conn.commit()
        return (dict(rows[0]) if rows else None) if one else [dict(r) for r in rows]


def init_db():
    with closing(sqlite3.connect(str(DB), timeout=30)) as conn:
        conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,first_day TEXT);
        CREATE TABLE IF NOT EXISTS activity(day TEXT,uid INTEGER,PRIMARY KEY(day,uid));
        CREATE TABLE IF NOT EXISTS batches(uid INTEGER PRIMARY KEY,items TEXT);
        CREATE TABLE IF NOT EXISTS jobs(
          id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER,payload TEXT,
          state TEXT DEFAULT 'queued',created REAL);
        CREATE TABLE IF NOT EXISTS deliveries(day TEXT,uid INTEGER,count INTEGER,failed INTEGER);
        CREATE TABLE IF NOT EXISTS reports(day TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS progress(
          job_id INTEGER PRIMARY KEY,total INTEGER DEFAULT 0,processed INTEGER DEFAULT 0,
          delivered INTEGER DEFAULT 0,phase TEXT DEFAULT '',updated REAL);
        CREATE TABLE IF NOT EXISTS outbox(
          path TEXT PRIMARY KEY,uid INTEGER,job_id INTEGER,count INTEGER,created REAL);
        """)
    sql("UPDATE jobs SET state='queued' WHERE state='running'")
    sql("INSERT OR IGNORE INTO settings VALUES('installed_day',?)", (now().date().isoformat(),))


def setting(key, value=None):
    if value is not None:
        sql("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, str(value)))
    row = sql("SELECT value FROM settings WHERE key=?", (key,), True)
    return row["value"] if row else None


class TelegramError(Exception):
    pass


def api(method, files=None, **data):
    import requests
    offsets = {key: value[1].tell() for key, value in (files or {}).items()}
    for attempt in range(NETWORK_ATTEMPTS):
        for key, offset in offsets.items():
            files[key][1].seek(offset)
        delay = min(2 ** (attempt + 1), 20)
        try:
            response = requests.post(API + method, data=data, files=files,
                                     timeout=(120, 300) if files else (20, 100))
            try:
                result = response.json()
            except ValueError:
                if response.status_code < 500:
                    raise TelegramError("invalid API response")
                result = {"ok": False, "error_code": response.status_code}
            finally:
                response.close()
        except (requests.ConnectionError, requests.Timeout,
                requests.exceptions.ChunkedEncodingError) as exc:
            detail = str(exc).replace(TOKEN, "[TOKEN]") if TOKEN else str(exc)
            LOG.warning("API failure method=%s attempt=%s error=%s detail=%s",
                        method, attempt + 1, type(exc).__name__, detail[:800])
            if attempt == NETWORK_ATTEMPTS - 1:
                raise
            network_wait(delay)
            continue
        if result.get("ok"):
            return result["result"]
        code = result.get("error_code", response.status_code)
        if (code == 429 or code >= 500) and attempt < NETWORK_ATTEMPTS - 1:
            delay = max(delay, result.get("parameters", {}).get("retry_after", 0))
            network_wait(delay)
            continue
        raise TelegramError(str(result.get("error_code")) + " " +
                            result.get("description", "Telegram error"))
    raise TelegramError("rate limited")


def network_wait(seconds):
    """Cancellation remains responsive between network attempts."""
    job = getattr(JOB_CONTEXT, "job", None)
    if job:
        progress(job, phase="网络重试，{}秒后再试".format(seconds))
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if job and cancelled(job):
            raise TelegramError("job cancelled")
        time.sleep(min(1, max(0, end - time.monotonic())))


def progress(job, **values):
    sql("INSERT OR IGNORE INTO progress(job_id,updated) VALUES(?,?)",
        (job["id"], time.time()))
    allowed = {"total", "processed", "delivered", "phase"}
    values = {k: v for k, v in values.items() if k in allowed}
    values["updated"] = time.time()
    sql("UPDATE progress SET " + ",".join(k + "=?" for k in values) +
        " WHERE job_id=?", tuple(values.values()) + (job["id"],))


def status_text(uid):
    rows = sql("""SELECT j.id,j.state,p.total,p.processed,p.delivered,p.phase,p.updated
                  FROM jobs j LEFT JOIN progress p ON p.job_id=j.id
                  WHERE j.uid=? AND j.state IN ('queued','running') ORDER BY j.id""", (uid,))
    lines = []
    for r in rows:
        if r["state"] == "queued":
            lines.append("#{} 排队中".format(r["id"]))
        else:
            age = int(max(0, time.time() - (r["updated"] or time.time())))
            lines.append("#{} 处理中：{}；已处理 {}/{}，已交付 {} 个；{}秒前更新".format(
                r["id"], r["phase"] or "正在准备", r["processed"] or 0,
                r["total"] or "待获取", r["delivered"] or 0, age))
    return "\n".join(lines) or "现在没有排队任务喵。"


def send(uid, text, keyboard=False):
    kwargs = {"chat_id": uid, "text": text}
    if keyboard:
        kwargs["reply_markup"] = json.dumps(KEYBOARD, ensure_ascii=False)
    return api("sendMessage", **kwargs)


def upload(uid, path, preview=False, caption=""):
    job = getattr(JOB_CONTEXT, "job", None)
    if job:
        progress(job, phase="等待上传通道：{}".format(path.name))
    while not UPLOAD_LOCK.acquire(timeout=1):
        if job and cancelled(job):
            raise TelegramError("job cancelled")
    try:
        if job and cancelled(job):
            raise TelegramError("job cancelled")
        if job:
            progress(job, phase="正在上传：{}（{:.1f} MB）".format(
                path.name, path.stat().st_size / 1024 ** 2))
        return upload_unlocked(uid, path, preview, caption)
    finally:
        UPLOAD_LOCK.release()


def upload_unlocked(uid, path, preview=False, caption=""):
    kind = "document"
    method = "sendDocument"
    if preview:
        kind = "photo" if path.suffix == ".png" else "animation"
        method = "sendPhoto" if kind == "photo" else "sendAnimation"
    with path.open("rb") as handle:
        kwargs = {"chat_id": uid, "caption": caption}
        if not preview:
            kwargs["disable_content_type_detection"] = "true"
        mime = "application/octet-stream" if not preview else (
            "image/png" if path.suffix == ".png" else "image/gif")
        return api(method, files={kind: (path.name, handle, mime)}, **kwargs)


def retain_archive(job, path, count):
    """Bound retained failed ZIPs; never silently discard one to admit another."""
    with OUTBOX_LOCK:
        used = sum(p.stat().st_size for p in OUTBOX.iterdir() if p.is_file())
        if used + path.stat().st_size > 1024 ** 3:
            raise ValueError("failed ZIP storage full (1GB)")
        target = OUTBOX / path.name
        shutil.copyfile(str(path), str(target))
        sql("INSERT OR REPLACE INTO outbox VALUES(?,?,?,?,?)",
            (str(target), job["uid"], job["id"], count, time.time()))


def retry_archives(job):
    # Serialize retry sweeps so repeated /retryfiles does not send the same ZIP twice.
    with OUTBOX_LOCK:
        rows = sql("SELECT * FROM outbox WHERE uid=? ORDER BY created", (job["uid"],))
        progress(job, total=sum(r["count"] for r in rows), processed=0, delivered=0)
        delivered = 0
        for row in rows:
            if cancelled(job):
                return
            path = Path(row["path"])
            upload(job["uid"], path, caption="补发任务 #{} 的 ZIP，{} 个表情。".format(
                row["job_id"], row["count"]))
            # Bookkeeping and removing the pending record form one transaction.
            with closing(sqlite3.connect(str(DB), timeout=30)) as conn:
                with conn:
                    conn.execute("INSERT INTO deliveries VALUES(?,?,?,0)",
                                 (now().date().isoformat(), job["uid"], row["count"]))
                    conn.execute("DELETE FROM outbox WHERE path=?", (str(path),))
                    conn.execute("UPDATE progress SET delivered=delivered+? WHERE job_id=?",
                                 (row["count"], row["job_id"]))
            path.unlink()
            delivered += row["count"]
            progress(job, processed=delivered, delivered=delivered)
        progress(job, phase="补发完成")
        try:
            send(job["uid"], "补发完成喵，交付 {} 个表情。".format(delivered))
        except Exception:
            pass


def describe(sticker):
    return {
        "file_id": sticker["file_id"],
        "unique": sticker["file_unique_id"],
        "kind": "tgs" if sticker.get("is_animated") else (
            "webm" if sticker.get("is_video") else "webp"),
    }


def record(uid, count, failed=0):
    sql("INSERT INTO deliveries VALUES(?,?,?,?)",
        (now().date().isoformat(), uid, count, failed))


def enqueue(uid, payload):
    active = sql("SELECT COUNT(*) n FROM jobs WHERE uid=? AND state IN ('queued','running')",
                 (uid,), True)["n"]
    if uid != ADMIN and active >= 10:
        send(uid, "你的待处理任务有点多喵，先等前面的做完再发。")
        return False
    sql("INSERT INTO jobs(uid,payload,created) VALUES(?,?,?)",
        (uid, json.dumps(payload), time.time()))
    sql("INSERT OR IGNORE INTO activity VALUES(?,?)", (now().date().isoformat(), uid))
    count = sql("SELECT COUNT(*) n FROM jobs WHERE state IN ('queued','running')",
                one=True)["n"]
    send(uid, "收到啦，已加入队列喵！当前待完成任务：{} 个。".format(count))
    return True


def handle(message):
    if message.get("chat", {}).get("type") != "private":
        return
    uid = message["chat"]["id"]
    sql("INSERT OR IGNORE INTO users VALUES(?,?)", (uid, now().date().isoformat()))
    text = ALIASES.get(message.get("text", ""), message.get("text", "")).strip()
    command = text.split("@")[0] if text.startswith("/") else text
    if command in ("/start", "/help"):
        send(uid, WELCOME, True)
    elif command == "/batch":
        if sql("SELECT uid FROM batches WHERE uid=?", (uid,), True):
            send(uid, "已经在收图啦，发完点「打包带走」喵。")
        else:
            sql("INSERT INTO batches VALUES(?,?)", (uid, "[]"))
            send(uid, "把想存的表情发过来喵！最多 50 个，发完点「打包带走」。", True)
    elif command == "/done":
        batch = sql("SELECT items FROM batches WHERE uid=?", (uid,), True)
        if not batch or not json.loads(batch["items"]):
            send(uid, "「打包带走」只提交手动收集的表情。\n"
                 "整套链接会自动转换、自动发送 ZIP，不需要点这个按钮。\n" + status_text(uid))
        elif enqueue(uid, {"items": json.loads(batch["items"]), "batch": True}):
            sql("DELETE FROM batches WHERE uid=?", (uid,))
    elif command == "/retryfiles":
        if sql("SELECT path FROM outbox WHERE uid=? LIMIT 1", (uid,), True):
            enqueue(uid, {"retry_outbox": True})
        else:
            send(uid, "没有保留的失败 ZIP 喵。此功能只能补发本次更新后保留的包。")
    elif command == "/pack":
        send(uid, "打开想保存的表情包详情，选择「分享」或「复制链接」，"
             "再把完整链接粘贴发给我喵。不是在这里点击一个示例链接哦。")
    elif command == "/status":
        batch = sql("SELECT items FROM batches WHERE uid=?", (uid,), True)
        msg = status_text(uid)
        pending = sql("SELECT COUNT(*) n FROM outbox WHERE uid=?", (uid,), True)["n"]
        if pending:
            msg += "\n保留了 {} 个待补发 ZIP，发送 /retryfiles 只补发这些包。".format(pending)
        if batch and json.loads(batch["items"]):
            msg += "\n手动待收集：{} / 50（不是整套下载进度）".format(len(json.loads(batch["items"])))
        send(uid, msg)
    elif command == "/clear":
        # Atomic, user-scoped removal; an absent job is treated as cancelled.
        with closing(sqlite3.connect(str(DB), timeout=30)) as conn:
            with conn:
                conn.execute("DELETE FROM batches WHERE uid=?", (uid,))
                conn.execute("DELETE FROM progress WHERE job_id IN (SELECT id FROM jobs WHERE uid=?)", (uid,))
                conn.execute("DELETE FROM jobs WHERE uid=?", (uid,))
        send(uid, "清空啦喵！待打包的表情和你的任务历史都已丢弃，可以重新收图啦。\n"
             "已提交发送的附件可能仍会到达；聊天里的旧消息、已下载文件和统计不会删除。",
             True)
    elif command == "/cancel":
        sql("DELETE FROM batches WHERE uid=?", (uid,))
        sql("UPDATE jobs SET state='cancelled' WHERE uid=? AND state IN ('queued','running')",
            (uid,))
        send(uid, "好吧，不弄了喵。正在转换的单个文件结束后就停，不再发后续文件。")
    elif command == "/stats":
        if uid == ADMIN:
            send(uid, report_text(now().date().isoformat()))
        else:
            send(uid, "统计只给管理员看喵。")
    else:
        match = re.fullmatch(r"(?:https?://)?(?:t\.me|telegram\.me)/addstickers/([A-Za-z0-9_]+)(?:\?.*)?", text)
        if match:
            enqueue(uid, {"pack": match.group(1), "batch": True})
            return
        if message.get("sticker"):
            item = describe(message["sticker"])
        elif message.get("animation"):
            animation = message["animation"]
            item = {"file_id": animation["file_id"], "unique": animation["file_unique_id"],
                    "kind": "gif" if animation.get("mime_type") == "image/gif" else "mp4"}
        else:
            send(uid, "发一个表情或表情包链接给我嘛，点「怎么用喵」查看说明。")
            return
        batch = sql("SELECT items FROM batches WHERE uid=?", (uid,), True)
        if batch:
            items = json.loads(batch["items"])
            if len(items) >= MAX_ITEMS:
                send(uid, "这批已经 50 个啦，先点「打包带走」喵。")
                return
            items.append(item)
            sql("UPDATE batches SET items=? WHERE uid=?", (json.dumps(items), uid))
            sql("INSERT OR IGNORE INTO activity VALUES(?,?)", (now().date().isoformat(), uid))
            send(uid, "收好第 {} 个啦，发完点「打包带走」喵。".format(len(items)))
        else:
            enqueue(uid, {"items": [item], "batch": False})


def convert_file(src, out, kind):
    from PIL import Image
    if kind == "webp":
        with Image.open(str(src)) as image:
            image.convert("RGBA").save(str(out), "PNG")
    elif kind == "tgs":
        # lottie uses variadic math.hypot, introduced in Python 3.8.
        # Keep the server's existing Python installation untouched.
        if sys.version_info < (3, 8):
            math.hypot = lambda *values: math.sqrt(sum(v * v for v in values))
        from lottie.parsers.tgs import parse_tgs
        from lottie.exporters.gif import export_gif
        with gzip.open(str(src), "rb") as handle:
            raw = handle.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("TGS too large")
        obj = json.loads(raw)
        fps = float(obj.get("fr", 60))
        if fps <= 0 or float(obj.get("op", 0)) - float(obj.get("ip", 0)) > fps * 10:
            raise ValueError("TGS duration")
        if not 0 < obj.get("w", 0) <= 512 or not 0 < obj.get("h", 0) <= 512:
            raise ValueError("TGS dimensions")
        if any(asset.get("p") or asset.get("u") for asset in obj.get("assets", [])):
            raise ValueError("External assets not supported")
        animation = parse_tgs(str(src))
        export_gif(animation, str(out), skip_frames=max(1, int(math.ceil(fps / 15))))
    else:
        subprocess.run([
            FFMPEG, "-nostdin", "-v", "error", "-y", "-threads", "1",
            "-filter_complex_threads", "1", "-protocol_whitelist", "file,pipe",
            "-i", str(src),
            "-filter_complex",
            "[0:v]fps=15,scale=512:512:force_original_aspect_ratio=decrease,"
            "split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer",
            "-loop", "0", str(out),
        ], check=True, timeout=100)
    if not out.exists() or out.stat().st_size > MAX_UPLOAD:
        raise ValueError("output too large")
    with Image.open(str(out)) as image:
        image.verify()


def converted(item):
    import requests
    key = hashlib.sha256(("v1:" + item["unique"]).encode()).hexdigest()
    ext = ".png" if item["kind"] == "webp" else ".gif"
    output = CACHE / (key + ext)
    if output.exists():
        os.utime(str(output), None)
        return output
    info = api("getFile", file_id=item["file_id"])
    if info.get("file_size", 0) > MAX_DOWNLOAD:
        raise ValueError("file exceeds download limit")
    with tempfile.TemporaryDirectory(dir=str(TEMP)) as directory:
        source = Path(directory) / ("source." + item["kind"])
        url = "https://api.telegram.org/file/bot" + TOKEN + "/" + info["file_path"]
        for attempt in range(NETWORK_ATTEMPTS):
            try:
                with requests.get(url, stream=True, timeout=(20, 90)) as response:
                    response.raise_for_status()
                    size = 0
                    with source.open("wb") as handle:
                        for block in response.iter_content(65536):
                            size += len(block)
                            if size > MAX_DOWNLOAD:
                                raise ValueError("download limit")
                            handle.write(block)
                    if info.get("file_size") and size != info["file_size"]:
                        raise requests.ConnectionError("incomplete download")
                break
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError, requests.HTTPError) as exc:
                if isinstance(exc, requests.HTTPError) and (
                        exc.response is None or exc.response.status_code < 500):
                    raise
                if attempt == NETWORK_ATTEMPTS - 1:
                    raise
                network_wait(2 ** (attempt + 1))
        target = Path(directory) / ("result" + ext)
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--convert",
             str(source), str(target), item["kind"]],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=150,
        )
        if proc.returncode:
            LOG.warning("conversion failed kind=%s", item["kind"])
            raise ValueError("conversion failed")
        shutil.copyfile(str(source), str(CACHE / (key + ".original." + item["kind"])))
        os.replace(str(target), str(output))
    return output


def cancelled(job):
    row = sql("SELECT state FROM jobs WHERE id=?", (job["id"],), True)
    return not row or row["state"] == "cancelled"


def process(job):
    uid = job["uid"]
    payload = json.loads(job["payload"])
    if payload.get("retry_outbox"):
        return retry_archives(job)
    progress(job, phase="获取表情列表")
    if payload.get("pack"):
        pack = api("getStickerSet", name=payload["pack"])
        items = [describe(s) for s in pack["stickers"]]
        send(uid, "这套有 {} 个表情，按每批最多 50 个打包喵。".format(len(items)))
    else:
        items = payload["items"]
    if uid != ADMIN and len(items) > 250:
        raise ValueError("pack too large")
    resume_from = int(payload.get("resume_from", 0))
    if resume_from:
        items = items[resume_from:]
        progress(job, phase="断点恢复：跳过已交付 {} 个".format(resume_from))
    failures = 0
    delivered = 0
    pending = 0
    progress(job, total=len(items), processed=0, delivered=0, phase="准备转换")
    for start in range(0, len(items), MAX_ITEMS):
        if cancelled(job):
            return
        files = []
        errors = []
        group = items[start:start + MAX_ITEMS]
        for index, item in enumerate(group, start + resume_from + 1):
            if cancelled(job):
                return
            try:
                wait_public_slot(job)
                progress(job, phase="下载/转换第 {} 个".format(index))
                path = converted(item)
                if cancelled(job):
                    return
                if not payload.get("batch"):
                    try:
                        upload(uid, path, True, "好啦！这是预览喵 ✨\n"
                               "保存原文件请下载下方附件，GIF 也可从 ZIP 解压。")
                    except Exception:
                        send(uid, "预览没发成功喵，原文件照常给你。")
                    upload(uid, path, caption="原文件收好喵！")
                    if path.suffix == ".gif":
                        with tempfile.TemporaryDirectory(dir=str(TEMP)) as directory:
                            archive = Path(directory) / "gif_original.zip"
                            with zipfile.ZipFile(str(archive), "w", zipfile.ZIP_STORED) as z:
                                z.write(str(path), "sticker.gif")
                            upload(uid, archive, caption="真正的 GIF 原图喵：下载 ZIP 并解压，"
                                   "里面是 sticker.gif，不是视频。")
                    record(uid, 1)
                    delivered += 1
                else:
                    files.append((index, path))
            except Exception as exc:
                failures += 1
                record(uid, 0, 1)
                errors.append("{}: {}".format(index, type(exc).__name__))
                LOG.warning("job=%s item=%s failed=%s", job["id"], index, type(exc).__name__)
            progress(job, processed=index, delivered=delivered)
        if payload.get("batch"):
            # Deliver originals first. An optional preview must not block ZIP delivery.
            parts = []
            current = []
            total = 0
            for entry in files:
                size = entry[1].stat().st_size
                if current and total + size > ZIP_TARGET:
                    parts.append(current)
                    current, total = [], 0
                current.append(entry)
                total += size
            if current or errors:
                parts.append(current)
            with tempfile.TemporaryDirectory(dir=str(TEMP)) as directory:
                for number, entries in enumerate(parts, 1):
                    if cancelled(job):
                        return
                    archive = Path(directory) / ("stickers_{}_{}_{}.zip".format(
                        job["id"], start // MAX_ITEMS + 1, number))
                    with zipfile.ZipFile(str(archive), "w", zipfile.ZIP_STORED) as z:
                        for index, path in entries:
                            z.write(str(path), "{:03d}{}".format(index, path.suffix))
                        if errors:
                            z.writestr("failed.txt", "\n".join(errors))
                        z.writestr("README.txt", "{}\n{}\n".format(
                            BOT_NAME, ("@" + ADMIN_USERNAME) if ADMIN_USERNAME else ""))
                    progress(job, phase="上传第 {} 批第 {} 包（{:.1f} MB）".format(
                        start // MAX_ITEMS + 1, number, archive.stat().st_size / 1024 ** 2))
                    try:
                        upload(uid, archive, caption="任务 #{}：第 {} 批第 {} 包，{} 个表情。".format(
                            job["id"], start // MAX_ITEMS + 1, number, len(entries)))
                    except Exception:
                        if cancelled(job):
                            return
                        retain_archive(job, archive, len(entries))
                        pending += len(entries)
                        continue
                    record(uid, len(entries))
                    delivered += len(entries)
                    progress(job, delivered=delivered)
            if files and not pending and not cancelled(job):
                try:
                    progress(job, phase="ZIP 已交付，发送可选预览")
                    upload(uid, files[0][1], True, "这批预览喵，原文件 ZIP 已在上方发送。")
                except Exception:
                    pass
    progress(job, phase="有 ZIP 待补发" if pending else "完成", delivered=delivered)
    if failures or payload.get("batch"):
        try:
            send(uid, "任务 #{}：已交付 {} 个，处理失败 {} 个，待补发 {} 个。{}".format(
                job["id"], delivered, failures, pending,
                "\n失败 ZIP 已保留，发送 /retryfiles 补发，不用重新转换。" if pending else ""))
        except Exception:
            LOG.warning("job=%s completion notification failed", job["id"])


def cleanup():
    files = [p for p in CACHE.iterdir() if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    for path in sorted(files, key=lambda p: p.stat().st_mtime):
        if total <= 1024 ** 3:
            break
        total -= path.stat().st_size
        path.unlink()
    for path in TEMP.iterdir():
        if time.time() - path.stat().st_mtime > 3600:
            if path.is_dir():
                shutil.rmtree(str(path))
            else:
                path.unlink()


def wait_public_slot(job):
    """Public jobs share one worker; starts are at least five seconds apart."""
    global PUBLIC_NEXT_START
    if job["uid"] == ADMIN:
        return
    while time.monotonic() < PUBLIC_NEXT_START:
        if cancelled(job):
            raise TelegramError("job cancelled")
        progress(job, phase="普通队列间隔等待（至少5秒）")
        time.sleep(max(0, min(1, PUBLIC_NEXT_START - time.monotonic())))
    PUBLIC_NEXT_START = time.monotonic() + 5


def run_job(job):
    try:
        JOB_CONTEXT.job = job
        if not cancelled(job):
            process(job)
            sql("UPDATE jobs SET state='done' WHERE id=? AND state='running'", (job["id"],))
    except Exception as exc:
        LOG.warning("job=%s error=%s", job["id"], type(exc).__name__)
        if not cancelled(job):
            sql("UPDATE jobs SET state='failed' WHERE id=? AND state='running'", (job["id"],))
            record(job["uid"], 0, 1)
            try:
                send(job["uid"], "这个任务没完成喵。错误编号：#{} / {}".format(
                    job["id"], type(exc).__name__))
            except Exception:
                pass
    finally:
        JOB_CONTEXT.job = None
        with SCHEDULER_LOCK:
            ACTIVE_JOBS.pop(job["id"], None)


def dispatch_jobs():
    """Unlimited administrator job concurrency; a single shared public lane."""
    with SCHEDULER_LOCK:
        public_busy = any(uid != ADMIN for uid in ACTIVE_JOBS.values())
        rows = sql("SELECT * FROM jobs WHERE state='queued' ORDER BY id")
        for job in rows:
            if job["uid"] != ADMIN and public_busy:
                continue
            # Only this dispatcher claims jobs. Record before starting a thread.
            sql("UPDATE jobs SET state='running' WHERE id=? AND state='queued'", (job["id"],))
            ACTIVE_JOBS[job["id"]] = job["uid"]
            try:
                threading.Thread(target=run_job, args=(job,), daemon=True).start()
            except Exception:
                ACTIVE_JOBS.pop(job["id"], None)
                sql("UPDATE jobs SET state='queued' WHERE id=? AND state='running'", (job["id"],))
                raise
            if job["uid"] != ADMIN:
                public_busy = True
        # Cached outputs and temporary files may still be referenced by any job.
        if not ACTIVE_JOBS:
            cleanup()


def worker():
    while True:
        try:
            dispatch_jobs()
        except Exception as exc:
            LOG.warning("scheduler retry: %s", type(exc).__name__)
        time.sleep(1)


def report_text(day):
    active = sql("SELECT COUNT(*) n FROM activity WHERE day=?", (day,), True)["n"]
    counts = sql("SELECT COALESCE(SUM(count),0) n,COALESCE(SUM(failed),0) f "
                 "FROM deliveries WHERE day=?", (day,), True)
    new = sql("SELECT COUNT(*) n FROM users WHERE first_day=?", (day,), True)["n"]
    total = sql("SELECT COUNT(*) n FROM users", one=True)["n"]
    return (text("report_title") + "\n日期：{}（北京时间）\n"
            "使用人数：{}\n保存数量：{}\n失败数量：{}\n新增用户：{}\n累计用户：{}\n\n"
            "使用人数按提交转换的账号去重；保存数量按成功发出的文件计，预览不重复算。"
            ).format(day, active, counts["n"], counts["f"], new, total)


def reporter():
    while True:
        try:
            current = now()
            if current.hour > REPORT_HOUR or (
                    current.hour == REPORT_HOUR and current.minute >= REPORT_MINUTE):
                day = (current.date() - dt.timedelta(days=1)).isoformat()
                if day >= setting("installed_day") and not sql(
                        "SELECT day FROM reports WHERE day=?", (day,), True):
                    send(ADMIN, report_text(day))
                    sql("INSERT OR IGNORE INTO reports VALUES(?)", (day,))
        except Exception as exc:
            LOG.warning("report retry: %s", type(exc).__name__)
        time.sleep(30)


def main():
    if not TOKEN or not ADMIN:
        raise SystemExit("BOT_TOKEN and ADMIN_ID required")
    init_db()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=reporter, daemon=True).start()
    while True:
        try:
            updates = api("getUpdates", offset=int(setting("offset") or "0"),
                          timeout=45, allowed_updates='["message"]')
            for update in updates:
                try:
                    if update.get("message"):
                        handle(update["message"])
                except Exception as exc:
                    LOG.warning("message failed: %s", type(exc).__name__)
                setting("offset", update["update_id"] + 1)
        except Exception as exc:
            LOG.warning("poll retry: %s", type(exc).__name__)
            time.sleep(5)


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--convert":
        try:
            import resource
        except ImportError:  # Windows: no address-space limit available
            resource = None
        if resource is not None:
            resource.setrlimit(resource.RLIMIT_AS, (650 * 1024 ** 2, 650 * 1024 ** 2))
            resource.setrlimit(resource.RLIMIT_FSIZE, (48 * 1024 ** 2, 48 * 1024 ** 2))
        convert_file(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
    else:
        main()
