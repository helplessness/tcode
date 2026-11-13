"""Local ontology migration, validation and draft intake commands."""
import argparse
import json
from .bundle import migrate, build, validate, activate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    migration = commands.add_parser('migrate')
    migration.add_argument('--knowledge-root', required=True)
    migration.add_argument('--output', required=True)
    builder = commands.add_parser('build')
    builder.add_argument('--input', required=True)
    builder.add_argument('--knowledge-root', required=True)
    check = commands.add_parser('validate')
    check.add_argument('--knowledge-root', required=True)
    recovery = commands.add_parser('activate')
    recovery.add_argument('--knowledge-root', required=True)
    recovery.add_argument('--manifest', required=True)
    intake = commands.add_parser('import', help='create document candidates; never publish')
    intake.add_argument('files', nargs='+')
    intake.add_argument('--knowledge-root', required=True)
    intake.add_argument('--extractor', choices=['mentions', 'model'], default='mentions')
    intake.add_argument('--model')
    intake.add_argument('--base-url', default='http://127.0.0.1:11434/v1')
    intake.add_argument('--idempotency-key')
    intake.add_argument('--source-id')
    intake.add_argument('--base-revision', type=int)
    intake.add_argument('--max-file-bytes', type=int, default=100 * 1024 * 1024)
    intake.add_argument('--enqueue-only', action='store_true')
    intake.add_argument('--draft-only', action='store_true', help='all imports are draft-only, even without this flag')
    management = []
    for name in ('inspect', 'run', 'retry', 'cancel'):
        command = commands.add_parser(name)
        command.add_argument('job_id')
        if name == 'inspect':
            command.add_argument('--limit', type=int, default=100)
            command.add_argument('--cursor')
        management.append(command)
    for command in [intake, *management]:
        command.add_argument('--data-root', required=True)
        command.add_argument('--scope', choices=['workspace', 'public'], default='workspace')
        command.add_argument('--workspace-id', default='')
    for name in ('backup-private', 'restore-private'):
        command = commands.add_parser(name)
        command.add_argument('--input', required=True)
        command.add_argument('--output', required=True)
        command.add_argument('--workspace-id', required=True)
        command.add_argument('--knowledge-root', required=True)
    args = parser.parse_args()
    try:
        if args.command in ('import', 'inspect', 'run', 'retry', 'cancel'):
            from .intake import DraftIntake
            store = DraftIntake(args.data_root, scope=args.scope, workspace_id=args.workspace_id)
            try:
                if args.command == 'import':
                    job_id = store.submit(args.files, knowledge_root=args.knowledge_root, mode=args.extractor,
                                          model=args.model, base_url=args.base_url, idempotency_key=args.idempotency_key,
                                          source_id=args.source_id, base_revision=args.base_revision, max_file_bytes=args.max_file_bytes)
                    result = store.inspect(job_id) if args.enqueue_only else store.run(job_id)
                elif args.command == 'inspect':
                    result = store.inspect(args.job_id, limit=args.limit, cursor=args.cursor)
                elif args.command == 'cancel':
                    result = store.cancel(args.job_id)
                else:
                    result = store.run(args.job_id, retry=args.command == 'retry')
            finally:
                store.close()
        elif args.command in ('backup-private', 'restore-private'):
            from .private_backup import backup, restore
            action = backup if args.command == 'backup-private' else restore
            result = action(args.input, args.output, args.workspace_id, args.knowledge_root)
        elif args.command == 'migrate':
            result = migrate(args.knowledge_root, args.output)
        elif args.command == 'build':
            result = build(args.input, args.knowledge_root)
        elif args.command == 'activate':
            result = activate(args.knowledge_root, args.manifest)
        else:
            result = validate(args.knowledge_root)
    except (ValueError, OSError) as exc:
        parser.exit(1, f'{exc}\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
