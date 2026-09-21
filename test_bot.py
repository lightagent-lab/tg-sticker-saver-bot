import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock
import bot


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = bot.DB
        self.old_outbox = bot.OUTBOX
        bot.OUTBOX = Path(self.temp.name) / "outbox"
        bot.OUTBOX.mkdir()
        bot.DB = Path(self.temp.name) / "test.sqlite3"
        bot.init_db()
        self.sent = patch.object(bot, "send", return_value={"message_id": 1})
        self.send = self.sent.start()
        self.throttle = patch.object(bot, "wait_public_slot")
        self.throttle.start()

    def tearDown(self):
        self.sent.stop()
        self.throttle.stop()
        bot.DB = self.old_db
        bot.OUTBOX = self.old_outbox
        self.temp.cleanup()

    def message(self, text=None, sticker=None, uid=100):
        msg = {"chat": {"id": uid, "type": "private"}}
        if text:
            msg["text"] = text
        if sticker:
            msg["sticker"] = sticker
        bot.handle(msg)

    def test_start(self):
        self.message("/start")
        self.assertIn(bot.BOT_NAME, self.send.call_args[0][1])
        self.assertIn("@", self.send.call_args[0][1])  # admin line is rendered
        self.assertEqual(bot.sql("SELECT COUNT(*) n FROM users", one=True)["n"], 1)

    def test_localized_welcome(self):
        self.assertEqual(bot.text("welcome").count("{"), 0)  # no unformatted fields
        for lang in ("zh", "en"):
            bot.BOT_LANG = lang
            self.assertIn(bot.BOT_NAME, bot.text("welcome"))
            self.assertNotIn("{ADMIN_LINE}", bot.text("welcome"))
        bot.BOT_LANG = "zh"

    def test_batch_enqueue(self):
        self.message("/batch")
        self.message(sticker={"file_id": "a", "file_unique_id": "b"})
        self.message("/done")
        job = bot.sql("SELECT * FROM jobs", one=True)
        self.assertEqual(json.loads(job["payload"])["items"][0]["kind"], "webp")
        self.assertFalse(bot.sql("SELECT * FROM batches"))

    def test_batch_limit(self):
        self.message("/batch")
        for _ in range(51):
            self.message(sticker={"file_id": "a", "file_unique_id": "b"})
        row = bot.sql("SELECT items FROM batches", one=True)
        self.assertEqual(len(json.loads(row["items"])), 50)

    def test_queue_limit(self):
        for _ in range(11):
            self.message(sticker={"file_id": "a", "file_unique_id": "b"})
        self.assertEqual(len(bot.sql("SELECT * FROM jobs")), 10)

    def test_admin_queue_has_no_ten_job_limit(self):
        with patch.object(bot, "ADMIN", 100):
            for _ in range(15):
                self.message(sticker={"file_id": "a", "file_unique_id": "b"})
        self.assertEqual(len(bot.sql("SELECT * FROM jobs")), 15)

    def test_dispatch_admin_parallel_public_serial(self):
        with patch.object(bot, "ADMIN", 100), patch.object(bot, "ACTIVE_JOBS", {}), \
                patch.object(bot.threading, "Thread") as thread:
            for uid in (200, 100, 300, 100):
                bot.enqueue(uid, {"items": [], "batch": True})
            bot.dispatch_jobs()
            self.assertEqual(thread.call_count, 3)
            self.assertEqual(list(bot.ACTIVE_JOBS.values()).count(100), 2)
            self.assertEqual(bot.sql("SELECT state FROM jobs WHERE uid=300", one=True)["state"], "queued")
            bot.dispatch_jobs()
            self.assertEqual(thread.call_count, 3)

    def test_public_minimum_interval_and_admin_exempt(self):
        self.throttle.stop()
        clock = [100.0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(bot, "ADMIN", 100), patch.object(bot, "PUBLIC_NEXT_START", 0), \
                patch.object(bot.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(bot.time, "sleep", side_effect=sleep), \
                patch.object(bot, "cancelled", return_value=False), \
                patch.object(bot, "progress"):
            bot.wait_public_slot({"id": 1, "uid": 200})
            bot.wait_public_slot({"id": 2, "uid": 100})
            self.assertEqual(clock[0], 100)
            bot.wait_public_slot({"id": 3, "uid": 300})
            self.assertEqual(clock[0], 105)
            clock[0] = 120
            bot.wait_public_slot({"id": 4, "uid": 200})
            self.assertEqual(clock[0], 120)
        self.throttle.start()

    def test_cancel_isolated(self):
        for uid in (100, 200):
            self.message(sticker={"file_id": "a", "file_unique_id": "b"}, uid=uid)
        self.message("/cancel")
        self.assertEqual(bot.sql("SELECT state FROM jobs WHERE uid=100", one=True)["state"], "cancelled")
        self.assertEqual(bot.sql("SELECT state FROM jobs WHERE uid=200", one=True)["state"], "queued")

    def test_stats_private(self):
        self.message("/stats", uid=100)
        self.assertIn("只给管理员", self.send.call_args[0][1])

    def test_clear_user_history_isolated(self):
        for uid in (100, 200):
            self.message(sticker={"file_id": "a", "file_unique_id": "b"}, uid=uid)
            self.message("/batch", uid=uid)
        old = bot.sql("SELECT * FROM jobs WHERE uid=100", one=True)
        bot.record(100, 1)
        self.message("🗑 清空列表")
        self.assertFalse(bot.sql("SELECT * FROM jobs WHERE uid=100"))
        self.assertFalse(bot.sql("SELECT * FROM batches WHERE uid=100"))
        self.assertTrue(bot.cancelled(old))
        self.assertEqual(len(bot.sql("SELECT * FROM jobs WHERE uid=200")), 1)
        self.assertEqual(len(bot.sql("SELECT * FROM batches WHERE uid=200")), 1)
        self.assertIn("保存数量：1", bot.report_text(bot.now().date().isoformat()))
        self.message("/batch")
        self.message(sticker={"file_id": "new", "file_unique_id": "new"})
        batch = bot.sql("SELECT items FROM batches WHERE uid=100", one=True)
        self.assertEqual(len(json.loads(batch["items"])), 1)

    def test_count_preview_not_double(self):
        bot.record(100, 1)
        bot.record(100, 0, 1)
        text = bot.report_text(bot.now().date().isoformat())
        self.assertIn("保存数量：1", text)
        self.assertIn("失败数量：1", text)

    def test_pack_link(self):
        self.message("https://t.me/addstickers/HotCherry")
        job = bot.sql("SELECT payload FROM jobs", one=True)
        self.assertEqual(json.loads(job["payload"])["pack"], "HotCherry")

    def test_batch_process(self):
        self.message("/batch")
        self.message(sticker={"file_id": "a", "file_unique_id": "b"})
        self.message("/done")
        job = bot.sql("SELECT * FROM jobs", one=True)
        image = Path(self.temp.name) / "sample.png"
        image.write_bytes(b"mock image")
        with patch.object(bot, "converted", return_value=image), patch.object(bot, "upload") as upload:
            bot.process(job)
            self.assertEqual(upload.call_count, 2)  # representative preview + ZIP
        self.assertIn("保存数量：1", bot.report_text(bot.now().date().isoformat()))

    def test_status_distinguishes_progress(self):
        self.message("/batch")
        self.message("https://t.me/addstickers/Example")
        job = bot.sql("SELECT * FROM jobs", one=True)
        bot.sql("UPDATE jobs SET state='running' WHERE id=?", (job["id"],))
        bot.progress(job, total=94, processed=12, delivered=0, phase="下载/转换第 13 个")
        self.message("/status")
        text = self.send.call_args[0][1]
        self.assertIn("12/94", text)
        self.assertNotIn("0 / 50", text)
        self.message("/done")
        self.assertIn("自动发送 ZIP", self.send.call_args[0][1])
        self.assertIn("12/94", self.send.call_args[0][1])

    def test_connection_retry_rewinds_upload(self):
        import requests
        stream = io.BytesIO(b"payload")
        reads = []
        def post(*args, **kwargs):
            reads.append(kwargs["files"]["document"][1].read())
            if len(reads) == 1:
                raise requests.ConnectionError("disconnected")
            response = Mock(status_code=200)
            response.json.return_value = {"ok": True, "result": {"message_id": 5}}
            return response
        with patch("requests.post", side_effect=post), patch.object(bot, "network_wait"):
            result = bot.api("sendDocument", files={"document": ("a.zip", stream, "application/zip")})
        self.assertEqual(reads, [b"payload", b"payload"])
        self.assertEqual(result["message_id"], 5)

    def test_retry_server_error_and_rate_limit(self):
        bad = Mock(status_code=502)
        bad.json.side_effect = ValueError("not JSON")
        limited = Mock(status_code=429)
        limited.json.return_value = {"ok": False, "error_code": 429,
                                     "parameters": {"retry_after": 80}}
        good = Mock(status_code=200)
        good.json.return_value = {"ok": True, "result": True}
        with patch("requests.post", side_effect=[bad, limited, good]), \
                patch.object(bot, "network_wait") as wait:
            self.assertTrue(bot.api("getFile", file_id="x"))
        self.assertEqual(wait.call_args[0][0], 80)

    def test_retry_bounded_and_permanent_error_not_retried(self):
        import requests
        with patch("requests.post", side_effect=requests.Timeout("slow")) as post, \
                patch.object(bot, "network_wait"):
            with self.assertRaises(requests.Timeout):
                bot.api("getFile", file_id="x")
            self.assertEqual(post.call_count, bot.NETWORK_ATTEMPTS)
        bad = Mock(status_code=400)
        bad.json.return_value = {"ok": False, "error_code": 400, "description": "bad file"}
        with patch("requests.post", return_value=bad) as post:
            with self.assertRaises(bot.TelegramError):
                bot.api("getFile", file_id="x")
            self.assertEqual(post.call_count, 1)

    def test_pack_splits_and_zip_precedes_preview(self):
        self.message("https://t.me/addstickers/Example")
        job = bot.sql("SELECT * FROM jobs", one=True)
        image = Path(self.temp.name) / "image.png"
        image.write_bytes(b"small")
        pack = {"stickers": [{"file_id": str(i), "file_unique_id": str(i)} for i in range(94)]}
        sizes = []
        def uploaded(uid, path, preview=False, caption=""):
            if not preview:
                import zipfile
                with zipfile.ZipFile(str(path)) as archive:
                    sizes.append(len([x for x in archive.namelist() if x.endswith(".png")]))
        with patch.object(bot, "api", return_value=pack), \
                patch.object(bot, "converted", return_value=image), \
                patch.object(bot, "upload", side_effect=uploaded) as upload:
            bot.process(job)
        self.assertEqual(sizes, [50, 44])
        self.assertEqual(upload.call_args_list[0][0][1].suffix, ".zip")
        p = bot.sql("SELECT * FROM progress WHERE job_id=?", (job["id"],), True)
        self.assertEqual((p["processed"], p["delivered"]), (94, 94))

    def test_completion_message_failure_does_not_fail_delivered_job(self):
        self.message("https://t.me/addstickers/Example")
        job = bot.sql("SELECT * FROM jobs", one=True)
        image = Path(self.temp.name) / "image.png"
        image.write_bytes(b"small")
        self.send.side_effect = [None, RuntimeError("notification lost")]
        with patch.object(bot, "api", return_value={"stickers": [
                {"file_id": "x", "file_unique_id": "y"}]}), \
                patch.object(bot, "converted", return_value=image), \
                patch.object(bot, "upload"):
            bot.process(job)
        self.assertIn("保存数量：1", bot.report_text(bot.now().date().isoformat()))

    def test_failed_zip_retained_and_retried_without_reconversion(self):
        self.message("https://t.me/addstickers/Example")
        job = bot.sql("SELECT * FROM jobs", one=True)
        image = Path(self.temp.name) / "image.png"
        image.write_bytes(b"small")
        with patch.object(bot, "api", return_value={"stickers": [
                {"file_id": "x", "file_unique_id": "y"}]}), \
                patch.object(bot, "converted", return_value=image), \
                patch.object(bot, "upload", side_effect=ConnectionError("offline")):
            bot.process(job)
        row = bot.sql("SELECT * FROM outbox", one=True)
        self.assertTrue(Path(row["path"]).exists())
        self.assertIn("保存数量：0", bot.report_text(bot.now().date().isoformat()))
        with patch.object(bot, "upload"), patch.object(bot, "converted") as convert, \
                patch.object(bot, "cancelled", return_value=False):
            bot.retry_archives({"id": 999, "uid": 200})
            self.assertTrue(bot.sql("SELECT * FROM outbox"))
            bot.retry_archives({"id": 998, "uid": 100})
            bot.retry_archives({"id": 997, "uid": 100})
            convert.assert_not_called()
        self.assertFalse(bot.sql("SELECT * FROM outbox"))
        self.assertFalse(Path(row["path"]).exists())
        self.assertIn("保存数量：1", bot.report_text(bot.now().date().isoformat()))

    def test_upload_lock_released_after_error(self):
        image = Path(self.temp.name) / "image.png"
        image.write_bytes(b"small")
        with patch.object(bot, "upload_unlocked", side_effect=ConnectionError("offline")):
            with self.assertRaises(ConnectionError):
                bot.upload(100, image)
        self.assertTrue(bot.UPLOAD_LOCK.acquire(blocking=False))
        bot.UPLOAD_LOCK.release()


if __name__ == "__main__":
    unittest.main()
