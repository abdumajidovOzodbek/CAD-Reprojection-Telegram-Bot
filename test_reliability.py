"""Unit tests for Telegram retry logic, async auth cache, and DB optimizations."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


def _install_stub_modules_if_missing() -> None:
    """Stub heavy GIS/Telegram/Postgres libs when running in minimal environments."""
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    for mod_name in ("fiona", "geopandas", "pandas"):
        if importlib.util.find_spec(mod_name) is None and mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)

    if importlib.util.find_spec("shapely") is None and "shapely" not in sys.modules:
        shapely_mod = types.ModuleType("shapely")
        geom_mod = types.ModuleType("shapely.geometry")
        base_mod = types.ModuleType("shapely.geometry.base")
        ops_mod = types.ModuleType("shapely.ops")

        class BaseGeometry:
            pass

        class Polygon(BaseGeometry):
            pass

        class MultiPolygon(BaseGeometry):
            pass

        class Point(BaseGeometry):
            pass

        class GeometryCollection(BaseGeometry):
            pass

        base_mod.BaseGeometry = BaseGeometry
        geom_mod.Polygon = Polygon
        geom_mod.MultiPolygon = MultiPolygon
        geom_mod.Point = Point
        geom_mod.GeometryCollection = GeometryCollection
        ops_mod.polygonize = lambda g: []
        sys.modules["shapely"] = shapely_mod
        sys.modules["shapely.geometry"] = geom_mod
        sys.modules["shapely.geometry.base"] = base_mod
        sys.modules["shapely.ops"] = ops_mod

    if importlib.util.find_spec("telegram") is None and "telegram" not in sys.modules:
        tg = types.ModuleType("telegram")
        tg_const = types.ModuleType("telegram.constants")
        tg_err = types.ModuleType("telegram.error")
        tg_ext = types.ModuleType("telegram.ext")

        class TelegramError(Exception):
            pass

        class NetworkError(TelegramError):
            pass

        class TimedOut(NetworkError):
            def __init__(self, message: str = "Timed out"):
                super().__init__(message)

        class BadRequest(TelegramError):
            pass

        class RetryAfter(TelegramError):
            def __init__(self, retry_after: float):
                self.retry_after = retry_after
                super().__init__(f"Retry in {retry_after}s")

        class ChatAction:
            UPLOAD_DOCUMENT = "upload_document"

        tg.Update = MagicMock()
        tg.InlineKeyboardButton = MagicMock()
        tg.InlineKeyboardMarkup = MagicMock()
        tg_const.ChatAction = ChatAction
        tg_err.TelegramError = TelegramError
        tg_err.NetworkError = NetworkError
        tg_err.TimedOut = TimedOut
        tg_err.BadRequest = BadRequest
        tg_err.RetryAfter = RetryAfter
        tg_ext.Application = MagicMock()
        tg_ext.CommandHandler = MagicMock()
        tg_ext.ContextTypes = MagicMock()
        tg_ext.MessageHandler = MagicMock()
        tg_ext.CallbackQueryHandler = MagicMock()
        tg_ext.filters = MagicMock()

        sys.modules["telegram"] = tg
        sys.modules["telegram.constants"] = tg_const
        sys.modules["telegram.error"] = tg_err
        sys.modules["telegram.ext"] = tg_ext

    if importlib.util.find_spec("psycopg2") is None and "psycopg2" not in sys.modules:
        pg = types.ModuleType("psycopg2")
        pg_extras = types.ModuleType("psycopg2.extras")
        pg_pool = types.ModuleType("psycopg2.pool")

        class InterfaceError(Exception):
            pass

        class OperationalError(Exception):
            pass

        pg.InterfaceError = InterfaceError
        pg.OperationalError = OperationalError
        pg_extras.RealDictCursor = object
        pg_pool.SimpleConnectionPool = MagicMock()
        pg.extras = pg_extras
        pg.pool = pg_pool

        sys.modules["psycopg2"] = pg
        sys.modules["psycopg2.extras"] = pg_extras
        sys.modules["psycopg2.pool"] = pg_pool


_install_stub_modules_if_missing()

import bot  # noqa: E402
import database  # noqa: E402
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut  # noqa: E402


class RetryHelpersTests(unittest.TestCase):
    def test_timed_out_is_recognized_as_retryable(self) -> None:
        # Exact error from production traceback: TimedOut("Timed out")
        self.assertTrue(bot._is_retryable_telegram_error(TimedOut("Timed out")))
        self.assertTrue(bot._is_retryable_telegram_error(NetworkError("504 Gateway Time-out")))
        self.assertTrue(bot._is_retryable_telegram_error(RetryAfter(2)))
        self.assertTrue(bot._is_retryable_telegram_error(asyncio.TimeoutError()))
        self.assertFalse(bot._is_retryable_telegram_error(BadRequest("File is too big")))

    def test_retry_delay_honors_retry_after(self) -> None:
        self.assertEqual(bot._retry_delay(RetryAfter(3), 0), 3.5)
        self.assertEqual(bot._retry_delay(TimedOut("Timed out"), 0, base_delay=2.0), 2.0)
        self.assertEqual(bot._retry_delay(TimedOut("Timed out"), 1, base_delay=2.0), 4.0)


class DownloadAndUploadRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_retries_on_get_file_timed_out(self) -> None:
        calls: list[float] = []

        class FakeTelegramFile:
            async def download_to_drive(self, custom_path: Path, **_kwargs) -> None:
                custom_path.write_bytes(b"ok")

        class FakeBot:
            async def get_file(self, _file_id: str, **kwargs):
                calls.append(kwargs["read_timeout"])
                if len(calls) == 1:
                    raise TimedOut("Timed out")
                return FakeTelegramFile()

        status_msg = MagicMock()
        status_msg.edit_text = MagicMock(return_value=asyncio.sleep(0))

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "test.dxf"
            with patch("bot.asyncio.sleep", return_value=None):
                await bot._download_telegram_document(
                    FakeBot(), "file-123", dest, status_message=status_msg
                )
            self.assertEqual(dest.read_bytes(), b"ok")
        self.assertEqual(calls, [45.0, 90.0])

    async def test_download_retries_on_download_to_drive_failure(self) -> None:
        attempts = 0

        class FakeTelegramFile:
            async def download_to_drive(self, custom_path: Path, **_kwargs) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise NetworkError("Connection reset by peer")
                custom_path.write_bytes(b"payload")

        class FakeBot:
            async def get_file(self, _file_id: str, **_kwargs):
                return FakeTelegramFile()

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "test.dwg"
            with patch("bot.asyncio.sleep", return_value=None):
                await bot._download_telegram_document(FakeBot(), "file-123", dest)
            self.assertEqual(dest.read_bytes(), b"payload")
        self.assertEqual(attempts, 2)

    async def test_download_does_not_retry_file_too_big(self) -> None:
        calls = 0

        class FakeBot:
            async def get_file(self, _file_id: str, **_kwargs):
                nonlocal calls
                calls += 1
                raise BadRequest("File is too big")

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "huge.dwg"
            with self.assertRaises(bot.ConversionError) as ctx:
                await bot._download_telegram_document(FakeBot(), "file-123", dest)
            self.assertIn("20 MB", str(ctx.exception))
        self.assertEqual(calls, 1)

    async def test_upload_rewinds_stream_and_retries_on_timed_out(self) -> None:
        reads: list[bytes] = []

        async def fake_send_fn(**kwargs) -> None:
            data = kwargs["document"].read()
            reads.append(data)
            if len(reads) == 1:
                raise TimedOut("Timed out")

        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "out.zip"
            archive.write_bytes(b"zip-bytes")
            with patch("bot.asyncio.sleep", return_value=None):
                await bot._send_document_with_retry(fake_send_fn, archive, filename="out.zip")

        self.assertEqual(reads, [b"zip-bytes", b"zip-bytes"])


class AuthCacheAndDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_cache_hits_and_invalidates(self) -> None:
        bot._AUTH_CACHE.clear()
        fake_db = MagicMock()
        fake_db.is_authorized.return_value = True

        with patch.object(bot, "db_manager", fake_db):
            self.assertTrue(await bot._check_authorized(111))
            self.assertTrue(await bot._check_authorized(111))
            self.assertEqual(fake_db.is_authorized.call_count, 1)

            bot._remove_authorized_user(111)
            self.assertFalse(await bot._check_authorized(111))
            self.assertEqual(fake_db.is_authorized.call_count, 1)

            bot._add_authorized_user(111, first_name="Test")
            self.assertTrue(await bot._check_authorized(111))
            self.assertEqual(fake_db.is_authorized.call_count, 1)

    def test_database_readonly_skips_select_1_and_uses_rollback(self) -> None:
        fake_cursor = MagicMock()
        fake_cursor.fetchone.return_value = (True,)
        fake_conn = MagicMock()
        fake_conn.closed = 0
        fake_conn.cursor.return_value.__enter__.return_value = fake_cursor

        fake_pool = MagicMock()
        fake_pool.getconn.return_value = fake_conn

        with patch("database.psycopg2.pool.SimpleConnectionPool", return_value=fake_pool):
            db = database.DatabaseManager("postgresql://example")
            self.assertTrue(db.is_authorized(42))

        executed_sql = [call.args[0] for call in fake_cursor.execute.call_args_list]
        self.assertEqual(len(executed_sql), 1)
        self.assertIn("SELECT EXISTS", executed_sql[0])
        fake_conn.rollback.assert_called_once()
        fake_conn.commit.assert_not_called()

    def test_refresh_user_profile_does_not_overwrite_is_admin(self) -> None:
        fake_cursor = MagicMock()
        fake_conn = MagicMock()
        fake_conn.closed = 0
        fake_conn.cursor.return_value.__enter__.return_value = fake_cursor

        fake_pool = MagicMock()
        fake_pool.getconn.return_value = fake_conn

        with patch("database.psycopg2.pool.SimpleConnectionPool", return_value=fake_pool):
            db = database.DatabaseManager("postgresql://example")
            db.refresh_user_profile(7657310374, "Ozodbek", "Abdumajidov", "user", "Full")

        sql = fake_cursor.execute.call_args[0][0]
        self.assertIn("UPDATE authorized_users", sql)
        self.assertNotIn("is_admin", sql)
        fake_conn.commit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
