import pytest

from myself_agent.evaluation_metrics import VARIANTS, summarize


def observation(**overrides):
    row = dict(
        case_id="case",
        variant="custom",
        repeat=1,
        accepted=True,
        seconds=1.0,
        model_calls=1,
        total_tokens=10,
        reported_passed=True,
        artifact_checks_passed=True,
    )
    row.update(overrides)
    return row


def test_empty_observations_have_zero_runs_and_no_measurements():
    result = summarize([])
    for variant in VARIANTS:
        metrics = result["variants"][variant]
        assert (
            metrics["runs"],
            metrics["median_seconds"],
            metrics["observed_model_calls"],
            metrics["observed_total_tokens"],
        ) == (0, None, None, None)
    assert result["comparative_superiority_established"] is False


def test_custom_acceptance_wrong_acceptance_and_usage_summary():
    rows = [
        observation(
            case_id="accepted",
            accepted=True,
            seconds=2,
            model_calls=2,
            total_tokens=100,
            reported_passed=True,
            artifact_checks_passed=True,
        ),
        observation(
            case_id="rejected",
            accepted=False,
            seconds=4,
            model_calls=3,
            total_tokens=200,
            reported_passed=True,
            artifact_checks_passed=False,
        ),
    ]

    metrics = summarize(rows)["variants"]["custom"]
    assert (
        metrics["accepted"],
        metrics["not_accepted"],
        metrics["wrong_acceptances"],
        metrics["median_seconds"],
        metrics["observed_model_calls"],
        metrics["observed_total_tokens"],
    ) == (1, 1, 1, 3, 5, 300)


def test_missing_usage_is_counted_and_not_silently_zero():
    metrics = summarize([observation(seconds=None, model_calls=None, total_tokens=None)])[
        "variants"
    ]["custom"]

    assert (
        metrics["missing_call_records"],
        metrics["missing_token_records"],
        metrics["median_seconds"],
        metrics["observed_model_calls"],
        metrics["observed_total_tokens"],
    ) == (1, 1, None, None, None)


def test_known_zero_usage_is_observed_zero_not_none():
    metrics = summarize([observation(model_calls=0, total_tokens=0)])["variants"]["custom"]

    assert (
        metrics["missing_call_records"],
        metrics["missing_token_records"],
        metrics["observed_model_calls"],
        metrics["observed_total_tokens"],
    ) == (0, 0, 0, 0)


def test_duplicate_case_variant_repeat_raises_value_error():
    row = observation()
    with pytest.raises(ValueError):
        summarize([row, dict(row)])


@pytest.mark.parametrize(
    "overrides",
    [
        {"variant": "unknown"},
        {"accepted": 1},
    ],
)
def test_invalid_variant_or_non_boolean_acceptance_raises_value_error(overrides):
    with pytest.raises(ValueError):
        summarize([observation(**overrides)])


def test_acceptance_cannot_contradict_independent_evidence():
    with pytest.raises(ValueError, match="contradicts"):
        summarize([observation(accepted=True, artifact_checks_passed=False)])
