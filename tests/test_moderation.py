"""Submission language, final decisions and author-visible rejection messages."""
import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import Message

from boterator_app.ai import AiAnalyzer, build_system_prompt
from boterator_app.db import create_pool, ensure_schema
from boterator_app.slave import SlaveRuntime, _author_rejection_reason


def message(text=None, **fields):
    return Message.model_validate({
        "message_id": 1, "date": 1700000000, "chat": {"id": 2, "type": "private"},
        "from": {"id": 2, "is_bot": False, "first_name": "Tester"}, "text": text, **fields,
    })


def analyzer():
    return AiAnalyzer(SimpleNamespace(ai_provider="deepseek", deepseek_api_key="test", deepseek_model="test"))


def rubric(language, **overrides):
    fields = {"fit": 3, "quality": 3, "clarity": 3, "authenticity_risk": "low", "flags": []}
    if language is not None:
        fields["submission_language"] = language
    return json.dumps({**fields, **overrides})


class ModerationTests(unittest.IsolatedAsyncioTestCase):
    async def analyze(self, submission, response, **settings):
        check = analyzer()
        check._analyze_deepseek = AsyncMock(return_value=response)
        check._extract_image = AsyncMock(return_value=None)
        result = await check.analyze(None, submission, settings, "en")
        return result, check._analyze_deepseek

    async def test_language_has_priority_over_quality_and_does_not_wait_for_rechecks(self):
        result, call = await self.analyze(message("La lluvia dejó un río en la calle."),
                                          rubric("non_english", quality=2, fit=2), required_language="en")
        self.assertEqual((result.recommendation, result.public_reason_code), ("reject", "non_english"))
        call.assert_awaited_once()
        self.assertIn('"submission_language"', call.await_args.args[4])
        self.assertIn("transliterated", call.await_args.args[4])
        self.assertIn("La lluvia", call.await_args.args[0])

    async def test_english_poem_remains_eligible(self):
        result, _ = await self.analyze(message("𝑇ℎ𝑒 𝑙𝑎𝑚𝑝𝑠 𝑜𝑓 Diwali"), rubric("english"), required_language="en")
        self.assertEqual((result.recommendation, result.submission_language), ("publish", "english"))

    async def test_missing_language_and_invalid_no_text_require_retry(self):
        for response in (rubric(None), rubric([]), rubric("unknown"), rubric("no_text"),
                         "Verdict: publish | publish chance 99%"):
            with self.subTest(response=response):
                result, _ = await self.analyze(message("Un poema"), response, required_language="en")
                self.assertTrue(result.failed)
                self.assertIsNone(result.recommendation)

    async def test_textless_media_and_legacy_custom_prompts(self):
        result, _ = await self.analyze(message(sticker={"file_id": "a", "file_unique_id": "b", "type": "regular",
                                       "width": 64, "height": 64, "is_animated": False, "is_video": False}),
                                       rubric("no_text"), required_language="en")
        self.assertEqual(result.submission_language, "no_text")
        legacy = {"system_prompt": "Verdict: publish/review/reject | publish chance NN%"}
        answer = "Verdict: publish | publish chance 95%"
        result, _ = await self.analyze(message("English caption"), answer + "\nSubmission language: english",
                                       required_language="en", **legacy)
        self.assertEqual((result.recommendation, result.publish_score), ("publish", 95))
        result, _ = await self.analyze(message("English caption"), answer, required_language="en", **legacy)
        self.assertTrue(result.failed)
        self.assertNotIn("submission_language", build_system_prompt("en", {}))
        self.assertEqual(build_system_prompt("en", legacy), legacy["system_prompt"])

    async def test_forward_and_anonymous_moderation_snapshot(self):
        runtime = SlaveRuntime.__new__(SlaveRuntime)
        runtime.settings = {"publish_mode": "copy"}
        runtime.moderator_chat_id = -9
        runtime.bot = AsyncMock()
        photo = message(caption="Original caption", has_media_spoiler=True, show_caption_above_media=True,
                        photo=[{"file_id": "img", "file_unique_id": "img1", "width": 64, "height": 64}])
        await runtime._moderation_snapshot(photo)
        method = runtime.bot.await_args.args[0]
        self.assertEqual((method.chat_id, method.photo, method.caption), (-9, "img", "Original caption"))
        self.assertTrue(method.has_spoiler and method.show_caption_above_media)
        runtime.settings["publish_mode"] = "forward"
        await runtime._moderation_snapshot(photo)
        runtime.bot.forward_message.assert_awaited_once_with(chat_id=-9, from_chat_id=2, message_id=1)

    def test_public_reasons(self):
        for code in ("quality", "fit", "clarity", "originality", "off_topic", "spam", "unsafe", None):
            with self.subTest(code=code):
                reason = _author_rejection_reason("ai_reject", None, code, "en")
                self.assertTrue(reason)
                self.assertNotRegex(reason, r"AI|score|model|analysis")
        self.assertIn("English", _author_rejection_reason("non_english", None, None, "en"))
        self.assertIn("before voting closed", _author_rejection_reason("timeout", None, None, "en"))
        self.assertEqual(_author_rejection_reason("custom", "The opening line is missing.", None, "en"),
                         "The opening line is missing.")
        self.assertIn("model train", _author_rejection_reason("custom", "The model train is hard to see.", None, "en"))
        self.assertNotIn("A.I.", _author_rejection_reason("custom", "A.I. rejected this.", None, "en"))
        self.assertNotIn("DeepSeek", _author_rejection_reason("custom", "DeepSeek rejected it.", None, "en"))


