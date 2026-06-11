"""Citation — a verifiable reference from an agent answer back to a source.

The agent emits citations whenever it grounds a claim in retrieved
documents. Each citation points to a specific document chunk so the
user can trace the answer back to its origin.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Citation(BaseModel):
    """A pointer from an answer back to its source chunk.

    Attributes:
        chunk_id: Deterministic identifier of the source chunk (matches
            :attr:`DocumentChunk.chunk_id`).
        source: Source file path or document name.
        page: Page number, when known. ``0`` means "no page".
        quote: Optional short verbatim excerpt the claim is grounded in.
        confidence: Optional confidence score in ``[0.0, 1.0]``.

    Example:
        ```python
        Citation(chunk_id="abc123", source="manuals/pump.pdf", page=42)
        ```
    """

    chunk_id: str = Field(default="", description="Deterministic chunk identifier")
    source: str = Field(default="", description="File path or document name")
    page: int = Field(default=0, ge=0, description="Page number")
    quote: str = Field(default="", description="Optional excerpt grounding the claim")
    confidence: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Confidence in the citation"
    )

    def inline_marker(self) -> str:
        """Render a compact inline marker like ``[source:page]``.

        Note:
            :attr:`AgentResponse.text` now carries numeric ``[n]`` markers
            renormalized at egress; this ``[source:page]`` format is used
            nowhere in the rendered text. Kept for backward compatibility.
        """
        if self.page > 0:
            return f"[{self.source}:{self.page}]"
        return f"[{self.source}]" if self.source else f"[{self.chunk_id}]"


class AgentResponse(BaseModel):
    """Structured agent output carrying text and grounded citations.

    Attributes:
        text: The rendered answer, carrying inline ``[n]`` citation markers
            renormalized to ``1..N`` at egress (the trailing ``<citations>``
            block is stripped).
        citations: List of source citations, one per chunk the agent
            relied on, in display order: ``citations[0]`` corresponds to the
            inline ``[1]`` marker (numbered by first appearance in the
            prose; citations referenced only in the ``<citations>`` block
            are appended after the inline ones). May be empty when the
            answer is not grounded in documents.
        is_fallback: ``True`` when ``text`` is a synthetic fallback the
            runtime substituted because the LLM returned no usable output
            (an empty completion or a citations-only block), not a real
            model answer. Lets programmatic callers and monitors tell a
            genuine response apart from a degraded one.
        completeness: ``"partial"`` when the runtime was forced to finalize
            the turn before the agent could confirm it had retrieved
            everything (a no-progress / suppressed-read break), so the answer
            may be incomplete; ``"complete"`` otherwise. Distinct from
            ``is_fallback`` — a partial answer is a *real* model answer that
            may be missing data, not a synthetic non-answer. Monitors that
            count fallbacks must not treat a partial answer as degraded.
    """

    text: str = Field(default="", description="Rendered answer text")
    citations: list[Citation] = Field(default_factory=list, description="Source citations")
    is_fallback: bool = Field(
        default=False, description="True when text is a synthetic empty-output fallback"
    )
    completeness: Literal["complete", "partial"] = Field(
        default="complete",
        description="'partial' when finalization was forced before the agent confirmed completeness",
    )
