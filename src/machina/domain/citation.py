"""Citation — a verifiable reference from an agent answer back to a source.

The agent emits citations whenever it grounds a claim in retrieved
documents. Each citation points to a specific document chunk so the
user can trace the answer back to its origin.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Citation(BaseModel):
    """A pointer from an answer back to its source chunk.

    Args:
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
        """Render a compact inline marker like ``[source:page]``."""
        if self.page > 0:
            return f"[{self.source}:{self.page}]"
        return f"[{self.source}]" if self.source else f"[{self.chunk_id}]"


class AgentResponse(BaseModel):
    """Structured agent output carrying text and grounded citations.

    Args:
        text: The rendered answer (with inline citation markers preserved
            but the trailing ``<citations>`` block stripped).
        citations: List of source citations, one per chunk the agent
            relied on. May be empty when the answer is not grounded in
            documents.
    """

    text: str = Field(default="", description="Rendered answer text")
    citations: list[Citation] = Field(default_factory=list, description="Source citations")
