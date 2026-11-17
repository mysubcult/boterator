"""Integration tests: explicit disposable database only, Telegram is mocked."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from boterator_app.db import create_pool
from boterator_app.reddit_feed import SCHEMA, Post, cycle


@unittest.skipUnless(os.getenv('REDDIT_TEST_DATABASE_URL'), 'requires disposable database')
class DatabaseFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url = os.environ['REDDIT_TEST_DATABASE_URL']
        if not url.endswith('/boterator_reddit_test_20261117'):
            raise RuntimeError('Refusing non-test database')
        self.pool = await create_pool(url)
        self.conn = await self.pool.acquire()
        await self.conn.execute(SCHEMA)
        await self.conn.execute('TRUNCATE reddit_feed_posts, reddit_feed_state')
        self.feed = {'name':'test','style':'thought','channel':'@test','bot_id':1}
        self.old = Post('t3_old','Old','https://example.org','https://reddit.com')
        self.new = Post('t3_new','New','https://example.org','https://reddit.com')
        self.reddit = SimpleNamespace(fetch=AsyncMock(return_value=[self.old]))
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
                                   session=SimpleNamespace(close=AsyncMock()))
        self.bot_patch = patch('boterator_app.reddit_feed.bot_for', AsyncMock(return_value=self.bot))
        self.check_patch = patch('boterator_app.reddit_feed.check_bot', AsyncMock(return_value='test'))
        self.bot_patch.start()
        self.check_patch.start()

    async def asyncTearDown(self):
        self.bot_patch.stop()
        self.check_patch.stop()
        await self.pool.release(self.conn)
        await self.pool.close()

    async def test_first_run_then_new_post_and_deduplication(self):
        await cycle(self.conn,self.reddit,self.feed,'run')
        self.bot.send_message.assert_not_called()
        self.reddit.fetch.return_value = [self.new,self.old]
        await cycle(self.conn,self.reddit,self.feed,'run')
        self.assertEqual(self.bot.send_message.await_count,1)
        row = await self.conn.fetchrow("SELECT * FROM reddit_feed_posts WHERE post_id='t3_new'")
        self.assertEqual(row['status'],'sent')
        self.assertEqual(row['message_ids'],[99])
        await cycle(self.conn,self.reddit,self.feed,'run')
        self.assertEqual(self.bot.send_message.await_count,1)
        self.reddit.fetch.return_value = [Post('t3_repost','New','https://example.org','https://reddit.com')]
        await cycle(self.conn,self.reddit,self.feed,'run')
        self.assertEqual(self.bot.send_message.await_count,1)

    async def test_uncertain_send_blocks_next_poll(self):
        await cycle(self.conn,self.reddit,self.feed,'init')
        self.reddit.fetch.return_value = [self.new]
        self.bot.send_message.side_effect = TimeoutError()
        await cycle(self.conn,self.reddit,self.feed,'run')
        await cycle(self.conn,self.reddit,self.feed,'run')
        self.assertEqual(self.bot.send_message.await_count,1)
        self.assertEqual(await self.conn.fetchval("SELECT status FROM reddit_feed_posts WHERE post_id='t3_new'"),'sending')

    async def test_empty_feed_does_not_initialize(self):
        self.reddit.fetch.return_value = []
        from boterator_app.reddit_feed import SourceError
        with self.assertRaises(SourceError):
            await cycle(self.conn,self.reddit,self.feed,'init')
        self.assertEqual(await self.conn.fetchval('SELECT COUNT(*) FROM reddit_feed_state'),0)

    async def test_held_gallery_is_recorded_and_can_later_resume(self):
        self.feed['style'] = 'photos'
        await cycle(self.conn,self.reddit,self.feed,'init')
        self.reddit.fetch.return_value = [Post('t3_gallery','Gallery','https://reddit.com/gallery/a','https://reddit.com',complete=False,gallery=True)]
        await cycle(self.conn,self.reddit,self.feed,'run')
        self.assertEqual(await self.conn.fetchval("SELECT status FROM reddit_feed_posts WHERE post_id='t3_gallery'"),'held')
        self.bot.send_message.assert_not_called()
        self.bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=100))
        self.reddit.fetch.return_value = [Post('t3_gallery','Gallery','https://reddit.com/gallery/a','https://reddit.com',photos=('https://i.redd.it/a.jpg',))]
        await cycle(self.conn,self.reddit,self.feed,'run')
        self.assertEqual(await self.conn.fetchval("SELECT status FROM reddit_feed_posts WHERE post_id='t3_gallery'"),'sent')
        self.bot.send_photo.assert_awaited_once()

    async def test_second_worker_cannot_take_lock(self):
        await self.conn.fetchval('SELECT pg_try_advisory_lock(74236918024)')
        async with self.pool.acquire() as other:
            self.assertFalse(await other.fetchval('SELECT pg_try_advisory_lock(74236918024)'))
        await self.conn.fetchval('SELECT pg_advisory_unlock(74236918024)')
