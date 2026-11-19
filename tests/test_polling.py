import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram.exceptions import TelegramRetryAfter, TelegramServerError
from aiogram.methods import DeleteWebhook

from boterator_app.slave import SlaveRuntime


class PollingRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_transient_errors_do_not_disable_receiver(self):
        method = DeleteWebhook()
        bot = SimpleNamespace(delete_webhook=AsyncMock(side_effect=[
            TelegramServerError(method, "Bad Gateway"),
            TelegramRetryAfter(method, "Too many requests", retry_after=13),
            True,
        ]))
        runtime = SimpleNamespace(
            bot_id=123, bot=bot, pool=AsyncMock(), _notify_health=AsyncMock(),
            dp=SimpleNamespace(resolve_used_update_types=Mock(return_value=[]), start_polling=AsyncMock()),
        )
        with patch("boterator_app.slave.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await SlaveRuntime._run(runtime)
        self.assertEqual([call.args for call in sleep.await_args_list], [(5,), (13,)])
        runtime.pool.execute.assert_not_awaited()
        runtime.dp.start_polling.assert_awaited_once()
        self.assertTrue(all(call.kwargs == {"drop_pending_updates": False} for call in bot.delete_webhook.await_args_list))
