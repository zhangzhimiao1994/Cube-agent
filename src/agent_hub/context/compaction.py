from __future__ import annotations

from uuid import uuid4

from agent_hub.context.builder import ContextBuildInput, estimate_tokens
from agent_hub.runtime.contracts import Artifact


class ContextCompactor:
    def compact(self, value: ContextBuildInput, *, max_summary_tokens: int = 512) -> Artifact:
        if max_summary_tokens < 1:
            raise ValueError("max_summary_tokens must be positive")
        protected: list[str] = []
        protected.extend(f"UNRESOLVED_APPROVAL: {item}" for item in value.unresolved_approvals)
        protected.extend(f"CURRENT_CONSTRAINT: {item}" for item in value.current_constraints)
        preserved: list[str] = list(protected)
        if value.compacted_summary:
            preserved.append(value.compacted_summary)
        preserved.extend(value.recent_transcript)
        summary = "\n".join(preserved)
        if estimate_tokens(summary) > max_summary_tokens:
            protected_text = "\n".join(protected)
            remaining_chars = max(0, max_summary_tokens * 4 - len(protected_text) - 1)
            tail = "\n".join(item for item in preserved if item not in protected)
            summary = protected_text if not tail else f"{protected_text}\n{tail[:remaining_chars]}"
        return Artifact(
            id=uuid4(),
            version=1,
            type="text",
            producer="context_compactor",
            content={
                "text": summary or "No prior context.",
                "unresolved_approvals": value.unresolved_approvals,
                "current_constraints": value.current_constraints,
            },
        )
