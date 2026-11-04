"""Connection-pool reuse for prepared web turns, without transport retries."""
import types
import unittest
from unittest.mock import AsyncMock, patch
from utils.Client import Client


def session(label):
    return types.SimpleNamespace(headers={'fixture': label}, cookies={'cookie': label},
        post=AsyncMock(return_value=label), close=AsyncMock())


class ClientTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepared_stream_uses_primary_pool_and_legacy_keeps_separate_pool(self):
        for enabled in (False, True):
            primary, secondary = session('primary'), session('secondary')
            with patch('utils.Client.AsyncSession', side_effect=[primary, secondary]):
                client = Client(stream_on_primary=enabled)
            await client.post('https://invalid.test/prepare')
            result = await client.post_stream('https://invalid.test/generate', stream=True)
            self.assertEqual(result, 'primary' if enabled else 'secondary')
            self.assertEqual(primary.post.await_count, 2 if enabled else 1)
            self.assertEqual(secondary.post.await_count, 0 if enabled else 1)
            await client.close()
            primary.close.assert_awaited_once(); secondary.close.assert_awaited_once()

    async def test_reuse_preserves_explicit_stream_headers_cookies_and_deadline(self):
        primary, secondary = session('primary'), session('secondary')
        with patch('utils.Client.AsyncSession', side_effect=[primary, secondary]):
            client = Client(stream_on_primary=True)
        await client.post_stream('https://invalid.test/generate', headers={'fixture': 'explicit'},
            cookies={'cookie': 'explicit'}, timeout=.5, stream=True)
        primary.post.assert_awaited_once_with('https://invalid.test/generate', headers={'fixture': 'explicit'},
            cookies={'cookie': 'explicit'}, timeout=.5, stream=True)
        secondary.post.assert_not_awaited(); await client.close()

    async def test_failed_stream_is_not_replayed_on_another_pool(self):
        primary, secondary = session('primary'), session('secondary')
        primary.post.side_effect = ConnectionError('synthetic transport failure')
        with patch('utils.Client.AsyncSession', side_effect=[primary, secondary]):
            client = Client(stream_on_primary=True)
        with self.assertRaises(ConnectionError):
            await client.post_stream('https://invalid.test/generate', stream=True)
        primary.post.assert_awaited_once(); secondary.post.assert_not_awaited()
        await client.close()


if __name__ == '__main__':unittest.main(verbosity=2)
