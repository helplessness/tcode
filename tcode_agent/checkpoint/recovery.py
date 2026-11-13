"""Recovery utilities for checkpoint-based resumption across all modes."""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from .manager import Checkpoint, CheckpointManager


class RecoveryHelper:
    """Helper for recovering from checkpoints in supervisor/team/swarm modes."""

    @classmethod
    def should_resume_task(
        cls,
        task_id: str,
        workdir: Optional[str] = None,
    ) -> tuple[bool, Optional[Checkpoint]]:
        """
        Check if task should be resumed from checkpoint.
        
        Returns: (should_resume, checkpoint_or_none)
        """
        checkpoint = CheckpointManager.load(task_id, workdir=workdir)
        if not checkpoint:
            return False, None
        
        # Don't resume if already completed
        if checkpoint.step_name == "completed":
            return False, None
        
        # Resume if checkpoint is recent (within 24 hours) and not yet finalized
        import time
        age_seconds = time.time() - checkpoint.timestamp
        if age_seconds > 86400:  # 24 hours
            return False, None
        
        return True, checkpoint

    @classmethod
    def apply_checkpoint_context(
        cls,
        checkpoint: Checkpoint,
        prompt: Optional[str] = None,
    ) -> str:
        """
        Build a resume prompt from checkpoint.
        
        Tells agent where to resume from and what was completed.
        """
        parts = [
            "## Resume from Checkpoint",
            f"Task: {checkpoint.task_id}",
            f"Previous step: {checkpoint.step_name}",
            f"Messages processed: {checkpoint.message_cursor}",
        ]
        
        if checkpoint.artifacts:
            parts.append("## Already Completed:")
            for key, value in checkpoint.artifacts.items():
                parts.append(f"- {key}: {value}")
        
        if checkpoint.pending_action:
            parts.append(f"## Next Action: {checkpoint.pending_action}")
        
        if prompt:
            parts.append(f"## User Request: {prompt}")
        
        return "\n".join(parts)

    @classmethod
    def record_recovery(
        cls,
        task_id: str,
        checkpoint: Checkpoint,
        workdir: Optional[str] = None,
    ) -> None:
        """Record that a checkpoint was used for recovery."""
        try:
            checkpoint.metadata["recovered_at"] = __import__("time").time()
            checkpoint.step_name = "resumed"
            CheckpointManager.save(checkpoint, workdir=workdir)
        except Exception:
            pass

    @classmethod
    def mark_step_complete(
        cls,
        task_id: str,
        step_name: str,
        workdir: Optional[str] = None,
        message_cursor: int = 0,
        artifacts: Optional[dict[str, Any]] = None,
        pending_action: str = "",
    ) -> None:
        """Mark a step as complete in checkpoint."""
        checkpoint = CheckpointManager.load(task_id, workdir=workdir)
        if not checkpoint:
            checkpoint = Checkpoint(
                task_id=task_id,
                session_id=task_id,
            )
        
        checkpoint.step_name = step_name
        checkpoint.message_cursor = message_cursor
        if artifacts:
            checkpoint.artifacts.update(artifacts)
        if pending_action:
            checkpoint.pending_action = pending_action
        
        CheckpointManager.save(checkpoint, workdir=workdir)

    @classmethod
    def get_task_checkpoint_history(
        cls,
        task_id: str,
        workdir: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Get checkpoint history for debugging."""
        checkpoints = CheckpointManager.list_history(task_id, workdir=workdir, limit=limit)
        return [c.to_dict() for c in checkpoints]
