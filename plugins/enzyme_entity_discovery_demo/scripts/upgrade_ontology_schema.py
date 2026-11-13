"""将插件本体 0.3.0 兼容升级为 0.4.0；旧发布保留，可用 activate 回滚。"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'core'))
from enzyme_ontology_core.schema_upgrade import upgrade

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--knowledge-root', type=Path, default=ROOT / 'knowledge')
    args = parser.parse_args()
    print(json.dumps(upgrade(args.knowledge_root), ensure_ascii=False, indent=2))
