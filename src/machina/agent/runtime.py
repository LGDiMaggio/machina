"""Agent runtime — orchestrates LLM, connectors, and domain logic.

The :class:`Agent` is the central class of Machina.  It receives
messages (from Telegram, CLI, or programmatically), resolves entities,
gathers context from connectors, calls the LLM with domain-aware
prompts, and executes tool calls.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import time
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

import structlog

from machina.agent.citations import parse_response, renormalize_markers, strip_markers
from machina.agent.entity_resolver import RESOLUTION_MIN_CONFIDENCE, EntityResolver
from machina.agent.prompts import (
    DOC_DISPLAY_WINDOW,
    build_context_message,
    build_system_prompt,
    safe_source,
    safe_text,
)
from machina.connectors.base import ConnectorRegistry, set_sandbox_mode
from machina.connectors.capabilities import Capability
from machina.connectors.comms.types import is_affirmation, is_decline
from machina.domain.citation import AgentResponse, Citation
from machina.domain.plant import Plant
from machina.exceptions import LLMError
from machina.llm.provider import LLMProvider
from machina.llm.tools import BUILTIN_TOOLS, MUTATING_TOOLS
from machina.observability.tracing import ActionTracer
from machina.workflows.engine import WorkflowEngine

if TYPE_CHECKING:
    from machina.domain.failure_mode import FailureMode
    from machina.workflows.models import Workflow, WorkflowResult

logger = structlog.get_logger(__name__)

# Tools whose execution mutates external state, memoised per turn in the LLM
# loop so a model that re-requests the same write does not trigger the side
# effect twice. Sourced from llm.tools.MUTATING_TOOLS (single source of truth,
# co-located with the tool definitions) to prevent drift between the dispatch
# table and this guard.
_SIDE_EFFECTING_TOOLS: frozenset[str] = MUTATING_TOOLS

# Finalize-only tripwire for tool-call FRAGMENTS (U6, PR #55 gap family 5):
# payloads that are recognisably tool-call-shaped but do NOT parse — truncated
# JSON (the model ran out of tokens mid-call) or hopelessly mixed quoting.
# BOTH regexes must hit: a string-valued name key (a "name" key, or shape C's
# string-valued "function" key — gap family 6) AND a call-marker key
# (arguments/parameters/tool_calls, or a function key opening an object).
# Truncated plain-data JSON carries a name key but no call marker, so it is
# not suppressed. See _looks_like_leaked_tool_call_fragment.
_LEAK_FRAGMENT_NAME_RE = re.compile(r"[\"'](?:name|function)[\"']\s*:\s*[\"']")
_LEAK_FRAGMENT_MARKER_RE = re.compile(
    r"[\"'](?:arguments|parameters|tool_calls)[\"']\s*:|[\"']function[\"']\s*:\s*\{"
)


# Trivial English stopwords dropped when tokenizing LLM free-text symptoms at
# the ``diagnose_failure`` tool boundary. Deliberately tiny: only glue words
# that carry no diagnostic signal. Modifiers like "high"/"low" are kept — they
# simply never overlap an indicator token, so they cannot cause false hits.
_SYMPTOM_STOPWORDS: frozenset[str] = frozenset(
    {"a", "an", "and", "are", "at", "for", "in", "is", "of", "on", "or", "the", "to", "with"}
)

_SYMPTOM_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _symptom_tokens(text: str) -> set[str]:
    """Normalize free text into matchable tokens for ``diagnose_failure``.

    Lowercases, splits on non-alphanumerics, drops stopwords and
    single-character tokens (unit suffixes such as the ``s`` in ``mm_s``
    or the ``c`` in ``temperature_c`` would otherwise create spurious
    cross-mode matches).

    This tokenization lives at the LLM tool boundary ONLY: it lets
    "high vibration" match the canonical indicator
    ``vibration_velocity_mm_s`` via the shared token ``vibration``.
    Because indicators are tokenized with the same function, passing an
    exact canonical indicator name as a symptom still matches — the
    fuzzy matching is a strict superset of exact matching. The
    alarm/workflow path (:class:`FailureAnalyzer.diagnose`) keeps its
    exact-set-intersection semantics untouched.
    """
    return {
        tok
        for tok in _SYMPTOM_TOKEN_SPLIT_RE.split(text.lower())
        if len(tok) > 1 and tok not in _SYMPTOM_STOPWORDS
    }


def _strip_code_fence(text: str) -> str:
    """Strip one surrounding markdown code fence (``` / ```json), if present.

    The closing fence is optional — weak models drop it routinely. A fence
    whose payload shares the opener line is left untouched; the stripped body
    must still parse as a tool-call shape downstream, so leniency here cannot
    misclassify prose.
    """
    if not text.startswith("```"):
        return text
    first_nl = text.find("\n")
    if first_nl == -1:
        return text
    inner = text[first_nl + 1 :]
    if inner.endswith("```"):
        inner = inner[:-3]
    return inner.strip()


def _parse_leak_payload(text: str) -> dict[str, Any] | list[Any] | None:
    """Parse candidate leak text as JSON, falling back to a Python literal.

    The literal fallback (``ast.literal_eval`` — safe, no code execution)
    accepts the single-quoted pseudo-JSON some models emit
    (PR #55 gap family 4): ``{'name': ..., 'arguments': {...}}``. Only dict
    or list results are meaningful as tool-call candidates.
    """
    try:
        obj: Any = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        try:
            obj = ast.literal_eval(text)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            return None
    return obj if isinstance(obj, dict | list) else None


def _is_degenerate_json_answer(text: str) -> bool:
    """Whether ``text`` is an empty JSON container (``{}`` or ``[]``).

    The 2026-06-10 post-fix conversational eval (deepseek-r1:8b) surfaced a
    mode where the model's final completion — typically right after a
    leaked-read recovery fed the result back — is literally ``{}``. Such an
    answer carries zero information, exactly like an empty completion, but
    no other guard sees it: it is not tool-call-shaped (no name/function
    marker), not an empty string, and not a prior-turn echo. Parsing (rather
    than string-matching) also catches whitespace variants like ``{ }``.

    A non-empty JSON answer never matches — legitimate data that happens to
    be JSON passes through (tool-call-shaped payloads are the leak
    detector's job), and non-container JSON (``null``, ``0``, a quoted
    string) is out of scope by design.

    Args:
        text: The rendered answer text.

    Returns:
        ``True`` when the stripped text parses to an empty dict or list.
    """
    stripped = text.strip()
    # Cheap gate: every JSON container opens with a bracket; prose skips the
    # parse entirely.
    if not stripped.startswith(("{", "[")):
        return False
    try:
        obj = json.loads(stripped)
    except ValueError:
        return False
    return isinstance(obj, dict | list) and not obj


# Lifetime of a stored pending write-confirmation (seconds). A pending
# confirmation is meant for the IMMEDIATE next message; an hour is
# generous-but-safe and bounds the window in which a much-later bare
# affirmation ("ok"/"sì") could execute a stale write. Measured with
# ``time.monotonic()`` so it is immune to wall-clock changes. Named so it is
# tunable.
_PENDING_ACTION_TTL_SECONDS: float = 3600.0

# Surfaced to the user when the LLM yields no usable text — an empty
# completion, or a response that was nothing but a citations block. Weak
# local models hit this often; blank output reads as a crash, so we say
# something honest and actionable instead of delivering nothing.
_EMPTY_RESPONSE_FALLBACK = (
    "I couldn't produce a response to that. Try rephrasing your question, "
    "or switch to a more capable model."
)

# Fed back to the model when a side-effecting tool call is re-issued within the
# same turn. The first call's result is replayed (the write does NOT run again);
# the note tells the model to stop re-calling and summarise.
_DUPLICATE_TOOL_NOTE = (
    "This action was already completed earlier in this turn. Do not call it "
    "again — summarise the result for the user."
)

# Same situation, but the original call was a sandbox no-op (no real write). We
# must NOT claim the action "executed" — that would mislead the model about a
# mutation that never happened.
_DUPLICATE_TOOL_NOTE_SANDBOX = (
    "This action was already simulated earlier in this turn (sandbox mode — no "
    "real change was made). Do not call it again — summarise the result."
)

# Re-fed when a READ-ONLY tool call is re-issued with identical arguments within
# the same turn. The cached result is replayed (the connector is NOT queried
# again) and the note nudges the model to answer instead of looping. Weak local
# models routinely re-request the same lookup every iteration, which — without
# this — burns the whole iteration budget on redundant reads.
_DUPLICATE_READ_NOTE = (
    "You already retrieved this exact information earlier in this turn. Do not "
    "request it again — answer the user with the data you already have."
)

# Hard cap on how many times the same side-effecting call may be suppressed in
# one turn before the loop stops offering tools and forces a final answer. The
# annotated duplicate result is only a cooperative hint; a model that ignores it
# would otherwise loop to ``max_iterations``. Bounds the worst case tightly.
_MAX_DUPLICATE_SUPPRESSIONS = 2

# Above this many assets, ``list_assets`` returns a count plus a grouped summary
# instead of every record, so a large plant cannot flood the prompt/answer (R1.2).
_ENUM_SUMMARY_THRESHOLD = 50

# Prefix of the grounding note appended to a stored assistant reply. Single
# source of truth shared by _HISTORY_SOURCES_TEMPLATE (which writes the note)
# and _strip_history_note (which removes it before echo comparison) so the two
# cannot silently desync if the wording is ever changed.
_HISTORY_NOTE_PREFIX = "\n\n[Sources used in this answer:"

# Appended to the assistant reply stored in conversation history (never to the
# user-facing text) so a follow-up like "what are the sources?" resolves from
# memory instead of forcing a fresh document search. See _history_text.
_HISTORY_SOURCES_TEMPLATE = "{rendered}" + _HISTORY_NOTE_PREFIX + " {sources}]"


def _history_text(rendered: str, citations: list[Citation]) -> str:
    """Build the assistant text to store in history, carrying its grounding.

    The rendered answer keeps inline ``[n]`` markers, but ``parse_response``
    strips the ``<citations>`` block and the per-turn "Retrieved Context"
    system message is never recorded — so the source filenames that grounded
    the answer would otherwise be lost from the conversation. Without them, a
    follow-up ("what are the sources?") has nothing to resolve against and the
    model re-runs document search and repeats the whole prior answer.

    Appending the cited sources to the *history* entry (not to the user-facing
    ``AgentResponse.text``) lets such follow-ups be answered from memory. Each
    ``Citation.source`` is already ``safe_source``-sanitised by the citation
    parser, so no filesystem path can leak back into the next turn's prompt.

    Args:
        rendered: The user-facing answer text (citation block already stripped).
        citations: Citations parsed for this turn; may be empty.

    Returns:
        ``rendered`` unchanged when there are no cited sources, otherwise
        ``rendered`` with a trailing source note. Sources are de-duplicated
        in first-seen order.
    """
    sources = list(dict.fromkeys(c.source for c in citations if c.source))
    if not sources:
        return rendered
    return _HISTORY_SOURCES_TEMPLATE.format(rendered=rendered, sources=", ".join(sources))


# Substituted (and flagged via ``AgentResponse.is_fallback``) when a turn's
# rendered answer merely repeats the previous turn's answer. Weak local models
# copy the prior assistant message straight out of conversation history, so the
# user sees the same long paragraph on every follow-up; delivering it again
# reads as the agent being stuck. Mirrors :data:`_EMPTY_RESPONSE_FALLBACK` — an
# honest, distinct degrade. The echo is NOT written back to history, so it
# cannot keep priming the next turn. See _is_echo / _is_echo_of_previous.
_REPEATED_RESPONSE_FALLBACK = (
    "I seem to be repeating my previous answer. Could you rephrase your "
    "question, or switch to a more capable model? The current model may be "
    "struggling to follow this conversation."
)

# Appended to a real answer when the runtime had to force the turn to finalize
# before the agent confirmed it had retrieved everything (a no-progress or
# suppressed-read break). The answer stands, but the user is told it may be
# incomplete rather than letting an unverified "that's everything" claim go
# unqualified (R1.3 / R1.4). Paired with ``AgentResponse.completeness="partial"``.
_PARTIAL_COMPLETENESS_HEDGE = (
    "\n\n_Note: I stopped before confirming I'd retrieved everything here, so this "
    "may be incomplete — ask me to re-check a specific item if you need certainty._"
)

# Substituted (and flagged via ``AgentResponse.is_fallback``) when the model
# emitted a tool/function call as its final answer text and the runtime could
# not safely recover it (an unknown tool, a leaked write that must not be
# auto-executed, or a repeated leak). Guarantees raw tool-call JSON never
# reaches the user. See _detect_leaked_tool_call and the _finalize_turn backstop.
_TOOL_CALL_LEAK_FALLBACK = (
    "I couldn't complete that request properly — the model returned an internal "
    "tool instruction instead of an answer. Please rephrase, or switch to a more "
    "capable model."
)

# Fed back to the model (as the tool result) when it supplies a non-empty but
# unparseable arguments blob, so it can retry with valid JSON instead of the
# error being silently swallowed as ``{}`` and crashing a downstream tool that
# needs required keys. FIXED text — never the raw exception, which echoes the
# offending (possibly injected) argument bytes back into the prompt (H1).
_INVALID_ARGS_MESSAGE = (
    "Arguments could not be parsed as valid JSON. Retry this call with valid JSON arguments."
)

# Hard cap on how many unparseable-argument retries one turn may feed back
# before the loop stops offering tools and forces a final answer. The fed-back
# error is "progress" to the no-progress guard, so a model that emits *different*
# junk every iteration would otherwise loop to max_iterations — this counter is
# the real bound (H1).
_MAX_ARG_CORRECTION_ATTEMPTS = 2

# Reasoning models (e.g. deepseek-r1) emit their chain of thought as
# <think>...</think> blocks inside message content (U7/R10). Matched
# case-insensitively and across newlines. An UNCLOSED <think> matches to
# end-of-string: weak models truncate mid-reasoning, and everything after the
# opener is reasoning, never answer — so the whole tail is scrubbed and the
# empty-response fallback fires. Near-miss tags (<thinking>, <b>) never match.
_THINK_BLOCK_RE = re.compile(r"<think>.*?(?:</think>|\Z)", re.IGNORECASE | re.DOTALL)

# Orphan closers the regex above cannot reach: some serving stacks emit
# deepseek-r1 reasoning WITHOUT the opener (the content ends the chain of
# thought with a bare "</think>" before the answer), and nested openers
# leave a stray closer behind the non-greedy sub. _finalize_turn therefore
# runs a second pass: while a closer remains, drop everything up to and
# including the FIRST one — pre-closer text is reasoning by construction.
# Fail-closed trade-off (same family as R9): a legit literal "</think>"
# inside an answer is sacrificed rather than ever showing reasoning.
# Near-miss tags ("<thinking>", "<b>") still never match.
_THINK_CLOSE_TAG = "</think>"

# Minimum rendered length (characters) before the cross-turn echo guard applies.
# Short generic replies ("Yes.", "In stock.") legitimately recur across turns;
# the degenerate-echo failure mode is always a long canned paragraph, so the
# guard only inspects substantial answers and never trips on terse ones.
_MIN_ECHO_LENGTH = 200

# Normalised char-similarity at/above which two answers are treated as the same
# answer. High enough that two genuinely different maintenance answers never
# collide, low enough to catch an echo that drifts by a word or two.
_ECHO_SIMILARITY_THRESHOLD = 0.92


def _normalized_for_echo(text: str) -> str:
    """Collapse runs of whitespace and lowercase, for echo comparison."""
    return " ".join(text.split()).lower()


def _strip_history_note(text: str) -> str:
    """Drop a trailing ``[Sources used in this answer: ...]`` note, if present.

    The previous turn's stored assistant text may carry the grounding note
    :func:`_history_text` appends; the current turn's freshly rendered answer
    never does. Stripping it before comparison keeps the note's presence from
    masking an otherwise-verbatim echo.
    """
    idx = text.rfind(_HISTORY_NOTE_PREFIX)
    return text[:idx] if idx != -1 else text


def _is_echo(rendered: str, previous: str) -> bool:
    """Whether ``rendered`` repeats ``previous`` closely enough to be an echo.

    Only substantial answers (``len(rendered) >= _MIN_ECHO_LENGTH``) are
    considered — short replies legitimately recur across turns. Comparison is
    whitespace/case-insensitive, ignores any trailing grounding note on the
    stored ``previous`` text, and runs over the MARKER-STRIPPED representation
    of both sides: stored history is marker-stripped (U3), so two near-identical
    answers differing only in inline ``[n]`` markers must still compare equal.
    Returns ``True`` when normalised character similarity meets
    :data:`_ECHO_SIMILARITY_THRESHOLD`.

    Args:
        rendered: This turn's freshly rendered answer.
        previous: The previous turn's stored assistant text (may carry a
            grounding note, stripped before comparison).

    Returns:
        ``True`` when the two are the same answer up to the threshold.
    """
    if len(rendered) < _MIN_ECHO_LENGTH:
        return False
    import difflib

    current = _normalized_for_echo(strip_markers(rendered))
    prior = _normalized_for_echo(strip_markers(_strip_history_note(previous)))
    if not current or not prior:
        return False
    return difflib.SequenceMatcher(None, current, prior).ratio() >= _ECHO_SIMILARITY_THRESHOLD


def _footer_source(citation: Citation) -> str:
    """``source:page`` body for one Sources-footer entry."""
    if citation.page > 0 and citation.source:
        return f"{citation.source}:{citation.page}"
    return citation.source or citation.chunk_id


def _format_response_for_channel(response: AgentResponse) -> str:
    """Render an :class:`AgentResponse` for delivery on a channel.

    ``response.text`` carries renormalized inline ``[n]`` markers (1..N by
    first appearance — see :func:`machina.agent.citations.renormalize_markers`).
    When citations are present, a compact ``Sources`` footer is appended whose
    entries are numbered by 1-based position in ``response.citations`` — the
    *citations list order == displayed number order* invariant — so every
    inline ``[n]`` lines up with footer entry ``[n] source:page`` and the
    operator can trace the answer back to its origin in chat surfaces that
    don't expose the structured field.
    """
    if not response.citations:
        return response.text
    sources = "\n".join(
        f"  • [{i}] {_footer_source(c)}" for i, c in enumerate(response.citations, 1)
    )
    return f"{response.text}\n\n— Sources:\n{sources}"


def _executed_write_fallback(func_name: str, tool_result: Any) -> str:
    """Fallback narration for a confirmed write whose summary came back empty.

    Used only on the two-turn confirmation path, where the write has ALREADY
    executed. The message must reflect success and never invite a retry — the
    generic :data:`_EMPTY_RESPONSE_FALLBACK` ("try rephrasing / switch models")
    would read as a failure and could drive a duplicate write. Surfaces the
    result identifier when the tool returned one.
    """
    identifier = ""
    if isinstance(tool_result, dict):
        identifier = str(tool_result.get("id") or tool_result.get("work_order_id") or "")
    suffix = f" ({identifier})" if identifier else ""
    return (
        f"Done — the {func_name} action completed{suffix}. I couldn't generate a "
        "full summary; switch to a more capable model for a detailed narration."
    )


class Agent:
    """Maintenance AI agent that orchestrates reasoning and actions.

    The agent receives user queries, resolves referenced assets,
    gathers context from configured connectors, and uses an LLM to
    produce grounded, domain-aware responses.

    Args:
        name: Human-readable agent name.
        description: What this agent specialises in.
        plant: The plant with its asset registry.
        connectors: List of connector instances to register.
        channels: Communication channels (Telegram, CLI, etc.).
        llm: LLM provider string (e.g. ``"openai:gpt-4o"``) or
             an :class:`LLMProvider` instance.
        temperature: LLM sampling temperature.
        max_history: Maximum conversation turns to keep in memory.
        workflows: List of workflow definitions to register.
        sandbox: If ``True``, write actions are logged but not executed.
        confirmations: If ``True`` (default), the agent requires human
            confirmation before executing write/mutation tool calls.

    Example:
        ```python
        from machina import Agent, Plant
        from machina.connectors.cmms import GenericCmmsConnector
        from machina.connectors.comms.cli import CliChannel

        plant = Plant(name="Demo Plant")
        cmms = GenericCmmsConnector(data_dir="sample_data/cmms")

        agent = Agent(
            name="Maintenance Assistant",
            plant=plant,
            connectors=[cmms],
            channels=[CliChannel()],
            llm="openai:gpt-4o",
        )
        agent.run()
        ```
    """

    def __init__(
        self,
        *,
        name: str = "Machina Agent",
        description: str = "Maintenance AI assistant",
        plant: Plant | None = None,
        connectors: list[Any] | None = None,
        channels: list[Any] | None = None,
        llm: str | LLMProvider = "openai:gpt-4o",
        temperature: float = 0.1,
        max_history: int = 20,
        workflows: list[Workflow] | None = None,
        sandbox: bool = False,
        confirmations: bool = True,
    ) -> None:
        self.name = name
        self.description = description
        self.plant = plant or Plant(name="Default")
        self._channels = channels or []
        self._max_history = max_history
        self._max_message_length = 10_000

        # LLM provider
        if isinstance(llm, str):
            self._llm = LLMProvider(model=llm, temperature=temperature)
        else:
            self._llm = llm

        # Connector registry
        self._registry = ConnectorRegistry()
        _registered_ids: set[int] = set()
        for i, conn in enumerate(connectors or []):
            cname = getattr(conn, "__class__", type(conn)).__name__
            self._registry.register(f"{cname}_{i}", conn)
            _registered_ids.add(id(conn))

        # Channels are also registered so workflow steps that dispatch via
        # ``find_by_capability`` (e.g. ``channels.send_message``) can reach
        # comms connectors passed as ``channels=``. Dedup by identity: a
        # connector passed to BOTH ``connectors=`` and ``channels=`` is
        # registered once, not twice. See issue #31.
        for i, chan in enumerate(self._channels):
            if id(chan) in _registered_ids:
                continue
            cname = getattr(chan, "__class__", type(chan)).__name__
            self._registry.register(f"channel_{cname}_{i}", chan)
            _registered_ids.add(id(chan))

        # Entity resolver
        self._resolver = EntityResolver(self.plant)

        # Action tracer
        self.tracer = ActionTracer()

        # Sandbox mode — stored on the instance, propagated to the
        # workflow engine via the ``sandbox`` property setter below, and
        # mirrored into the ``connectors.base._sandbox_mode`` contextvar
        # so ``@sandbox_aware`` connector methods see the same value as
        # the engine's heuristic gate.
        self._sandbox = sandbox
        set_sandbox_mode(sandbox)

        # Confirmation gate — agent-loop-local switch (no contextvar, unlike
        # sandbox). Read directly by ``_llm_loop`` to decide whether a
        # write/mutation tool call must be confirmed by a human before it
        # executes. On by default. The gate logic itself is consumed
        # elsewhere; this just holds the value.
        self._confirmations = confirmations

        # Workflow engine
        self._workflows: dict[str, Workflow] = {}
        self._engine = WorkflowEngine(
            registry=self._registry,
            tracer=self.tracer,
            llm=self._llm,
            sandbox=sandbox,
        )
        for wf in workflows or []:
            self._workflows[wf.name] = wf

        # Conversation history per chat
        self._histories: dict[str, list[dict[str, str]]] = {}

        # Turn-surviving pending-action store for the two-turn confirmation
        # degrade (U5). Keyed (chat_id, user_id) →
        # (func_name, args, prompt, stored_monotonic_ts).
        # Follows the ``self._histories`` lifecycle (persists across turns) —
        # deliberately NOT ``self._turn_chunks``, which is reset/popped each
        # turn and would wipe a pending action before the confirming message
        # arrives. On a shared/group channel the key includes ``user_id`` so a
        # different participant cannot confirm another user's pending write; an
        # empty (untrusted) ``user_id`` is never stored (fail-safe withhold).
        # The trailing monotonic timestamp drives TTL expiry on resume so a
        # much-later bare affirmation cannot execute a stale write.
        self._pending_actions: dict[tuple[str, str], tuple[str, dict[str, Any], str, float]] = {}

        # Per-turn chunk registry (chat_id -> chunk_id -> {source, page, content}).
        # Populated by _gather_context and the search_documents tool; consumed by
        # citation parsing at the end of each turn for the source/page fallback.
        self._turn_chunks: dict[str, dict[str, dict[str, Any]]] = {}

        # Per-turn ordered index map (chat_id -> [chunk_id by display position]).
        # Element ``i`` is the chunk the model saw as ``[i + 1]``; an empty
        # string marks a displayed-but-unregistered slot so the visible index
        # stays aligned with what the model saw. Built from the SAME
        # ``enumerate(results[:DOC_DISPLAY_WINDOW], 1)`` the prompt rendering
        # uses — never from the filtered registry, which would drift off-by-k.
        self._turn_ordered: dict[str, list[str]] = {}
        # Per-turn, chat-scoped completeness marker. The LLM loop sets
        # ``"partial"`` when it force-finalizes a turn (no-progress / suppression
        # / iteration-exhaustion break) so _finalize_turn can hedge the answer
        # and flag ``AgentResponse.completeness``. Absent → complete. Popped in
        # _finalize_turn (and the error path), like the chunk registries.
        self._turn_completeness: dict[str, Literal["partial"]] = {}

    # ------------------------------------------------------------------
    # Sandbox mode — single mutation point, propagates to the engine
    # ------------------------------------------------------------------

    @property
    def sandbox(self) -> bool:
        """Whether write actions are intercepted (``True``) or executed.

        Read this attribute through normal access — no behaviour change
        for existing call sites that branch on ``if self.sandbox``.
        """
        return self._sandbox

    @sandbox.setter
    def sandbox(self, value: bool) -> None:
        """Toggle sandbox mode atomically across every enforcement layer.

        Guarantees that the workflow engine's heuristic gate and the
        connector-level ``@sandbox_aware`` decorator both see the new
        value on the next call.  Three pieces of state are updated in
        one place so they cannot drift:

        * ``self._sandbox`` — the canonical value read by ``Agent``
          itself in the ``if self.sandbox`` branches.
        * ``self._engine.sandbox`` — the workflow engine's snapshot.
          Without this, a mutation after construction would leave the
          engine running in its construction-time mode (the original
          ``--live``-ignored bug).
        * ``connectors.base._sandbox_mode`` contextvar — the variable
          the ``@sandbox_aware`` decorator on connector methods checks.
          Without this update, a custom connector write action whose
          name does not match the engine's keyword heuristic (e.g.
          ``cmms.dispatch_field_team``) would bypass both engine and
          decorator and execute against the real system.
        """
        self._sandbox = value
        self._engine.sandbox = value
        set_sandbox_mode(value)

    # ------------------------------------------------------------------
    # Confirmation gate — agent-loop-local, no contextvar
    # ------------------------------------------------------------------

    @property
    def confirmations(self) -> bool:
        """Whether write/mutation tool calls require human confirmation.

        Read this attribute through normal access — the value is consumed
        inside the agent loop to gate side-effecting tool calls. Unlike
        :attr:`sandbox`, there is no contextvar or engine snapshot: the
        switch is purely agent-loop-local.
        """
        return self._confirmations

    @confirmations.setter
    def confirmations(self, value: bool) -> None:
        """Toggle the confirmation gate at runtime.

        Sets the agent-loop-local flag read by the loop. No contextvar or
        engine state is involved (in contrast to :attr:`sandbox`), so this
        setter only updates ``self._confirmations``.
        """
        self._confirmations = value

    # ------------------------------------------------------------------
    # Public API — factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, path: str | Path) -> Agent:
        """Create an Agent from a ``machina.yaml`` configuration file.

        Connectors and channels are instantiated from their ``type``
        strings.  Workflows cannot be defined in YAML (they may
        contain Python callables); register them after construction
        with :meth:`register_workflow`.

        Args:
            path: Path to the YAML configuration file.

        Returns:
            A fully configured ``Agent`` instance.

        Example:
            ```python
            from machina import Agent

            agent = Agent.from_config("machina.yaml")
            agent.run()
            ```
        """
        from machina.config import load_config
        from machina.connectors.factory import create_channel, create_connector

        config = load_config(path)

        plant = Plant(name=config.plant.name, location=config.plant.location)

        connectors = [
            create_connector(cc.type, cc.settings)
            for cc in config.connectors.values()
            if cc.enabled
        ]

        if config.channels:
            channels = [create_channel(ch.type, ch.settings) for ch in config.channels]
        else:
            from machina.connectors.comms.cli import CliChannel

            channels = [CliChannel()]

        return cls(
            name=config.name,
            description=config.description,
            plant=plant,
            connectors=connectors,
            channels=channels,
            llm=config.llm.provider,
            temperature=config.llm.temperature,
            sandbox=config.sandbox,
            confirmations=config.confirmations,
        )

    # ------------------------------------------------------------------
    # Public API — workflows
    # ------------------------------------------------------------------

    @property
    def workflows(self) -> dict[str, Workflow]:
        """Registered workflows (read-only copy)."""
        return dict(self._workflows)

    def register_workflow(self, workflow: Workflow) -> None:
        """Register a workflow for later execution.

        Args:
            workflow: The workflow definition to register.
        """
        self._workflows[workflow.name] = workflow
        logger.info("workflow_registered", workflow=workflow.name)

    async def trigger_workflow(
        self,
        workflow_name: str,
        event: dict[str, Any] | None = None,
    ) -> WorkflowResult:
        """Trigger a registered workflow by name.

        Args:
            workflow_name: Name of a previously registered workflow.
            event: Event data to pass to the workflow.

        Returns:
            A :class:`WorkflowResult` with per-step outcomes.

        Raises:
            WorkflowError: If the workflow is not found.
        """
        from machina.exceptions import WorkflowError

        workflow = self._workflows.get(workflow_name)
        if workflow is None:
            raise WorkflowError(f"Workflow '{workflow_name}' not registered")
        return await self._engine.execute(workflow, event)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect all connectors and load assets."""
        channel_ids = {id(ch) for ch in self._channels}
        for name, conn in self._registry.all().items():
            # Channels are connected below, with a sandbox guard. Skip them
            # here to avoid double-connect now that they share the registry.
            if id(conn) in channel_ids:
                continue
            with self.tracer.trace("connector_connect", connector=name):
                await conn.connect()
            logger.info("connector_ready", connector=name)

        # Auto-load assets from CMMS connectors
        cmms_connectors = self._registry.find_by_capability(Capability.READ_ASSETS)
        for cname, conn in cmms_connectors:
            with self.tracer.trace("load_assets", connector=cname) as span:
                assets = await conn.read_assets()  # type: ignore[attr-defined]
                for asset in assets:
                    self.plant.register_asset(asset)
                span.output_summary = f"Loaded {len(assets)} assets"
            logger.info(
                "assets_loaded",
                connector=cname,
                count=len(assets),
            )

        # Auto-load failure modes and build domain services
        await self._build_domain_services()

        # Connect channels. In sandbox mode we skip outbound I/O so
        # channels like EmailConnector do not perform real SMTP logins.
        # See issue #31.
        for channel in self._channels:
            cname = getattr(channel, "__class__", type(channel)).__name__
            if self.sandbox:
                logger.info("sandbox_skip_channel_connect", channel=cname)
                continue
            await channel.connect()

        logger.info(
            "agent_started",
            agent=self.name,
            asset_count=len(self.plant.assets),
            connectors=list(self._registry.all().keys()),
        )

    async def _build_domain_services(self) -> None:
        """Build domain services from loaded data and register them with the workflow engine."""
        from machina.domain.services.asset_service import AssetService
        from machina.domain.services.failure_analyzer import FailureAnalyzer
        from machina.domain.services.maintenance_scheduler import MaintenanceScheduler
        from machina.domain.services.work_order_factory import WorkOrderFactory

        # Collect failure modes from connectors that provide them (shared
        # with the diagnose_failure tool so the two sites cannot drift).
        all_failure_modes = await self._collect_failure_modes()

        # Collect maintenance plans from connectors that provide them
        all_plans = []
        for _name, conn in self._registry.all().items():
            if hasattr(conn, "_maintenance_plans"):
                all_plans.extend(conn._maintenance_plans)

        analyzer = FailureAnalyzer(failure_modes=all_failure_modes)
        factory = WorkOrderFactory()
        scheduler = MaintenanceScheduler(plans=all_plans)
        asset_service = AssetService(plant=self.plant)

        self._engine._services = {
            "failure_analyzer": analyzer,
            "work_order_factory": factory,
            "maintenance_scheduler": scheduler,
            "domain": asset_service,
        }

        # Defence against ``_engine`` being replaced or rebuilt after
        # construction (e.g. by tests, subclasses, or future hot-reload
        # logic).  Re-apply the canonical sandbox value so the engine's
        # snapshot cannot drift from ``self._sandbox``.
        self._engine.sandbox = self._sandbox

        if all_failure_modes:
            logger.info(
                "domain_services_ready",
                agent=self.name,
                failure_modes=len(all_failure_modes),
            )

    async def _collect_failure_modes(self) -> list[FailureMode]:
        """Harvest failure modes from capability-declaring connectors, deduped by code.

        Single source for both the workflow path
        (:meth:`_build_domain_services`) and the ``diagnose_failure`` tool —
        factored out so the two sites cannot drift. Discovers providers via
        :attr:`Capability.READ_FAILURE_MODES` and awaits each connector's
        public ``read_failure_modes()`` at call time, so it never serves a
        stale snapshot. A provider that raises :class:`ConnectorError`
        (e.g. not connected) contributes nothing instead of aborting the
        whole harvest — the empty-catalog honesty note downstream stays
        intact. Duplicate codes across connectors keep the first occurrence
        (registration order).
        """
        from machina.exceptions import ConnectorError

        by_code: dict[str, FailureMode] = {}
        providers = self._registry.find_by_capability(Capability.READ_FAILURE_MODES)
        for name, conn in providers:
            try:
                modes = await conn.read_failure_modes()  # type: ignore[attr-defined]
            except ConnectorError as exc:
                logger.warning(
                    "failure_mode_harvest_failed",
                    agent=self.name,
                    connector=name,
                    operation="collect_failure_modes",
                    error=str(exc),
                )
                continue
            for fm in modes:
                if fm.code not in by_code:
                    by_code[fm.code] = fm
        return list(by_code.values())

    async def stop(self) -> None:
        """Disconnect all connectors and channels."""
        channel_ids = {id(ch) for ch in self._channels}
        for channel in self._channels:
            cname = getattr(channel, "__class__", type(channel)).__name__
            if self.sandbox:
                logger.info("sandbox_skip_channel_disconnect", channel=cname)
                continue
            await channel.disconnect()
        for _name, conn in self._registry.all().items():
            if id(conn) in channel_ids:
                continue
            await conn.disconnect()
        logger.info("agent_stopped", agent=self.name)

    async def handle_message(
        self,
        text: str,
        *,
        chat_id: str = "default",
        confirmer: Callable[[str], Awaitable[bool]] | None = None,
        user_id: str = "",
    ) -> str:
        """Process a user message and return the agent's response text.

        This is the main entry point for programmatic usage. The returned
        string is the rendered answer with inline ``[n]`` citation markers
        renormalized to ``1..N`` by first appearance and the trailing
        ``<citations>`` block stripped. Use :meth:`handle_message_full` to
        also access structured :class:`Citation` objects —
        ``citations[n-1]`` aligns with the inline ``[n]`` marker.

        Args:
            text: The user's message.
            chat_id: Identifier for the conversation.
            confirmer: Optional async callable that renders a confirmation
                prompt and returns the user's yes/no decision. Supplied by a
                channel that can confirm a write synchronously (e.g.
                ``CliChannel``). When ``None`` and :attr:`confirmations` is on,
                a mutating tool call is NOT executed (fail-safe).
            user_id: Identifier for the sender, forwarded for cross-user
                confirmation scoping. Note: ``confirmations`` only gates writes
                that flow through the agent LLM loop; ``trigger_workflow`` is a
                deliberate direct-execution path guarded by ``sandbox`` only.

        Returns:
            The agent's response text.

        Raises:
            LLMError: If the underlying LLM call fails.
        """
        response = await self.handle_message_full(
            text, chat_id=chat_id, confirmer=confirmer, user_id=user_id
        )
        return response.text

    async def handle_message_full(
        self,
        text: str,
        *,
        chat_id: str = "default",
        confirmer: Callable[[str], Awaitable[bool]] | None = None,
        user_id: str = "",
    ) -> AgentResponse:
        """Process a user message and return the structured agent response.

        Args:
            text: The user's message.
            chat_id: Identifier for the conversation.
            confirmer: Optional async callable that renders a confirmation
                prompt and returns the user's yes/no decision (see
                :meth:`handle_message`). When ``None`` and
                :attr:`confirmations` is on, a mutating tool call is NOT
                executed (fail-safe — a programmatic caller that wants
                autonomous writes sets ``confirmations=False`` or passes a
                ``confirmer``).
            user_id: Identifier for the sender, forwarded for cross-user
                confirmation scoping.

        Returns:
            An :class:`AgentResponse` with the rendered text and any
            citations the agent emitted.

        Raises:
            LLMError: If the underlying LLM call fails.
        """
        if len(text) > self._max_message_length:
            original_length = len(text)
            text = text[: self._max_message_length]
            logger.warning(
                "message_truncated",
                agent=self.name,
                chat_id=chat_id,
                original_length=original_length,
                max_length=self._max_message_length,
            )

        logger.info(
            "message_received",
            agent=self.name,
            chat_id=chat_id,
            message_preview=text[:100],
        )

        # Two-turn confirmation resume (U5). If a write is pending for this
        # (chat_id, user_id), interpret THIS message deterministically (never
        # via the LLM): a bare affirmation executes the pending write and
        # re-enters the loop in narration-only mode; anything else (a decline
        # OR an unrelated message) cancels the pending action and falls through
        # to normal processing — so an unrelated next message never silently
        # executes.
        pending = self._pending_actions.get((chat_id, user_id))
        if pending is not None:
            confirmed = await self._resume_pending_action(
                pending, text, chat_id=chat_id, user_id=user_id
            )
            if confirmed is not None:
                return confirmed

        # Reset the per-turn chunk registry and ordered index map.
        self._turn_chunks[chat_id] = {}
        self._turn_ordered[chat_id] = []

        try:
            # 1. Entity resolution
            resolved = self._resolver.resolve(text)

            # 2. Gather context from connectors
            context_data = await self._gather_context(text, resolved, chat_id=chat_id)

            # 3. Build messages
            messages = self._build_messages(text, chat_id, context_data)

            # 4. Call LLM (with tool-calling loop)
            try:
                raw_response = await self._llm_loop(
                    messages, chat_id, confirmer=confirmer, user_id=user_id
                )
            except Exception as exc:
                logger.error(
                    "llm_error",
                    agent=self.name,
                    error=str(exc),
                )
                raise LLMError(f"LLM call failed: {exc}") from exc
        except BaseException:
            # On any failure before finalization (entity resolution, context
            # gathering, or a wrapped LLM error) drop the per-turn registry so a
            # long-lived agent does not accumulate orphan slots, then re-raise
            # unchanged. The success path's cleanup lives in _finalize_turn.
            self._turn_chunks.pop(chat_id, None)
            self._turn_ordered.pop(chat_id, None)
            self._turn_completeness.pop(chat_id, None)
            raise

        # 5/6. Parse citations, update history, clean up the per-turn registry,
        #      and log — the shared turn-finalization tail.
        return self._finalize_turn(chat_id=chat_id, user_text=text, raw_response=raw_response)

    def run(self) -> None:
        """Start the agent with all channels (blocking, sync wrapper).

        Connects connectors, loads assets, and starts listening on
        all configured channels.  Automatically detects Jupyter
        notebooks and other environments with an already-running
        event loop.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            # Already inside an event loop (Jupyter, async REPL, etc.)
            # Schedule the coroutine on the existing loop.
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(asyncio.run, self._run_async()).result()
        else:
            asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        """Async main loop — start agent and listen on channels."""
        await self.start()

        if not self._channels:
            logger.warning("no_channels", agent=self.name)
            return

        # Use the first channel for listen (typically Telegram or CLI)
        channel = self._channels[0]

        # Bind this channel's synchronous confirmation primitive (if any) into
        # the handler so the HITL gate can prompt in-turn. A channel without it
        # (async channels) leaves ``confirmer=None`` → the runtime fails safe
        # (U4) and U5 extends that into the two-turn propose→confirm flow.
        from machina.connectors.comms.types import supports_sync_confirmation

        sync_confirm = supports_sync_confirmation(channel)

        async def _handler(msg: Any) -> str:
            confirmer: Callable[[str], Awaitable[bool]] | None = None
            if sync_confirm:

                async def confirmer(prompt: str, _msg: Any = msg) -> bool:
                    return bool(await channel.request_confirmation(_msg.chat_id, prompt))

            response = await self.handle_message_full(
                msg.text,
                chat_id=msg.chat_id,
                confirmer=confirmer,
                user_id=getattr(msg, "user_id", ""),
            )
            return _format_response_for_channel(response)

        try:
            await channel.listen(_handler)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await self.stop()

    # ------------------------------------------------------------------
    # Internal: context gathering
    # ------------------------------------------------------------------

    async def _gather_context(
        self,
        text: str,
        resolved: list[Any],
        *,
        chat_id: str = "default",
    ) -> dict[str, Any]:
        """Gather context from connectors based on resolved entities."""
        context: dict[str, Any] = {
            "resolved_entities": resolved,
        }

        if not resolved:
            return context

        # Resolution-confidence gate (U5): the top match is selected here and its
        # data prefetched as THE asset for the turn. A weak guess must not be
        # treated as authoritative — withhold the commit (no prefetch, no
        # ``context["asset"]``) and let the agent ask which asset is meant. The
        # candidates stay in ``resolved_entities`` (with their confidence) so the
        # prompt can render them for disambiguation (R3.1/R3.2).
        top = resolved[0]
        if getattr(top, "confidence", 1.0) < RESOLUTION_MIN_CONFIDENCE:
            logger.info(
                "low_confidence_resolution_withheld",
                agent=self.name,
                asset_id=top.asset.id,
                confidence=top.confidence,
                operation="gather_context",
            )
            context["resolution_uncertain"] = True
            return context

        asset = resolved[0].asset
        context["asset"] = asset

        # Gather work orders, spare parts in parallel
        tasks: list[Any] = []
        task_names: list[str] = []

        wo_connectors = self._registry.find_by_capability(Capability.READ_WORK_ORDERS)
        if wo_connectors:
            wo_cname, wo_conn = wo_connectors[0]

            async def _get_wos(_cname: str = wo_cname, _conn: Any = wo_conn) -> list[Any]:
                with self.tracer.trace(
                    "connector_query",
                    connector=_cname,
                    asset_id=asset.id,
                    operation="read_work_orders",
                ):
                    return await _conn.read_work_orders(asset_id=asset.id)  # type: ignore[no-any-return]

            tasks.append(_get_wos())
            task_names.append("work_orders")

        sp_connectors = self._registry.find_by_capability(Capability.READ_SPARE_PARTS)
        if sp_connectors:
            sp_cname, sp_conn = sp_connectors[0]

            async def _get_parts(_cname: str = sp_cname, _conn: Any = sp_conn) -> list[Any]:
                with self.tracer.trace(
                    "connector_query",
                    connector=_cname,
                    asset_id=asset.id,
                    operation="read_spare_parts",
                ):
                    return await _conn.read_spare_parts(asset_id=asset.id)  # type: ignore[no-any-return]

            tasks.append(_get_parts())
            task_names.append("spare_parts")

        # Document search
        doc_connectors = self._registry.find_by_capability(Capability.SEARCH_DOCUMENTS)
        if doc_connectors:
            doc_cname, doc_conn = doc_connectors[0]

            async def _search_docs(_cname: str = doc_cname, _conn: Any = doc_conn) -> list[Any]:
                with self.tracer.trace(
                    "connector_query",
                    connector=_cname,
                    asset_id=asset.id,
                    operation="search_documents",
                ):
                    results = await _conn.search(text, asset_id=asset.id)
                    # Sanitise source and content at the LLM boundary so
                    # absolute file paths never reach the prompt context —
                    # safe_source for the metadata field, safe_text for paths
                    # embedded in the chunk body. See prompts.safe_source/safe_text.
                    return [
                        {
                            "content": safe_text(r.content),
                            "source": safe_source(r.source),
                            "page": r.page,
                            "chunk_id": getattr(r, "chunk_id", ""),
                            "section_title": getattr(r, "section_title", ""),
                            "is_table": getattr(r, "is_table", False),
                        }
                        for r in results
                    ]

            tasks.append(_search_docs())
            task_names.append("document_results")

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for name, result in zip(task_names, results, strict=False):
                if isinstance(result, BaseException):
                    logger.warning(
                        "context_gather_error",
                        agent=self.name,
                        source=name,
                        error=str(result),
                    )
                else:
                    context[name] = result

        # Register any retrieved document chunks against the per-turn registry
        # so citation parsing can validate chunk_id references later.
        self._register_document_results(chat_id, context.get("document_results") or [])

        return context

    def _register_document_results(self, chat_id: str, results: list[dict[str, Any]]) -> None:
        """Register retrieved chunks for citation parsing.

        Builds two per-turn structures from the **same** ``results[:5]``
        enumeration the prompt renders:

        * ``self._turn_chunks[chat_id]`` — ``chunk_id`` → metadata, backing
          the source/page citation fallback (skips empty ``chunk_id`` rows).
        * ``self._turn_ordered[chat_id]`` — ``chunk_id`` by display position,
          so the visible ``[n]`` the model saw resolves directly. A
          displayed-but-unregistered row (empty ``chunk_id``) is appended as
          an empty string so later indices stay aligned with the prompt and
          do not drift off-by-k.

        Truncation to ``[:DOC_DISPLAY_WINDOW]`` mirrors
        :func:`format_document_results`, which only renders that window.
        """
        registry = self._turn_chunks.setdefault(chat_id, {})
        ordered = self._turn_ordered.setdefault(chat_id, [])
        for r in results[:DOC_DISPLAY_WINDOW]:
            chunk_id = r.get("chunk_id") or ""
            ordered.append(chunk_id)
            if not chunk_id:
                continue
            registry[chunk_id] = {
                "source": r.get("source", ""),
                "page": r.get("page", 0),
                "content": r.get("content", ""),
            }

    # ------------------------------------------------------------------
    # Internal: turn finalization
    # ------------------------------------------------------------------

    def _finalize_turn(
        self,
        *,
        chat_id: str,
        user_text: str,
        raw_response: str,
        fallback_text: str = _EMPTY_RESPONSE_FALLBACK,
    ) -> AgentResponse:
        """Parse the raw LLM output into the final response and close the turn.

        Shared tail of both turn paths (:meth:`handle_message_full` and
        :meth:`_resume_pending_action`): the two differ only in how they set up
        and produce ``raw_response`` (entity resolution + context gathering vs.
        the narration-only re-entry), but converge on an identical finalization:

        1. Parse citations against the per-turn chunk registry (the ordered
           index map resolves visible ``[n]`` markers; the registry backs the
           source/page fallback).
        2. Run the validator chain on the rendered text, in order: think-tag
           scrub → empty-response fallback → degenerate-JSON fallback → echo
           guard → leaked-tool-call backstop → completeness hedge. The scrub (U7) removes reasoning-model
           ``<think>...</think>`` blocks (case-insensitive, spans newlines;
           an UNCLOSED ``<think>`` swallows to end-of-string — truncated
           reasoning is still reasoning, never answer). Strip-and-keep-remainder
           semantics: meaningful text surviving the scrub proceeds through the
           rest of the chain; a think-only response strips to empty and the
           empty-response fallback fires naturally.
        3. Renormalize inline citation markers (U3) — runs AFTER the full
           validator chain and only when the surviving text is the parsed
           prose with at least one resolved citation: raw per-turn indices
           (``[6]``, ``[8]``) are rewritten to ``1..N`` by first appearance,
           unresolvable markers are stripped fail-closed, and ``citations``
           is reordered to match the displayed numbering. Every fallback
           branch zeroes ``citations``, so the pass no-ops on degraded paths.
        4. Append the user message and the rendered assistant reply to
           history. The stored assistant text is MARKER-STRIPPED on both
           branches (the echo-path override included) — a kept ``[1]`` would
           always be in-range against the next turn's fresh registry and
           silently resolve to a different chunk if echoed.
        5. In a ``finally``, always drop the per-turn chunk registry and ordered
           index map for ``chat_id`` — even on error — so a long-lived agent
           does not accumulate orphan slots from failed turns.
        6. Log ``response_generated`` and return the :class:`AgentResponse`.

        Args:
            chat_id: Conversation identifier.
            user_text: The user message to record in history.
            raw_response: The raw LLM output (with any trailing citation block).
            fallback_text: Substituted (and flagged via
                :attr:`AgentResponse.is_fallback`) when the rendered answer is
                empty. Defaults to :data:`_EMPTY_RESPONSE_FALLBACK`; the
                post-write narration path passes a write-aware string so a
                successful write is never reported as a failure.

        Returns:
            The rendered :class:`AgentResponse` with parsed citations.
        """
        is_fallback = False
        completeness: Literal["complete", "partial"] = "complete"
        # Assistant text to record in history, when it must differ from the
        # user-facing ``rendered`` (set only on the echo path — see below).
        history_override: str | None = None
        try:
            rendered, citations = parse_response(
                raw_response,
                self._turn_chunks.get(chat_id, {}),
                self._turn_ordered.get(chat_id, []),
            )
            # Scrub reasoning-model <think> blocks FIRST (U7), so a think-only
            # response strips to empty and falls into the empty-response
            # fallback below naturally. When the scrub removed nothing, the
            # rendered text is left byte-identical (no spurious trimming of
            # clean answers). Never log the scrubbed content itself.
            scrubbed = _THINK_BLOCK_RE.sub("", rendered)
            # Orphan-closer pass: a bare "</think>" surviving the sub means
            # the opener was never emitted (r1 via some serving stacks) or was
            # nested — either way, everything before the FIRST remaining
            # closer is reasoning. Drop it, repeatedly, case-insensitively.
            # See _THINK_CLOSE_TAG for the fail-closed trade-off.
            while (closer_at := scrubbed.lower().find(_THINK_CLOSE_TAG)) != -1:
                scrubbed = scrubbed[closer_at + len(_THINK_CLOSE_TAG) :]
            if scrubbed != rendered:
                cleaned = scrubbed.strip()
                logger.info(
                    "think_block_scrubbed",
                    agent=self.name,
                    chat_id=chat_id,
                    operation="finalize_turn",
                    scrubbed_chars=len(rendered) - len(scrubbed),
                )
                rendered = cleaned
            # A model that returns nothing (empty completion) or only a
            # citations block leaves an empty rendered answer. Surface an
            # explicit fallback instead of delivering blank output — weak
            # local models hit this routinely. Citations with no prose have
            # nothing to attribute, so drop them. Log at WARNING so the
            # degradation is queryable (the INFO line below would otherwise
            # look healthy — its length is the fallback's, not the empty raw).
            if not rendered.strip():
                logger.warning(
                    "empty_llm_response",
                    agent=self.name,
                    chat_id=chat_id,
                    raw_response_length=len(raw_response),
                )
                rendered = fallback_text
                citations = []
                is_fallback = True
            # Degenerate-JSON guard (2026-06-10 post-fix deepseek eval): a
            # rendered answer that parses to an EMPTY JSON container ("{}" or
            # "[]") carries zero information — exactly like an empty
            # completion — but is non-empty text, so the guard above never
            # fires. Observed after leaked-read recoveries: the loop seam
            # feeds the read result back and the model's next completion is
            # literally "{}".
            elif _is_degenerate_json_answer(rendered):
                logger.warning(
                    "degenerate_json_answer_suppressed",
                    agent=self.name,
                    chat_id=chat_id,
                    operation="finalize_turn",
                    response_length=len(rendered),
                )
                rendered = fallback_text
                citations = []
                is_fallback = True
            # A loop-seam leak suppression (_handle_text_only_completion)
            # substituted the fallback text BEFORE the gate ran, so by itself it
            # would read as ordinary prose here and leave the structured flag
            # unset. Recognise the sentinel and flag it — orchestrators and
            # monitors must distinguish a leak fallback from a real answer via
            # ``is_fallback``, never by string-matching the text. The leak was
            # already logged at the seam (operation="llm_loop"), so no second
            # warning is emitted here.
            elif rendered == _TOOL_CALL_LEAK_FALLBACK:
                citations = []
                is_fallback = True
            # The echo guard applies only to the normal turn path (identified by
            # the default empty-response fallback). The two-turn post-write
            # narration path passes a write-aware ``fallback_text``; a write has
            # already executed there, so suppressing its narration with a
            # "rephrase / switch model" message could imply the write failed and
            # invite a duplicate-write retry.
            elif fallback_text is _EMPTY_RESPONSE_FALLBACK and self._is_echo_of_previous(
                chat_id, user_text, rendered
            ):
                # The model reproduced the previous turn's answer near-verbatim
                # (weak models copy the prior assistant message out of history).
                # Surface an honest, distinct degrade to the user, but record the
                # REAL echoed text in history: it must stay the comparison
                # baseline so a third, fourth, … consecutive repeat is also
                # caught. Storing the fallback instead would let the echo leak
                # again on the next turn (baseline no longer matches).
                logger.warning(
                    "repeated_response_suppressed",
                    agent=self.name,
                    chat_id=chat_id,
                    operation="finalize_turn",
                    response_length=len(rendered),
                )
                history_override = rendered
                rendered = _REPEATED_RESPONSE_FALLBACK
                citations = []
                is_fallback = True
            # Backstop (U3/U6): a tool/function call that reached finalize as
            # the answer text (e.g. the forced-final ``complete()`` returned
            # one, or a leak slipped past the loop seam) must never be shown
            # raw — ANY detector hit is suppressed, hallucinated names
            # included (R9). Store the fallback in history too (no override)
            # so the JSON cannot re-prime the next turn. Uses the write-aware
            # ``fallback_text`` on the narration path, the specific leak
            # message on the normal path.
            elif (leaked := self._detect_leaked_tool_call(rendered)) is not None:
                logger.warning(
                    "tool_call_leak_suppressed",
                    agent=self.name,
                    chat_id=chat_id,
                    tool=leaked[0],
                    # known= is log-only; suppression at the egress gate is
                    # unconditional.
                    known=leaked[0] in self._known_tool_names(),
                    operation="finalize_turn",
                )
                rendered = (
                    _TOOL_CALL_LEAK_FALLBACK
                    if fallback_text is _EMPTY_RESPONSE_FALLBACK
                    else fallback_text
                )
                citations = []
                is_fallback = True
            # Fragment tripwire (PR #55 gap family 5): a truncated/partial
            # tool call never parses, so the detector above cannot return
            # (name, args) for it — but it is still a leak, not an answer.
            # Finalize-only by design: there is no call to recover, so the
            # loop seam has nothing to do with it.
            elif self._looks_like_leaked_tool_call_fragment(rendered):
                logger.warning(
                    "tool_call_leak_suppressed",
                    agent=self.name,
                    chat_id=chat_id,
                    tool=None,
                    known=False,
                    operation="finalize_turn",
                )
                rendered = (
                    _TOOL_CALL_LEAK_FALLBACK
                    if fallback_text is _EMPTY_RESPONSE_FALLBACK
                    else fallback_text
                )
                citations = []
                is_fallback = True
            # Egress renormalization (U3): the surviving text is the parsed
            # prose and at least one citation resolved — rewrite raw per-turn
            # indices ([6], [8], ...) to a clean 1..N by first appearance,
            # strip unresolvable markers fail-closed, and reorder ``citations``
            # to match the displayed numbering (the *list order == displayed
            # number order* invariant the channel footer enumerates). Every
            # fallback branch above zeroed ``citations``, so this is a no-op
            # on all degraded paths; with zero parsed citations the text stays
            # byte-identical (no renumbering, no stripping).
            if not is_fallback and citations:
                rendered, citations = renormalize_markers(
                    rendered, citations, self._turn_ordered.get(chat_id, [])
                )
            self._add_to_history(chat_id, "user", user_text)
            # Carry the turn's grounding into history so follow-ups resolve from
            # memory instead of re-running document search (see _history_text).
            stored = (
                history_override
                if history_override is not None
                else _history_text(rendered, citations)
            )
            # History fail-closed (U3): strip inline [n] markers from the
            # stored text on BOTH branches — _history_text's result AND the
            # echo-path override (which bypasses _history_text). A renormalized
            # [1] kept in history would always be in-range against the NEXT
            # turn's fresh registry, so an echoed marker would silently resolve
            # to a different chunk. The source note _history_text appends never
            # matches the marker pattern and survives intact.
            stored = strip_markers(stored)
            self._add_to_history(chat_id, "assistant", stored)
            # A real answer the loop was forced to finalize early may be missing
            # data. Hedge the USER-facing text only — ``stored`` above already
            # captured the clean answer so echo detection and source follow-ups
            # stay accurate — and flag it structurally (R1.3/R1.4/R5). Gated to
            # the normal turn path (default fallback) so the post-write narration
            # path is never hedged.
            if (
                not is_fallback
                and fallback_text is _EMPTY_RESPONSE_FALLBACK
                and self._turn_completeness.get(chat_id) == "partial"
            ):
                completeness = "partial"
                rendered = rendered + _PARTIAL_COMPLETENESS_HEDGE
        finally:
            self._turn_chunks.pop(chat_id, None)
            self._turn_ordered.pop(chat_id, None)
            self._turn_completeness.pop(chat_id, None)

        logger.info(
            "response_generated",
            agent=self.name,
            chat_id=chat_id,
            response_length=len(rendered),
            citation_count=len(citations),
            is_fallback=is_fallback,
            completeness=completeness,
        )
        return AgentResponse(
            text=rendered,
            citations=citations,
            is_fallback=is_fallback,
            completeness=completeness,
        )

    def _is_echo_of_previous(self, chat_id: str, user_text: str, rendered: str) -> bool:
        """Whether ``rendered`` merely repeats the previous turn's answer.

        Reads the most recent assistant and user messages already in history.
        Called from :meth:`_finalize_turn` BEFORE this turn is appended, so the
        "previous" entries are genuinely the prior turn's.

        Returns ``False`` when the current user message matches the previous
        one: asking the same question twice and getting the same answer is
        legitimate, not a degenerate echo. Otherwise defers the content
        comparison to :func:`_is_echo` (length floor + normalised similarity).

        Args:
            chat_id: Conversation identifier.
            user_text: The current turn's user message.
            rendered: The current turn's freshly rendered answer.

        Returns:
            ``True`` when the answer should be suppressed as a repeat.
        """
        history = self._histories.get(chat_id, [])
        prev_assistant: str | None = None
        prev_user: str | None = None
        for entry in reversed(history):
            if prev_assistant is None and entry["role"] == "assistant":
                prev_assistant = entry["content"]
            elif prev_user is None and entry["role"] == "user":
                prev_user = entry["content"]
            if prev_assistant is not None and prev_user is not None:
                break
        if prev_assistant is None:
            return False
        if prev_user is not None and _normalized_for_echo(prev_user) == _normalized_for_echo(
            user_text
        ):
            return False
        return _is_echo(rendered, prev_assistant)

    # ------------------------------------------------------------------
    # Internal: message building
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        """Build the domain-aware system prompt string.

        Single source of the capability-gathering loop + ``build_system_prompt``
        call, shared by :meth:`_build_messages` (the normal turn) and
        :meth:`_resume_pending_action` (the two-turn narration re-entry). Keeping
        both call sites on this one helper makes the "identical system prompt"
        guarantee structural rather than a comment obligation.
        """
        all_caps: list[str] = []
        for _, conn in self._registry.all().items():
            all_caps.extend(conn.capabilities)

        return build_system_prompt(
            plant_name=self.plant.name,
            asset_count=len(self.plant.assets),
            capabilities=all_caps,
            workflows=list(self._workflows.keys()),
            sandbox=self._sandbox,
        )

    def _build_messages(
        self,
        text: str,
        chat_id: str,
        context_data: dict[str, Any],
    ) -> list[dict[str, str]]:
        """Build the LLM message list with system prompt, context, and history."""
        system = self._build_system_prompt()

        messages: list[dict[str, str]] = [{"role": "system", "content": system}]

        # Add domain context
        context_str = build_context_message(
            resolved_entities=context_data.get("resolved_entities"),
            asset=context_data.get("asset"),
            work_orders=context_data.get("work_orders"),
            alarms=context_data.get("alarms"),
            spare_parts=context_data.get("spare_parts"),
            document_results=context_data.get("document_results"),
        )
        if context_str:
            messages.append(
                {"role": "system", "content": f"## Retrieved Context\n\n{context_str}"}
            )

        # Add conversation history
        history = self._histories.get(chat_id, [])
        messages.extend(history)

        # Add current user message
        messages.append({"role": "user", "content": text})

        return messages

    # ------------------------------------------------------------------
    # Internal: LLM tool-calling loop
    # ------------------------------------------------------------------

    async def _llm_loop(
        self,
        messages: list[dict[str, str]],
        chat_id: str,
        *,
        max_iterations: int = 5,
        confirmer: Callable[[str], Awaitable[bool]] | None = None,
        user_id: str = "",
    ) -> str:
        """Call the LLM, execute tool calls, and return final response.

        When :attr:`confirmations` is on, every mutating tool call
        (``func_name in _SIDE_EFFECTING_TOOLS``) is gated:

        * **sandbox on** → the gate is skipped (the write short-circuits to a
          no-op inside the tool; confirming a no-op would mislead).
        * **``confirmer`` available** (synchronous channels, e.g. CLI) → the
          decision is awaited; on yes the write executes, on no a structured
          ``{"declined": ...}`` result is returned without executing.
        * **no ``confirmer``** (programmatic callers / async channels) → the
          write is NOT executed; :meth:`_await_write_confirmation` stores the
          pending action and returns a fail-safe
          ``{"confirmation_required": ...}`` result (the two-turn flow).

        Note: the two-turn confirmation narration does NOT re-enter this loop.
        :meth:`_resume_pending_action` narrates an already-executed write via the
        no-tools :meth:`LLMProvider.complete` path, so there is no orphan
        ``role:tool`` message and no risk of a second write here.
        """
        tools = self._get_available_tools()

        # Per-turn memo of side-effecting tool calls (keyed by name + args).
        # If the model re-requests the same write across loop iterations — the
        # mechanism behind the duplicate-work-order report — we reuse the first
        # result instead of executing the side effect again. Read-only tools
        # are never memoised; they may legitimately be re-issued.
        executed_side_effects: dict[str, Any] = {}

        # Per-turn set of declined proposal keys (same canonical key as the
        # memo). A chatty model that re-proposes a write the user already
        # declined this turn is auto-declined WITHOUT re-prompting, so it
        # cannot ratchet repeated [y/N] prompts up to max_iterations. A
        # genuinely different proposal still prompts.
        declined_side_effects: set[str] = set()

        # Per-turn count of how many times each write was suppressed as a
        # duplicate. The annotation we feed back is only a cooperative hint; a
        # model that ignores it would loop to max_iterations. Once any key hits
        # _MAX_DUPLICATE_SUPPRESSIONS we stop offering tools and force a final
        # answer — guaranteeing quick termination regardless of model behaviour.
        suppression_counts: dict[str, int] = {}

        # Per-turn count of unparseable-argument retries fed back for
        # self-correction. Bounds the worst case independently of the
        # no-progress guard (see _MAX_ARG_CORRECTION_ATTEMPTS).
        arg_error_count = 0

        # Per-turn memo of READ-ONLY tool results (keyed by name + args) and the
        # set of every tool-call key seen this turn. An identical read is
        # replayed from cache instead of re-querying the connector; an iteration
        # whose calls are ALL repeats (no new information requested) means the
        # model is looping, so we stop offering tools and force a final answer.
        # This is the read-only analogue of the write-duplicate guard above —
        # without it, a weak model that re-requests the same lookup every
        # iteration runs the full max_iterations on redundant reads.
        executed_reads: dict[str, Any] = {}
        seen_call_keys: set[str] = set()

        for _iteration in range(max_iterations):
            with self.tracer.trace(
                "llm_call",
                operation="complete_with_tools",
            ) as span:
                if tools:
                    result = await self._llm.complete_with_tools(messages, tools)
                else:
                    text = await self._llm.complete(messages)
                    return text

            content = result.get("content", "")
            tool_calls = result.get("tool_calls")

            if not tool_calls:
                should_return, value = await self._handle_text_only_completion(
                    content, seen_call_keys, messages, chat_id
                )
                if should_return:
                    return value
                continue

            # Process tool calls
            span.output_summary = f"{len(tool_calls)} tool calls"
            messages.append(
                {
                    "role": "assistant",
                    "content": content or "",
                    "tool_calls": tool_calls,
                }
            )

            # Tracks whether this iteration requested anything the turn had not
            # already seen. If every call is a verbatim repeat, the model is
            # looping without making progress and we break after the loop.
            iteration_made_progress = False
            iteration_had_arg_error = False

            for tc in tool_calls:
                func_name = tc.function.name
                raw_arguments = getattr(tc.function, "arguments", None)
                if raw_arguments is None or not str(raw_arguments).strip():
                    # No arguments supplied — a valid no-arg call (e.g.
                    # list_assets), not a parse failure.
                    args = {}
                else:
                    try:
                        args = json.loads(raw_arguments)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        # Genuinely malformed args. Instead of silently coercing to
                        # {} (which masks the error and crashes a tool that needs
                        # required keys like asset_id), feed a FIXED error back so
                        # the model self-corrects. Bounded by
                        # _MAX_ARG_CORRECTION_ATTEMPTS so persistent junk still
                        # terminates (H1). The counter advances once per
                        # ITERATION (below), not once per bad call, so a single
                        # iteration emitting several malformed calls still leaves
                        # the model its full quota of correction rounds.
                        iteration_had_arg_error = True
                        logger.warning(
                            "invalid_tool_arguments",
                            agent=self.name,
                            tool=func_name,
                            operation="llm_loop",
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "content": json.dumps({"error": _INVALID_ARGS_MESSAGE}),
                                "tool_call_id": tc.id,
                            }
                        )
                        # The fed-back error is new information; mark progress so a
                        # single bad call doesn't trip the no-progress guard — the
                        # dedicated counter is the real bound.
                        iteration_made_progress = True
                        continue

                # Canonical key for EVERY call (read or write), used for both the
                # read-replay cache and the no-progress detector.
                call_key = f"{func_name}:{json.dumps(args, sort_keys=True, default=str)}"
                is_repeat_call = call_key in seen_call_keys
                seen_call_keys.add(call_key)

                # ``memo_key`` is the same string for write tools; reuse it rather
                # than recomputing the f-string (and risking the two drifting).
                memo_key: str | None = call_key if func_name in _SIDE_EFFECTING_TOOLS else None

                # An iteration makes progress when it asks for something genuinely
                # new, OR re-issues a read that will actually re-run because its
                # earlier attempt was not cached (e.g. it errored — the retry is
                # real work, not a no-op). Only a verbatim repeat served from the
                # read cache, or a write the suppression path will short-circuit,
                # counts as "no progress". Without the read-retry clause, a read
                # that errors then retries would trip the no-progress break and
                # deny the model a follow-up step after recovery.
                if not is_repeat_call or (memo_key is None and call_key not in executed_reads):
                    iteration_made_progress = True

                # The confirmation gate applies only to mutating tools and only
                # when confirmations are on AND we are not in sandbox (sandbox
                # already no-ops the write — confirming a no-op would mislead).
                gate_write = memo_key is not None and self._confirmations and not self.sandbox

                with self.tracer.trace(
                    "tool_call",
                    operation=func_name,
                ) as tool_span:
                    if memo_key is None and call_key in executed_reads:
                        # Read-only call re-issued verbatim — replay the cached
                        # result instead of re-querying the connector, and nudge
                        # the model to answer with the data it already has.
                        tool_result = {
                            "already_retrieved": True,
                            "note": _DUPLICATE_READ_NOTE,
                            "result": executed_reads[call_key],
                        }
                        logger.info(
                            "duplicate_read_call_suppressed",
                            agent=self.name,
                            tool=func_name,
                            operation=func_name,
                        )
                    elif memo_key is not None and memo_key in executed_side_effects:
                        # Re-feed the prior result, flagged as already done so the
                        # model stops re-issuing the write and moves on to
                        # summarising. The write does NOT run again. In sandbox the
                        # prior result is a no-op, so we must not claim a real
                        # execution happened — that would mislead the model.
                        prior = executed_side_effects[memo_key]
                        is_sandbox_noop = isinstance(prior, dict) and prior.get("sandbox") is True
                        suppression_counts[memo_key] = suppression_counts.get(memo_key, 0) + 1
                        tool_result = {
                            "already_executed": not is_sandbox_noop,
                            "note": (
                                _DUPLICATE_TOOL_NOTE_SANDBOX
                                if is_sandbox_noop
                                else _DUPLICATE_TOOL_NOTE
                            ),
                            "result": prior,
                        }
                        logger.info(
                            "duplicate_tool_call_suppressed",
                            agent=self.name,
                            tool=func_name,
                            operation=func_name,
                        )
                    elif gate_write and memo_key in declined_side_effects:
                        # Same proposal the user already declined this turn:
                        # auto-decline without re-prompting (anti-friction).
                        tool_result = {"declined": True, "tool": func_name}
                        logger.info(
                            "write_auto_declined",
                            agent=self.name,
                            tool=func_name,
                            operation=func_name,
                        )
                    elif gate_write and confirmer is not None:
                        # Synchronous path (e.g. CLI): ask, then act on yes.
                        approved = await confirmer(self._confirmation_prompt(func_name, args))
                        if approved:
                            tool_result = await self._execute_tool(
                                func_name, args, chat_id=chat_id
                            )
                            is_error = isinstance(tool_result, dict) and "error" in tool_result
                            if memo_key is not None and not is_error:
                                executed_side_effects[memo_key] = tool_result
                        else:
                            tool_result = {"declined": True, "tool": func_name}
                            if memo_key is not None:
                                declined_side_effects.add(memo_key)
                            logger.info(
                                "write_declined",
                                agent=self.name,
                                tool=func_name,
                                operation=func_name,
                            )
                    elif gate_write:
                        # No synchronous primitive available. Do NOT execute.
                        tool_result = await self._await_write_confirmation(
                            func_name, args, chat_id, user_id
                        )
                    else:
                        tool_result = await self._execute_tool(func_name, args, chat_id=chat_id)
                        # Only memoise successful results. A failed side effect
                        # (e.g. a transient workflow error returned as
                        # {"error": ...}) must not suppress a legitimate retry
                        # of the same call later in the turn.
                        is_error = isinstance(tool_result, dict) and "error" in tool_result
                        if memo_key is not None and not is_error:
                            executed_side_effects[memo_key] = tool_result
                        elif memo_key is None and not is_error:
                            # Cache the read so a verbatim re-issue this turn is
                            # served from here instead of re-querying.
                            executed_reads[call_key] = tool_result
                    tool_span.output_summary = str(tool_result)[:200]
                    # Full result, JSON-encoded, for consumers that need more
                    # than the truncated summary (e.g. the conversational eval
                    # asserts on tool RESULTS, not just invocation — the one
                    # signal context echo cannot fake). Recording only; no
                    # behavioural change. Capped at 64 KiB: a truncated value
                    # fails json.loads downstream, so the eval falls back to
                    # output_summary by design rather than parsing garbage.
                    raw = json.dumps(tool_result, default=str)
                    tool_span.metadata["result_json"] = (
                        raw if len(raw) <= 65_536 else raw[:65_536] + "...[truncated]"
                    )

                messages.append(
                    {
                        "role": "tool",
                        "content": json.dumps(tool_result, default=str),
                        "tool_call_id": tc.id,
                    }
                )

            # Count this iteration (not each malformed call) toward the
            # correction budget, so the model gets _MAX_ARG_CORRECTION_ATTEMPTS
            # full rounds to recover regardless of how many bad calls it packed
            # into any single iteration.
            if iteration_had_arg_error:
                arg_error_count += 1

            # If this whole iteration only re-issued calls already made this turn
            # (read or write), the model is looping without asking for anything
            # new. Stop offering tools and force a final answer — this is the
            # general termination guard that covers repeated read-only lookups,
            # which the write-only suppression counter below does not catch.
            if not iteration_made_progress:
                logger.info(
                    "no_new_tool_calls_forcing_final_answer",
                    agent=self.name,
                    operation="llm_loop",
                )
                break

            # Still reachable, and NOT subsumed by the no-progress break above:
            # the no-progress break fires only when an iteration is ALL repeats,
            # whereas this guard catches the model that interleaves a genuinely
            # new call with the same suppressed write every iteration (so
            # ``iteration_made_progress`` stays True but the write loops). The
            # annotation we feed back is only a hint; an uncooperative model would
            # otherwise loop to max_iterations.
            if any(c >= _MAX_DUPLICATE_SUPPRESSIONS for c in suppression_counts.values()):
                logger.info(
                    "duplicate_suppression_limit_reached",
                    agent=self.name,
                    operation="llm_loop",
                )
                break

            # The model kept emitting unparseable arguments past the correction
            # budget. Each fed-back error counts as "progress", so only this
            # dedicated counter bounds it — stop offering tools and force a final
            # answer (H1).
            if arg_error_count >= _MAX_ARG_CORRECTION_ATTEMPTS:
                logger.info(
                    "arg_correction_limit_reached",
                    agent=self.name,
                    operation="llm_loop",
                )
                break

        # Reached only by force-finalization: a no-progress break, the
        # duplicate-suppression limit, or iteration exhaustion. The agent was
        # cut off before it signalled it was done, so the answer may be
        # incomplete — mark the turn partial so _finalize_turn hedges it (R1.4).
        # The natural returns above (model produced its own final answer) leave
        # the marker unset, i.e. complete.
        self._turn_completeness[chat_id] = "partial"
        # Exhausted/forced — get final response without tools
        return await self._llm.complete(messages)

    # ------------------------------------------------------------------
    # Confirmation gate helpers
    # ------------------------------------------------------------------

    def _confirmation_prompt(self, func_name: str, args: dict[str, Any]) -> str:
        """Build a concrete, human-readable description of a pending write (R6).

        Pure function of the tool name and its arguments — no I/O. The channel
        renders this text verbatim, so it must state exactly what will happen.

        Args:
            func_name: The mutating tool the model requested.
            args: The arguments the model supplied for the call.

        Returns:
            A one-paragraph confirmation question naming the concrete action.
        """
        if func_name == "create_work_order":
            asset = args.get("asset_id") or "(unspecified asset)"
            wo_type = args.get("type") or "corrective"
            priority = args.get("priority") or "medium"
            description = args.get("description") or "(no description)"
            return (
                "Create a work order?\n"
                f"  • Asset: {asset}\n"
                f"  • Type: {wo_type}\n"
                f"  • Priority: {priority}\n"
                f"  • Description: {description}"
            )

        if func_name == "execute_workflow":
            workflow = args.get("workflow_name") or "(unnamed workflow)"
            event = args.get("event")
            summary = self._summarize_event(event)
            return f"Run workflow {workflow!r}?\n  • Event: {summary}"

        # Generic fallback for any other (future) mutating tool — better a
        # weaker description than no gate. New write tools should add a branch
        # above so R6 stays concrete.
        rendered_args = ", ".join(f"{k}={v!r}" for k, v in args.items()) or "(no arguments)"
        return f"Execute {func_name}?\n  • Arguments: {rendered_args}"

    @staticmethod
    def _summarize_event(event: Any) -> str:
        """Summarise a workflow ``event`` payload for the confirmation prompt.

        Highlights the target asset and a few key fields; falls back to a
        plain marker when the payload is empty or not a mapping.
        """
        if not isinstance(event, dict) or not event:
            return "(no event payload)"
        parts: list[str] = []
        asset = event.get("asset_id") or event.get("asset")
        if asset:
            parts.append(f"asset={asset}")
        for key in ("alarm_id", "failure_mode", "priority", "severity", "type"):
            if event.get(key):
                parts.append(f"{key}={event[key]}")
        if not parts:
            # Show up to three arbitrary keys so the user sees something.
            parts = [f"{k}={v}" for k, v in list(event.items())[:3]]
        return ", ".join(parts)

    @staticmethod
    def _is_affirmation(text: str) -> bool:
        """Deterministically recognise a bare affirmation (NOT via the LLM).

        Thin delegator to
        :func:`machina.connectors.comms.types.is_affirmation` — the single
        source of truth for the affirmation grammar, shared with a channel's
        synchronous ``request_confirmation``. Returns ``True`` only when the
        WHOLE message — after strip + lowercase — is a single recognised
        affirmation token (English or Italian); a compound such as
        ``"ok, but set priority high"`` is NOT an affirmation, so the gate is
        never bypassed by an ambiguous "yes …" prefix.

        Args:
            text: The raw incoming message text.

        Returns:
            ``True`` if the message is exactly one affirmation token.
        """
        return is_affirmation(text)

    @staticmethod
    def _is_decline(text: str) -> bool:
        """Deterministically recognise a bare decline (NOT via the LLM).

        Thin delegator to :func:`machina.connectors.comms.types.is_decline`.
        Both a decline and any unrelated message clear the pending action; this
        helper exists for symmetry and clearer logging, not because the two
        branches differ in effect (both cancel).

        Args:
            text: The raw incoming message text.

        Returns:
            ``True`` if the message is exactly one decline token.
        """
        return is_decline(text)

    async def _await_write_confirmation(
        self,
        func_name: str,
        args: dict[str, Any],
        chat_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Handle a gated write when no synchronous confirmer is available.

        The write is NOT executed. The proposed action is stored in the
        turn-surviving :attr:`_pending_actions` store keyed
        ``(chat_id, user_id)`` so the next inbound message for the same
        ``(chat_id, user_id)`` can confirm it (the two-turn degrade — see
        :meth:`handle_message_full`). A structured ``confirmation_required``
        result is returned so the turn ends with the confirmation question as
        the response and a programmatic caller never writes unconfirmed.

        Two safety rules apply when deciding whether to store:

        * **Empty (untrusted) ``user_id`` → withhold, never store.** On a shared
          async channel any anonymous participant could otherwise confirm
          another sender's pending write. Without an identified sender the write
          is withheld: the result carries ``unconfirmable: True`` and an
          explanatory prompt, nothing is stored, and nothing executes.
        * **A different, live pending action already exists → keep it, reject
          the new one.** The first proposal survives and stays confirmable; the
          second returns ``already_pending: True`` without overwriting. An
          identical re-proposal (same tool + args) is a no-op that re-returns the
          existing prompt. Stale pendings are popped by the TTL check on resume
          BEFORE any new proposal reaches here, so keep-first only blocks a live
          pending.

        Args:
            func_name: The mutating tool that was gated.
            args: The arguments the model supplied.
            chat_id: Conversation identifier (part of the pending key).
            user_id: Sender identifier (part of the pending key). Empty when the
                channel cannot supply a trusted identity — the write is then
                withheld (see above).

        Returns:
            A structured result the LLM loop feeds back as the tool result.
        """
        prompt = self._confirmation_prompt(func_name, args)

        if not user_id:
            # Fail-safe: no trusted sender identity, so a deferred confirmation
            # cannot be bound to anyone. Withhold the write — do NOT store, do
            # NOT execute — and explain why. (CLI is unaffected: it sets
            # user_id="cli_user" and uses the synchronous confirmer path, not
            # this two-turn store.)
            logger.warning(
                "write_withheld_anonymous",
                agent=self.name,
                tool=func_name,
                operation=func_name,
                chat_id=chat_id,
                reason="empty_user_id_no_trusted_sender",
            )
            return {
                "confirmation_required": True,
                "tool": func_name,
                "prompt": (
                    "This write needs confirmation, but deferred confirmation on "
                    "this channel requires an identified sender (none was "
                    "supplied), so the action was withheld and NOT performed. "
                    "Use a channel that provides a sender identity, or a channel "
                    "that can confirm in the same turn.\n\n"
                    f"{prompt}"
                ),
                "unconfirmable": True,
            }

        # Keep-first: a DIFFERENT live pending action must not be silently
        # overwritten by a second proposal in the same turn. Staleness is
        # handled by the TTL check on resume (which pops an expired pending
        # before a new proposal arrives), so anything still here is live.
        existing = self._pending_actions.get((chat_id, user_id))
        if existing is not None:
            if existing[:2] == (func_name, dict(args)):
                # Identical re-proposal — no-op; re-return the existing prompt.
                return {
                    "confirmation_required": True,
                    "tool": func_name,
                    "prompt": existing[2],
                }
            # Different proposal — reject it, keep the first confirmable.
            logger.warning(
                "pending_write_rejected_existing",
                agent=self.name,
                tool=func_name,
                operation=func_name,
                chat_id=chat_id,
                kept_tool=existing[0],
                kept_operation=existing[0],
            )
            return {
                "confirmation_required": True,
                "tool": func_name,
                "prompt": prompt,
                "already_pending": True,
            }

        self._pending_actions[(chat_id, user_id)] = (
            func_name,
            dict(args),
            prompt,
            time.monotonic(),
        )
        logger.info(
            "write_confirmation_required",
            agent=self.name,
            tool=func_name,
            operation=func_name,
            chat_id=chat_id,
        )
        return {
            "confirmation_required": True,
            "tool": func_name,
            "prompt": prompt,
        }

    async def _resume_pending_action(
        self,
        pending: tuple[str, dict[str, Any], str, float],
        text: str,
        *,
        chat_id: str,
        user_id: str,
    ) -> AgentResponse | None:
        """Resume (or cancel) a pending two-turn write based on this message.

        Called at the top of :meth:`handle_message_full` when a pending action
        exists for ``(chat_id, user_id)``. The decision is deterministic — the
        LLM is never asked to interpret the confirmation.

        * **Expired** (age exceeds :data:`_PENDING_ACTION_TTL_SECONDS`): pop the
          stale pending and return ``None`` WITHOUT executing — the incoming
          message is then processed as a fresh message, so a much-later "ok" is
          never read as a confirmation of a stale write.
        * **Affirmation** (the whole message is one yes-token): pop the pending
          action, execute it via :meth:`_execute_tool` (so the connector's own
          ``@sandbox_aware`` check still applies if state changed since the
          proposal), then re-enter :meth:`_llm_loop` in **narration-only** mode
          so the model narrates the outcome and emits citations instead of
          returning a raw payload. Returns the narrated :class:`AgentResponse`.
        * **Anything else** (a decline OR an unrelated message): pop/clear the
          pending action and return ``None`` so the caller falls through to
          normal processing of this message (an unrelated message never
          silently executes the pending write).

        Args:
            pending: The stored ``(func_name, args, prompt, stored_ts)`` tuple.
            text: The raw incoming message.
            chat_id: Conversation identifier.
            user_id: Sender identifier.

        Returns:
            The narrated response when the write was confirmed and executed;
            ``None`` when the pending action was cancelled or expired (caller
            proceeds with normal processing).
        """
        func_name, args, _prompt, stored_ts = pending

        # TTL check FIRST: an aged pending is treated as if it were never there.
        # Pop it, do not execute, and fall through so the incoming message is
        # processed fresh (a stale "ok" must not confirm a stale write).
        if time.monotonic() - stored_ts > _PENDING_ACTION_TTL_SECONDS:
            self._pending_actions.pop((chat_id, user_id), None)
            logger.info(
                "pending_write_expired",
                agent=self.name,
                tool=func_name,
                operation=func_name,
                chat_id=chat_id,
                age_seconds=round(time.monotonic() - stored_ts, 1),
            )
            return None

        if not self._is_affirmation(text):
            # Decline or unrelated: cancel and let the caller process normally.
            self._pending_actions.pop((chat_id, user_id), None)
            logger.info(
                "pending_write_cancelled",
                agent=self.name,
                tool=func_name,
                operation=func_name,
                chat_id=chat_id,
                declined=self._is_decline(text),
            )
            return None

        # Affirmation: pop FIRST so a re-entrant failure cannot leave a
        # confirmable ghost, then execute the write through the normal tool
        # path (the connector's sandbox check still applies if state changed).
        self._pending_actions.pop((chat_id, user_id), None)
        logger.info(
            "pending_write_confirmed",
            agent=self.name,
            tool=func_name,
            operation=func_name,
            chat_id=chat_id,
        )

        # Fresh per-turn citation state for the narration pass.
        self._turn_chunks[chat_id] = {}
        self._turn_ordered[chat_id] = []
        try:
            tool_result = await self._execute_tool(func_name, args, chat_id=chat_id)

            # Narrate the already-executed write with the NO-TOOLS completion
            # path (``complete``), not ``_llm_loop``. The previous tool-calling
            # re-entry hand-built an orphan ``role:tool`` message (no
            # ``tool_call_id``, no preceding assistant ``tool_calls``), which
            # OpenAI-compatible providers reject with a 400 — and since the write
            # had already executed, the user saw an error and a retried "yes"
            # could create a DUPLICATE write. The narration only summarises an
            # already-executed result, so it needs no tools and no tool-role
            # message: the executed result is embedded as plain TEXT. The system
            # prompt is built identically to the normal turn (via
            # :meth:`_build_system_prompt`). Citations still parse from this
            # output through :meth:`_finalize_turn`.
            system_prompt = self._build_system_prompt()
            messages: list[dict[str, str]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
                {
                    "role": "user",
                    "content": (
                        f"The confirmed action {func_name!r} has been executed. "
                        "Summarise the outcome for the user and cite any sources.\n\n"
                        f"Result:\n{json.dumps(tool_result, default=str)}"
                    ),
                },
            ]
            raw_response = await self._llm.complete(messages)
        except BaseException:
            # Drop the per-turn registry on any failure during execution or
            # narration so a long-lived agent does not accumulate orphan slots,
            # then re-raise unchanged. The success path's cleanup lives in
            # _finalize_turn. All three per-turn dicts are cleared in lockstep
            # (the marker is unused on this path today, but keeping the cleanup
            # symmetric means a future loop here can't leak a stale "partial").
            self._turn_chunks.pop(chat_id, None)
            self._turn_ordered.pop(chat_id, None)
            self._turn_completeness.pop(chat_id, None)
            raise

        # Parse citations, update history, clean up the per-turn registry, and
        # log — the shared turn-finalization tail (same as handle_message_full).
        # The write ALREADY executed, so if the narration comes back empty the
        # fallback must report success, never invite a retry (a reworded retry
        # could mint a duplicate write past the per-turn memo).
        return self._finalize_turn(
            chat_id=chat_id,
            user_text=text,
            raw_response=raw_response,
            fallback_text=_executed_write_fallback(func_name, tool_result),
        )

    async def _execute_tool(
        self,
        name: str,
        args: dict[str, Any],
        *,
        chat_id: str = "default",
    ) -> Any:
        """Execute a tool call by dispatching to the appropriate connector.

        ``chat_id`` scopes any side effects that touch per-turn state
        (currently the citation chunk registry) so concurrent chats stay
        isolated.
        """
        logger.debug("executing_tool", tool=name, args=args)

        if name == "search_assets":
            return self._tool_search_assets(args.get("query", ""))

        if name == "get_asset_details":
            return self._tool_get_asset_details(args.get("asset_id", ""))

        if name == "list_assets":
            return self._tool_list_assets()

        if name == "read_work_orders":
            connectors = self._registry.find_by_capability(Capability.READ_WORK_ORDERS)
            if connectors:
                _, conn = connectors[0]
                wos = await conn.read_work_orders(  # type: ignore[attr-defined]
                    asset_id=args.get("asset_id", ""),
                    status=args.get("status", ""),
                )
                return [wo.model_dump(mode="json") for wo in wos]
            return {"error": "No CMMS connector available"}

        if name == "get_work_order":
            connectors = self._registry.find_by_capability(Capability.GET_WORK_ORDER)
            if connectors:
                _, conn = connectors[0]
                work_order_id = args.get("work_order_id", "")
                # Args can arrive from the leak-recovery path (model text, no
                # provider schema enforcement), so validate before dispatch.
                if not isinstance(work_order_id, str) or not work_order_id:
                    return {"error": "work_order_id must be a non-empty string"}
                try:
                    wo = await conn.get_work_order(  # type: ignore[attr-defined]
                        work_order_id
                    )
                except Exception as exc:
                    # A connector failure (ConnectorError/timeout) must degrade
                    # to a tool-level error the model can react to, not kill the
                    # whole turn — including the recovered-read re-entry path.
                    logger.warning(
                        "work_order_lookup_failed",
                        agent=self.name,
                        tool=name,
                        work_order_id=work_order_id,
                        operation="execute_tool",
                        error=str(exc),
                    )
                    return {"error": safe_text(str(exc))}
                if wo is None:
                    return {"error": f"Work order {work_order_id!r} not found"}
                return wo.model_dump(mode="json")
            return {"error": "No CMMS connector available"}

        if name == "create_work_order":
            return await self._tool_create_work_order(args, chat_id=chat_id)

        if name == "search_documents":
            connectors = self._registry.find_by_capability(Capability.SEARCH_DOCUMENTS)
            if connectors:
                _, conn = connectors[0]
                raw_filters = args.get("filters")
                filters = raw_filters if isinstance(raw_filters, dict) else None
                results = await conn.search(  # type: ignore[attr-defined]
                    args.get("query", ""),
                    asset_id=args.get("asset_id", ""),
                    filters=filters,
                )
                # Sanitise source at the LLM boundary — the tool result is
                # serialised straight into the conversation history.  The
                # citation fields (chunk_id / section_title / is_table) come
                # from the v0.3 RAG upgrade and feed citation validation.
                # Surface a visible ``citation_index`` on the tool result so
                # the model can cite tool-retrieved chunks by ``[n]`` — the
                # same index contract the pre-fetch context uses. The index
                # is offset by any chunks already displayed this turn (e.g.
                # from pre-fetch context) so it matches the ordered map
                # _register_document_results builds. Only the first
                # DOC_DISPLAY_WINDOW results are indexed, mirroring
                # format_document_results' display window.
                offset = len(self._turn_ordered.get(chat_id, []))
                serialized = [
                    {
                        "citation_index": offset + i,
                        "content": safe_text(r.content),
                        "source": safe_source(r.source),
                        "page": r.page,
                        "chunk_id": getattr(r, "chunk_id", ""),
                        "section_title": getattr(r, "section_title", ""),
                        "is_table": getattr(r, "is_table", False),
                    }
                    for i, r in enumerate(results[:DOC_DISPLAY_WINDOW], 1)
                ]
                # Register tool-retrieved chunks against the in-flight chat
                # only, so concurrent chats do not see each other's chunks
                # when citation parsing validates references later.
                self._register_document_results(chat_id, serialized)
                return serialized
            return {"error": "No document connector available"}

        if name == "check_spare_parts":
            connectors = self._registry.find_by_capability(Capability.READ_SPARE_PARTS)
            if connectors:
                _, conn = connectors[0]
                parts = await conn.read_spare_parts(  # type: ignore[attr-defined]
                    asset_id=args.get("asset_id", ""),
                    sku=args.get("sku", ""),
                )
                return [p.model_dump(mode="json") for p in parts]
            return {"error": "No spare parts connector available"}

        if name == "diagnose_failure":
            # Args can arrive from the leak-recovery path (model text, no
            # provider schema enforcement), so coerce before dispatch — a
            # wrong-typed arg must degrade to a tool-level error the model
            # can react to, not raise and kill the whole turn.
            asset_id = args.get("asset_id", "")
            if not isinstance(asset_id, str):
                return {"error": "asset_id must be a string"}
            symptoms = args.get("symptoms", [])
            if not isinstance(symptoms, list):
                symptoms = []
            symptoms = [s for s in symptoms if isinstance(s, str)]
            return await self._tool_diagnose_failure(asset_id, symptoms)

        if name == "get_maintenance_schedule":
            return {"info": "Maintenance schedule lookup not yet connected to a data source."}

        if name == "execute_workflow":
            return await self._tool_execute_workflow(
                args.get("workflow_name", ""),
                args.get("event"),
            )

        return {"error": f"Unknown tool: {name}"}

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _tool_search_assets(self, query: str) -> list[dict[str, Any]]:
        """Search assets using the entity resolver."""
        resolved = self._resolver.resolve(query)
        return [
            {
                "id": r.asset.id,
                "name": r.asset.name,
                "type": r.asset.type.value,
                "location": r.asset.location,
                "criticality": r.asset.criticality.value,
                "confidence": r.confidence,
            }
            for r in resolved[:5]
        ]

    def _tool_get_asset_details(self, asset_id: str) -> dict[str, Any]:
        """Get full asset details."""
        try:
            asset = self.plant.get_asset(asset_id)
            return asset.model_dump(mode="json")
        except Exception:
            logger.warning(
                "asset_lookup_failed",
                agent=self.name,
                asset_id=asset_id,
                operation="get_asset_details",
            )
            return {"error": f"Asset {asset_id!r} not found"}

    async def _handle_text_only_completion(
        self,
        content: str,
        seen_call_keys: set[str],
        messages: list[dict[str, str]],
        chat_id: str,
    ) -> tuple[bool, str]:
        """Handle an LLM completion that carried no structured ``tool_calls`` (U3).

        The model may have emitted a tool call as plain-text content (weak local
        models do this). Returns ``(True, value)`` when the loop should return
        ``value`` — the plain answer, or :data:`_TOOL_CALL_LEAK_FALLBACK` for a
        leaked write, a hallucinated (unknown-name) tool, or a re-leak — or
        ``(False, "")`` when the loop should ``continue`` after this method has
        recovered a leaked KNOWN READ, mutating ``messages`` and
        ``seen_call_keys`` in place. Detection is shape-based (U6/R9); the
        known/unknown name lookup happens here, as disposition, against THIS
        agent's tool surface (:meth:`_known_tool_names`) — the same
        capability-derived set the loop offers and dispatch executes, so a
        leaked capability-derived READ (e.g. ``get_work_order``) recovers
        instead of being misclassified as hallucinated.
        """
        leaked = self._detect_leaked_tool_call(content)
        if leaked is None:
            return True, content or ""
        leaked_name, leaked_args = leaked
        if leaked_name not in self._known_tool_names():
            # A hallucinated tool (U6/R9): not on this agent's tool surface, so
            # it can neither be executed nor recovered — suppress, never
            # re-enter.
            logger.warning(
                "tool_call_leak_suppressed",
                agent=self.name,
                chat_id=chat_id,
                tool=leaked_name,
                known=False,
                operation="llm_loop",
            )
            return True, _TOOL_CALL_LEAK_FALLBACK
        if leaked_name in _SIDE_EFFECTING_TOOLS:
            # A write emitted as prose is exactly the low-trust output the gate
            # withholds — never auto-execute it off the dedup/confirm path.
            logger.warning(
                "tool_call_leak_suppressed",
                agent=self.name,
                chat_id=chat_id,
                tool=leaked_name,
                known=True,
                operation="llm_loop",
            )
            return True, _TOOL_CALL_LEAK_FALLBACK
        leaked_key = f"{leaked_name}:{json.dumps(leaked_args, sort_keys=True, default=str)}"
        if leaked_key in seen_call_keys:
            # Already recovered this exact call once — a re-leak means the model
            # is stuck; stop rather than loop on it.
            logger.warning(
                "tool_call_leak_suppressed",
                agent=self.name,
                chat_id=chat_id,
                tool=leaked_name,
                known=True,
                operation="llm_loop",
            )
            return True, _TOOL_CALL_LEAK_FALLBACK
        # Recover a leaked READ: run it once and feed the result back so the
        # model answers from it next iteration. Recording the key lets the check
        # above (and the no-progress guard) bound any re-leak.
        logger.warning(
            "tool_call_leak_recovered",
            agent=self.name,
            chat_id=chat_id,
            tool=leaked_name,
            operation="llm_loop",
        )
        seen_call_keys.add(leaked_key)
        leaked_result = await self._execute_tool(leaked_name, leaked_args, chat_id=chat_id)
        messages.append({"role": "assistant", "content": content or ""})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"That last message was an internal {leaked_name} tool "
                    "request, not an answer. Here is its result: "
                    f"{json.dumps(leaked_result, default=str)}. "
                    "Now answer my question in plain language."
                ),
            }
        )
        return False, ""

    @staticmethod
    def _detect_leaked_tool_call(content: str) -> tuple[str, dict[str, Any]] | None:
        """Recognise a tool/function call the model emitted as plain text (U3/U6).

        Weak models sometimes serialize a tool call into the message *content*
        instead of producing a structured ``tool_call``. Returns ``(name, args)``
        for EVERY payload that parses as a tool-call shape, regardless of
        whether the name is a known builtin (R9) — models also hallucinate
        tools that do not exist, and such a leak must still be intercepted.
        Disposition (recover a known read, suppress a known write or an
        unknown/hallucinated tool) is the caller's job, not a filter here.
        Anything that does not cleanly parse as a tool-call shape is treated
        as ordinary prose so a normal answer is never misclassified.

        The payload is normalized before shape matching (PR #55 gap families
        1-4): a surrounding markdown code fence is stripped, a
        ``{"tool_calls": [...]}`` provider frame is unwrapped, a top-level
        array of call objects resolves to its FIRST call (recovery of a known
        read stays bounded by ``seen_call_keys``; a write never executes off
        the structured path regardless), and single-quoted pseudo-JSON is
        parsed via a safe Python-literal fallback. Truncated/partial payloads
        (family 5) never parse and are handled by the finalize-only
        :meth:`_looks_like_leaked_tool_call_fragment` tripwire instead.

        Three parsed shapes are recognised: A — ``"function"`` is a nested
        object carrying ``"name"``; B — a top-level ``"name"`` key; C — the
        tool name is the string VALUE of a ``"function"`` key alongside an
        ``arguments``/``parameters`` key (gap family 6, deepseek-r1:8b eval
        baseline 2026-06-10).
        """
        text = _strip_code_fence((content or "").strip())
        if not text or text[0] not in "{[":
            return None
        obj = _parse_leak_payload(text)
        if isinstance(obj, dict) and isinstance(obj.get("tool_calls"), list):
            # The provider-frame wrapper serialized whole into content.
            obj = obj["tool_calls"]
        if isinstance(obj, list):
            obj = obj[0] if obj and isinstance(obj[0], dict) else None
        if not isinstance(obj, dict):
            return None
        fn = obj.get("function")
        # Shapes B and C read their args from the top-level object; shape A
        # reads from the nested function object instead.
        obj_args: Any = obj.get("arguments", obj.get("parameters", {}))
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            # Shape A: {"type":"function","function":{"name":..,"arguments":..}}
            name = fn["name"]
            raw_args: Any = fn.get("arguments", fn.get("parameters", {}))
        elif isinstance(obj.get("name"), str) and ("arguments" in obj or "parameters" in obj):
            # Shape B: {"name":.., "arguments":..}
            name = obj["name"]
            raw_args = obj_args
        elif isinstance(fn, str) and ("arguments" in obj or "parameters" in obj):
            # Shape C: {"function":"<name>", "arguments":..} — the tool name is
            # the string VALUE of the function key (deepseek-r1 eval baseline
            # 2026-06-10), not a nested object (A) or a "name" key (B). An
            # EMPTY string is still detected (matching shapes A/B): the empty
            # name dispositions as unknown → suppressed fail-closed.
            name = fn
            raw_args = obj_args
        else:
            return None
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except (json.JSONDecodeError, ValueError):
                raw_args = {}
        if not isinstance(raw_args, dict):
            raw_args = {}
        return name, raw_args

    @staticmethod
    def _looks_like_leaked_tool_call_fragment(content: str) -> bool:
        """Whether ``content`` is an UNPARSABLE tool-call-shaped fragment.

        Covers truncated/partial tool-call JSON (PR #55 gap family 5 — the
        model ran out of tokens mid-call) that shape-based detection cannot
        return ``(name, args)`` for. Used ONLY by the ``_finalize_turn``
        backstop: a fragment carries no recoverable call, so suppression is
        the only disposition, and it must happen at the sole egress gate.
        Payloads that parse cleanly are never flagged here — the full
        detector owns them, so a deliberate JSON answer that merely contains
        a ``name`` field is not suppressed (fail-closed only on the
        unparsable, marker-bearing shape; R9/U6 trade-off).
        """
        text = _strip_code_fence((content or "").strip())
        if not text or text[0] not in "{[":
            return False
        if _parse_leak_payload(text) is not None:
            return False
        return bool(_LEAK_FRAGMENT_NAME_RE.search(text) and _LEAK_FRAGMENT_MARKER_RE.search(text))

    def _tool_list_assets(self) -> dict[str, Any] | list[dict[str, Any]]:
        """Enumerate the full asset registry (R1.1).

        A thin, authoritative read of the in-memory registry — unlike
        reconstructing the asset list from work orders, which silently omits
        assets that have none. For a large plant the result is bounded: above
        :data:`_ENUM_SUMMARY_THRESHOLD` it returns a count plus a grouped
        summary instead of every record (R1.2).
        """
        assets = self.plant.list_assets()
        if len(assets) > _ENUM_SUMMARY_THRESHOLD:
            return self._summarize_assets(assets)
        return [
            {
                "id": a.id,
                "name": a.name,
                "type": a.type.value,
                "location": a.location,
                "criticality": a.criticality.value,
            }
            for a in assets
        ]

    @staticmethod
    def _summarize_assets(assets: list[Any]) -> dict[str, Any]:
        """Bounded summary of a large asset registry (R1.2)."""
        by_criticality: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for a in assets:
            by_criticality[a.criticality.value] = by_criticality.get(a.criticality.value, 0) + 1
            by_type[a.type.value] = by_type.get(a.type.value, 0) + 1
        return {
            "total": len(assets),
            "note": (
                f"{len(assets)} assets in the registry — too many to list individually. "
                "Counts by criticality and type are below; ask about a specific area, "
                "type, or asset ID for detail."
            ),
            "by_criticality": by_criticality,
            "by_type": by_type,
        }

    async def _tool_create_work_order(
        self, args: dict[str, Any], *, chat_id: str = "default"
    ) -> dict[str, Any]:
        """Create a work order via the CMMS connector."""
        if self.sandbox:
            logger.info(
                "sandbox_create_work_order",
                agent=self.name,
                args=args,
            )
            return {"sandbox": True, "action": "create_work_order", "args": args}

        from machina.domain.services.work_order_factory import auto_work_order_id
        from machina.domain.work_order import Priority, WorkOrder, WorkOrderType

        connectors = self._registry.find_by_capability(Capability.CREATE_WORK_ORDER)
        if not connectors:
            return {"error": "No CMMS connector available for creating work orders"}

        _, conn = connectors[0]
        wo_type = args.get("type", "corrective")
        priority = args.get("priority", "medium")
        asset_id = args.get("asset_id", "")
        description = args.get("description", "")
        # Deterministic, content-based ID (shared with WorkOrderFactory) so a
        # model that re-requests this tool inside the LLM loop collapses to a
        # single work order instead of creating one per call. The old
        # ``id(args) % 10000`` scheme used the memory address of a per-call
        # dict — non-deterministic, dedup-proof, prone to cross-turn collisions.
        wo = WorkOrder(
            # Scope the dedup hash by chat_id (U7): a reworded retry in the same
            # conversation still collapses to one WO, but the same content in a
            # later session is a new WO, not a months-old collision. The
            # autonomous workflow path (WorkOrderFactory) passes no session and
            # keeps the content-only hash.
            id=auto_work_order_id(asset_id, wo_type, priority, description, session_id=chat_id),
            type=WorkOrderType(wo_type),
            priority=Priority(priority),
            asset_id=asset_id,
            description=description,
        )
        created = await conn.create_work_order(wo)  # type: ignore[attr-defined]
        logger.info(
            "work_order_created",
            agent=self.name,
            work_order_id=created.id,
            asset_id=created.asset_id,
        )
        return created.model_dump(mode="json")  # type: ignore[no-any-return]

    async def _tool_diagnose_failure(
        self,
        asset_id: str,
        symptoms: list[str],
    ) -> dict[str, Any]:
        """Diagnose probable failure modes against the live failure-mode catalog.

        Harvests failure modes from the registered connectors at call time
        (via :meth:`_collect_failure_modes`, the same source
        :meth:`_build_domain_services` feeds the workflow analyzer), filters
        the catalog to the resolved asset's declared ``failure_modes`` when
        present, and matches the LLM's free-text symptoms by token overlap
        against each mode's ``typical_indicators``
        (see :func:`_symptom_tokens`). The alarm/workflow path through
        :class:`~machina.domain.services.failure_analyzer.FailureAnalyzer`
        keeps its exact-match semantics — fuzzy matching lives at this tool
        boundary only.

        An empty ``probable_failures`` list ALWAYS carries an explanatory
        ``note`` so the model can distinguish "unknown asset" from "no
        catalog configured" from "catalog present but nothing matched".
        """
        from machina.exceptions import AssetNotFoundError

        result: dict[str, Any] = {
            "asset_id": asset_id,
            "symptoms": symptoms,
            "probable_failures": [],
        }

        # Resolve the asset first: an unknown asset gets a distinct, honest
        # note instead of a full-catalog guess for equipment we know nothing
        # about. Catch ONLY the lookup failure — a connector/catalog problem
        # later must not masquerade as "asset not found".
        try:
            asset = self.plant.get_asset(asset_id)
        except AssetNotFoundError:
            logger.warning(
                "diagnose_failure_asset_not_found",
                agent=self.name,
                asset_id=asset_id,
                operation="diagnose_failure",
            )
            result["note"] = safe_text(f"Asset '{asset_id}' not found in the asset registry.")
            return result
        result["asset_name"] = asset.name

        # Call-time harvest: derive the catalog from whatever connectors
        # declare the capability NOW — never goes stale.
        catalog = await self._collect_failure_modes()
        if not catalog:
            logger.warning(
                "diagnose_failure_no_catalog",
                agent=self.name,
                asset_id=asset_id,
                operation="diagnose_failure",
            )
            result["note"] = "No failure-mode data configured on any connector."
            return result

        notes: list[str] = []

        # Per-asset applicability filter: when the asset declares its own
        # failure modes, match ONLY against those — a pump must never get the
        # conveyor's belt-wear diagnosis just because both list a vibration
        # indicator. Assets that declare nothing fall back to the full
        # catalog, and the result says so.
        declared = set(asset.failure_modes)
        if declared:
            candidates = [fm for fm in catalog if fm.code in declared]
            if not candidates:
                # The asset names failure modes, but NONE of them exist in the
                # harvested catalog — an honest configuration-mismatch note,
                # not a garbled "nothing matched" with an empty indicator list.
                logger.warning(
                    "diagnose_failure_declared_modes_not_in_catalog",
                    agent=self.name,
                    asset_id=asset_id,
                    operation="diagnose_failure",
                    declared=sorted(declared),
                )
                result["note"] = safe_text(
                    f"Asset declares {len(declared)} failure mode(s) "
                    f"({', '.join(sorted(declared))}) but none are present in "
                    "the configured catalog (possible configuration mismatch)."
                )
                return result
        else:
            candidates = catalog
            notes.append(
                "Asset declares no failure modes; diagnosis ran against the "
                "full failure-mode catalog."
            )

        symptom_tokens: set[str] = set()
        for symptom in symptoms:
            symptom_tokens |= _symptom_tokens(symptom)

        ranked: list[dict[str, Any]] = []
        for fm in candidates:
            if not fm.typical_indicators:
                continue
            matched = [
                ind for ind in fm.typical_indicators if _symptom_tokens(ind) & symptom_tokens
            ]
            if not matched:
                continue
            ranked.append(
                {
                    "code": fm.code,
                    "name": fm.name,
                    "category": fm.category,
                    # Numeric indicator-match ratio 0-1: the fraction of this
                    # mode's typical_indicators the symptoms hit. Distinct from
                    # FailureAnalyzer's CATEGORICAL confidence on the
                    # alarm/workflow path — do not conflate the two.
                    "confidence": round(len(matched) / len(fm.typical_indicators), 2),
                    "matching_indicators": matched,
                    "recommended_actions": fm.recommended_actions,
                }
            )
        # Rank by evidence first (matched-indicator count DESC), ratio second:
        # a mode matching 1 of 2 indicators must not outrank one matching 3 of 6.
        ranked.sort(
            key=lambda entry: (len(entry["matching_indicators"]), float(entry["confidence"])),
            reverse=True,
        )
        result["probable_failures"] = ranked[:5]

        if not ranked:
            # Tell the model WHAT it could have matched so it can re-ask the
            # user in the catalog's vocabulary instead of guessing.
            known = sorted({ind for fm in candidates for ind in fm.typical_indicators})
            notes.append(
                "No catalog entry matched these symptoms. Known indicators: "
                + safe_text(", ".join(known[:20]))
                + "."
            )
        if notes:
            result["note"] = " ".join(notes)
        return result

    async def _tool_execute_workflow(
        self,
        workflow_name: str,
        event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a registered workflow and return results."""
        try:
            result = await self.trigger_workflow(workflow_name, event or {})
            return {
                "workflow_name": result.workflow_name,
                "success": result.success,
                "duration_ms": result.duration_ms,
                "steps": [
                    {
                        "step": sr.step_name,
                        "success": sr.success,
                        # Scrub user-home / UNC paths from step output and error
                        # text before it enters the LLM message history.
                        "output_summary": safe_text(str(sr.output)[:500]) if sr.output else None,
                        "error": safe_text(str(sr.error)) if sr.error else None,
                    }
                    for sr in result.step_results
                ],
            }
        except Exception as exc:
            logger.warning(
                "workflow_execution_failed",
                agent=self.name,
                operation="execute_workflow",
                workflow=workflow_name,
                error=str(exc),
            )
            return {"error": safe_text(str(exc)), "workflow_name": workflow_name}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_available_tools(self) -> list[dict[str, Any]]:
        """Return the tool definitions relevant to configured connectors."""
        from machina.connectors.capabilities import Capability

        all_caps: set[Capability] = set()
        for _, conn in self._registry.all().items():
            all_caps.update(conn.capabilities)

        cap_to_tool: dict[Capability, list[str]] = {
            Capability.READ_ASSETS: ["search_assets", "list_assets", "get_asset_details"],
            Capability.READ_WORK_ORDERS: ["read_work_orders"],
            Capability.GET_WORK_ORDER: ["get_work_order"],
            Capability.CREATE_WORK_ORDER: ["create_work_order"],
            Capability.SEARCH_DOCUMENTS: ["search_documents"],
            Capability.READ_SPARE_PARTS: ["check_spare_parts"],
        }

        enabled_tool_names: set[str] = set()
        for cap in all_caps:
            for tool_name in cap_to_tool.get(cap, []):
                enabled_tool_names.add(tool_name)

        # Always include diagnosis and schedule tools
        enabled_tool_names.add("diagnose_failure")
        enabled_tool_names.add("get_maintenance_schedule")

        # Include workflow tool only when workflows are registered
        if self._workflows:
            enabled_tool_names.add("execute_workflow")

        return [tool for tool in BUILTIN_TOOLS if tool["function"]["name"] in enabled_tool_names]

    def _known_tool_names(self) -> frozenset[str]:
        """Names on THIS agent's dispatchable tool surface (leak disposition).

        Used to disposition a tool/function call the model emitted as
        plain-text content instead of a structured ``tool_call`` (weak local
        models do this): known read → recover via bounded re-entry, known
        write → suppress, unknown → suppress. Derived from the SAME
        :meth:`_get_available_tools` list the loop offers to the model and
        ``_execute_tool`` dispatches, so the disposition can never drift from
        dispatch — a module-level set built from ``BUILTIN_TOOLS`` would
        misclassify capability-derived tools (e.g. ``get_work_order``) as
        hallucinated and degrade recoverable reads to the leak fallback.
        Detection itself stays shape-based and name-agnostic (U6/R9); this is
        the post-detection lookup in the callers, NOT a filter inside the
        detector.
        """
        return frozenset(t["function"]["name"] for t in self._get_available_tools())

    def _add_to_history(self, chat_id: str, role: str, content: str) -> None:
        """Add a message to the conversation history."""
        if chat_id not in self._histories:
            self._histories[chat_id] = []
        self._histories[chat_id].append({"role": role, "content": content})
        # Trim to max length
        if len(self._histories[chat_id]) > self._max_history * 2:
            self._histories[chat_id] = self._histories[chat_id][-self._max_history * 2 :]
