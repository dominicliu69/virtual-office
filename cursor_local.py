"""Local Cursor session via the official Python SDK.

Office creates one local agent for a workspace, then later turns resume that
same agent and send the owner's text. This is not the IDE sidebar chat, and it
does not use a webhook or a Cloud Agent.
"""
import codecs
import os
import re
import threading
import time
from pathlib import Path

from command_adapters import DeliveryError
from platform_adapters import MARKER, secret_file, validate_key

LOCAL_AGENT = re.compile(r'agent-[A-Za-z0-9_.-]{1,140}\Z')
LOCAL_RUN = re.compile(r'[A-Za-z0-9_.-]{1,160}\Z')
MODEL = re.compile(r'[A-Za-z0-9._-]{1,64}\Z')
DEFAULT_MODEL = 'composer-2.5'
SUBAGENT_TOOLS = frozenset({'agent', 'task', 'subagent'})
SUBAGENT_RETAIN_SECONDS = 30


class LocalStartError(Exception):
    def __init__(self, code, uncertain=False):
        self.code, self.uncertain = code, uncertain
        super().__init__(code)


def active_run_error(error):
    return getattr(error, 'code', None) == 'internal' and 'already has active run' in str(error)


def release_active_runs(agent_id, cwd, api_key):
    """Drop a run CreateAgent left running before any office text was sent."""
    from cursor_sdk import Agent, CursorAgentError
    try:
        listed = Agent.list_runs(agent_id, {'cwd': cwd, 'apiKey': api_key, 'runtime': 'local'})
    except CursorAgentError:
        return False
    released = False
    for run in getattr(listed, 'items', ()):
        if getattr(run, 'status', None) != 'running':
            continue
        run_id = getattr(run, 'id', None)
        if not isinstance(run_id, str) or not run_id:
            continue
        try:
            Agent.cancel_run(run_id, agent_id=agent_id)
            released = True
        except CursorAgentError:
            continue
    return released


def install_windows_bridge_reader():
    """The SDK waits for its bridge on a socket selector.

    Windows can only select sockets, so the bridge pipe raises WinError 10038
    before a local agent exists. Read that pipe without a selector instead.
    """
    if os.name != 'nt':
        return
    import cursor_sdk._bridge as bridge
    current = bridge._read_discovery
    if getattr(current, '_office_windows', False):
        return

    def read_discovery(process, timeout):
        if process.stderr is None:
            raise bridge.CursorSDKError('Bridge process stderr is unavailable')
        stderr_fd = process.stderr.fileno()
        was_blocking = os.get_blocking(stderr_fd)
        os.set_blocking(stderr_fd, False)
        try:
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            deadline = time.monotonic() + timeout
            stderr_lines = []
            pending = ''

            def drain():
                nonlocal pending
                while True:
                    try:
                        chunk = os.read(stderr_fd, 8192)
                    except BlockingIOError:
                        return None
                    if not chunk:
                        final_text = decoder.decode(b'', final=True)
                        if final_text:
                            pending += final_text
                        if pending:
                            line = pending
                            pending = ''
                            stderr_lines.append(line)
                            return bridge.parse_discovery_line(line)
                        return None
                    pending += decoder.decode(chunk)
                    while '\n' in pending:
                        line, pending = pending.split('\n', 1)
                        line += '\n'
                        stderr_lines.append(line)
                        discovery = bridge.parse_discovery_line(line)
                        if discovery is not None:
                            return discovery

            while time.monotonic() < deadline:
                discovery = drain()
                if discovery is not None:
                    return discovery
                exit_code = process.poll()
                if exit_code is not None:
                    discovery = drain()
                    if discovery is not None:
                        return discovery
                    raise bridge.CursorSDKError(
                        'Bridge exited before discovery with status %s: %s%s' % (
                            exit_code, ''.join(stderr_lines), pending))
                time.sleep(0.05)
            raise bridge.CursorSDKError('Timed out waiting for bridge discovery')
        finally:
            os.set_blocking(stderr_fd, was_blocking)

    read_discovery._office_windows = True
    bridge._read_discovery = read_discovery


