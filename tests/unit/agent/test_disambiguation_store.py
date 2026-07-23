"""Cross-turn disambiguation store — the answer to "which asset?" (U6).

Withholding the commit on an ambiguous turn only helps if the user can then
*answer*. The candidates are recorded across the turn boundary so a reply naming
one of them ("P-202", "la seconda", "2") selects it, and so the ambiguity cannot
launder into an ungated commit on turn 2.

Cross-turn state, deliberately: a per-turn marker is popped in
``_finalize_turn``, i.e. before the answer could ever arrive.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from machina.agent.entity_resolver import (
    MAX_RENDERED_CANDIDATES,
    ResolvedEntity,
    match_disambiguation_reply,
)
from machina.agent.runtime import _PENDING_ACTION_TTL_SECONDS, Agent
from machina.domain.asset import Asset, AssetType, Criticality
from machina.domain.plant import Plant

_AMBIGUOUS_QUERY = "the cooling water pump"


def _asset(asset_id: str, name: str = "Cooling Water Pump") -> Asset:
    return Asset(
        id=asset_id,
        name=name,
        type=AssetType.ROTATING_EQUIPMENT,
        location="Building A",
        criticality=Criticality.A,
    )


def _tie_plant() -> Plant:
    """Two distinct assets sharing one name — the tie that prompts the question."""
    plant = Plant(name="Test Plant")
    plant.register_asset(_asset("P-201"))
    plant.register_asset(_asset("P-202"))
    return plant


class _RecordingLLM:
    """Fake LLM that records the messages it saw and replies with a fixed answer."""

    def __init__(self, answer: str = "Which one do you mean?") -> None:
        self.model = "fake:model"
        self.answer = answer
        self.seen: list[dict[str, str]] = []

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        self.seen = list(messages)
        return self.answer

    async def complete_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        self.seen = list(messages)
        return {"content": self.answer, "tool_calls": None}


async def _ask_ambiguously(agent: Agent, chat_id: str = "c1") -> list[ResolvedEntity]:
    """Run the ambiguous turn's resolution + gate, leaving a live store entry."""
    resolved = agent._resolver.resolve(_AMBIGUOUS_QUERY)
    # Precondition — if the cascade stops producing a tie, every test below
    # would pass vacuously against an empty store. Fail loudly here instead.
    assert len(resolved) == 2
    assert resolved[0].confidence == resolved[1].confidence
    await agent._gather_context(_AMBIGUOUS_QUERY, resolved, chat_id=chat_id)
    assert chat_id in agent._disambiguations
    return resolved


