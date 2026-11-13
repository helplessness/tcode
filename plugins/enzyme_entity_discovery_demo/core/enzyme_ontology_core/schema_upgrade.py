"""仅扩展本体定义的本地维护操作；原始事实与旧发布不可变。"""
from pathlib import Path
import shutil
import tempfile
import os

from .bundle import (read_json, write_json, digest, file_digest, verify_manifest, validate,
                     validate_database, database_path, writer_lock, check, sync_dir)


def extended_ontology(old):
    import copy
    result = copy.deepcopy(old)
    result['ontology_version'] = '0.4.0'
    result['ontology_iri'] = 'urn:tcode:enzyme-entity-demo:ontology:0.4.0'
    for predicate in result['predicates']:
        if predicate['id'] in {'member_of', 'uses_cofactor', 'requires_cofactor', 'catalyzes_reported', 'supports_capability'}:
            if 'EnzymeVariant' not in predicate['domains']:
                predicate['domains'].append('EnzymeVariant')
            predicate['description'] += ' 变体关系仅适用于该精确变体，不传播给野生型、同家族或其他变体。'
    result['predicates'].append({'id': 'participates_in_pathway', 'label': '参与通路',
        'domains': ['Protein', 'ProteinGroup', 'EnzymeVariant', 'Reaction'], 'ranges': ['Pathway'],
        'materialize': True, 'description': '仅记录明确报道的通路参与关系；不推断参与者催化整条通路，也不自动推断任意两者之间的催化关系。'})
    result['predicates'].append({'id': 'may_catalyze', 'label': '可能催化',
        'domains': ['Protein', 'ProteinGroup', 'EnzymeVariant'], 'ranges': ['Reaction'],
        'materialize': True, 'description': '未实验确认的候选催化关系；必须保留 hypothesis 或 analogy 等证据状态，不得作为已报道催化事实。'})
    return result


def upgrade(knowledge_root, *, fail_before_activation=False):
    root = Path(knowledge_root).resolve()
    with writer_lock(root):
        previous = verify_manifest(root, read_json(root / 'entity_manifest.json'))
        if previous['storage_format'] != 'sqlite-v2':
            raise ValueError('schema upgrade requires sqlite-v2')
        old = read_json(root / previous['ontology_path'])
        if old['ontology_version'] == '0.4.0':
            return {'ok': True, 'already_current': True, 'bundle_digest': previous['bundle_digest']}
        if old['ontology_version'] != '0.3.0':
            raise ValueError('unsupported source ontology version')
        baseline = validate(root)
        ontology = extended_ontology(old)
        source = database_path(root, previous).parent
        temporary = Path(tempfile.mkdtemp(prefix='.schema-upgrade-', dir=root))
        try:
            # 只复制清单覆盖的文件，不复制运行时临时文件或修改旧发布。
            for entry in previous['files']:
                relative = (root / entry['path']).relative_to(source)
                target = temporary / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / entry['path'], target)
            write_json(temporary / 'ontology.json', ontology)
            actual = validate_database(temporary / 'entities.sqlite', ontology, temporary)
            if actual['logical_digest'] != baseline['logical_digest'] or file_digest(temporary / 'entities.sqlite') != baseline['database_sha256']:
                raise ValueError('schema migration changed existing facts')
            migration = {'from_bundle': previous['bundle_digest'], 'from_release': previous['release_id'],
                         'from_ontology': '0.3.0', 'to_ontology': '0.4.0', 'fact_payloads_unchanged': True,
                         'logical_digest': actual['logical_digest'], 'automatic_property_inheritance': False}
            write_json(temporary / 'schema-migration.json', migration)
            files = [{'path': str(p.relative_to(temporary)), 'sha256': file_digest(p)} for p in sorted(temporary.rglob('*')) if p.is_file()]
            release_id = digest({'files': files, 'input': previous['bundle_digest']})
            prefix = 'public/releases/' + release_id + '/'
            manifest = {**previous, 'ontology_version': '0.4.0', 'release_id': release_id,
                        'input_digest': previous['bundle_digest'], 'database_path': prefix + 'entities.sqlite',
                        'ontology_path': prefix + 'ontology.json', 'files': [{**e, 'path': prefix + e['path']} for e in files]}
            manifest.pop('bundle_digest')
            manifest['bundle_digest'] = digest(manifest)
            check('release-manifest', manifest)
            write_json(temporary / 'manifest.json', manifest)
            destination = root / prefix
            if destination.exists():
                if read_json(destination / 'manifest.json') != manifest:
                    raise ValueError('release collision')
                verify_manifest(root, manifest)
            else:
                for file in temporary.rglob('*'):
                    if file.is_file():
                        with file.open('rb') as stream:
                            os.fsync(stream.fileno())
                for folder in sorted((p for p in temporary.rglob('*') if p.is_dir()), reverse=True):
                    sync_dir(folder)
                sync_dir(temporary)
                os.rename(temporary, destination)
                sync_dir(destination.parent)
            if fail_before_activation:
                raise RuntimeError('injected before activation')
            write_json(root / 'entity_manifest.json', manifest)
            return {'ok': True, **migration, 'release_id': release_id, 'bundle_digest': manifest['bundle_digest']}
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