def local_agent_id(value):
    if not isinstance(value, str) or not LOCAL_AGENT.fullmatch(value):
        raise ValueError('cursor_local_agent_invalid')
    return value


def merge_text(text, piece):
    """Keep a growing reply without saving the same sentence twice."""
    piece = piece if isinstance(piece, str) else ''
    if not piece:
        return text
    if not text or piece.startswith(text) or text in piece:
        return piece[:256000]
    if piece in text:
        return text
    return (text + piece)[:256000]


def stream_items(run):
    events = getattr(run, '_events', None)
    if callable(events):
        yield from events()
        return
    yield from run.messages()


def visible_text(item):
    update = getattr(item, 'interaction_update', None)
    if getattr(update, 'type', None) == 'text-delta':
        text = getattr(update, 'text', '')
        return text if isinstance(text, str) else ''
    message = getattr(item, 'sdk_message', None)
    return public_piece(message if message is not None else item)


def public_piece(message):
    """Assistant text only. Thinking and tool arguments stay out of Office."""
    kind = message.get('type') if isinstance(message, dict) else getattr(message, 'type', None)
    if kind == 'tool_call':
        name = message.get('name') if isinstance(message, dict) else getattr(message, 'name', None)
        call = message.get('call_id') if isinstance(message, dict) else getattr(message, 'call_id', None)
        status = message.get('status') if isinstance(message, dict) else getattr(message, 'status', None)
        if (isinstance(name, str) and name.lower() in SUBAGENT_TOOLS and isinstance(call, str)
                and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', call) and status in {None, 'running', 'completed', 'error'}):
            return {'subagent': True, 'call_id': call, 'status': status or 'running'}
        return 'tool'
    if kind != 'assistant':
        return None
    if isinstance(message, dict):
        content = (message.get('message') or {}).get('content')
    else:
        content = getattr(getattr(message, 'message', None), 'content', None)
    if not content:
        return ''
    parts = []
    for block in content:
        btype = block.get('type') if isinstance(block, dict) else getattr(block, 'type', None)
        btext = block.get('text') if isinstance(block, dict) else getattr(block, 'text', None)
        if btype == 'text' and isinstance(btext, str):
            parts.append(btext)
    return ''.join(parts)


class SdkRuntime:
    """Thin cursor-sdk wrapper. Imported only when a local session is used."""

    def available(self):
        try:
            import cursor_sdk  # noqa: F401
            return True
        except ImportError:
            return False

    def create(self, api_key, model, cwd):
        from cursor_sdk import Agent, CursorAgentError, LocalAgentOptions
        install_windows_bridge_reader()
        try:
            agent = Agent.create(model=model, api_key=api_key, local=LocalAgentOptions(cwd=cwd))
        except CursorAgentError as error:
            rejected = getattr(error, 'status', None) == 401 or getattr(error, 'code', None) == 'unauthenticated'
            raise LocalStartError('platform_auth_required' if rejected else 'cursor_local_start_failed') from error
        try:
            release_active_runs(agent.agent_id, cwd, api_key)
            return local_agent_id(agent.agent_id)
        finally:
            agent.close()

    def cancel(self, api_key, run_id, agent_id):
        from cursor_sdk import Agent
        install_windows_bridge_reader()
        Agent.cancel_run(run_id, agent_id=agent_id)

    def send(self, api_key, model, cwd, agent_id, text):
        from cursor_sdk import Agent, AgentOptions, CursorAgentError, LocalAgentOptions
        install_windows_bridge_reader()
        try:
            agent = Agent.resume(agent_id, AgentOptions(
                api_key=api_key, model=model, local=LocalAgentOptions(cwd=cwd)))
            try:
                run = agent.send(text)
            except CursorAgentError as error:
                if not active_run_error(error):
                    raise
                release_active_runs(agent_id, cwd, api_key)
                run = agent.send(text)
        except CursorAgentError as error:
            if 'agent' in locals():
                agent.close()
            raise LocalStartError('cursor_local_start_failed', uncertain=False) from error
        except Exception as error:
            if 'agent' in locals():
                agent.close()
            raise LocalStartError('cursor_local_delivery_unknown', uncertain=True) from error
        return run, agent


class CursorLocalAdapter:
    mode, serial, same_session = 'cursor_local', True, True

    def __init__(self, config, emit, runtime=None):
        self.config, self.emit = config, emit
        self.sessions = config.get('command_adapters', {}).get('cursor_local', {}).get('sessions', {})
        self.runtime = runtime or SdkRuntime()
        self.reason = 'cursor_api_key_required'
        self.lock, self.workers = threading.RLock(), {}
        self.live_native, self.interrupts = {}, set()
        self.subagents = {}
        self.stop = threading.Event()

    def session(self, sid):
        local_agent_id(sid)
        item = self.sessions[sid]
        key = validate_key(secret_file(item['credential_file']).get('api_key'))
        cwd = item.get('cwd')
        model = item.get('model') or DEFAULT_MODEL
        if not isinstance(cwd, str) or not Path(cwd).is_dir():
            raise ValueError('cursor_workspace_unavailable')
        if not isinstance(model, str) or not MODEL.fullmatch(model):
            raise ValueError('cursor_model_invalid')
        return key, model, str(Path(cwd))

    def available(self, sid):
        try:
            self.session(sid)
        except (ValueError, OSError, KeyError, TypeError) as error:
            code = error.args[0] if error.args else ''
            self.reason = code if isinstance(code, str) and (
                code.startswith('cursor_') or code.startswith('platform_') or code.startswith('invalid_')) else 'cursor_api_key_required'
            return False
        if not self.runtime.available():
            self.reason = 'cursor_sdk_required'
            return False
        return not self.stop.is_set()

    def can_dispatch(self, sid):
        with self.lock:
            return not self.stop.is_set() and not (sid in self.workers and self.workers[sid].is_alive())

    def interrupt(self, sid):
        """Stop the local run so the next Enter does not wait for it to finish."""
        local_agent_id(sid)
        with self.lock:
            self.interrupts.add(sid)
            native = self.live_native.get(sid)
        if not native:
            return False
        try:
            key, _model, _cwd = self.session(sid)
            self.runtime.cancel(key, native, sid)
        except (ValueError, OSError, KeyError, TypeError, LocalStartError):
            pass
        return True

    def event(self, sid, command, native, kind, **fields):
        self.emit('cursor:' + sid, {'type': kind, 'adapter': self.mode, 'session_id': sid,
            'run_id': command, 'native_run_id': native, **fields})

    def submit(self, sid, text):
        match = MARKER.match(text)
        if not match or not self.available(sid):
            raise DeliveryError(self.reason if not self.available(sid) else 'cursor_api_key_required')
        if not self.can_dispatch(sid):
            raise DeliveryError('platform_agent_busy')
        body = text[match.end():].strip()
        if not body:
            raise DeliveryError('invalid_command')
        command = match[1]
        try:
            key, model, cwd = self.session(sid)
            run, agent = self.runtime.send(key, model, cwd, sid, body)
        except LocalStartError as error:
            raise DeliveryError(error.code, error.uncertain)
        native = getattr(run, 'id', None)
        try:
            if not isinstance(native, str) or not LOCAL_RUN.fullmatch(native):
                raise DeliveryError('cursor_invalid_run_receipt', True)
            self.event(sid, command, native, 'accepted')
            with self.lock:
                self.live_native[sid] = native
                self.interrupts.discard(sid)
            worker = threading.Thread(target=self.observe, args=(sid, command, native, text, run, agent), daemon=True)
            with self.lock:
                self.workers[sid] = worker
            worker.start()
        except Exception:
            agent.close()
            raise
        return {'native_message_id': native, 'delivery_mode': self.mode}

    def observe(self, sid, command, native, prompt, run, agent):
        started, text, segment = False, '', 0

        def begin():
            nonlocal started
            if not started:
                self.event(sid, command, native, 'started', text=prompt)
                started = True

        def flush(final=False):
            if text:
                self.event(sid, command, native, 'text', text=text, segment=segment, final=final)

        try:
            for item in stream_items(run):
                if self.stop.is_set() or sid in self.interrupts:
                    raise LocalStartError('cursor_local_interrupted')
                piece = visible_text(item)
                if isinstance(piece, dict) and piece.get('subagent'):
                    begin()
                    flush()
                    if text:
                        segment += 1
                        text = ''
                    self.note_subagent(sid, command, piece['call_id'], piece['status'])
                    self.event(sid, command, native, 'status', step='子 Agent 正在工作' if piece['status'] == 'running' else '子 Agent 已結束')
                elif piece == 'tool':
                    begin()
                    flush()
                    if text:
                        segment += 1
                        text = ''
                    self.event(sid, command, native, 'status', step='Cursor 正在執行工具')
                elif isinstance(piece, str) and piece:
                    begin()
                    text = merge_text(text, piece)
                    flush()
            if sid in self.interrupts:
                raise LocalStartError('cursor_local_interrupted')
            result = run.wait()
            if sid in self.interrupts or getattr(result, 'status', None) == 'cancelled':
                raise LocalStartError('cursor_local_interrupted')
            state = {'finished': 'completed', 'error': 'failed', 'cancelled': 'cancelled', 'expired': 'failed'}.get(
                getattr(result, 'status', None))
            if not state:
                raise LocalStartError('cursor_observation_interrupted')
            final = result.result if isinstance(getattr(result, 'result', None), str) else ''
            begin()
            text = final
            flush(True)
            self.event(sid, command, native, 'finished', state=state, text=text,
                detail_code=None if text or state != 'completed' else 'completed_without_reply')
        except Exception as error:
            stopped = sid in self.interrupts or getattr(error, 'code', None) == 'cursor_local_interrupted'
            if stopped:
                begin()
                self.event(sid, command, native, 'finished', state='cancelled', text=text,
                    detail_code='cursor_local_interrupted')
            else:
                self.event(sid, command, native, 'interrupted', state='delivery_unknown',
                    detail_code=error.code if isinstance(error, LocalStartError) else 'cursor_observation_interrupted')
        finally:
            agent.close()
            with self.lock:
                if self.workers.get(sid) is threading.current_thread():
                    self.workers.pop(sid, None)
                    if self.live_native.get(sid) == native:
                        self.live_native.pop(sid, None)
                    self.interrupts.discard(sid)

    def note_subagent(self, sid, parent_run, call_id, status):
        import time
        parent = 'cursor:' + sid
        ident = 'cursor:' + sid + ':sub:' + call_id
        state = {'running': 'working', 'completed': 'done', 'error': 'error'}.get(status, 'working')
        with self.lock:
            current = self.subagents.get(ident, {})
            self.subagents[ident] = {
                'id': ident, 'name': '子 Agent ' + call_id[:8], 'parent': parent,
                'parent_run_id': parent_run, 'status': state,
                'terminal_at': current.get('terminal_at') if state == 'working' else time.time(),
            }

    def subagent_snapshot(self, roots):
        import time
        now = time.time()
        with self.lock:
            rows = list(self.subagents.values())
        result = []
        for row in rows:
            if row['status'] in {'done', 'error'} and row.get('terminal_at') and now - row['terminal_at'] > SUBAGENT_RETAIN_SECONDS:
                continue
            root = roots.get(row['parent'])
            if not root:
                continue
            result.append({
                'id': row['id'], 'name': row['name'], 'provider': 'cursor', 'native_url': None,
                'is_subagent': True, 'parent_agent_id': row['parent'], 'root_agent_id': row['parent'],
                'depth': 1, 'parent_run_id': row['parent_run_id'], 'source_kind': 'cursor_sdk',
                'evidence': 'cursor_sdk', 'state': row['status'],
                'step': '子 Agent 正在工作' if row['status'] == 'working' else '子 Agent 已結束',
                'state_changed_at': None, 'latest_message': None, 'workspace': root.get('workspace'),
                'ground_desk_id': None,
                'command_capability': {'can_send': False, 'enabled': False, 'mode': 'observation', 'reason': 'observed_subagent'},
            })
        return result

    def close(self):
        self.stop.set()
        for worker in list(self.workers.values()):
            if worker.is_alive():
                worker.join(timeout=16)
