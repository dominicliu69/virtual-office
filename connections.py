"""Owner-facing portable setup. Secrets stay in this installation's private files."""
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
import threading
import urllib.parse
import uuid
from configuration import agent_name, codex_targets, load_config, save_config
from hermes_acp import private_json
from cursor_local import DEFAULT_MODEL, LocalStartError, SdkRuntime, local_agent_id
from platform_adapters import HTTPS, RemoteError, CURSOR_RUN, cursor_agent_id, cursor_headers, secret_file, validate_key, webhook_url
from history import redact, timestamp


class ConnectionError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


class Connections:
    def __init__(self, bridge, transport=None, local_runtime=None):
        self.bridge, self.transport = bridge, transport or HTTPS()
        self.local_runtime = local_runtime or SdkRuntime()
        self.root = Path(bridge.config['data_dir']) / 'platform-secrets'
        self._cloud_sync = threading.Lock()

    def status(self):
        cursor_sessions=self.bridge.config.get('command_adapters',{}).get('cursor_cloud',{}).get('sessions',{})
        saved=(self.root / 'cursor.json').is_file() or any(Path(value['credential_file']).is_file() for value in cursor_sessions.values())
        return {'cursor_key_saved':saved,
                'cursor_desktop_cloud':True, 'cursor_desktop_local_commands':False,
                'cursor_local':True,
                'grok_webhook':True, 'grok_reply_requires_local_execution':True}

    def save(self, config):
        path = Path(config['config_path'])
        check = path.with_name('platform-check-' + uuid.uuid4().hex + '.json')
        try:
            save_config(check, config); load_config(check)
        finally:
            check.unlink(missing_ok=True)
        save_config(path, config)
        self.bridge.refresh_connections(config)

    def connect_agent(self, agent_id, data):
        """An enrolled agent may attach only its own supported native route."""
        if not isinstance(data,dict):raise ConnectionError('invalid_connection')
        if data.get('adapter')=='codex_app' and set(data)=={'adapter','session_id','workspace_path'}:
            result=self.connect_codex(agent_id,data['session_id'],data['workspace_path'])
        elif data.get('adapter')=='grok_webhook' and set(data)=={'adapter','url','key'}:
            with self.bridge.commands.lock,self.bridge.history.lock:
                existing=self.bridge.commands.bindings.get(agent_id)
                if existing:
                    if existing['adapter']!='grok_webhook':raise ConnectionError('native_binding_conflict',409)
                    settings=self.bridge.config.get('command_adapters',{}).get('grok_webhook',{}).get('sessions',{}).get(existing['session_id'],{})
                    previous=secret_file(settings['credential_file'])
                    if not isinstance(data['url'],str) or not isinstance(data['key'],str) or not hmac.compare_digest(previous['url'],data['url']) or not hmac.compare_digest(previous['key'],data['key']):
                        raise ConnectionError('native_binding_conflict',409)
                result=self.connect_grok({'agent_id':agent_id,'url':data['url'],'key':data['key']})
        else:raise ConnectionError('native_setup_required',409)
        result['conversation']=self.bridge.inbox.ensure(agent_id)
        return result

    def connect_codex(self, agent_id, session_id, workspace):
        try:
            if str(uuid.UUID(session_id))!=session_id or not isinstance(workspace,str) or not Path(workspace).is_absolute():raise ValueError()
        except (ValueError,TypeError,AttributeError):raise ConnectionError('invalid_native_session')
        adapter=self.bridge.commands.adapters.get('codex_app')
        if not adapter or not adapter.available():raise ConnectionError('codex_queue_unavailable',409)
        with self.bridge.commands.lock,self.bridge.history.lock:
            config=load_config(self.bridge.config['config_path'])
            agent=next((a for a in config['agents'] if a['id']==agent_id and a['provider']=='codex'),None)
            if not agent:raise ConnectionError('agent_not_selected',404)
            if agent_id in config.get('disabled_command_agents',[]):raise ConnectionError('dispatch_disabled',409)
            binding={'adapter':'codex_app','session_id':session_id}
            previous=config.get('command_bindings',{}).get(agent_id)
            if previous and previous!=binding:raise ConnectionError('native_binding_conflict',409)
            other=codex_targets(config).get(session_id)
            if other and other['id']!=agent_id:raise ConnectionError('native_session_already_selected',409)
            expected=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))/'state_5.sqlite'
            if Path(config['codex_db']).resolve()!=expected.resolve():raise ConnectionError('codex_home_mismatch',409)
            try:
                with sqlite3.connect(Path(config['codex_db']).resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as db:
                    row=db.execute('SELECT rollout_path,cwd,archived FROM threads WHERE id=?',(session_id,)).fetchone()
                if not row or row[2] or not row[1]:raise ValueError()
                native_cwd=Path(row[1]).resolve();actual=Path(workspace).resolve()
                if actual!=native_cwd and native_cwd not in actual.parents:raise ValueError()
                # Verify the database points to this exact original session.
                path=Path(row[0])
                if path.is_symlink():raise ValueError()
                with path.open('rb') as source:metadata=json.loads(source.readline(1024*1024))
                if metadata.get('type')!='session_meta' or metadata.get('payload',{}).get('id')!=session_id:raise ValueError()
            except (OSError,sqlite3.Error,ValueError,TypeError):raise ConnectionError('native_session_not_verified',409)
            if not previous:
                active=self.bridge.commands.db.execute("SELECT 1 FROM commands WHERE agent_id=? AND (state IN ('sending','queued','running','waiting_user','delivery_unknown') OR (state='pending' AND (native_message_id IS NOT NULL OR run_id IS NOT NULL))) LIMIT 1",(agent_id,)).fetchone()
                if active:raise ConnectionError('connection_has_active_commands',409)
            config.setdefault('command_bindings',{})[agent_id]=binding
            config.setdefault('command_adapters',{}).setdefault('codex_app',{})
            if agent['driver']=='push':agent.setdefault('native_observe_after',timestamp())
            self.save(config)
        return {'connected':True,'agent_id':agent_id,'mode':'background_queue','same_session':True,
                'persistent':True,'native_target_verified':True,'execution_verified':False}

    def connect_cursor(self, data):
        if not isinstance(data, dict) or set(data)-{'name','session_id','api_key'} or not {'name','session_id'} <= set(data):
            raise ConnectionError('invalid_connection')
        try:
            name, sid = agent_name(data['name']), cursor_agent_id(data['session_id'])
        except (ValueError, TypeError):
            raise ConnectionError('cursor_connection_details_required')
        ident = 'cursor:' + sid
        with self.bridge.commands.lock, self.bridge.history.lock:
            config=load_config(self.bridge.config['config_path'])
            self._cursor_target(config,ident,name)
            membership=self._cursor_membership(config,ident,sid)
            settings=config.get('command_adapters',{}).get('cursor_cloud',{}).get('sessions',{}).get(sid,{})
            try:
                # Read an existing target's key first. The shared legacy file
                # is a fallback for old installations, never a write target.
                key=data.get('api_key') or secret_file(settings.get('credential_file') or str(self.root/'cursor.json')).get('api_key')
                validate_key(key)
            except (ValueError,OSError,KeyError,TypeError):
                raise ConnectionError('cursor_connection_details_required')
            self._cursor_change_allowed(config,ident,sid,key)
        try:
            native = self.transport.json('GET', 'https://api.cursor.com/v1/agents/' + sid, cursor_headers(key))
        except RemoteError as error:
            raise ConnectionError(error.code, 409)
        if native.get('id') != sid or native.get('status') not in {'ACTIVE','IDLE'}:
            raise ConnectionError('cursor_agent_not_available', 409)
        with self.bridge.commands.lock, self.bridge.history.lock:
            config = load_config(self.bridge.config['config_path'])
            if membership!=self._cursor_membership(config,ident,sid):
                raise ConnectionError('agent_membership_changed',409)
            agent=self._cursor_target(config,ident,name)
            unchanged=self._cursor_change_allowed(config,ident,sid,key)
            if unchanged:
                return {'connected':True,'agent_id':ident,'mode':'cursor_cloud','same_session':True,
                        'native_target_verified':True,'execution_verified':False}
            if not agent:
                config['agents'].append({'id':ident,'source_id':sid,'name':name,'provider':'cursor','driver':'cursor'})
            # New credentials get a target-scoped file. A failed config save
            # cannot rotate a currently selected Agent's working credential,
            # and another legacy binding can keep using its old shared file.
            credential=self.root/('cursor-'+hashlib.sha256(sid.encode()).hexdigest()[:24]+'-'+uuid.uuid4().hex[:12]+'.json')
            config.setdefault('command_bindings',{})[ident]={'adapter':'cursor_cloud','session_id':sid}
            config.setdefault('command_adapters',{}).setdefault('cursor_cloud',{}).setdefault('sessions',{})[sid]={'credential_file':str(credential)}
            private_json(credential, {'api_key':key})
            self.bridge.history.secrets = (*self.bridge.history.secrets, key)
            self.save(config)
        return {'connected':True,'agent_id':ident,'mode':'cursor_cloud','same_session':True,
                'native_target_verified':True,'execution_verified':False}

    def _saved_cursor(self):
        path = self.root / 'cursor.json'
        if not path.is_file():
            return {}
        try:
            saved = secret_file(str(path))
        except (ValueError, OSError, KeyError, TypeError):
            return {}
        return saved if isinstance(saved, dict) else {}

    def _cursor_api_key(self):
        saved = self._saved_cursor().get('api_key')
        if isinstance(saved, str):
            try:
                return validate_key(saved)
            except ValueError:
                pass
        config = self.bridge.config
        for adapter in ('cursor_local', 'cursor_cloud'):
            sessions = config.get('command_adapters', {}).get(adapter, {}).get('sessions', {})
            if not isinstance(sessions, dict):
                continue
            for item in sessions.values():
                if not isinstance(item, dict):
                    continue
                try:
                    return validate_key(secret_file(item.get('credential_file')).get('api_key'))
                except (ValueError, OSError, KeyError, TypeError):
                    continue
        return None

    def _list_unarchived_cloud(self, key):
        headers = cursor_headers(key)
        found, cursor, seen = [], None, set()
        for _ in range(10):
            query = {'includeArchived': 'false', 'limit': '100'}
            if cursor:
                if cursor in seen:
                    break
                seen.add(cursor)
                query['cursor'] = cursor
            url = 'https://api.cursor.com/v1/agents?' + urllib.parse.urlencode(query)
            page = self.transport.json('GET', url, headers)
            items = page.get('items') if isinstance(page, dict) else None
            if not isinstance(items, list):
                raise RemoteError('platform_invalid_response')
            found.extend(items)
            nxt = page.get('nextCursor') if isinstance(page, dict) else None
            if not isinstance(nxt, str) or not nxt:
                break
            cursor = nxt
        return found

    def _cloud_display_name(self, raw, sid, taken):
        try:
            base = agent_name(raw) if isinstance(raw, str) and raw.strip() else 'Cloud Agent'
        except (ValueError, TypeError):
            base = 'Cloud Agent'
        if base not in taken:
            taken.add(base)
            return base
        number = 2
        while number < 1000:
            try:
                candidate = agent_name(base + ' ' + str(number))
            except ValueError:
                candidate = 'Cloud ' + sid[-8:]
            if candidate not in taken:
                taken.add(candidate)
                return candidate
            number += 1
        fallback = 'Cloud ' + sid[-12:]
        taken.add(fallback)
        return fallback

    def sync_cursor_cloud(self):
        """Place unarchived Cloud Agents on the floor. A failed list changes nothing."""
        with self._cloud_sync:
            key = self._cursor_api_key()
            if not key:
                return {'synced': False, 'reason': 'cursor_connection_details_required'}
            try:
                items = self._list_unarchived_cloud(key)
            except RemoteError:
                return {'synced': False, 'reason': 'platform_read_failed'}
            remote = {}
            for item in items:
                if not isinstance(item, dict) or item.get('status') == 'ARCHIVED':
                    continue
                try:
                    sid = cursor_agent_id(item.get('id'))
                except (ValueError, TypeError):
                    continue
                remote.setdefault(sid, item)
            with self.bridge.commands.lock, self.bridge.history.lock:
                config = load_config(self.bridge.config['config_path'])
                retired = set(config.get('logged_out_agent_ids', []))
                known = {agent['source_id'] for agent in config['agents'] if agent['provider'] == 'cursor'}
                added = []
                for sid, item in remote.items():
                    ident = 'cursor:' + sid
                    if ident in retired or sid in known:
                        continue
                    self._enroll_cloud(config, sid, item.get('name'), key)
                    added.append(ident)
                    known.add(sid)
                away = sorted({
                    agent['id'] for agent in config['agents']
                    if config.get('command_bindings', {}).get(agent['id']) == {'adapter': 'cursor_cloud', 'session_id': agent['source_id']}
                    and agent['source_id'] not in remote})
                previous = list(config.get('cursor_cloud_away_ids', []))
                if away:
                    config['cursor_cloud_away_ids'] = away
                else:
                    config.pop('cursor_cloud_away_ids', None)
                if added or previous != away:
                    self.save(config)
                self._cloud_floor(config, remote)
            return {'synced': True, 'added': added, 'away': away}

    def _enroll_cloud(self, config, sid, raw_name, key):
        """Add one Cloud Agent that already exists. The caller holds both locks."""
        ident = 'cursor:' + sid
        if any(agent['source_id'] == sid and agent['provider'] == 'cursor' for agent in config['agents']):
            return ident
        taken = {agent['name'] for agent in config['agents']}
        name = self._cloud_display_name(raw_name, sid, taken)
        credential = self.root / ('cursor-cloud-' + hashlib.sha256(sid.encode()).hexdigest()[:24] + '.json')
        private_json(credential, {'api_key': key})
        self.bridge.history.secrets = (*self.bridge.history.secrets, key)
        config['agents'].append({'id': ident, 'source_id': sid, 'name': name, 'provider': 'cursor', 'driver': 'cursor'})
        config.setdefault('command_bindings', {})[ident] = {'adapter': 'cursor_cloud', 'session_id': sid}
        config.setdefault('command_adapters', {}).setdefault('cursor_cloud', {}).setdefault('sessions', {})[sid] = {
            'credential_file': str(credential)}
        return ident

    def _remember_cursor_key(self, key):
        saved = self._saved_cursor()
        record = {'api_key': key}
        if isinstance(saved.get('workspace'), str):
            record['workspace'] = saved['workspace']
        private_json(self.root / 'cursor.json', record)

    def _repository_url(self, value):
        if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(ch) <= 32 for ch in value):
            raise ConnectionError('cursor_repository_invalid')
        try:
            parsed = urllib.parse.urlsplit(value)
        except ValueError:
            raise ConnectionError('cursor_repository_invalid')
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or parsed.query or not parsed.path or parsed.path == '/'):
            raise ConnectionError('cursor_repository_invalid')
        return value

    def create_cursor_cloud(self, data):
        """Create one Cloud Agent. Office archive still does not archive it on Cursor."""
        if not isinstance(data, dict) or set(data) - {'api_key', 'prompt', 'repository', 'branch'} or 'prompt' not in data:
            raise ConnectionError('invalid_connection')
        text = data.get('prompt')
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 24000 or any(ord(ch) < 32 and ch not in '\n\r\t' for ch in text):
            raise ConnectionError('invalid_connection')
        text = text.strip()
        repository = data.get('repository') or None
        branch = data.get('branch') or None
        if repository is not None and not isinstance(repository, str):
            raise ConnectionError('cursor_repository_invalid')
        if branch is not None and not isinstance(branch, str):
            raise ConnectionError('cursor_repository_invalid')
        repository = repository.strip() if isinstance(repository, str) and repository.strip() else None
        branch = branch.strip() if isinstance(branch, str) and branch.strip() else None
        if branch and not repository:
            raise ConnectionError('cursor_repository_invalid')
        if repository:
            repository = self._repository_url(repository)
        if branch and (not re.fullmatch(r'(?!.*\.\.)[A-Za-z0-9._/-]{1,255}', branch) or branch.startswith(('-', '/')) or branch.endswith(('/', '.'))):
            raise ConnectionError('cursor_repository_invalid')
        with self.bridge.commands.lock, self.bridge.history.lock:
            try:
                key = data.get('api_key') or self._saved_cursor().get('api_key')
                validate_key(key)
            except (ValueError, OSError, KeyError, TypeError):
                raise ConnectionError('cursor_connection_details_required')
        if not data.get('api_key'):
            try:
                self.sync_cursor_cloud()
            except Exception:
                pass
        command_id = str(uuid.uuid4())
        payload = {'prompt': {'text': text}}
        if repository:
            entry = {'url': repository}
            if branch:
                entry['startingRef'] = branch
            payload['repos'] = [entry]
        try:
            value = self.transport.json('POST', 'https://api.cursor.com/v1/agents', cursor_headers(key), payload)
        except RemoteError as error:
            raise ConnectionError(error.code, 409)
        agent = value.get('agent') if isinstance(value.get('agent'), dict) else {}
        run = value.get('run') if isinstance(value.get('run'), dict) else {}
        try:
            sid = cursor_agent_id(agent.get('id'))
        except (ValueError, TypeError):
            raise ConnectionError('cursor_cloud_start_failed', 409)
        native = run.get('id')
        if run.get('agentId') != sid or not isinstance(native, str) or not CURSOR_RUN.fullmatch(native):
            raise ConnectionError('cursor_cloud_start_failed', 409)
        marked = '[OFFICE:' + command_id + ']\n' + text
        with self.bridge.commands.lock, self.bridge.history.lock:
            config = load_config(self.bridge.config['config_path'])
            ident = self._enroll_cloud(config, sid, agent.get('name'), key)
            self._remember_cursor_key(key)
            fingerprint = self.bridge.commands.fingerprint(ident, text)
            at = timestamp()
            self.bridge.commands.db.execute(
                'INSERT INTO commands VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (command_id, ident, sid, 'cursor_cloud', fingerprint,
                 redact(text, self.bridge.history.secrets, 6000), None, 'queued', native, None, at, at, None))
            self.bridge.commands.db.commit()
            self.save(config)
        adapter = self.bridge.commands.adapters.get('cursor_cloud')
        if adapter:
            adapter.resume(sid, command_id, native, marked)
        return {'connected': True, 'agent_id': ident, 'mode': 'cursor_cloud', 'same_session': True,
                'native_target_verified': True, 'execution_verified': False}

    def _cloud_floor(self, config, remote):
        now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        for agent in config['agents']:
            if config.get('command_bindings', {}).get(agent['id']) != {'adapter': 'cursor_cloud', 'session_id': agent['source_id']}:
                continue
            item = remote.get(agent['source_id'])
            if not item:
                continue
            active = self.bridge.commands.db.execute(
                "SELECT 1 FROM commands WHERE agent_id=? AND state IN ('sending','queued','running','waiting_user','delivery_unknown') LIMIT 1",
                (agent['id'],)).fetchone()
            if active or self.bridge.sources.get(agent['name'], {}).get('run_id'):
                continue
            desired = 'working' if item.get('status') == 'ACTIVE' else 'idle'
            step = 'Cloud Agent 正在工作' if desired == 'working' else '沒有進行中的工作'
            current = self.bridge.sources.get(agent['name'], {})
            if current.get('status') == desired and current.get('step') == step:
                continue
            self.bridge.sources[agent['name']] = {
                'status': desired, 'step': step, 'provider': 'cursor', 'observed_at': now, 'changed_at': now}

    def cursor_local_home(self):
        """What the office needs to open another local conversation. Never the key."""
        saved = self._saved_cursor()
        workspace = saved.get('workspace')
        if not isinstance(workspace, str) or not Path(workspace).is_dir():
            workspace = None
        catalog = self.bridge.workspaces.catalog() if hasattr(self.bridge, 'workspaces') else []
        config = self.bridge.config
        archived = set(config.get('archived_agent_ids', []))
        local_sessions = config.get('command_adapters', {}).get('cursor_local', {}).get('sessions', {})
        parked = []
        for agent in config.get('agents', []):
            if agent['id'] not in archived:
                continue
            binding = config.get('command_bindings', {}).get(agent['id'], {})
            if binding.get('adapter') == 'cursor_local' and binding.get('session_id') == agent['source_id']:
                item = local_sessions.get(agent['source_id'], {})
                parked.append({'agent_id': agent['id'], 'name': agent['name'], 'workspace': item.get('cwd')})
            elif binding.get('adapter') == 'cursor_cloud' and binding.get('session_id') == agent['source_id']:
                parked.append({'agent_id': agent['id'], 'name': agent['name'], 'workspace': None})
        return {'has_key': isinstance(saved.get('api_key'), str), 'workspace': workspace,
                'workspaces': [{'id': row['id'], 'name': row['name'], 'path': row['path']} for row in catalog],
                'archived': parked}

    def _cursor_local_membership(self, data, restore):
        ident = data.get('agent_id')
        if set(data) != {'action', 'agent_id'} or not isinstance(ident, str):
            raise ConnectionError('invalid_connection')
        with self.bridge.commands.lock, self.bridge.history.lock:
            config = load_config(self.bridge.config['config_path'])
            agent = next((a for a in config['agents'] if a['id'] == ident), None)
            binding = config.get('command_bindings', {}).get(ident)
            if not agent or binding not in (
                    {'adapter': 'cursor_local', 'session_id': agent['source_id']},
                    {'adapter': 'cursor_cloud', 'session_id': agent['source_id']}):
                raise ConnectionError('agent_not_selected', 404)
            archived = list(config.get('archived_agent_ids', []))
            if restore:
                archived = [item for item in archived if item != ident]
                duplicate = False
            elif ident in archived:
                duplicate = True
            else:
                active = self.bridge.commands.db.execute(
                    "SELECT 1 FROM commands WHERE agent_id=? AND state IN ('sending','queued','running','waiting_user','delivery_unknown') LIMIT 1",
                    (ident,)).fetchone()
                if active:
                    raise ConnectionError('agent_has_active_commands', 409)
                archived.append(ident)
                duplicate = False
            if not duplicate:
                config['archived_agent_ids'] = sorted(set(archived))
                self.save(config)
        try:
            self.sync_cursor_cloud()
        except Exception:
            pass
        return {'archived': not restore, 'agent_id': ident, 'duplicate': duplicate}

    def rename_agent(self, data):
        """Change the office display name. The Cursor session id stays the same."""
        if not isinstance(data, dict) or set(data) != {'agent_id', 'name'}:
            raise ConnectionError('invalid_connection')
        ident = data.get('agent_id')
        try:
            name = agent_name(data.get('name'))
        except (ValueError, TypeError):
            raise ConnectionError('invalid_connection')
        with self.bridge.commands.lock, self.bridge.history.lock:
            config = load_config(self.bridge.config['config_path'])
            agent = next((item for item in config['agents'] if item['id'] == ident), None)
            binding = config.get('command_bindings', {}).get(ident, {})
            if not agent or binding.get('adapter') not in {'cursor_local', 'cursor_cloud'}:
                raise ConnectionError('agent_not_selected', 404)
            if any(item['name'] == name and item['id'] != ident for item in config['agents']):
                raise ConnectionError('name_in_use', 409)
            old = agent['name']
            agent['name'] = name
            source = self.bridge.sources.pop(old, None)
            if source is not None:
                self.bridge.sources[name] = source
            self.save(config)
        if ident in self.bridge.agents:
            self.bridge.agents[ident]['name'] = name
        return {'agent_id': ident, 'name': name}

    def connect_cursor_local(self, data):
        """Open, archive, or reopen one local SDK conversation."""
        allowed = {'name', 'workspace', 'api_key', 'model', 'create_folder', 'action', 'agent_id'}
        if not isinstance(data, dict) or set(data) - allowed:
            raise ConnectionError('invalid_connection')
        action = data.get('action') or 'open'
        if action in {'archive', 'reopen'}:
            return self._cursor_local_membership(data, restore=action == 'reopen')
        if action != 'open' or (data.get('name') is None and 'name' in data):
            raise ConnectionError('invalid_connection')
        if 'workspace' not in data and not self._saved_cursor().get('workspace'):
            raise ConnectionError('invalid_workspace')
        raw_workspace = data.get('workspace', self._saved_cursor().get('workspace'))
        try:
            from workspaces import workspace_path
            cwd = workspace_path(raw_workspace)
        except (ValueError, TypeError):
            raise ConnectionError('invalid_workspace')
        creating = data.get('create_folder') is True
        folder = Path(cwd)
        if creating and not folder.exists():
            if not folder.parent.is_dir():
                raise ConnectionError('invalid_workspace')
            folder.mkdir()
        if not folder.is_dir():
            raise ConnectionError('invalid_workspace')
        model = data.get('model') or DEFAULT_MODEL
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', model):
            raise ConnectionError('cursor_connection_details_required')
        with self.bridge.commands.lock, self.bridge.history.lock:
            config = load_config(self.bridge.config['config_path'])
            if 'name' in data:
                try:
                    name = agent_name(data['name'])
                except (ValueError, TypeError):
                    raise ConnectionError('invalid_connection')
                if any(a['name'] == name for a in config['agents']):
                    raise ConnectionError('name_in_use', 409)
            else:
                taken = {a['name'] for a in config['agents']}
                name, number = '新對話', 2
                while name in taken:
                    name = '新對話 ' + str(number)
                    number += 1
            try:
                key = data.get('api_key') or self._saved_cursor().get('api_key')
                validate_key(key)
            except (ValueError, OSError, KeyError, TypeError):
                raise ConnectionError('cursor_connection_details_required')
        if not self.local_runtime.available():
            raise ConnectionError('cursor_sdk_required', 409)
        try:
            sid = local_agent_id(self.local_runtime.create(key, model, cwd))
        except LocalStartError as error:
            raise ConnectionError(error.code, 409)
        except ValueError:
            raise ConnectionError('cursor_local_agent_invalid', 409)
        ident = 'cursor:' + sid
        with self.bridge.commands.lock, self.bridge.history.lock:
            config = load_config(self.bridge.config['config_path'])
            if any(a['name'] == name for a in config['agents']) or any(a['id'] == ident for a in config['agents']):
                raise ConnectionError('name_in_use', 409)
            credential = self.root / ('cursor-local-' + hashlib.sha256(sid.encode()).hexdigest()[:24] + '.json')
            config['agents'].append({'id': ident, 'source_id': sid, 'name': name, 'provider': 'cursor', 'driver': 'cursor'})
            config.setdefault('command_bindings', {})[ident] = {'adapter': 'cursor_local', 'session_id': sid}
            config.setdefault('command_adapters', {}).setdefault('cursor_local', {}).setdefault('sessions', {})[sid] = {
                'credential_file': str(credential), 'cwd': cwd, 'model': model}
            private_json(credential, {'api_key': key})
            private_json(self.root / 'cursor.json', {'api_key': key, 'workspace': cwd})
            self.bridge.history.secrets = (*self.bridge.history.secrets, key)
            self.save(config)
            if hasattr(self.bridge, 'workspaces'):
                created = self.bridge.workspaces.create(cwd)
                self.bridge.workspaces.observe(ident, cwd, 'cursor_local')
                self.bridge.workspaces.assign(ident, created['id'], 'manual')
        try:
            self.sync_cursor_cloud()
        except Exception:
            pass
        return {'connected': True, 'agent_id': ident, 'name': name, 'mode': 'cursor_local', 'same_session': True,
                'native_target_verified': True, 'execution_verified': False}

    def _cursor_target(self, config, ident, name):
        if ident in config.get('disabled_command_agents',[]):raise ConnectionError('dispatch_disabled',409)
        agent=next((a for a in config['agents'] if a['id']==ident),None)
        if agent and (agent['name']!=name or agent['driver']!='cursor'):
            raise ConnectionError('agent_already_selected',409)
        if any(a['name']==name and a['id']!=ident for a in config['agents']):
            raise ConnectionError('name_in_use',409)
        return agent

    def _cursor_membership(self, config, ident, sid):
        # Metadata lookup runs without lifecycle locks. Scope its eventual
        # result to this target's original membership and route, including an
        # ABA logout/rejoin whose visible Agent descriptor looks identical.
        event=self.bridge.history.db.execute('SELECT event_id FROM office_membership_events '
            'WHERE agent_id=? ORDER BY rowid DESC LIMIT 1',(ident,)).fetchone()
        value={'agent':next((a for a in config['agents'] if a['id']==ident),None),
            'binding':config.get('command_bindings',{}).get(ident),
            'settings':config.get('command_adapters',{}).get('cursor_cloud',{}).get('sessions',{}).get(sid),
            'retired':ident in config.get('logged_out_agent_ids',[]),
            'disabled':ident in config.get('disabled_command_agents',[]),
            'membership_event':event['event_id'] if event else None}
        return json.dumps(value,sort_keys=True,separators=(',',':'))

    def _cursor_change_allowed(self, config, ident, sid, key):
        unchanged=False
        binding=config.get('command_bindings',{}).get(ident)
        if binding=={'adapter':'cursor_cloud','session_id':sid}:
            try:
                settings=config['command_adapters']['cursor_cloud']['sessions'][sid]
                previous=secret_file(settings['credential_file']).get('api_key')
                unchanged=isinstance(previous,str) and hmac.compare_digest(previous,key)
            except (OSError,ValueError,KeyError,TypeError):pass
        if not unchanged:
            active=self.bridge.commands.db.execute("SELECT 1 FROM commands WHERE agent_id=? AND (state IN ('sending','queued','running','waiting_user','delivery_unknown') OR (state='pending' AND (native_message_id IS NOT NULL OR run_id IS NOT NULL))) LIMIT 1",(ident,)).fetchone()
            if active:raise ConnectionError('connection_has_active_commands',409)
        return unchanged

    def connect_grok(self, data):
        if not isinstance(data,dict) or set(data)!={'agent_id','url','key'}:
            raise ConnectionError('invalid_connection')
        try:
            webhook_url(data['url']);validate_key(data['key'])
        except (ValueError, TypeError):raise ConnectionError('invalid_webhook_details')
        with self.bridge.commands.lock, self.bridge.history.lock:
            config = load_config(self.bridge.config['config_path'])
            agent = next((a for a in config['agents'] if a['id']==data['agent_id'] and a['provider']=='grok_bot' and a['driver']=='push'), None)
            if not agent:raise ConnectionError('agent_not_selected',404)
            if agent['id'] in config.get('disabled_command_agents',[]):raise ConnectionError('dispatch_disabled',409)
            # The caller must first prove the return route via the scoped agent
            # credential. A saved webhook alone never proves two-way delivery.
            seen = self.bridge.history.db.execute('SELECT 1 FROM event_receipts WHERE agent_id=? LIMIT 1',(agent['id'],)).fetchone()
            if not seen:raise ConnectionError('grok_return_route_required',409)
            active=self.bridge.commands.db.execute("SELECT 1 FROM commands WHERE agent_id=? AND (state IN ('sending','queued','running','waiting_user','delivery_unknown') OR (state='pending' AND (native_message_id IS NOT NULL OR run_id IS NOT NULL)))",(agent['id'],)).fetchone()
            if active or self.bridge.commands.reported_work_in_progress(agent['id']):
                raise ConnectionError('connection_has_active_commands',409)
            credential = self.root / ('grok-' + hashlib.sha256(agent['id'].encode()).hexdigest()[:24] + '.json')
            previous_binding=config.get('command_bindings',{}).get(agent['id'])
            unchanged=False
            if previous_binding=={'adapter':'grok_webhook','session_id':agent['source_id']}:
                try:
                    old_settings=config['command_adapters']['grok_webhook']['sessions'][agent['source_id']]
                    previous=secret_file(old_settings['credential_file'])
                    unchanged=(hmac.compare_digest(previous.get('url',''),data['url']) and
                               hmac.compare_digest(previous.get('key',''),data['key']))
                except (KeyError,OSError,ValueError,TypeError):pass
            if not unchanged:
                # Invalidate old proof before any credential/config write. A
                # crash can require another test, never preserve stale success.
                self.bridge.get_connection_checks().route_changed(agent['id'])
            private_json(credential, {'url':data['url'],'key':data['key']})
            config.setdefault('command_bindings',{})[agent['id']]={'adapter':'grok_webhook','session_id':agent['source_id']}
            config.setdefault('command_adapters',{}).setdefault('grok_webhook',{}).setdefault('sessions',{})[agent['source_id']]={'credential_file':str(credential)}
            self.bridge.history.secrets = (*self.bridge.history.secrets, data['key'], data['url'])
            self.save(config)
        return {'connected':True,'agent_id':agent['id'],'mode':'grok_webhook','same_session':False,
                'return_route_verified':True,'execution_verified':False}
