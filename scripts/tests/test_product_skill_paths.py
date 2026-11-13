"""Public regressions for product skill paths and old sandbox configurations."""

import asyncio
import json
from pathlib import Path
import subprocess
import sys

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from tcode_agent.runtime.sandbox.base import load_sandbox_config
from tcode_agent.runtime.sandbox.runner_policy import SandboxRunnerPolicy
from tcode_agent.skill.manager import SkillManager
from tcode_agent.tools.builtin.skill_run import SkillRunTool


PROJECT = Path(__file__).resolve().parents[2]


def test_catalog_reads_product_and_legacy_skills(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    legacy = workspace / ".claude/skills/legacy-compat-fixture/SKILL.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("---\nname: legacy-compat-fixture\ndescription: A legacy skill\n---\nFixture.\n")
    monkeypatch.setenv("TCODE_PROJECT_ROOT", str(PROJECT))
    monkeypatch.setenv("TCODE_WORKDIR", str(workspace))
    monkeypatch.setenv("TCODE_CONFIG_ROOT", str(tmp_path))
    monkeypatch.setattr("tcode_agent.skill.routing_config.load_skill_routing_config", lambda _: {
        "catalog": {"inherit_parent_skills": False, "include_home_skills": False, "write_snapshot": False},
    })
    SkillManager.clear()
    try:
        asyncio.run(SkillManager.load(workdir=str(workspace)))
        for name in ("anatomy", "boltz-runner", "paper-literature-search", "primer-design", "docx", "pdf"):
            manifest = PROJECT / ".tcode/skills" / name / "SKILL.md"
            skill = SkillManager.get(name)
            assert skill is not None and Path(skill.location) == manifest
        assert SkillManager.get("boltz-runner").skill_id == "project/boltz-runner@1"
        assert Path(SkillManager.get("legacy-compat-fixture").location) == legacy
        for name in ("docx", "pdf"):
            assert "Version 2.0, January 2004" in (PROJECT / ".tcode/skills" / name / "LICENSE.txt").read_text()
            assert not (PROJECT / ".claude/skills" / name / "LICENSE.txt").exists()
        pdf = SkillManager.get("pdf")
        assert set(pdf.actions) == {"extract-text", "extract-tables", "render-pages", "ocr"}
        for action in pdf.actions.values():
            assert action.capability == "document.pdf"
            SkillRunTool._validate_entrypoint(pdf.location, action)
    finally:
        SkillManager.clear()


def test_pdf_text_action_writes_existing_artifact_contract(tmp_path):
    entry = PROJECT / ".tcode/skills/pdf/scripts/tcode_pdf_actions.py"
    source = tmp_path / "fixture.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }),
        }),
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 100 Td (Local PDF fixture) Tj ET")
    page[NameObject("/Contents")] = stream
    writer.write(source)
    output = tmp_path / "artifacts/text.json"
    subprocess.run([sys.executable, str(entry), str(source), str(output)], check=True, timeout=30)
    data = json.loads(output.read_text())
    assert data["page_count"] == len(data["pages"]) > 0
    assert data["pages"][0]["page"] == 1 and data["full_text"] == "Local PDF fixture"


@pytest.mark.parametrize("legacy_trusted", [False, True])
def test_runner_keeps_legacy_policy_without_trusting_other_roots(tmp_path, monkeypatch, legacy_trusted):
    project, unrelated = tmp_path / "project", tmp_path / "other"
    for root in (project, unrelated):
        (root / ".claude/skills").mkdir(parents=True)
        (root / ".tcode/skills").mkdir(parents=True)
    old, new = project / ".claude/skills", project / ".tcode/skills"
    other = unrelated / ".claude/skills"
    workspace = tmp_path / "workdirs/tenant/user/workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("TCODE_ISOLATION_MODE", "development")
    monkeypatch.setenv("TCODE_PROJECT_ROOT", str(project))
    monkeypatch.setenv("TCODE_SANDBOX_RUNNER_WORKSPACE_ROOT", str(tmp_path / "workdirs"))
    monkeypatch.setenv("TCODE_SANDBOX_RUNNER_REQUIRE_OWNER_METADATA", "true")
    monkeypatch.setenv("TCODE_SANDBOX_RUNNER_ALLOWED_MOUNT_ROOTS", f"{old},{other}")
    monkeypatch.setenv("TCODE_SANDBOX_RUNNER_TRUSTED_MOUNT_ROOTS", str(old) if legacy_trusted else "")
    config = load_sandbox_config()
    policy = SandboxRunnerPolicy.from_env(config)
    assert set(policy.allowed_mount_roots) == {old, new, other}
    assert set(policy.trusted_mount_roots) == ({old, new} if legacy_trusted else set())
    payload = {"tool_name": "skill_run", "command": "true", "host_workspace": str(workspace),
               "metadata": {"tenant_id": "tenant", "user_id": "user", "workspace_id": "workspace"}}
    mount = {"source": str(new), "target": "/workspace/.external/skills", "read_only": True}
    if legacy_trusted:
        spec = policy.validate({**payload, "mounts": [mount]}, config)
        assert spec.mounts[-1].source == new and spec.mounts[-1].read_only
    else:
        with pytest.raises(ValueError, match="does not belong to this owner"):
            policy.validate({**payload, "mounts": [mount]}, config)
    with pytest.raises(ValueError, match="must be read-only"):
        policy.validate({**payload, "mounts": [{**mount, "read_only": False}]}, config)
