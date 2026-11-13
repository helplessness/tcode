"""Explicitly transfer side history and private artifacts into its main chat."""

import hashlib
import json
import shutil
from dataclasses import fields
from pathlib import Path

from ..research.config import load_research_settings
from ..research.journal import ResearchEventSink
from ..research.models import ResearchEventInput
from ..research.store import ResearchEventStore
from .paper_library_loader import paper_library_module
from .paper_wiki_loader import paper_wiki_module

IMPORT_PREFIX = "[SIDE CHAT CONTEXT IMPORT]"


def rewrite_paths(value, source, destination):
    if isinstance(value, str):
        return value.replace(str(source), str(destination))
    if isinstance(value, list):
        return [rewrite_paths(item, source, destination) for item in value]
    if isinstance(value, dict):
        return {key: rewrite_paths(item, source, destination) for key, item in value.items()}
    return value


def promote_side_chat_artifacts(side, parent, import_id):
    """Copy to a new owned location; merge formal stores through their APIs."""
    source = Path(side.workdir).resolve()
    target_root = Path(parent.workdir).resolve()
    # Legacy shared-workdir side chats have no private store to publish.
    if source == target_root:
        return {"artifact_root": str(source), "paper_ids": []}
    workspace = (side.metadata or {}).get("agent_workspace") or {}
    if not workspace.get("isolated") or Path(workspace.get("execution_workdir", "")).resolve() != source:
        raise ValueError("无法确认侧聊临时工作区，未转入文件")
    destination = target_root / ".tcode" / "side-chat-imports" / side.id / import_id
    if destination.is_symlink():
        raise ValueError("注入目录不能是符号链接")
    def ignore(directory, names):
        return [name for name in names if name in {"logs", "tmp", "__pycache__"} or (Path(directory) / name).is_symlink()]
    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=ignore)
    library_relative = Path(".tcode/plugins/paper_research_workflow/library")
    paper_ids = []
    if (source / library_relative / "registry.json").is_file():
        library = paper_library_module().PaperLibraryStore
        source_store = library(library_root=source / library_relative, workdir=str(source))
        target_store = library(library_root=target_root / library_relative, workdir=str(target_root))
        wiki = paper_wiki_module()
        compiler = wiki.PaperWikiCompiler(wiki_store=wiki.PaperWikiStore(library_root=target_root / library_relative, workdir=str(target_root)), library_store=target_store)
        for entry in source_store._load_registry().get("documents", []):
            doc_id = str(entry.get("doc_id") or "")
            if not doc_id:
                continue
            record = source_store.get_record(doc_id, sync_evidence=False)
            record = rewrite_paths(record.get("record", record), source, destination)
            try:
                imported = target_store.import_portable_record(record, pack_id=f"side-chat:{side.id}", source_doc_id=doc_id)
                # Retain original PDFs/derived binaries in the durable import,
                # without reparsing or replacing an existing main library record.
                published = imported["record"]
                for field in ("raw_artifacts", "visual_artifacts"):
                    if record.get(field):
                        published[field] = record[field]
                published["artifacts"] = {
                    **(record.get("artifacts") or {}), **published["artifacts"],
                }
                pdf = str(record.get("pdf_path") or "")
                if pdf and Path(pdf).is_file():
                    published.update(pdf_path=pdf, source_pdf_missing=False)
                    published["available_layers"] = list(dict.fromkeys([*published["available_layers"], "original_pdf"]))
                    published["missing_layers"] = [layer for layer in published["missing_layers"] if layer != "original_pdf"]
                target_store._write_record(doc_id, published)
                with target_store._file_lock("registry"):
                    registry = target_store._load_registry()
                    for item in registry.get("documents", []):
                        if item.get("doc_id") == doc_id:
                            for field in ("pdf_path", "source_pdf_missing", "available_layers", "missing_layers"):
                                item[field] = published[field]
                    target_store._save_registry(registry)
            except FileExistsError:
                pass  # Already imported/owned by the main library; do not overwrite.
            target_store.sync_evidence_for_record(doc_id)
            # Explicit injection publishes new evidence-backed pages. Existing
            # pages still go through the compiler's conflict/proposal checks.
            compiler.compile_document(doc_id, created_by="side_chat_injection", auto_approve_new_pages=True)
            paper_ids.append(doc_id)
    settings = load_research_settings()
    if settings.journal_active and settings.database_path(str(source)).is_file():
        events = ResearchEventStore.list_events(tenant_id=parent.tenant_id, user_id=parent.user_id, workspace_id=parent.workspace_id, workdir=str(source), limit=5000)
        allowed = {field.name for field in fields(ResearchEventInput)}
        for event in events:
            data = {key: value for key, value in event.to_dict().items() if key in allowed}
            data.update(session_id=parent.id, idempotency_key=f"side-chat-import:{side.id}:{event.event_id}", payload=rewrite_paths(data.get("payload", {}), source, destination))
            persisted = ResearchEventSink.append(ResearchEventInput(**data), workdir=str(target_root))
            if persisted is None:
                raise ValueError("研究记录转入失败，请重试注入")
    return {"artifact_root": str(destination), "paper_ids": paper_ids}


def make_side_chat_import(side, messages, artifacts):
    body = json.dumps({"side_session_id": side.id, "messages": messages, "artifacts": artifacts}, ensure_ascii=False)
    identity = hashlib.sha256(body.encode()).hexdigest()[:24]
    content = (IMPORT_PREFIX + "\n以下是用户明确注入、已经发生的侧聊完整记录及证据，等同于当前会话已完成的讨论。"
               "保留其中的需求、决定、文件和未解决问题；不要重复执行已完成的工具操作。\n" + body)
    return {"id": f"side-import-{identity}", "content": content, "pending": True, "side_session_id": side.id}
