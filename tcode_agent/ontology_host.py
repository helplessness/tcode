"""Host-owned ontology roots and per-call read capability, never a plugin write grant."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from .workspace_lifecycle import managed_workspace_owner_from_path, managed_workspace_storage_root, assert_managed_workspace_creation_allowed


def plugin_root():
    return Path(os.environ.get('TCODE_ONTOLOGY_PLUGIN_ROOT') or Path(__file__).resolve().parents[1] / 'plugins/enzyme_entity_discovery_demo').resolve()


def load_core():
    from .ontology_core.governance import PrivateKnowledge, ConflictError
    return PrivateKnowledge, ConflictError


def platform_knowledge_root(server_root):
    """Return the platform-owned system bundle; domain packages never replace it."""
    explicit = str(os.environ.get('TCODE_ONTOLOGY_KNOWLEDGE_ROOT') or '').strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    from .ontology_core.bundle import canonical, digest, file_digest
    key = hashlib.sha256(str(Path(server_root).resolve()).encode()).hexdigest()[:20]
    root = Path(os.environ.get('TCODE_ONTOLOGY_SYSTEM_ROOT') or Path.home() / '.tcode/ontology-system').expanduser().resolve() / key
    release = root / 'public/releases/generic-v1'
    database = release / 'entities.sqlite'
    ontology_file = release / 'ontology.json'
    if not database.is_file():
        release.mkdir(parents=True, exist_ok=True)
        ontology = {
            'ontology_version': '1.0.0',
            'classes': [{'id': 'Entity'}, {'id': 'Document'}],
            'predicates': [{'id': 'related_to', 'domains': ['Entity', 'Document'], 'ranges': ['Entity', 'Document']}],
        }
        ontology_file.write_bytes(canonical(ontology) + b'\n')
        db = sqlite3.connect(database)
        try:
            db.executescript('''
CREATE TABLE entity(id TEXT PRIMARY KEY,type TEXT NOT NULL,label TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE alias(term TEXT NOT NULL,entity_id TEXT NOT NULL REFERENCES entity(id),PRIMARY KEY(term,entity_id));
CREATE TABLE alias_token(token TEXT NOT NULL,entity_id TEXT NOT NULL REFERENCES entity(id),PRIMARY KEY(token,entity_id));
CREATE TABLE assertion(id TEXT PRIMARY KEY,subject_id TEXT NOT NULL REFERENCES entity(id),predicate TEXT NOT NULL,object_id TEXT NOT NULL REFERENCES entity(id),state TEXT NOT NULL,polarity TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE evidence(id TEXT PRIMARY KEY,wiki_ref TEXT UNIQUE NOT NULL,payload TEXT NOT NULL);
CREATE TABLE meta(schema_version TEXT NOT NULL,scope TEXT NOT NULL CHECK(scope='public'),revision INTEGER NOT NULL);
CREATE TABLE source_revision(source_id TEXT,revision INTEGER,object_path TEXT NOT NULL,object_sha256 TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(source_id,revision));
CREATE TABLE span(span_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL);
CREATE TABLE evidence_span(evidence_id TEXT PRIMARY KEY REFERENCES evidence(id),span_id TEXT NOT NULL REFERENCES span(span_id));
CREATE TABLE assertion_evidence(assertion_id TEXT REFERENCES assertion(id),evidence_id TEXT REFERENCES evidence(id),PRIMARY KEY(assertion_id,evidence_id));
CREATE TABLE external_id(namespace TEXT,accession TEXT,entity_id TEXT REFERENCES entity(id),PRIMARY KEY(namespace,accession));
CREATE TABLE change_set(batch_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE review_decision(batch_id TEXT PRIMARY KEY,decision TEXT NOT NULL,reviewer_user_id TEXT,payload_hash TEXT NOT NULL);
CREATE TABLE audit_event(event_id TEXT PRIMARY KEY,action TEXT NOT NULL,actor_kind TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE assertion_revision(assertion_id TEXT,revision INTEGER,review_state TEXT,conflict_state TEXT,payload TEXT NOT NULL,PRIMARY KEY(assertion_id,revision));
INSERT INTO meta VALUES('2.0','public',0);
''')
            db.commit()
        finally:
            db.close()
        files = [
            {'path': 'public/releases/generic-v1/entities.sqlite', 'sha256': file_digest(database)},
            {'path': 'public/releases/generic-v1/ontology.json', 'sha256': file_digest(ontology_file)},
        ]
        manifest = {'bundle_version': 'generic-v1', 'database_path': files[0]['path'], 'files': files,
                    'logical_digest': digest([]), 'ontology_path': files[1]['path'], 'ontology_version': '1.0.0',
                    'release_id': 'generic-v1', 'schema_version': '2.0',
                    'statistics': {'assertions': 0, 'entities': 0, 'evidence': 0, 'source_cards': 0}, 'storage_format': 'sqlite-v2'}
        manifest['bundle_digest'] = digest(manifest)
        (root / 'entity_manifest.json').write_bytes(canonical(manifest) + b'\n')
    # Installed domain bundles contribute through the same validated immutable
    # release path. Their directories are never used as the active system root.
    from .ontology_registry import registered_ontology_packages
    from .ontology_core.system_admin import contribute_package
    packages = registered_ontology_packages()
    if os.environ.get('TCODE_ONTOLOGY_DISABLE_DOMAIN_PLUGINS') != '1':
        bundled = plugin_root() / 'knowledge'
        if (bundled / 'entity_manifest.json').is_file():
            packages.setdefault('enzyme-entity-discovery-demo', bundled)
    for provider_id, package_root in sorted(packages.items()):
        contribute_package(root, provider_id, package_root, publish=True)
    from .ontology_core.scientific import extend_schema
    extend_schema(root)
    return root


def private_root(server_root, owner):
    server_root = Path(server_root).resolve()
    configured = os.environ.get('TCODE_ONTOLOGY_PRIVATE_ROOT')
    base = Path(configured).expanduser() if configured else Path.home() / '.tcode/ontology-private'
    base = base.resolve()
    managed = managed_workspace_storage_root(server_root)
    if managed and (base == managed or base.is_relative_to(managed)):
        raise ValueError('ontology private root must be outside managed workspaces')
    server_key = hashlib.sha256(str(server_root).encode()).hexdigest()[:20]
    scope_key = hashlib.sha256(json.dumps(tuple(owner), separators=(',', ':')).encode()).hexdigest()
    root = base / server_key / scope_key
    if any(p.is_symlink() for p in [root, root.parent]):
        raise ValueError('unsafe ontology storage path')
    return root, scope_key


def ontology_model_config(server_root):
    """Use the same merged user configuration as chat; support legacy local roots."""
    from .config.manager import ConfigManager
    config = ConfigManager.load(str(server_root))
    if not config.get('provider') and not os.environ.get('TCODE_CONFIG_ROOT'):
        local = Path(server_root) / '.tcode-local' / 'tcode.json'
        if local.is_file():
            config = json.loads(local.read_text())
    return config


def ontology_model_settings(server_root, requested_model=None):
    from .config.manager import ConfigManager
    from .config.options import resolve_option_value
    from urllib.parse import urlparse
    import ipaddress
    config = ontology_model_config(server_root)
    with ConfigManager.bind_snapshot('ontology-selection', config, workdir=str(server_root)):
        selected = ConfigManager.resolve_model_selection()
    model_ref = str(requested_model or f"{selected['provider_id']}/{selected['model_id']}").strip()
    parsed = ConfigManager.parse_model_ref(model_ref)
    if not parsed:
        raise ValueError('模型配置无效，请在模型设置中选择已配置的模型。')
    provider_id, model = parsed
    provider = config.get('provider', {}).get(provider_id, {})
    if model not in provider.get('models', {}):
        raise ValueError('所选模型未配置，请在模型设置中添加或重新选择模型。')
    options = provider.get('options', {})
    npm = str(provider.get('npm') or '').lower()
    default_url = 'https://api.anthropic.com' if provider_id.startswith('anthropic') or 'anthropic' in npm else 'http://localhost:11434' if provider_id.startswith('ollama') or 'ollama' in npm else 'https://api.openai.com/v1'
    base_url = str(resolve_option_value(options, 'baseURL', 'base_url') or default_url).rstrip('/')
    url = urlparse(base_url)
    try:
        address = ipaddress.ip_address(url.hostname or '')
    except ValueError:
        address = None
    local = url.hostname == 'localhost' or bool(address and (address.is_loopback or address.is_private))
    if url.scheme not in ('http', 'https') or (url.scheme == 'http' and not local) or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('模型服务地址无效，请在模型设置中检查服务地址。')
    api_key = resolve_option_value(options, 'apiKey', 'api_key')
    if url.scheme == 'https' and not api_key:
        raise ValueError('模型凭据不可用，请在模型设置中检查 API Key。')
    timeout = float(options.get('timeoutSeconds') or options.get('timeout_seconds') or 60)
    if not 1 <= timeout <= 3600:
        raise ValueError('模型超时设置无效，请设置为 1 至 3600 秒。')
    return {'model': model, 'model_ref': model_ref, 'base_url': base_url,
            'allowed_model_endpoints': [base_url], 'model_timeout_seconds': timeout,
            'model_reasoning_effort': None, 'model_api_key': api_key or 'local-only'}


def ontology_model_options(server_root):
    """Configured choices only; no credentials or endpoint details in the UI."""
    providers = ontology_model_config(server_root).get('provider', {})
    return [{'id': f'{pid}/{mid}', 'name': str(model.get('name') or mid)}
            for pid, provider in providers.items() for mid, model in provider.get('models', {}).items()
            if isinstance(model, dict)]


def ontology_model_error(exc, model_ref):
    """Safe actionable messages; never disclose provider bodies, prompts or keys."""
    chain, current = [], exc
    while current is not None and id(current) not in {id(e) for e in chain}:
        chain.append(current)
        current = current.__cause__ or current.__context__
    statuses = {getattr(e, 'status_code', None) for e in chain}
    names = ' '.join(type(e).__name__.lower() for e in chain)
    if statuses & {401, 403}:
        reason = '认证失败，请检查 API Key 和模型访问权限'
    elif 429 in statuses:
        reason = '模型服务限流或额度不足，请检查额度后重试'
    elif 'timeout' in names:
        reason = '模型连接或响应超时，请检查网络和服务状态后重试'
    elif 'connect' in names or any(isinstance(e, OSError) for e in chain):
        reason = '无法连接模型，请检查网络、服务地址以及模型服务是否启动'
    else:
        reason = '模型请求失败，请检查模型设置、服务状态和协议是否匹配'
    return f'{reason}（{model_ref}）。'


@contextmanager
def ontology_model_runtime(server_root, settings):
    """Fresh provider and one event loop per background job; secrets stay in memory."""
    import asyncio
    from .config.manager import ConfigManager
    from .providers.factory import create_provider
    from .providers.base import ChatMessage
    from .providers.openai import OpenAIProvider
    from .ontology_core.extraction import ModelRequestError
    config = ontology_model_config(server_root)
    with ConfigManager.bind_snapshot('ontology-job', config, workdir=str(server_root)), asyncio.Runner() as runner:
        provider = None
        try:
            pinned = ontology_model_settings(server_root, settings['model_ref'])
            if pinned['base_url'] != settings['base_url']:
                raise ValueError('模型配置已变更，请重新提交导入任务。')
            provider = create_provider(workdir=str(server_root), model_ref=settings['model_ref'])
            request_options = {'reasoning_effort': 'none'}
            if isinstance(provider, OpenAIProvider):
                request_options.pop('reasoning_effort')
                if settings['model'].startswith('deepseek-v4-'):
                    request_options['extra_body'] = {'thinking': {'type': 'disabled'}}
                    # Known Chat Completions provider: auto probing can accept a
                    # Responses route while dropping extraction-specific options.
                    if provider.api_mode == 'auto':
                        request_options['api_mode'] = 'chat'
                if request_options.get('api_mode', provider.api_mode) == 'chat':
                    request_options['response_format'] = {'type': 'json_object'}
            def generate(messages):
                async def request():
                    return await asyncio.wait_for(provider.chat(
                        [ChatMessage(**m) for m in messages], temperature=0, max_tokens=8192,
                        **request_options,
                    ), timeout=settings['model_timeout_seconds'])
                try:
                    answer = runner.run(request())
                except Exception as exc:
                    raise ModelRequestError(ontology_model_error(exc, settings['model_ref'])) from exc
                return answer.content, answer.finish_reason
            yield {'model_generate': generate}
        except ModelRequestError:
            raise
        except Exception as exc:
            raise ModelRequestError(ontology_model_error(exc, settings['model_ref'])) from exc
        finally:
            if provider is not None:
                close = getattr(provider, 'close', None)
                if close:
                    try:
                        runner.run(asyncio.wait_for(close(), timeout=5))
                    except Exception:
                        pass


def open_store(server_root, owner):
    cls, _ = load_core()
    root, scope_key = private_root(server_root, owner)
    return cls(root, scope_key, platform_knowledge_root(server_root))


def ontology_source_policy(server_root, owner):
    """Read the workspace-owned Agent source policy; missing policy preserves both sources."""
    root, _ = private_root(server_root, owner)
    path = root / 'source-policy.json'
    try:
        value = json.loads(path.read_text())
        return {'public': value.get('public') is not False, 'private': value.get('private') is not False,
                'version': int(value.get('version', 1))}
    except (OSError, TypeError, ValueError):
        return {'public': True, 'private': True, 'version': 0}


def update_ontology_source_policy(server_root, owner, *, public, private):
    root, _ = private_root(server_root, owner)
    root.mkdir(parents=True, exist_ok=True)
    current = ontology_source_policy(server_root, owner)
    value = {'public': bool(public), 'private': bool(private), 'version': current['version'] + 1}
    path = root / 'source-policy.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n')
    temporary.replace(path)
    return value


class OntologyReadCapability:
    __slots__ = ('_active', '_server_root', '_owner', '_workdir')

    def __init__(self, server_root, owner, workdir):
        self._active, self._server_root, self._owner, self._workdir = True, server_root, owner, workdir

    def close(self):
        self._active = False

    def source_policy(self):
        """Return the current policy; callers must not cache this grant."""
        if not self._active:
            raise RuntimeError('ontology capability expired')
        return ontology_source_policy(self._server_root, self._owner)

    def _call(self, method, **arguments):
        if method not in ('query', 'validate_report', 'read_facts') or (method == 'validate_report' and set(arguments) != {'claims'}):
            raise PermissionError('read-only ontology capability')
        if not self._active:
            raise RuntimeError('ontology capability expired')
        if method == 'validate_report':
            policy = ontology_source_policy(self._server_root, self._owner)
            arguments['allowed_scopes'] = [scope for scope in ('public', 'private') if policy[scope]]
        assert_managed_workspace_creation_allowed(self._workdir, server_workdir=self._server_root)
        store = open_store(self._server_root, self._owner)
        try:
            return getattr(store, method)(**arguments)
        finally:
            store.close()

    def read_facts(self, references):
        policy = ontology_source_policy(self._server_root, self._owner)
        if not policy['private']:
            raise PermissionError('workspace private ontology source is disabled')
        result = self._call('read_facts', references=references, allowed_scopes=[scope for scope in ('public','private') if policy[scope]])
        result['source_policy'] = policy
        return result

    def query(self, **arguments):
        policy = ontology_source_policy(self._server_root, self._owner)
        requested = arguments.get('scope', 'both')
        allowed = [scope for scope in ('public', 'private') if policy[scope] and requested in (scope, 'both')]
        if not allowed:
            return {'public': [], 'private': [], 'workspace_conflicts': [], 'conflicts_truncated': False,
                    'next_cursor': None, 'completeness': 'complete', 'source_policy': policy,
                    'unavailable_reason': 'no_enabled_ontology_sources'}
        arguments['scope'] = allowed[0] if len(allowed) == 1 else 'both'
        result = self._call('query', **arguments)
        result['source_policy'] = policy
        return result

    def validate_report(self, claims):
        # No actor => validation only, not a human decision or persisted approval.
        policy = ontology_source_policy(self._server_root, self._owner)
        references = [reference for claim in claims for reference in claim.get('assertion_ids', [])]
        if any(reference.startswith('private-assertion:') for reference in references) and not policy['private']:
            raise PermissionError('workspace private ontology source is disabled')
        if any(not reference.startswith('private-assertion:') for reference in references) and not policy['public']:
            raise PermissionError('public ontology source is disabled')
        result = self._call('validate_report', claims=claims)
        result['source_policy'] = policy
        return result


def read_capability(workdir, source=None, server_root=None):
    """Issue a scoped read capability after resolving the host-owned workspace identity.

    ``source`` remains accepted for SDK v1 callers, but authorization is performed by
    the plugin permission runtime rather than by a plugin-name allow-list.
    """
    # Preserve the SDK v1 compatibility contract without using plugin identity for
    # new authorization. New callers omit ``source`` after the permission runtime
    # has checked ``ontology.read``.
    if source is not None and source != 'enzyme-entity-discovery-demo':
        return None
    explicit_server_root = server_root is not None
    server_root = str(server_root or os.environ.get('TCODE_WORKDIR') or Path.cwd())
    if not explicit_server_root:
        resolved = Path(workdir).resolve()
        for parent in resolved.parents:
            if parent.name == 'workdirs':
                server_root = str(parent.parent)
                break
    owner = managed_workspace_owner_from_path(workdir, server_workdir=server_root)
    if owner is None:
        # Desktop plugin calls may inherit the project TCODE_WORKDIR while the
        # actual managed workspace lives under the desktop runtime root.  The
        # workspace path itself is host-owned, so recover only its exact
        # ``<server>/workdirs/<tenant>/<user>/<workspace>`` ancestor.
        resolved = Path(workdir).resolve()
        for parent in resolved.parents:
            if parent.name != 'workdirs':
                continue
            candidate = str(parent.parent)
            inferred_owner = managed_workspace_owner_from_path(
                str(resolved), server_workdir=candidate
            )
            if inferred_owner is not None:
                server_root, owner = candidate, inferred_owner
                break
    if owner is None:
        return None
    assert_managed_workspace_creation_allowed(workdir, server_workdir=server_root)
    return OntologyReadCapability(server_root, owner, workdir)
