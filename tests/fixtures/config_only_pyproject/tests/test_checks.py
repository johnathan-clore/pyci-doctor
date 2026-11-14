from evals.checks import score


def test_score():
    assert score("hi") == "PASS"