class TestReplyMatching:
    """The pure matcher: which offered candidate does this reply name?"""

    @staticmethod
    def _candidates() -> list[ResolvedEntity]:
        return [
            ResolvedEntity(asset=_asset("P-201"), confidence=0.9, match_reason="name_match"),
            ResolvedEntity(asset=_asset("P-202"), confidence=0.9, match_reason="name_match"),
        ]

    @pytest.mark.parametrize(
        ("reply", "expected"),
        [
            ("P-202", 1),
            ("p-202", 1),
            ("la seconda", 1),
            ("the second one", 1),
            ("il primo", 0),
            ("the first", 0),
            ("2", 1),
            ("1.", 0),
        ],
    )
    def test_reply_selects_the_named_candidate(self, reply: str, expected: int) -> None:
        assert match_disambiguation_reply(reply, self._candidates()) == expected

    @pytest.mark.parametrize(
        "reply",
        [
            "the cooling water pump",  # names BOTH by name — a worse question
            "C-301",  # names none of them
            "what about the compressor",
            "3",  # out of range
            "la prima o la seconda?",  # two positions at once
            "",
        ],
    )
    def test_reply_naming_none_or_several_is_a_miss(self, reply: str) -> None:
        assert match_disambiguation_reply(reply, self._candidates()) is None

    def test_cardinals_are_not_ordinals(self) -> None:
        """ "una pompa" must not select candidate 1 off an indefinite article."""
        assert match_disambiguation_reply("una pompa perde olio", self._candidates()) is None
        assert match_disambiguation_reply("one of the pumps is loud", self._candidates()) is None

    def test_embedded_digit_is_not_a_bare_index(self) -> None:
        """A digit inside prose describes the plant, it does not pick from a list."""
        assert match_disambiguation_reply("il guasto è sulla linea 2", self._candidates()) is None

    def test_no_candidates_is_a_miss(self) -> None:
        assert match_disambiguation_reply("la seconda", []) is None

    @pytest.mark.parametrize(
        "reply",
        [
            "prima era ok",  # IT: 'prima' as an ordinary adverb
            "prima di tutto controlla l'olio",
            "prima controlla P-301",  # names a NON-candidate explicitly
            "second thoughts on this",  # EN: 'second' in ordinary prose
            "on second thought, leave it",
            "il terzo turno ha segnalato rumore",  # 'terzo' describing a shift
        ],
    )
    def test_an_ordinal_inside_prose_does_not_pick_a_candidate(self, reply: str) -> None:
        """The bare-index tier's whole-reply rule, which the ordinal tier lacked.

        A single ordinal ANYWHERE used to return a position, and
        ``_apply_disambiguation`` then replaced the resolution with that
        candidate at confidence 1.0 / ``high`` / ``ambiguous=False`` — through
        every downstream gate. "prima controlla P-301" is the worst shape: it
        discarded the user's own explicit reference in favour of candidate 0.
        """
        assert match_disambiguation_reply(reply, self._candidates()) is None

    @pytest.mark.parametrize(
        ("reply", "expected"),
        [
            ("la seconda", 1),
            ("seconda", 1),
            ("la seconda, grazie", 1),
            ("the second one", 1),
            ("second, please", 1),
            ("il primo", 0),
        ],
    )
    def test_an_ordinal_reply_plus_glue_still_selects(self, reply: str, expected: int) -> None:
        """The rule is "the whole reply is the ordinal", not "the reply is one word"."""
        assert match_disambiguation_reply(reply, self._candidates()) == expected

    def test_an_exact_full_name_wins_over_a_candidate_whose_name_is_its_prefix(self) -> None:
        """Longest name wins — otherwise the tie is unanswerable BY NAME.

        Containment makes "Pompa" a match for the reply "Pompa Acqua", so both
        candidates matched and ``_sole`` returned ``None``. And prefix-nested
        names are precisely what stage 2 of the resolver ties at 0.9 on a query
        containing the longer string, i.e. the shape that armed the question.
        """
        candidates = [
            ResolvedEntity(
                asset=_asset("P-201", "Pompa"), confidence=0.9, match_reason="name_match"
            ),
            ResolvedEntity(
                asset=_asset("P-202", "Pompa Acqua"), confidence=0.9, match_reason="name_match"
            ),
        ]

        assert match_disambiguation_reply("Pompa Acqua", candidates) == 1
        # ...and the shorter name still selects the shorter-named asset.
        assert match_disambiguation_reply("Pompa", candidates) == 0

    def test_two_assets_with_the_same_name_still_collapse_to_a_miss(self) -> None:
        """Identical names are not proper substrings — the genuine tie survives."""
        candidates = [
            ResolvedEntity(asset=_asset("P-201"), confidence=0.9, match_reason="name_match"),
            ResolvedEntity(asset=_asset("P-202"), confidence=0.9, match_reason="name_match"),
        ]

        assert match_disambiguation_reply("Cooling Water Pump", candidates) is None


class TestAnsweringTheQuestion:
    @pytest.mark.asyncio
    async def test_exact_id_reply_resolves_and_clears_the_entry(self) -> None:
        agent = Agent(plant=_tie_plant())
        await _ask_ambiguously(agent)

        resolved = agent._apply_disambiguation(
            "P-202", agent._resolver.resolve("P-202"), chat_id="c1"
        )

        assert [r.asset.id for r in resolved] == ["P-202"]
        assert "c1" not in agent._disambiguations

    @pytest.mark.asyncio
    async def test_answered_turn_commits_the_chosen_asset(self) -> None:
        agent = Agent(plant=_tie_plant())
        await _ask_ambiguously(agent)

        resolved = agent._apply_disambiguation("la seconda", [], chat_id="c1")
        context = await agent._gather_context("la seconda", resolved, chat_id="c1")

        assert context["asset"].id == "P-202"
        assert context["resolution_verdict"].commits is True
        # And the write path now authorises that asset (the point of answering).
        assert agent._authorize_write_target("P-202", "c1") is None

    @pytest.mark.parametrize("reply", ["la seconda", "the second one", "2"])
    @pytest.mark.asyncio
    async def test_ordinal_replies_select_the_second_candidate(self, reply: str) -> None:
        agent = Agent(plant=_tie_plant())
        candidates = await _ask_ambiguously(agent)

        resolved = agent._apply_disambiguation(reply, agent._resolver.resolve(reply), chat_id="c1")

        assert [r.asset.id for r in resolved] == [candidates[1].asset.id]

    @pytest.mark.asyncio
    async def test_a_reply_matching_several_candidates_is_a_miss(self) -> None:
        """Both assets are called "Cooling Water Pump" — repeating it resolves nothing."""
        agent = Agent(plant=_tie_plant())
        await _ask_ambiguously(agent)

        resolved = agent._apply_disambiguation(
            _AMBIGUOUS_QUERY, agent._resolver.resolve(_AMBIGUOUS_QUERY), chat_id="c1"
        )
        context = await agent._gather_context(_AMBIGUOUS_QUERY, resolved, chat_id="c1")

        assert context["resolution_verdict"].ambiguous is True
        assert "asset" not in context


