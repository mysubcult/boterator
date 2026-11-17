import asyncio
import html
import unittest
import time
from urllib.error import HTTPError
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

from boterator_app.reddit_feed import (Post, SourceError, from_json, from_rss, plan,
                                       split_text, utf16len, deliver, cycle, Reddit)


class Rendering(unittest.TestCase):
    def post(self, **changes):
        values = dict(id='t3_a', title='A < B & C', url='https://example.org/source',
                      permalink='https://www.reddit.com/r/test/comments/a')
        values.update(changes)
        return Post(**values)

    def test_gallery_preserves_order_and_all_images(self):
        photos = tuple(f'https://i.redd.it/{i}.jpg' for i in range(23))
        ops = plan(self.post(photos=photos), 'photos')
        actual = [m['media'] for op in ops for m in op.get('media', [])]
        self.assertEqual(actual, list(photos))
        self.assertEqual([len(op['media']) for op in ops], [10, 10, 3])
        self.assertEqual(ops[0]['media'][0]['caption'], 'A &lt; B &amp; C')
        self.assertNotIn('caption', ops[1]['media'][0])

    def test_eleventh_photo_is_not_dropped(self):
        ops = plan(self.post(photos=tuple(f'https://i.redd.it/{i}.jpg' for i in range(11))), 'photos')
        self.assertEqual(ops[-1]['method'], 'send_photo')
        self.assertEqual(ops[-1]['photo'], 'https://i.redd.it/10.jpg')

    def test_long_caption_and_unicode_are_lossless(self):
        text = '🌍 & <long>\n' * 1000
        ops = plan(self.post(title=text, photos=('https://i.redd.it/a.jpg',)), 'photos')
        self.assertIsNone(ops[0]['caption'])
        chunks = [html.unescape(op['text']) for op in ops[1:]]
        self.assertEqual(''.join(chunks), text)
        self.assertTrue(all(utf16len(t) <= 4000 for t in chunks))

    def test_text_channels_keep_style_and_source(self):
        p = self.post()
        self.assertEqual(plan(p, 'thought'), [{'method':'send_message','text':'A &lt; B &amp; C','preview':False}])
        op = plan(p, 'source')[0]
        self.assertTrue(op['preview'])
        self.assertEqual(op['text'], 'A &lt; B &amp; C [<a href="https://example.org/source">Source</a>]')
        self.assertIn('href="https://example.org/source">Source</a>', op['text'])

    def test_incomplete_gallery_is_never_sent(self):
        with self.assertRaises(SourceError):
            plan(self.post(photos=('https://i.redd.it/thumbnail.jpg',), complete=False), 'photos')

    def test_json_gallery_uses_declared_order(self):
        data = dict(name='t3_a', title='Gallery', url='https://www.reddit.com/gallery/a',
                    permalink='/r/test/comments/a', is_gallery=True,
                    gallery_data={'items':[{'media_id':'second'},{'media_id':'first'}]},
                    media_metadata={key:{'status':'valid','e':'Image','s':{'u':f'https://i.redd.it/{key}.jpg?x=1&amp;y=2'}}
                                    for key in ['first','second']})
        p = from_json(data)
        self.assertEqual(p.photos[0], 'https://i.redd.it/second.jpg?x=1&y=2')
        del data['media_metadata']['first']
        with self.assertRaises(SourceError):
            plan(from_json(data), 'photos')

    def test_reddit_non_content_filtered(self):
        for flag in ['stickied','over_18','removed_by_category','is_meta']:
            self.assertIsNone(from_json({flag:True}))

    def test_rss_ignores_thumbnail_and_keeps_source(self):
        rss = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>t3_a</id>
            <title>A &amp; B</title><link href="https://www.reddit.com/r/test/comments/a"/>
            <content>&lt;img src="https://preview.redd.it/thumb.jpg"/&gt;
            &lt;a href="https://www.reddit.com/gallery/a"&gt;[link]&lt;/a&gt;</content></entry></feed>'''
        p = from_rss(rss)[0]
        self.assertEqual(p.title, 'A & B')
        self.assertTrue(p.gallery)
        self.assertFalse(p.complete)
        self.assertFalse(p.photos)
        with self.assertRaises(SourceError):
            plan(p,'photos')

    def test_block_page_is_not_an_empty_feed(self):
        with self.assertRaises(SourceError):
            from_rss(b'<html><body>Blocked</body></html>')


class Transport(unittest.TestCase):
    def test_rss_retry_after_is_honoured(self):
        client = Reddit(None)
        url = 'https://www.reddit.com/r/test/top/.rss?t=week'
        error = HTTPError(url,429,'rate limit',{'Retry-After':'1800'},None)
        with patch('boterator_app.reddit_feed.urlopen',side_effect=error):
            with self.assertRaises(SourceError):
                client.read_rss(url)
        self.assertGreater(client.blocked_until[url],time.monotonic()+1790)



class Delivery(unittest.IsolatedAsyncioTestCase):
    def row(self):
        return dict(post_id='t3_a',next_operation=0,
                    operations=[{'method':'send_message','text':'hello','preview':False}])

    async def test_timeout_never_becomes_pending(self):
        conn = AsyncMock()
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=asyncio.TimeoutError))
        await deliver(conn,bot,{'name':'test','channel':'@test'},self.row())
        self.assertEqual(conn.execute.await_count,1)
        self.assertIn("status='sending'", conn.execute.call_args.args[0])

    async def test_success_records_telegram_id(self):
        conn = AsyncMock()
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
        await deliver(conn,bot,{'name':'test','channel':'@test'},self.row())
        self.assertEqual(conn.execute.call_args.args[1:], ('test','t3_a',1,[42],'sent'))

    async def test_resume_skips_already_sent_operations(self):
        conn = AsyncMock()
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=43)))
        row = self.row()
        row['operations'] *= 3
        row['next_operation'] = 2
        await deliver(conn,bot,{'name':'test','channel':'@test'},row)
        self.assertEqual(bot.send_message.await_count,1)
        self.assertEqual(conn.execute.call_args.args[3],3)

    async def test_explicit_rate_limit_is_retryable(self):
        conn = AsyncMock()
        error = TelegramRetryAfter(method=SendMessage(chat_id='@test',text='hello'),message='Too many requests',retry_after=2000)
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=error))
        await deliver(conn,bot,{'name':'test','channel':'@test'},self.row())
        self.assertIn("status='pending'", conn.execute.call_args.args[0])
        self.assertEqual(conn.execute.call_args.args[-1],2000)

    async def test_preview_does_not_need_database_or_telegram(self):
        reddit = SimpleNamespace(fetch=AsyncMock(return_value=[Post('t3_a','A','https://example.org','https://reddit.com')]))
        await cycle(None, reddit, {'name':'test','style':'thought','channel':'@test'}, 'preview')


if __name__ == '__main__':
    unittest.main()
