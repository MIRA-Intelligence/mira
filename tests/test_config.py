import pytest

from medpilot.config.schema import normalize_model_candidates, primary_model_candidate


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, []),
        ("m1", ["m1"]),
        (["a", "b"], ["a", "b"]),
        (["a", "a", "b"], ["a", "b"]),
        (["  x  ", "y"], ["x", "y"]),
        (["", "  ", "ok"], ["ok"]),
        ([], []),
    ],
)
def test_normalize_model_candidates(value, expected) -> None:
    assert normalize_model_candidates(value) == expected


@pytest.mark.parametrize(
    ("value", "fallback", "expected"),
    [
        ("first", None, "first"),
        (["a", "b"], None, "a"),
        (None, "fb", "fb"),
        ([], "fb", "fb"),
        (None, None, None),
    ],
)
def test_primary_model_candidate(value, fallback, expected) -> None:
    assert primary_model_candidate(value, fallback=fallback) == expected