class TestBoundedRetry:
    @pytest.mark.asyncio
    async def test_first_miss_restates_the_question_and_commits_nothing(self) -> None:
        agent = Agent(plant=_tie_plant())
        candidates = await _ask_ambiguously(agent)

        miss = "what is the weather"
        resolved = agent._apply_disambiguation(miss, agent._resolver.resolve(miss), chat_id="c1")
        context = await agent._gather_context(miss, resolved, chat_id="c1")

        # The same candidates are back in front of the model...
        assert [r.asset.id for r in resolved] == [c.asset.id for c in candidates]
        assert context["resolution_verdict"].ambiguous is True
        assert "asset" not in context
        # ...the entry is still live, with the miss counted...
        assert agent._disambiguations["c1"][2] == 1
        # ...and nothing was committed for a write to land on.
        refusal = agent._authorize_write_target("P-201", "c1")
        assert refusal is not None
        assert refusal["reason"] == "ambiguous_resolution"

    @pytest.mark.asyncio
    async def test_restating_does_not_reset_the_miss_counter(self) -> None:
        """The livelock guard's load-bearing detail.

        The restated turn re-enters ``_record_disambiguation`` with the same
        candidates. Resetting the counter there would make the retry unbounded
        again — the entry would outlive every subject change.
        """
        agent = Agent(plant=_tie_plant())
        await _ask_ambiguously(agent)

        miss = "what is the weather"
        resolved = agent._apply_disambiguation(miss, [], chat_id="c1")
        await agent._gather_context(miss, resolved, chat_id="c1")

        assert agent._disambiguations["c1"][2] == 1

    @pytest.mark.asyncio
    async def test_second_miss_abandons_the_entry_and_the_write_stays_gated(self) -> None:
        """Two unrelated turns and the conversation moves on — but ungated."""
        agent = Agent(plant=_tie_plant())
        await _ask_ambiguously(agent)

        miss = "what is the weather"
        for _ in range(2):
            resolved = agent._apply_disambiguation(
                miss, agent._resolver.resolve(miss), chat_id="c1"
            )
            context = await agent._gather_context(miss, resolved, chat_id="c1")

        assert "c1" not in agent._disambiguations
        # The turn proceeded under normal resolution — which found nothing.
        assert context["resolved_entities"] == []
        # Safety never depended on the entry persisting: U5 still refuses.
        refusal = agent._authorize_write_target("P-201", "c1")
        assert refusal is not None
        assert refusal["reason"] == "nothing_resolved"

    @pytest.mark.asyncio
    async def test_a_confident_fresh_resolution_supersedes_the_question(self) -> None:
        """The user abandons the question and names an asset outright.

        "crea un OdL per P-203" is a whole-token ID hit at 1.0 on an asset that
        was never a candidate. Replacing it with the recorded candidates
        answered a question the user had stopped asking, and put assets they
        were no longer discussing into the refusal message.
        """
        plant = _tie_plant()
        plant.register_asset(_asset("P-203", name="Boiler Feed Unit"))
        agent = Agent(plant=plant)
        await _ask_ambiguously(agent)

        message = "crea un OdL per P-203"
        resolved = agent._apply_disambiguation(
            message, agent._resolver.resolve(message), chat_id="c1"
        )
        context = await agent._gather_context(message, resolved, chat_id="c1")

        assert [r.asset.id for r in resolved] == ["P-203"]
        assert context["asset"].id == "P-203"
        # The question is retired, not left to catch the next message.
        assert "c1" not in agent._disambiguations
        # And the write the user actually asked for is authorised.
        assert agent._authorize_write_target("P-203", "c1") is None

    @pytest.mark.asyncio
    async def test_a_mid_band_fresh_resolution_does_not_supersede(self) -> None:
        """The supersede branch is ``high`` only — a 0.6 guess is still a miss.

        This is the boundary that keeps the branch from reopening the
        laundering hole: anything short of a fresh, independently strong,
        unambiguous resolution holds the entry and restates the question.
        """
        agent = Agent(plant=_tie_plant())
        candidates = await _ask_ambiguously(agent)
        mid = [
            ResolvedEntity(asset=_asset("P-201"), confidence=0.6, match_reason="location_match")
        ]

        resolved = agent._apply_disambiguation("qualcosa in building a", mid, chat_id="c1")

        assert [r.asset.id for r in resolved] == [c.asset.id for c in candidates]
        assert agent._disambiguations["c1"][2] == 1

    @pytest.mark.asyncio
    async def test_ambiguity_does_not_launder_through_a_resolveless_turn(self) -> None:
        """Turn 1 ambiguous, turn 2 resolves nothing — the write is still refused.

        The case a per-turn marker drops on the floor: by turn 2 the model has
        the candidate IDs in its own history and can name one unprompted.
        """
        agent = Agent(plant=_tie_plant())
        await _ask_ambiguously(agent)

        nothing = "what is the weather"
        resolved = agent._apply_disambiguation(
            nothing, agent._resolver.resolve(nothing), chat_id="c1"
        )
        await agent._gather_context(nothing, resolved, chat_id="c1")

        assert agent._authorize_write_target("P-202", "c1") is not None


