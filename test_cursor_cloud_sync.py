"""Unarchived Cloud Agents appear from the saved API key and leave when Cursor archives them."""
import os
from pathlib import Path
import tempfile
import unittest
from configuration import initialize, load_config
from dashboard import Bridge
from platform_adapters import RemoteError


ONE = 'bc-00000000-0000-0000-0000-000000000001'
TWO = 'bc-00000000-0000-0000-0000-000000000002'
ARCHIVED = 'bc-00000000-0000-0000-0000-000000000003'
KEY = 'fixture-key-PRIVATE-987654'


class CloudList:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []
        self.fail = False

    def json(self, method, url, headers, data=None):
        self.calls.append((method, url))
        if self.fail:
            raise RemoteError('platform_read_failed')
        if 'cursor=' not in url:
            return self.pages[0]
        return self.pages[1]


class CloudSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / 'config.json'
        initialize(self.path, 48431)
        config = load_config(self.path)
        config['run_id'] = 'cloud-sync-fixture'
        config['run_dir'] = str(self.root)
        self.bridge = Bridge(config)
        self.listing = CloudList([
            {'items': [
                {'id': ONE, 'name': 'Readme', 'status': 'ACTIVE'},
                {'id': ARCHIVED, 'name': 'Old', 'status': 'ARCHIVED'},
                {'id': 'not-a-cloud-id', 'name': 'Skip'},
            ], 'nextCursor': 'page-2'},
            {'items': [{'id': TWO, 'name': 'Readme', 'status': 'IDLE'}]},
        ])
        self.bridge.connections.transport = self.listing
        secret = self.bridge.connections.root / 'cursor.json'
        secret.parent.mkdir(parents=True, exist_ok=True)
        from hermes_acp import private_json
        private_json(secret, {'api_key': KEY, 'workspace': str(self.root)})

    def tearDown(self):
        self.bridge.commands.close()
        self.bridge.history.close()
        self.temp.cleanup()

    def visible(self):
        return {agent['id']: agent for agent in self.bridge.work_snapshot()['agents']}

    def test_unarchived_cloud_agents_appear_and_follow_the_cursor_list(self):
        first = self.bridge.connections.sync_cursor_cloud()
        self.assertTrue(first['synced'])
        self.assertEqual(set(first['added']), {'cursor:' + ONE, 'cursor:' + TWO})
        shown = self.visible()
        self.assertEqual(set(shown), {'cursor:' + ONE, 'cursor:' + TWO})
        self.assertEqual(shown['cursor:' + ONE]['state'], 'working')
        self.assertEqual(shown['cursor:' + TWO]['name'], 'Readme 2')
        self.assertEqual(shown['cursor:' + TWO]['state'], 'idle')
        self.assertEqual(shown['cursor:' + ONE]['command_capability']['mode'], 'cursor_cloud')
        self.assertTrue(shown['cursor:' + ONE]['command_capability']['same_session'])
        self.assertIn('includeArchived=false', self.listing.calls[0][1])
        self.assertIn('cursor=page-2', self.listing.calls[1][1])
        again = self.bridge.connections.sync_cursor_cloud()
        self.assertEqual(again['added'], [])
        self.assertEqual(set(self.visible()), set(shown))
        self.assertNotIn(KEY, self.path.read_text())
        saved = load_config(self.path)
        item = saved['command_adapters']['cursor_cloud']['sessions'][ONE]
        self.assertTrue(Path(item['credential_file']).is_file())
        if os.name != 'nt':
            self.assertEqual(Path(item['credential_file']).stat().st_mode & 0o777, 0o600)

        self.listing.fail = True
        failed = self.bridge.connections.sync_cursor_cloud()
        self.assertFalse(failed['synced'])
        self.assertEqual(set(self.visible()), set(shown))

        self.listing.fail = False
        self.listing.pages = [{'items': [{'id': TWO, 'name': 'Readme', 'status': 'IDLE'}]}, {'items': []}]
        left = self.bridge.connections.sync_cursor_cloud()
        self.assertEqual(left['away'], ['cursor:' + ONE])
        self.assertNotIn('cursor:' + ONE, self.visible())
        self.assertIn('cursor:' + TWO, self.visible())
        self.assertEqual(self.bridge.commands.capability('cursor:' + ONE)['reason'], 'cursor_cloud_away')
        self.assertFalse(self.bridge.commands.capability('cursor:' + ONE)['can_send'])
        still = load_config(self.path)
        self.assertIn('cursor:' + ONE, {agent['id'] for agent in still['agents']})

        self.listing.pages = [{'items': [
            {'id': ONE, 'name': 'Readme', 'status': 'IDLE'},
            {'id': TWO, 'name': 'Readme', 'status': 'IDLE'},
        ]}, {'items': []}]
        self.bridge.connections.sync_cursor_cloud()
        self.assertIn('cursor:' + ONE, self.visible())
        self.assertNotIn('cursor_cloud_away_ids', load_config(self.path))

    def test_office_archive_stays_hidden_until_reopen(self):
        self.bridge.connections.sync_cursor_cloud()
        ident = 'cursor:' + ONE
        before = len(self.listing.calls)
        self.bridge.connections.connect_cursor_local({'action': 'archive', 'agent_id': ident})
        self.assertGreater(len(self.listing.calls), before)
        self.assertNotIn(ident, self.visible())
        self.bridge.connections.sync_cursor_cloud()
        self.assertNotIn(ident, self.visible())
        home = self.bridge.connections.cursor_local_home()
        self.assertIn(ident, {item['agent_id'] for item in home['archived']})
        self.bridge.connections.connect_cursor_local({'action': 'reopen', 'agent_id': ident})
        self.assertIn(ident, self.visible())
