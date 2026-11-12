"""Creating a Cloud Agent enrolls it here and does not archive it on Cursor."""
import os
from pathlib import Path
import tempfile
import unittest
from configuration import initialize, load_config
from connections import ConnectionError
from dashboard import Bridge
from platform_adapters import RemoteError


ONE = 'bc-00000000-0000-0000-0000-000000000001'
RUN = 'run-00000000-0000-0000-0000-000000000001'
KEY = 'fixture-key-PRIVATE-987654'


class CreateTransport:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.run_id = RUN
        self.agent_id = ONE

    def json(self, method, url, headers, data=None):
        self.calls.append((method, url, data))
        if method == 'POST' and url.endswith('/v1/agents'):
            if self.fail:
                raise RemoteError('platform_request_failed')
            return {'agent': {'id': self.agent_id, 'name': 'Readme', 'status': 'ACTIVE'},
                    'run': {'id': self.run_id, 'agentId': self.agent_id, 'status': 'CREATING'}}
        if method == 'GET' and '/runs/' in url:
            return {'id': RUN, 'agentId': ONE, 'status': 'FINISHED', 'result': '已完成'}
        if method == 'GET':
            return {'items': [{'id': ONE, 'name': 'Readme', 'status': 'ACTIVE'}]}
        raise RemoteError('platform_read_failed')

    def open(self, method, url, headers, body=None):
        raise RemoteError('platform_read_failed')


class CloudCreateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / 'config.json'
        initialize(self.path, 48432)
        config = load_config(self.path)
        config['run_id'] = 'cloud-create-fixture'
        config['run_dir'] = str(self.root)
        self.bridge = Bridge(config)
        self.remote = CreateTransport()
        self.bridge.connections.transport = self.remote
        self.bridge.commands.adapters['cursor_cloud'].transport = self.remote

    def tearDown(self):
        adapter = self.bridge.commands.adapters['cursor_cloud']
        for worker in list(adapter.workers.values()):
            worker.join(2)
        self.bridge.commands.close()
        self.bridge.history.close()
        self.temp.cleanup()

    def visible(self):
        return {agent['id']: agent for agent in self.bridge.work_snapshot()['agents']}

    def test_create_places_the_agent_and_office_archive_stays_local(self):
        created = self.bridge.connections.create_cursor_cloud({
            'api_key': KEY, 'prompt': '  寫一個說明  ',
            'repository': 'https://github.com/acme/widgets', 'branch': 'main'})
        ident = 'cursor:' + ONE
        self.assertEqual(created['agent_id'], ident)
        self.assertEqual(created['mode'], 'cursor_cloud')
        post = [item for item in self.remote.calls if item[0] == 'POST']
        self.assertEqual(len(post), 1)
        self.assertEqual(post[0][1], 'https://api.cursor.com/v1/agents')
        self.assertEqual(post[0][2]['repos'], [{'url': 'https://github.com/acme/widgets', 'startingRef': 'main'}])
        self.assertEqual(post[0][2]['prompt']['text'], '寫一個說明')
        self.assertNotIn('[OFFICE:', post[0][2]['prompt']['text'])
        self.assertNotIn(KEY, post[0][2]['prompt']['text'])
        for worker in list(self.bridge.commands.adapters['cursor_cloud'].workers.values()):
            worker.join(2)
        shown = self.visible()
        self.assertEqual(shown[ident]['name'], 'Readme')
        self.assertEqual(shown[ident]['command_capability']['mode'], 'cursor_cloud')
        row = self.bridge.commands.db.execute('SELECT state, native_message_id, payload FROM commands WHERE agent_id=?', (ident,)).fetchone()
        self.assertEqual(row['state'], 'completed')
        self.assertEqual(row['native_message_id'], RUN)
        self.assertIsNone(row['payload'])
        self.assertNotIn(KEY, self.path.read_text())
        saved = load_config(self.path)
        item = saved['command_adapters']['cursor_cloud']['sessions'][ONE]
        self.assertTrue(Path(item['credential_file']).is_file())
        if os.name != 'nt':
            self.assertEqual(Path(item['credential_file']).stat().st_mode & 0o777, 0o600)
        before = len(self.remote.calls)
        self.bridge.connections.connect_cursor_local({'action': 'archive', 'agent_id': ident})
        later = self.remote.calls[before:]
        self.assertTrue(later)
        self.assertTrue(all(method == 'GET' and not url.rstrip('/').endswith('/archive') for method, url, _data in later))
        self.assertNotIn(ident, self.visible())
        self.bridge.connections.connect_cursor_local({'action': 'reopen', 'agent_id': ident})
        self.assertIn(ident, self.visible())

    def test_create_without_a_repository_omits_repos(self):
        self.bridge.connections.create_cursor_cloud({'api_key': KEY, 'prompt': '只是問一句'})
        post = next(item for item in self.remote.calls if item[0] == 'POST')
        self.assertEqual(post[2], {'prompt': {'text': '只是問一句'}})
        for worker in list(self.bridge.commands.adapters['cursor_cloud'].workers.values()):
            worker.join(2)

    def test_rejected_create_leaves_the_office_unchanged(self):
        self.remote.fail = True
        with self.assertRaises(ConnectionError) as failed:
            self.bridge.connections.create_cursor_cloud({'api_key': KEY, 'prompt': '不要建立'})
        self.assertEqual(failed.exception.code, 'platform_request_failed')
        self.assertEqual(self.visible(), {})
        self.assertNotIn('cursor_cloud', load_config(self.path).get('command_adapters', {}))

    def test_unconfirmed_receipt_is_not_enrolled(self):
        self.remote.run_id = 'not-a-run'
        with self.assertRaises(ConnectionError) as failed:
            self.bridge.connections.create_cursor_cloud({'api_key': KEY, 'prompt': '未確認'})
        self.assertEqual(failed.exception.code, 'cursor_cloud_start_failed')
        self.assertEqual(self.visible(), {})

    def test_bad_repository_never_calls_cursor(self):
        with self.assertRaises(ConnectionError) as failed:
            self.bridge.connections.create_cursor_cloud({'api_key': KEY, 'prompt': '問', 'branch': 'main'})
        self.assertEqual(failed.exception.code, 'cursor_repository_invalid')
        with self.assertRaises(ConnectionError):
            self.bridge.connections.create_cursor_cloud({'api_key': KEY, 'prompt': '問', 'repository': 'http://github.com/acme/widgets'})
        self.assertEqual(self.remote.calls, [])
        self.assertEqual(self.visible(), {})