class TestStoreLifecycle:
    @pytest.mark.asyncio
    async def test_a_single_weak_guess_records_nothing(self) -> None:
        """A weak guess is not a menu — there is nothing to choose between."""
        agent = Agent(plant=_tie_plant())
        weak = ResolvedEntity(asset=_asset("P-201"), confidence=0.16, match_reason="keyword_match")

        context = await agent._gather_context("the thing over there", [weak], chat_id="c1")

        assert context["resolution_verdict"].commits is False
        assert "c1" not in agent._disambiguations

    @pytest.mark.asyncio
    async def test_a_low_band_tie_is_recorded_because_it_is_still_asked_about(self) -> None:
        """The recording rule is ``ambiguous``, full stop — band does not veto it.

        A single candidate cannot tie, so the single-guess test above exercises
        only the ``not confident`` half of the withhold; it passes whether the
        recording is keyed on ``ambiguous`` or on ``not confident``. Two
        candidates at an identical sub-0.4 score separate them, and the answer
        is that the entry IS recorded: ``format_resolved_entities`` renders its
        "Ambiguous — ask the user which one they mean" nudge on the very same
        ``verdict.ambiguous``, so the user is put a question here. Declining to
        record would ask it and then have nowhere to put the answer.

        What the ``low`` exclusion actually excludes is the single weak guess
        above — no tie, no menu, nothing to choose between.
        """
        agent = Agent(plant=_tie_plant())
        weak = [
            ResolvedEntity(asset=_asset("P-201"), confidence=0.16, match_reason="keyword_match"),
            ResolvedEntity(asset=_asset("P-202"), confidence=0.16, match_reason="keyword_match"),
        ]

        context = await agent._gather_context("the thing over there", weak, chat_id="c1")

        verdict = context["resolution_verdict"]
        assert verdict.ambiguous is True  # precondition: the tie half is live
        assert verdict.band == "low"
        assert verdict.commits is False
        assert [c.asset.id for c in agent._disambiguations["c1"][0]] == ["P-201", "P-202"]
        # And the question is answerable, which is the whole reason to record it.
        answered = agent._apply_disambiguation("la seconda", [], chat_id="c1")
        assert [r.asset.id for r in answered] == ["P-202"]

    @pytest.mark.asyncio
    async def test_only_the_rendered_candidates_are_recorded(self) -> None:
        """The store holds what the prompt SHOWS, not everything that matched.

        The prompt renders ``MAX_RENDERED_CANDIDATES``; the store used to keep
        all of them and the positional tiers bounded on ``len(candidates)``. So
        "quinto" selected a fifth asset the user had never been shown and
        promoted it to confidence 1.0 — falsifying
        ``match_disambiguation_reply``'s documented guarantee.
        """
        plant = Plant(name="Test Plant")
        for n in range(1, 6):
            plant.register_asset(_asset(f"P-20{n}"))
        agent = Agent(plant=plant)
        resolved = agent._resolver.resolve(_AMBIGUOUS_QUERY)
        assert len(resolved) == 5  # precondition: more matched than are shown

        await agent._gather_context(_AMBIGUOUS_QUERY, resolved, chat_id="c1")

        offered = agent._disambiguations["c1"][0]
        assert len(offered) == MAX_RENDERED_CANDIDATES
        assert [c.asset.id for c in offered] == [r.asset.id for r in resolved[:3]]

    @pytest.mark.asyncio
    async def test_an_ordinal_past_the_rendered_slice_is_a_miss(self) -> None:
        plant = Plant(name="Test Plant")
        for n in range(1, 6):
            plant.register_asset(_asset(f"P-20{n}"))
        agent = Agent(plant=plant)
        await agent._gather_context(
            _AMBIGUOUS_QUERY, agent._resolver.resolve(_AMBIGUOUS_QUERY), chat_id="c1"
        )

        resolved = agent._apply_disambiguation("quinto", [], chat_id="c1")

        # Restated, not answered — and certainly not resolved to P-205.
        assert [r.asset.id for r in resolved] == ["P-201", "P-202", "P-203"]
        assert agent._disambiguations["c1"][2] == 1

    @pytest.mark.asyncio
    async def test_committed_turn_records_nothing(self) -> None:
        agent = Agent(plant=_tie_plant())
        resolved = agent._resolver.resolve("P-201")
        await agent._gather_context("P-201", resolved, chat_id="c1")

        assert "c1" not in agent._disambiguations

    @pytest.mark.asyncio
    async def test_expired_entry_behaves_as_a_fresh_turn(self) -> None:
        agent = Agent(plant=_tie_plant())
        candidates = await _ask_ambiguously(agent)
        agent._disambiguations["c1"] = (
            tuple(candidates),
            time.monotonic() - _PENDING_ACTION_TTL_SECONDS - 1,
            0,
        )

        resolved = agent._apply_disambiguation("la seconda", [], chat_id="c1")

        assert resolved == []
        assert "c1" not in agent._disambiguations

    @pytest.mark.asyncio
    async def test_another_conversation_is_unaffected(self) -> None:
        agent = Agent(plant=_tie_plant())
        await _ask_ambiguously(agent, chat_id="c1")

        resolved = agent._apply_disambiguation("la seconda", [], chat_id="c2")

        assert resolved == []
        assert "c1" in agent._disambiguations


