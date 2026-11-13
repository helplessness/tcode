"""管理命令：预建插件共享 Wiki 向量，不创建会话或读取测试答案。

python -m scripts.enzyme_wiki_index
"""
import argparse
import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from tcode_agent.plugins.embedding_api import PluginEmbeddingAPI
from tcode_agent.plugins.permissions import PluginPermission
from plugins.enzyme_reaction_discovery.tools.knowledge_tools import KnowledgeLibrary
from plugins.enzyme_reaction_discovery.tools.retrieval_tools import WikiIndex
from plugins.enzyme_reaction_discovery.tools.semantic_tools import HybridRetriever, TEXT_VERSION, faiss_modules

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/enzyme_reaction_discovery"
DIRECTORY = ".tcode/plugins/enzyme_reaction_discovery"


class IndexWorkspace:
    def __init__(self, workspace=ROOT):
        self.root = workspace.resolve(strict=True)
        self.cache = PLUGIN / ".cache"

    def resolve_knowledge_cache_path(self, name):
        """维护命令的显式发布边界；缓存留在插件内，FAISS 资产发布到卡片目录。"""
        if name not in {"wiki-index.sqlite3", "wiki-index.sqlite3.build.lock", "faiss.index"}:
            raise ValueError("unsupported Wiki index maintenance file")
        target = PLUGIN / "knowledge/cards/faiss.index" if name == "faiss.index" else self.cache / name
        return str(target)

    def resolve_write_path(self, value):
        path = (self.root / value).resolve()
        if path == PLUGIN / "knowledge/cards/faiss.index":
            return str(path)
        path.relative_to(self.cache)
        return str(path)

    def resolve_read_path(self, value):
        path = (self.root / value).resolve()
        if not any(path.is_relative_to(p) for p in (self.cache, PLUGIN / "knowledge", PLUGIN / "schemas")):
            raise PermissionError("index read outside Wiki/schema/cache")
        return str(path)

    def read_text(self, value, encoding="utf-8"):
        return Path(self.resolve_read_path(value)).read_text(encoding=encoding)


def import_vector_cache(source, index, state, descriptor):
    """显式、只读迁移旧库中同 Wiki/模型的向量；不复制旧 FAISS 或运行数据。"""
    source = Path(source).resolve(strict=True)
    if source == index.path.resolve():
        raise ValueError("source cache is already the shared cache")
    _faiss, np = faiss_modules()
    signature = descriptor['signature'] + ':' + TEXT_VERSION
    with index.vector_build_guard() as acquired:
        if not acquired:
            raise ValueError("shared index build already in progress; no automatic retry")
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=2)) as old, \
                closing(sqlite3.connect(index.path, timeout=2)) as db:
            old.execute('BEGIN')
            if old.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone() != (state['fingerprint'],):
                raise ValueError("source cache Wiki fingerprint mismatch")
            expected = dict(db.execute('SELECT stable_key,text_sha FROM records'))
            db.execute('BEGIN IMMEDIATE')
            HybridRetriever._check_fingerprint(db, index, state)
            imported = 0
            dims = {r[0] for r in db.execute('SELECT DISTINCT dim FROM embeddings WHERE signature=?', (signature,))}
            for key, sha, dim, blob in old.execute('SELECT stable_key,text_sha,dim,vector FROM embeddings WHERE signature=?', (signature,)):
                if expected.get(key) != sha:
                    continue
                if not 1 <= dim <= 8192 or len(blob) != dim * 4:
                    raise ValueError('source cache vector dimension invalid')
                matrix = HybridRetriever._vectors({'signature': descriptor['signature'],
                    'vectors': [np.frombuffer(blob, dtype='float32')]}, descriptor['signature'], 1)
                dims.add(matrix.shape[1])
                if len(dims) != 1:
                    raise ValueError('source cache contains mixed dimensions')
                db.execute('INSERT OR IGNORE INTO embeddings VALUES (?,?,?,?,?)', (key, sha, signature, dim, blob))
                imported += 1
            if not imported:
                raise ValueError('source cache has no vectors matching configured model and current Wiki')
            db.commit()
    return imported


async def build(args):
    workspace = IndexWorkspace()
    library = KnowledgeLibrary(workspace, PLUGIN)
    index = WikiIndex(library, workspace, DIRECTORY)
    state = index.ensure()
    api = PluginEmbeddingAPI(str(Path(args.config_workdir).resolve()), [PluginPermission("model.embedding", ["configured"])])
    retriever = HybridRetriever(api, {"build_batches": 1})
    descriptor = await api.describe()
    print(json.dumps({'shared_index': str(index.path), 'legacy_workspace_argument_ignored': bool(args.workspace)}, ensure_ascii=False), flush=True)
    if args.import_cache:
        count = import_vector_cache(args.import_cache, index, state, descriptor)
        print(json.dumps({'imported_matching_vectors': count, 'source_modified': False}), flush=True)
    if args.rebuild_faiss:
        # 只清除可重建的派生 FAISS 快照，保留 Wiki、运行记录与有效向量。
        with index.vector_build_guard() as acquired:
            if not acquired:
                raise ValueError('shared index build already in progress; no automatic retry')
            with sqlite3.connect(index.path) as db:
                db.execute("DELETE FROM vector_index WHERE signature=?", (descriptor['signature'] + ':' + TEXT_VERSION,))
    for batch in range(args.max_batches):
        async with asyncio.timeout(60):
            result = await retriever._ensure_vectors(index, state, descriptor)
        print(json.dumps({"batch": batch + 1, "knowledge_fingerprint": state["fingerprint"],
                          "model": descriptor["model"], **result}, ensure_ascii=False), flush=True)
        if result["status"] == "ready":
            index.materialize_faiss(descriptor["signature"] + ":" + TEXT_VERSION)
            return
        if result.get('reason') == 'index_build_in_progress':
            raise ValueError('shared index build already in progress; no automatic retry')
    raise ValueError("index build budget exhausted; cache retained, rerun maintenance explicitly")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", help="兼容旧命令；已忽略，索引统一写入插件 .cache")
    parser.add_argument("--config-workdir", default=str(ROOT / ".tcode-local" if (ROOT / ".tcode-local/tcode.json").is_file() else ROOT),
                        help="模型配置所在目录；默认优先项目的 .tcode-local，TCODE_CONFIG_ROOT 环境变量仍优先")
    parser.add_argument("--import-cache", help="只读迁移显式指定的旧 SQLite 中同知识/模型版本的向量，不读取会话")
    parser.add_argument("--max-batches", type=int, default=2000)
    parser.add_argument("--rebuild-faiss", action="store_true", help="明确重建派生 FAISS，不重嵌入未变 Wiki")
    args = parser.parse_args()
    if not 1 <= args.max_batches <= 10000:
        parser.error("max-batches must be 1-10000")
    try:
        asyncio.run(build(args))
    except (ValueError, PermissionError, TimeoutError) as exc:
        parser.exit(1, f"索引构建停止：{exc}\n未自动重试；已完成的向量批次可在恢复后复用。\n")


if __name__ == "__main__":
    main()
