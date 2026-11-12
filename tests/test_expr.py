import pytest

from meta_ads_guard.errors import ExpressionError
from meta_ads_guard.expr import parse, tokenize


def env(**values):
    def resolve(name, window):
        key = f"{name}[{window}]" if window else name
        return values.get(key, values.get(name))

    return resolve


@pytest.mark.parametrize(
    "source, values, expected",
    [
        ("spend > 1500 BDT and purchases == 0", {"spend": 1600, "purchases": 0}, True),
        ("spend > 1500 BDT and purchases == 0", {"spend": 1600, "purchases": 1}, False),
        ("spend > 1500 BDT and purchases == 0", {"spend": 1500, "purchases": 0}, False),
        ("frequency > 3.5", {"frequency": 3.51}, True),
        ("cpa > target × 1.4", {"cpa": 701, "target": 500}, True),
        ("cpa > target x 1.4", {"cpa": 699, "target": 500}, False),
        ("cpa > target * 1.4", {"cpa": 700.01, "target": 500}, True),
        ("ctr < 0.8% or cpm > 450", {"ctr": 1.2, "cpm": 451}, True),
        ("not (ctr >= 0.8)", {"ctr": 0.5}, True),
        ("!(ctr >= 0.8)", {"ctr": 0.9}, False),
        ("0.5 < roas < 1.2", {"roas": 0.9}, True),
        ("0.5 < roas < 1.2", {"roas": 1.3}, False),
        ("spend[today] > 2 * spend[yesterday]", {"spend[today]": 900, "spend[yesterday]": 400},
         True),
        ("spend >= 100 && clicks <= 3", {"spend": 100, "clicks": 3}, True),
        ("spend = 100", {"spend": 100}, True),
        ("spend != 100", {"spend": 100}, False),
        ("-spend < -10", {"spend": 20}, True),
        ("(spend - 10) / 2 == 45", {"spend": 100}, True),
        ("spend ÷ 4 == 25", {"spend": 100}, True),
        ("1_500 == spend", {"spend": 1500}, True),
        ("1e3 == spend", {"spend": 1000}, True),
        ("true", {}, True),
        ("false or spend > 1", {"spend": 2}, True),
        ("max(cpa, 10) > 100", {"cpa": None}, False),
        ("max(cpa, 200) > 100", {"cpa": None}, True),
        ("min(1, 2, 3) == 1", {}, True),
        ("abs(-5) == 5", {}, True),
        ("coalesce(cpa, 0) == 0", {"cpa": None}, True),
        ("round(1.256, 2) == 1.26", {}, True),
        ("missing(cpa) and spend > 0", {"cpa": None, "spend": 5}, True),
        ("0.1 + 0.2 == 0.3", {}, True),
        ("0.1 + 0.2 >= 0.3", {}, True),
    ],
)
def test_evaluate(source, values, expected):
    assert parse(source).evaluate(env(**values)) is expected


def test_missing_values_never_match():
    expr = parse("cpa > 100")
    assert expr.evaluate(env(cpa=None)) is False
    # a missing value is not "less than" either
    assert parse("cpa < 100").evaluate(env(cpa=None)) is False
    # but "not" of a failed comparison is true, as in plain boolean logic
    assert parse("not cpa > 100").evaluate(env(cpa=None)) is True


def test_division_by_zero_is_missing():
    assert parse("spend / clicks > 1").evaluate(env(spend=10, clicks=0)) is False
    assert parse("spend / clicks").value(env(spend=10, clicks=0)) is None


def test_precedence():
    assert parse("1 + 2 * 3 == 7").evaluate(env())
    assert parse("true or false and false").evaluate(env())
    assert not parse("(true or false) and false").evaluate(env())


def test_currencies_and_windows_are_collected():
    expr = parse("spend[7d] > 1500 BDT and cpm[today] < 300 BDT and frequency > 2")
    assert expr.currencies() == {"BDT"}
    assert expr.windows() == {"7d", "today"}
    names = [(v.name, v.window) for v in expr.variables()]
    assert names == [("spend", "7d"), ("cpm", "today"), ("frequency", None)]


def test_action_names_with_dots():
    expr = parse("actions.offsite_conversion.fb_pixel_purchase >= 2")
    assert expr.variables()[0].name == "actions.offsite_conversion.fb_pixel_purchase"


@pytest.mark.parametrize(
    "source, fragment",
    [
        ("", "empty expression"),
        ("spend >", "unexpected end"),
        ("spend > > 1", "unexpected '>'"),
        ("(spend > 1", "expected ')'"),
        ("spend > 1)", "unexpected ')'"),
        ("spend @ 1", "unexpected character"),
        ("foo(1)", "unknown function"),
        ("abs(1, 2)", "takes"),
        ("spend[] > 1", "empty window"),
        ("spend > 1 2", "unexpected '2'"),
    ],
)
def test_parse_errors(source, fragment):
    with pytest.raises(ExpressionError) as info:
        parse(source)
    assert fragment in str(info.value)


def test_error_points_at_position():
    with pytest.raises(ExpressionError) as info:
        parse("spend > 10 and $x")
    assert info.value.position == 15
    assert "^" in str(info.value)


def test_non_string_condition():
    with pytest.raises(ExpressionError):
        parse(42)


def test_tokenize_keywords_case_insensitive():
    kinds = [(t.kind, t.text) for t in tokenize("spend AND clicks Or NOT x")]
    assert ("KW", "and") in kinds and ("KW", "or") in kinds and ("KW", "not") in kinds


def test_no_python_eval():
    # Attribute access or dunder tricks are just unknown names to the parser.
    with pytest.raises(ExpressionError):
        parse("__import__('os').system('true')")