@unittest.skipUnless(os.getenv("MODERATION_TEST_DATABASE_URL"), "requires a disposable PostgreSQL database")
class SubmissionFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url = os.environ["MODERATION_TEST_DATABASE_URL"]
        if not url.endswith("/boterator_moderation_test"):
            raise RuntimeError("A disposable moderation test database is required")
        self.pool = await create_pool(url)
        self.addAsyncCleanup(self.pool.close)
        await ensure_schema(self.pool)
        await self.pool.execute("TRUNCATE incoming_messages, users, votes_history")
        self.ai = analyzer()
        row = {"id": 7, "owner_id": 3, "token": "7:test", "moderator_chat_id": -9, "target_channel": "@test",
               "settings": {"language": "en", "votes": 1, "vote_timeout": 1, "delay": 0,
                            "ai": {"enabled": True, "provider": "deepseek", "required_language": "en",
                                   "auto_publish_after_timeout": True}}}
        self.runtime = SlaveRuntime(self.pool, row, self.ai, "copy", 90, 24)
        self.addAsyncCleanup(self.runtime.bot.session.close)
        self.runtime._notify_author = AsyncMock()
        self.runtime._notify_health = AsyncMock()
        self.runtime._refresh_moderation_status = AsyncMock()
        self.snapshot = message("La lluvia dejó un río en la calle.", message_id=901, chat={"id": -9, "type": "group"})
        self.runtime._moderation_snapshot = AsyncMock(return_value=self.snapshot)
        self.runtime._send_by_publish_mode = AsyncMock(return_value=SimpleNamespace(message_id=905))
        self.runtime._send_moderation_status = AsyncMock(return_value=SimpleNamespace(message_id=902))
        self.msg = message("La lluvia dejó un río en la calle.")
        self.callback = SimpleNamespace(data="confirm|2|1", from_user=self.msg.from_user, answer=AsyncMock(),
                                        message=SimpleNamespace(edit_text=AsyncMock(), reply_to_message=self.msg))

    async def test_confirmation_language_gate_and_retries(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def delayed(*args):
            started.set()
            await finish.wait()
            return rubric(None)  # Missing check despite a high score.

        self.ai._analyze_deepseek = AsyncMock(side_effect=delayed)
        await self.runtime._handle_confirmation(self.callback)
        task = next(t for t in asyncio.all_tasks() if t.get_name().startswith("slave-7-ai-"))
        await asyncio.wait_for(started.wait(), 5)
        voter = SimpleNamespace(data="v|2|1|y", from_user=self.msg.from_user, answer=AsyncMock(),
                                message=SimpleNamespace(chat=SimpleNamespace(id=-9)))
        self.runtime.settings["selfvote"] = True
        await self.runtime._handle_vote(voter)
        self.assertIn("Checking the submission language", voter.answer.await_args.args[0])
        finish.set()
        await task
        await self.pool.execute("UPDATE incoming_messages SET created_at = now() - interval '2 hours'")
        await self.runtime._expire_old_votes()
        self.assertFalse(await self.pool.fetchval("SELECT is_voting_fail OR is_voting_success FROM incoming_messages"))
        self.assertEqual(await self.pool.fetchval("SELECT submission_language FROM incoming_messages"), "pending")

        self.ai._analyze_deepseek = AsyncMock(return_value=rubric("non_english"))
        await self.runtime._retry_failed_ai_analysis()
        result = await self.pool.fetchrow("SELECT is_voting_fail, is_voting_success, reject_reason FROM incoming_messages")
        self.assertEqual(tuple(result), (True, False, "non_english"))
        self.runtime._notify_author.assert_awaited_once()
        self.assertIn("This channel accepts submissions in English", self.runtime._notify_author.await_args.args[2])

        await self.runtime._handle_confirmation(self.callback)
        await self.runtime._expire_old_votes()
        await self.runtime.publish_ready_messages()
        self.runtime._notify_author.assert_awaited_once()
        self.runtime._moderation_snapshot.assert_awaited_once()
        self.runtime._send_by_publish_mode.assert_not_awaited()

    async def check_submission(self, classification, snapshot):
        self.runtime._moderation_snapshot.return_value = snapshot
        self.ai._analyze_deepseek = AsyncMock(return_value=classification)
        await self.runtime._handle_confirmation(self.callback)
        task = next(t for t in asyncio.all_tasks() if t.get_name().startswith("slave-7-ai-"))
        await task

    async def test_language_precedes_filter_rejections_and_has_accurate_timeout(self):
        snapshot = message("Regarde ceci https://example.org", message_id=901, chat={"id": -9, "type": "group"})
        for classification, expected in (("non_english", "non_english"), ("english", "auto_link"),
                                         (None, "language_check_expired")):
            with self.subTest(classification=classification):
                await self.pool.execute("TRUNCATE incoming_messages")
                await self.check_submission(rubric(classification), snapshot)
                if classification is None:
                    await self.pool.execute("UPDATE incoming_messages SET created_at = now() - interval '49 hours'")
                    await self.runtime._expire_old_votes()
                    self.assertIn("complete the language check", self.runtime._notify_author.await_args.args[2])
                result = await self.pool.fetchrow("SELECT is_voting_fail, reject_reason FROM incoming_messages")
                self.assertEqual(tuple(result), (True, expected))
                self.runtime._send_by_publish_mode.assert_not_awaited()

    async def test_english_autopublish_and_editorial_rejection(self):
        snapshot = message("The English version seen by moderators", message_id=901, chat={"id": -9, "type": "group"})
        await self.check_submission(rubric("english", quality=1), snapshot)
        self.assertIn(snapshot.text, self.ai._analyze_deepseek.await_args.args[0])
        self.assertEqual(await self.pool.fetchval("SELECT message->>'text' FROM incoming_messages"), snapshot.text)
        await self.pool.execute("UPDATE incoming_messages SET created_at = now() - interval '2 hours'")
        await self.runtime._expire_old_votes()
        self.assertEqual(self.runtime._notify_author.await_args.args[2],
                         "❌ Submission declined.\n\n🚫 Reason: The submission needs a more developed or distinctive idea.")

        await self.pool.execute("TRUNCATE incoming_messages")
        await self.check_submission(rubric("english"), snapshot)
        self.runtime.moderator_chat_id = -10  # A subsequent change cannot redirect the source.
        await self.pool.execute("UPDATE incoming_messages SET created_at = now() - interval '2 hours'")
        await self.runtime._expire_old_votes()
        self.assertTrue(await self.pool.fetchval("SELECT is_published AND ai_auto_approved FROM incoming_messages"))
        self.runtime._send_by_publish_mode.assert_awaited_once_with("@test", -9, 901)
