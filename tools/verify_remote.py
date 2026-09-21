"""Run explicitly on the deployed server; never print secrets."""
import gzip
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        os.environ[key] = value
import bot


def main():
    commands = [
        ("start", "开始收表情喵"), ("help", "怎么用喵"),
        ("batch", "批量收图"), ("pack", "整套搬走"),
        ("done", "打包带走"), ("status", "排到我没"),
        ("cancel", "不弄了喵"),
    ]
    bot.api("setMyCommands", commands=json.dumps([
        {"command": c, "description": d} for c, d in commands], ensure_ascii=False))
    identity = bot.api("getMe")
    print("BOT", identity["username"])
    found = {}
    for name in ("HotCherry", "UtyaDuck", "Animals", "AnimatedEmojies", "Ducks"):
        try:
            pack = bot.api("getStickerSet", name=name)
            for sticker in pack["stickers"]:
                item = bot.describe(sticker)
                if item["kind"] not in found:
                    found[item["kind"]] = item
            print("PACK", name, len(pack["stickers"]), sorted(found))
        except Exception as exc:
            print("PACK_UNAVAILABLE", name, type(exc).__name__)
    # Also exercise deterministic fixtures, irrespective of public pack availability.
    from PIL import Image
    static = bot.TEMP / "verify.webp"
    Image.new("RGBA", (128, 128), (255, 120, 180, 255)).save(str(static), "WEBP")
    bot.convert_file(static, bot.TEMP / "verify.png", "webp")
    print("STATIC_FIXTURE_OK")
    import subprocess
    video = bot.TEMP / "verify.webm"
    subprocess.run([bot.FFMPEG, "-v", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc=size=128x128:rate=15", "-t", "1",
                    "-c:v", "libvpx-vp9", "-threads", "1", str(video)], check=True)
    bot.convert_file(video, bot.TEMP / "verify.gif", "webm")
    print("VIDEO_FIXTURE_OK")
    if "tgs" not in found:
        raise RuntimeError("No real TGS sample found")
    for kind, item in found.items():
        result = bot.converted(item)
        with Image.open(str(result)) as image:
            print("REAL_CONVERSION", kind, image.format, image.size,
                  getattr(image, "n_frames", 1), result.stat().st_size)
        if kind == "tgs":
            preview = bot.upload(bot.ADMIN, result, True, "部署测试喵：动态贴纸已转成 GIF ✨")
            document = bot.upload(bot.ADMIN, result, caption="这是对应的 GIF 原文件喵。")
            print("DELIVERY", preview["message_id"], document["message_id"])
    # Test static delivery, then ZIP delivery; these are maintenance tests, not usage.
    image = bot.TEMP / "verify.png"
    print("PNG_PREVIEW", bot.upload(bot.ADMIN, image, True, "静态图片发送测试喵。")["message_id"])
    print("PNG_DOCUMENT", bot.upload(bot.ADMIN, image)["message_id"])
    import zipfile
    archive = bot.TEMP / "deployment_test.zip"
    with zipfile.ZipFile(str(archive), "w") as z:
        z.write(str(image), "test.png")
        z.write(str(bot.TEMP / "verify.gif"), "test.gif")
    print("ZIP_DOCUMENT", bot.upload(bot.ADMIN, archive, caption="批量打包发送测试喵。")["message_id"])
    print("REPORT", bot.send(bot.ADMIN, "日报样式测试（非正式日报）\n" +
                            bot.report_text(bot.now().date().isoformat()))["message_id"])
    print("READY_MESSAGE", bot.send(
        bot.ADMIN, "启动好啦喵！可以发你自己的表情测试了。\n"
        "图片、GIF、批量打包和整套下载已接好。\n"
        "每天北京时间 00:05，只给你发前一天的统计。\n"
        "动图默认 15 帧/秒，超出资源限制的文件会明确报错，不会静默截断。", True)["message_id"])


if __name__ == "__main__":
    main()
