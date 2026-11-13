"""维护命令：校验中文 Wiki，显式发布新清单；不联网、不构建向量。

python -m scripts.enzyme_wiki_manifest
python -m scripts.enzyme_wiki_manifest --version 4.0.1 --write
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from jsonschema import Draft202012Validator
from plugins.enzyme_reaction_discovery.tools.markdown_wiki import parse_wiki
from plugins.enzyme_reaction_discovery.tools.wiki_chunks import evidence_fragments

PLUGIN = Path(__file__).resolve().parents[1] / "plugins/enzyme_reaction_discovery"


def prepare_manifest(plugin=PLUGIN, version=None):
    root = Path(plugin).resolve()
    knowledge = root / "knowledge"
    current = json.loads((knowledge / "manifest.json").read_text(encoding="utf-8"))
    if version is not None and not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("knowledge version must be MAJOR.MINOR.PATCH")
    release = version or current["knowledge_version"]
    previous = {e["card_id"]: e for e in current["cards"]}
    validator = Draft202012Validator(json.loads((root / "schemas/knowledge_card.schema.json").read_text()))
    entries, proteins, card_ids, activity_count = [], set(), set(), 0
    for path in sorted((knowledge / "cards").glob("*.md")):
        if path.is_symlink() or not path.resolve().is_relative_to(knowledge / "cards"):
            raise ValueError("Wiki source must be a regular file inside knowledge/cards")
        match = re.fullmatch(r"(ERD-CARD-[0-9]{3,})(?:-[^/]+)?\.md", path.name)
        if not match or match[1] in card_ids:
            raise ValueError(f"invalid or duplicate Wiki filename: {path.name}")
        cid = match[1]
        text = path.read_text(encoding="utf-8")
        sha = hashlib.sha256(text.encode()).hexdigest()
        old = previous.get(cid, {})
        entry = {"card_id": cid, "path": path.relative_to(knowledge).as_posix(),
                 "card_version": old.get("card_version", release) if sha == old.get("sha256") else release,
                 "sha256": sha}
        document = parse_wiki(text, entry)
        activity_count += document["identity"]["identity_level"] == "named_enzyme"
        validator.validate(document)
        list(evidence_fragments(document))  # 发布前检验切片可索引，失败不替换 manifest。
        protein = document["identity"]["enzyme_id"]
        if protein in proteins:
            raise ValueError(f"duplicate enzyme identity: {protein}")
        proteins.add(protein)
        card_ids.add(cid)
        entries.append(entry)
    if not entries:
        raise ValueError("cannot publish an empty Wiki library")
    if list((knowledge / "cards").glob("*.json")):
        raise ValueError("retire legacy JSON outside production knowledge before publishing Markdown")
    changed = entries != current["cards"]
    if changed and release == current["knowledge_version"]:
        raise ValueError("Wiki changed: provide a new --version before publishing")
    result = {**current, "knowledge_version": release, "cards": entries}
    if activity_count:
        result["policy"] = {**current.get("policy", {}), "identity_level": "protein_or_named_enzyme",
                            "enzyme_activity_is_not_protein": True}
    if changed or release != current["knowledge_version"]:
        result["released_at"] = datetime.date.today().isoformat()
    return result, result != current


def publish_manifest(plugin=PLUGIN, version=None, *, write=False):
    manifest, changed = prepare_manifest(plugin, version)
    if write and changed:
        target = Path(plugin).resolve() / "knowledge/manifest.json"
        # 先完整校验，再同目录原子替换；不修改 Wiki 正文或任何运行数据。
        descriptor, temporary = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    root = Path(plugin).resolve() / "knowledge"
    protein_count = sum(parse_wiki((root / entry["path"]).read_text(encoding="utf-8"), entry)["identity"]["identity_level"] == "protein"
                        for entry in manifest["cards"])
    return {"valid": True, "changed": changed, "written": bool(write and changed),
            "knowledge_version": manifest["knowledge_version"], "card_count": len(manifest["cards"]),
            "protein_count": protein_count, "enzyme_activity_count": len(manifest["cards"]) - protein_count}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version")
    parser.add_argument("--write", action="store_true", help="显式发布；默认只校验")
    args = parser.parse_args()
    print(json.dumps(publish_manifest(version=args.version, write=args.write), ensure_ascii=False))


if __name__ == "__main__":
    main()
