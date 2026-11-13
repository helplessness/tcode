"""真实模型抽样验收：独立草稿目录，不审核、不发布、不改公共本体。"""
from pathlib import Path
import argparse
import hashlib
import json
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf', nargs='+', type=Path, help='原始 PDF 路径')
    parser.add_argument('--output', required=True, type=Path, help='新的测试输出目录，必须尚不存在')
    parser.add_argument('--pages', type=int, default=1, help='每篇抽取前几页，默认 1 页；不是全篇验收')
    args = parser.parse_args()
    if args.pages < 1:
        parser.error('--pages 必须大于零')
    plugin = Path(__file__).resolve().parents[1]
    repo = plugin.parents[1]
    sys.path[:0] = [str(repo), str(plugin / 'core')]
    from pypdf import PdfReader, PdfWriter
    from tcode_agent.ontology_host import ontology_model_settings
    from enzyme_ontology_core.intake import DraftIntake
    from enzyme_ontology_core.extraction import PROMPT_VERSION

    settings = ontology_model_settings(repo)
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    pointer = plugin / 'knowledge/entity_manifest.json'
    before = pointer.read_bytes()
    report = {'scope': '真实 PDF 前页抽样；仅草稿；不代表科学准确率',
              'settings': settings, 'prompt_version': PROMPT_VERSION, 'papers': []}
    try:
        for index, original in enumerate(args.pdf, 1):
            original = original.resolve()
            reader = PdfReader(original)
            count = min(args.pages, len(reader.pages))
            sample = root / f'paper-{index}-sample.pdf'
            writer = PdfWriter()
            for page in reader.pages[:count]:
                writer.add_page(page)
            writer.write(sample)
            store = DraftIntake(root / f'draft-{index}', workspace_id=f'live-acceptance-{index}')
            try:
                job = store.submit([sample], knowledge_root=plugin / 'knowledge', mode='model', **settings)
                print(f'开始：{original.name}，前 {count} 页，任务 {job}', flush=True)
                start = time.monotonic()
                result = store.run(job)
                with original.open('rb') as stream:
                    source_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
                entry = {'original': str(original), 'original_sha256': source_hash,
                         'sample': str(sample), 'pages': list(range(1, count + 1)),
                         'seconds': round(time.monotonic() - start, 2), 'result': result}
                report['papers'].append(entry)
                print(json.dumps(entry, ensure_ascii=False), flush=True)
            finally:
                store.close()
            (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        report['public_manifest_unchanged'] = pointer.read_bytes() == before
        (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['public_manifest_unchanged'] and all(p['result']['state'] == 'succeeded' for p in report['papers']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
