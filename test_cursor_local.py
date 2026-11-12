"""Local Cursor SDK session: create once, resume by sending text, no webhook."""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from configuration import initialize, load_config
from connections import ConnectionError
from cursor_local import LocalStartError, active_run_error, install_windows_bridge_reader
from dashboard import Bridge
from history import timestamp


AGENT_ID = 'agent-localfixture01'


class Block:
    def __init__(self, kind, text):
        self.type, self.text = kind, text


class Inner:
    def __init__(self, content):
        self.content = content


class Message:
    def __init__(self, kind, text='', message=None):
        self.type, self.text, self.message = kind, text, message


class Run:
    def __init__(self, run_id, result):
        self.id, self.result, self.status = run_id, result, 'finished'
        self._messages = [
            Message('thinking', 'PRIVATE_REASONING_MUST_NOT_SAVE'),
            Message('assistant', message=Inner([Block('text', 'Working. ')])),
            Message('tool_call', text='RAW_TOOL_MUST_NOT_SAVE'),
            Message('assistant', message=Inner([Block('text', 'still working')])),
        ]
    def messages(self):
        return iter(self._messages)
    def wait(self):
        return self


class Handle:
    def __init__(self):
        self.closed = False
    def close(self):
        self.closed = True


class BlockingRun(Run):
    def __init__(self, run_id):
        super().__init__(run_id, 'interrupted reply must not replace the cancel')
        self.gate = threading.Event()
    def messages(self):
        self.gate.wait(3)
        return iter(())


class Delta:
    def __init__(self, text):
        self.sdk_message = None
        self.interaction_update = type('Update', (), {'type': 'text-delta', 'text': text})()


class DeltaRun(Run):
    def __init__(self, run_id):
        super().__init__(run_id, '')
        self.status = 'finished'
    def _events(self):
        yield Delta('正在查看專案。')
        yield Delta('完整回覆在這裡。')
    def messages(self):
        raise AssertionError('a live stream must be read before the saved snapshot')


class FakeRuntime:
    def __init__(self):
        self.created, self.sent, self.handles, self.open_runs, self.cancelled = [], [], [], [], []
        self.fail = None
        self.enabled = True
        self.block = False
        self.deltas = False
    def available(self):
        return self.enabled
    def create(self, api_key, model, cwd):
        self.created.append((api_key, model, cwd))
        if self.fail:
            raise self.fail
        return AGENT_ID if len(self.created) == 1 else 'agent-localfixture%02d' % len(self.created)
    def send(self, api_key, model, cwd, agent_id, text):
        if self.fail:
            raise self.fail
        self.sent.append({'key': api_key, 'model': model, 'cwd': cwd, 'agent': agent_id, 'text': text})
        handle = Handle()
        self.handles.append(handle)
        run_id = 'run-local-%s' % len(self.sent)
        if self.block:
            self.block = False
            run = BlockingRun(run_id)
        elif self.deltas:
            self.deltas = False
            run = DeltaRun(run_id)
        else:
            run = Run(run_id, 'Final local reply %s' % len(self.sent))
        self.open_runs.append(run)
        return run, handle
    def cancel(self, api_key, run_id, agent_id):
        self.cancelled.append((run_id, agent_id))
        for run in self.open_runs:
            if run.id == run_id and hasattr(run, 'gate'):
                run.gate.set()


class LocalCursorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.path = self.root / 'config.json'
        initialize(self.path, 48421)
        self.config = load_config(self.path)
        self.config['run_id'] = 'local-cursor-fixture'
        self.config['run_dir'] = str(self.root)
        self.bridge = Bridge(self.config)
        self.runtime = FakeRuntime()
        self.bridge.connections.local_runtime = self.runtime
        self.bridge.commands.adapters['cursor_local'].runtime = self.runtime
        self.bridge.connections.transport = type('EmptyCloud', (), {'json': lambda *args, **kwargs: {'items': []}})()

    def tearDown(self):
        self.bridge.commands.close()
        self.bridge.history.close()
        self.temp.cleanup()

    def connect(self, **extra):
        data = {'name': 'Local Cursor', 'workspace': str(self.project), 'api_key': 'fixture-key-PRIVATE-987654'}
        data.update(extra)
        return self.bridge.connections.connect_cursor_local(data)

    def submit(self, agent, text):
        req = {'request_id': str(uuid.uuid4()), 'agent_id': agent, 'text': text}
        self.bridge.commands.create(req)
        return req

    def wait(self, request, states):
        end = time.monotonic() + 4
        row = None
        while time.monotonic() < end:
            self.bridge.commands.tick()
            row = next(r for r in self.bridge.commands.list(request['agent_id'])['commands'] if r['id'] == request['request_id'])
            if row['state'] in states:
                return row
            time.sleep(0.01)
        self.fail('fixture did not reach %r: %r' % (states, row))

    def test_create_then_two_sends_stay_on_the_same_local_session(self):
        result = self.connect()
        self.assertEqual(result['mode'], 'cursor_local')
        self.assertEqual(result['agent_id'], 'cursor:' + AGENT_ID)
        self.assertEqual(self.runtime.created[0][1], 'composer-2.5')
        self.assertEqual(Path(self.runtime.created[0][2]), self.project)
        cap = self.bridge.commands.capability(result['agent_id'])
        self.assertEqual(cap['mode'], 'cursor_local')
        self.assertTrue(cap['native_ready'])
        self.assertTrue(cap['same_session'])
        self.assertEqual(cap['public_sync']['scope'], 'office_dispatched_runs')
        floor = self.bridge.workspaces.for_agent(result['agent_id'])
        self.assertEqual(floor['mode'], 'manual')
        first = self.submit(result['agent_id'], 'Remember fixture 917')
        second = self.submit(result['agent_id'], 'Continue with previous fixture value')
        self.wait(first, {'completed'})
        self.wait(second, {'completed'})
        self.assertEqual([item['text'] for item in self.runtime.sent],
                         ['Remember fixture 917', 'Continue with previous fixture value'])
        self.assertTrue(all(item['agent'] == AGENT_ID for item in self.runtime.sent))
        self.assertTrue(all(handle.closed for handle in self.runtime.handles))
        rows = self.bridge.history.messages({result['agent_id']}, result['agent_id'])
        public = '\n'.join(row['text'] for row in rows)
        for secret in ('PRIVATE_REASONING', 'RAW_TOOL', '[OFFICE:', 'fixture-key-PRIVATE'):
            self.assertNotIn(secret, public)
        finals = [row['text'] for row in rows if row['phase'] == 'final']
        self.assertEqual(finals, ['Final local reply 1', 'Final local reply 2'])
        self.assertEqual(self.bridge.sources['Local Cursor']['status'], 'idle')
        self.assertNotIn('fixture-key-PRIVATE', self.path.read_text())
        saved = load_config(self.path)
        item = saved['command_adapters']['cursor_local']['sessions'][AGENT_ID]
        self.assertEqual(Path(item['cwd']), self.project)
        self.assertTrue(Path(item['credential_file']).is_file())
        if os.name != 'nt':
            self.assertEqual(Path(item['credential_file']).stat().st_mode & 0o777, 0o600)

    def test_missing_folder_or_key_does_not_create_a_session(self):
        with self.assertRaises(ConnectionError) as missing:
            self.connect(workspace=str(self.root / 'missing'))
        self.assertEqual(missing.exception.code, 'invalid_workspace')
        with self.assertRaises(ConnectionError) as key:
            self.connect(api_key='')
        self.assertEqual(key.exception.code, 'cursor_connection_details_required')
        self.runtime.enabled = False
        with self.assertRaises(ConnectionError) as sdk:
            self.connect(name='Other Cursor')
        self.assertEqual(sdk.exception.code, 'cursor_sdk_required')
        self.assertEqual(self.runtime.created, [])

    def test_start_failure_is_not_retried_and_restart_does_not_resend(self):
        result = self.connect()
        self.runtime.fail = LocalStartError('cursor_local_start_failed', uncertain=False)
        failed = self.submit(result['agent_id'], 'Do not start')
        row = self.wait(failed, {'failed'})
        self.assertEqual(row['detail_code'], 'cursor_local_start_failed')
        self.runtime.fail = None
        agent = result['agent_id']
        at = timestamp()
        self.bridge.commands.db.execute(
            'INSERT INTO commands VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('11111111-1111-4111-8111-111111111111', agent, AGENT_ID, 'cursor_local', 'fp', 'preview',
             'payload', 'running', 'run-local-open', None, at, at, None))
        self.bridge.commands.db.commit()
        from commands import Commands
        revived = Commands(load_config(self.path), self.bridge.history)
        try:
            kept = revived.db.execute(
                "SELECT state, detail_code FROM commands WHERE id='11111111-1111-4111-8111-111111111111'").fetchone()
            self.assertEqual(kept['state'], 'delivery_unknown')
            self.assertEqual(kept['detail_code'], 'interrupted_delivery')
            revived.tick()
            self.assertEqual(self.runtime.sent, [])
        finally:
            revived.close()

    def test_unknown_earlier_delivery_does_not_trap_the_next_local_message(self):
        result = self.connect()
        agent = result['agent_id']
        at = timestamp()
        self.bridge.commands.db.execute(
            'INSERT INTO commands VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('22222222-2222-4222-8222-222222222222', agent, AGENT_ID, 'cursor_local', 'fp', 'old',
             None, 'delivery_unknown', 'run-local-old', None, at, at, 'cursor_observation_interrupted'))
        self.bridge.commands.db.commit()
        nxt = self.submit(agent, 'Say hello only')
        self.wait(nxt, {'completed'})
        old = self.bridge.commands.db.execute(
            "SELECT state FROM commands WHERE id='22222222-2222-4222-8222-222222222222'").fetchone()
        self.assertEqual(old['state'], 'delivery_unknown')
        self.assertEqual([item['text'] for item in self.runtime.sent], ['Say hello only'])

    def test_saved_key_opens_another_conversation_and_archive_can_return(self):
        first = self.connect()
        home = self.bridge.connections.cursor_local_home()
        self.assertTrue(home['has_key'])
        self.assertEqual(Path(home['workspace']), self.project)
        second = self.bridge.connections.connect_cursor_local({'workspace': str(self.project)})
        self.assertEqual(second['name'], '新對話')
        self.assertNotEqual(second['agent_id'], first['agent_id'])
        self.assertEqual(self.runtime.created[1][0], 'fixture-key-PRIVATE-987654')
        self.bridge.connections.connect_cursor_local({'action': 'archive', 'agent_id': second['agent_id']})
        visible = {agent['id'] for agent in self.bridge.work_snapshot()['agents']}
        self.assertNotIn(second['agent_id'], visible)
        self.assertIn(first['agent_id'], visible)
        self.bridge.connections.connect_cursor_local({'action': 'reopen', 'agent_id': second['agent_id']})
        visible = {agent['id'] for agent in self.bridge.work_snapshot()['agents']}
        self.assertIn(second['agent_id'], visible)

    def test_new_folder_is_created_only_when_asked(self):
        parent = self.root / 'fresh'
        parent.mkdir()
        target = parent / 'job'
        self.connect(name='Folder Cursor', workspace=str(target), create_folder=True)
        self.assertTrue(target.is_dir())
        with self.assertRaises(ConnectionError) as missing:
            self.connect(name='Missing parent', workspace=str(self.root / 'absent' / 'job'), create_folder=True)
        self.assertEqual(missing.exception.code, 'invalid_workspace')

    def test_spawned_subagent_shows_beside_the_parent_and_then_leaves(self):
        result = self.connect()
        adapter = self.bridge.commands.adapters['cursor_local']
        adapter.note_subagent(AGENT_ID, 'office-command', 'call01', 'running')
        shown = [agent for agent in self.bridge.work_snapshot()['agents'] if agent.get('is_subagent')]
        self.assertEqual([agent['state'] for agent in shown], ['working'])
        self.assertEqual(shown[0]['root_agent_id'], result['agent_id'])
        adapter.note_subagent(AGENT_ID, 'office-command', 'call01', 'completed')
        shown = [agent for agent in self.bridge.work_snapshot()['agents'] if agent.get('is_subagent')]
        self.assertEqual(shown[0]['state'], 'done')
        adapter.subagents[shown[0]['id']]['terminal_at'] = time.time() - 31
        shown = [agent for agent in self.bridge.work_snapshot()['agents'] if agent.get('is_subagent')]
        self.assertEqual(shown, [])

    def test_second_message_waits_until_another_enter_interrupts(self):
        result = self.connect()
        self.runtime.block = True
        first = self.submit(result['agent_id'], 'Keep going')
        self.wait(first, {'queued', 'running'})
        self.assertFalse(self.bridge.commands.interrupt_active(result['agent_id'])['interrupted'])
        self.assertEqual(self.runtime.cancelled, [])
        second = self.submit(result['agent_id'], 'Stop and do this instead')
        held = self.wait(first, {'queued', 'running'})
        self.assertIn(held['state'], {'queued', 'running'})
        queued = next(row for row in self.bridge.commands.list(result['agent_id'])['commands'] if row['id'] == second['request_id'])
        self.assertEqual(queued['state'], 'pending')
        self.assertEqual(self.runtime.cancelled, [])
        self.assertTrue(self.bridge.commands.interrupt_active(result['agent_id'])['interrupted'])
        self.wait(first, {'cancelled'})
        self.wait(second, {'completed'})
        self.assertEqual(self.runtime.cancelled[0][1], AGENT_ID)
        self.assertEqual([item['text'] for item in self.runtime.sent], ['Keep going', 'Stop and do this instead'])
        cancelled = next(row for row in self.bridge.commands.list(result['agent_id'])['commands'] if row['id'] == first['request_id'])
        self.assertEqual(cancelled['detail_code'], 'cursor_local_interrupted')

    def test_growing_reply_is_saved_before_a_result_exists(self):
        result = self.connect()
        self.runtime.deltas = True
        sent = self.submit(result['agent_id'], 'Explain the tests')
        self.wait(sent, {'completed'})
        rows = self.bridge.history.messages({result['agent_id']}, result['agent_id'])
        self.assertIn('正在查看專案。完整回覆在這裡。', '\n'.join(row['text'] for row in rows))

    def test_rename_changes_only_the_office_display_name(self):
        first = self.connect()
        second = self.bridge.connections.connect_cursor_local({'workspace': str(self.project)})
        self.bridge.sources['Local Cursor']['status'] = 'working'
        renamed = self.bridge.connections.rename_agent({'agent_id': first['agent_id'], 'name': '專案助手'})
        self.assertEqual(renamed['name'], '專案助手')
        self.assertEqual(self.bridge.agents[first['agent_id']]['name'], '專案助手')
        self.assertEqual(self.bridge.sources['專案助手']['status'], 'working')
        self.assertNotIn('Local Cursor', self.bridge.sources)
        with self.assertRaises(ConnectionError) as used:
            self.bridge.connections.rename_agent({'agent_id': first['agent_id'], 'name': second['name']})
        self.assertEqual(used.exception.code, 'name_in_use')


class ActiveRunTests(unittest.TestCase):
    def test_only_the_sdk_active_run_error_is_released(self):
        class Marker(Exception):
            def __init__(self, code, message):
                self.code = code
                super().__init__(message)
        self.assertTrue(active_run_error(Marker('internal', 'Agent x already has active run')))
        self.assertFalse(active_run_error(Marker('unauthenticated', 'Invalid User API Key')))
        self.assertFalse(active_run_error(Marker('internal', 'bridge closed')))


class WindowsBridgeReaderTests(unittest.TestCase):
    def test_windows_bridge_pipe_is_not_selected_as_a_socket(self):
        install_windows_bridge_reader()
        if os.name != 'nt':
            return
        import cursor_sdk._bridge as bridge
        self.assertTrue(bridge._read_discovery._office_windows)
        install_windows_bridge_reader()
        self.assertTrue(bridge._read_discovery._office_windows)


if __name__ == '__main__':
    unittest.main()
