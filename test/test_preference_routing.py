from dataclasses import replace

import pytest

from kiro_crew.preference_routing import Advice, PreferenceExample, advise, resolve_budget_model


@pytest.fixture
def examples():
    return [
        PreferenceExample("one", "session-one", "classify rules", "worker", "fast"),
        PreferenceExample("two", "session-two", "sort rules", "worker", "fast"),
    ]


class TestPreferenceRouting:
    def test_two_independent_preferences_support_advice(self, examples):
        result = advise("classify this", "worker", examples, lambda _: [1, 0])
        assert result.budget == "fast"
        assert result.examples == ("one", "two")

    def test_copied_sessions_do_not_inflate_support(self, examples):
        examples[1] = replace(examples[1], group=examples[0].group)
        assert advise("classify", "worker", examples, lambda _: [1, 0]).budget is None

    def test_worker_preferences_never_downgrade_parent(self, examples):
        def forbidden(_):
            pytest.fail("no encoding without relevant examples")

        assert advise("coordinate", "parent", examples, forbidden).reason == "no_examples"

    def test_conflicting_revisions_do_not_supply_a_vote(self, examples):
        examples.append(replace(examples[0], id="revision", budget="frontier"))
        assert advise("classify", "worker", examples, lambda _: [1, 0]).budget is None

    @pytest.mark.parametrize("flag", ["continuation", "explicitly_pinned"])
    def test_keep_current_without_encoding(self, examples, flag):
        def forbidden(_):
            pytest.fail("continuations and manual choices need no scoring")

        assert advise("yes", "worker", examples, forbidden, **{flag: True}).reason == "keep_current"

    def test_conflicting_preferences_abstain(self, examples):
        examples[1] = replace(examples[1], budget="frontier")
        assert advise("classify", "worker", examples, lambda _: [1, 0]).reason == "ambiguous"

    @pytest.mark.parametrize("vector", [None, [], [0, 0], [float("nan"), 0], [1]])
    def test_invalid_or_missing_embeddings_cannot_recommend(self, examples, vector):
        result = advise(
            "query", "worker", examples, lambda text: [1, 0] if text == "query" else vector
        )
        assert result.budget is None

    def test_unrelated_queries_abstain(self, examples):
        result = advise(
            "query", "worker", examples, lambda text: [1, 0] if text == "query" else [0, 1]
        )
        assert result.reason == "insufficient_support"

    def test_only_exact_advertised_models_are_suggested(self):
        result = Advice("fast", "similar_preferences")
        assert resolve_budget_model(result, {"fast": "worker-model"}, []) is None
        assert resolve_budget_model(result, {"fast": "worker-model"}, ["other-model"]) is None
        assert (
            resolve_budget_model(result, {"fast": "worker-model"}, ["worker-model"])
            == "worker-model"
        )