class TestPublicEntryPoint:
    """The flow as a user meets it — two turns, no private methods."""

    @pytest.mark.asyncio
    async def test_two_turn_disambiguation_through_handle_message(self) -> None:
        llm = _RecordingLLM("I found two pumps by that name. Which one?")
        agent = Agent(plant=_tie_plant(), llm=llm)

        await agent.handle_message_full(_AMBIGUOUS_QUERY, chat_id="c1")
        asked = "\n".join(m["content"] for m in llm.seen)
        assert "Ambiguous" in asked
        assert "c1" in agent._disambiguations

        llm.answer = "P-202 last ran on Tuesday."
        await agent.handle_message_full("la seconda", chat_id="c1")
        answered = "\n".join(m["content"] for m in llm.seen)

        assert "(ID: P-202)" in answered
        assert "Ambiguous" not in answered
        assert "c1" not in agent._disambiguations

    @pytest.mark.asyncio
    async def test_store_works_on_an_anonymous_channel(self) -> None:
        """``user_id`` defaults to ``""`` — the case a ``_pending_actions`` mirror breaks.

        Recorded candidates are conversational memory, not an authorization, so
        they are keyed on ``chat_id`` alone. Keyed the other way this store
        would be permanently empty for ``Agent.ask()`` and the quickstart.
        """
        llm = _RecordingLLM("Which one?")
        agent = Agent(plant=_tie_plant(), llm=llm)

        await agent.handle_message_full(_AMBIGUOUS_QUERY, chat_id="c1", user_id="")
        assert "c1" in agent._disambiguations

        llm.answer = "Here are the details."
        await agent.handle_message_full("P-201", chat_id="c1", user_id="")

        assert "c1" not in agent._disambiguations
        assert "(ID: P-201)" in "\n".join(m["content"] for m in llm.seen)
