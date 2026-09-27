"""F27 alert-expression parsing: pure functions, no network, no session.

Everything here operates on text that arrived from the monitored system
(``generatorURL``, rule ``expr``), so every entry point is bounded and returns a
value instead of raising on malformed input.

**This is deliberately not a PromQL parser.** Tier detection uses a single
depth-tracking scan and refuses whenever the shape is not obviously safe. An
incomplete parser is more dangerous than an explicit heuristic: it gives wrong
answers where nobody expects one, while a heuristic fails predictably by falling
through to the next tier (ADR 0009).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

# Bounded because the input is external text (R6).
MAX_URL_LENGTH = 8 * 1024
MAX_EXPR_LENGTH = 8 * 1024

_IDENT_START = re.compile(r"[A-Za-z_:]")
_IDENT = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")

# `<op> <number>` at the very end of the expression, anchored by the caller to a
# top-level position. `Inf`/`NaN` are valid PromQL number literals.
_NUMBER = re.compile(
    r"""^[+-]?(?:
        \d+(?:\.\d*)?(?:[eE][+-]?\d+)?
        | \.\d+(?:[eE][+-]?\d+)?
        | 0[xX][0-9a-fA-F]+
        | [Ii][Nn][Ff]
        | [Nn][Aa][Nn]
    )$""",
    re.VERBOSE,
)

_COMPARISON_OPS = ("==", "!=", "<=", ">=", "<", ">")

# Set operators. Their presence at the top level means the expression is not a
# single `value <op> threshold` comparison, so tier 1 must not fire. The real
# alert that drove this design is `(A == 0) or (B == 1)`.
_SET_OPERATORS = frozenset({"and", "or", "unless"})

# Aggregation operators are *not* caught by the "identifier followed by `(`"
# rule, because `sum by (ns) (...)` puts a keyword between the two. Left in,
# `sum` would be queried as a metric name.
_AGGREGATION_OPS = frozenset(
    {
        "sum",
        "min",
        "max",
        "avg",
        "group",
        "stddev",
        "stdvar",
        "count",
        "count_values",
        "bottomk",
        "topk",
        "quantile",
        "limitk",
        "limit_ratio",
    }
)

# Never a metric name.
_KEYWORDS = (
    frozenset(
        {
            "and",
            "or",
            "unless",
            "by",
            "without",
            "on",
            "ignoring",
            "group_left",
            "group_right",
            "offset",
            "bool",
            "start",
            "end",
            "inf",
            "nan",
        }
    )
    | _AGGREGATION_OPS
)

# Clauses whose following parenthesised group holds label names, not metrics.
_LABEL_LIST_KEYWORDS = frozenset(
    {"by", "without", "on", "ignoring", "group_left", "group_right"}
)

# Tier 2 rebuilds a selector from a bare metric name plus the alert's own labels.
# This is an **allowlist on purpose**: an unknown label left out costs extra
# series (bounded by the query budget), while an unknown label left in silently
# selects nothing. Under-constraining fails visibly, over-constraining does not.
IDENTITY_LABELS: tuple[str, ...] = (
    "cluster",
    "namespace",
    "pod",
    "container",
    "instance",
    "job",
    "node",
    "service",
    "persistentvolumeclaim",
    "device",
    "mountpoint",
)

DEFAULT_RATE_WINDOW = "5m"

# D37: the outer 24h window cannot bound what the expression itself asks for.
# `rate(x_total[30d])` still makes the store scan a month. Six hours covers every
# rate/increase an alert rule realistically uses; longer belongs to a question
# this capability does not answer.
MAX_RANGE_SELECTOR_SECONDS = 6 * 3600

_DURATION_UNITS = {
    "ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400,
    "w": 604800, "y": 365 * 86400,
}
_DURATION_PART = re.compile(r"(\d+)(ms|s|m|h|d|w|y)")
_DURATION_FULL = re.compile(r"^(?:\d+(?:ms|s|m|h|d|w|y))+$")


class QueryScopeUnsafe(ValueError):
    """The expression asks for more than the client is willing to send.

    Raised before any request leaves. Refusing beats trimming: silently
    shortening someone's `[30d]` to `[6h]` answers a different question while
    looking like it answered theirs.
    """


@dataclass(frozen=True)
class ThresholdSplit:
    """Tier 1's result. **The operator is not optional** (D33).

    A threshold line without a direction cannot be read, and every derived fact
    the investigation stage needs — when the value entered the alerting range,
    how often it crossed, how long it stayed — is computed from it.
    """

    expression: str
    operator: str
    threshold: float


# `0.1 > metric` means `metric < 0.1`; normalising keeps the meaning when the
# rule author wrote the constant first.
_REVERSED_OPERATOR = {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "==": "==", "!=": "!="}


class _Scan:
    """One left-to-right pass recording depth and string state per character."""

    __slots__ = ("text", "paren", "brace", "bracket", "in_string")

    def __init__(self, text: str) -> None:
        self.text = text

    def positions(self):
        """Yield ``(index, char, paren_depth, brace_depth, bracket_depth)``.

        Characters inside string literals and comments are skipped entirely, so
        callers never have to think about `{color="or"}` looking like a set
        operator.
        """

        text = self.text
        i = 0
        paren = brace = bracket = 0
        length = len(text)
        while i < length:
            ch = text[i]
            if ch == "#":
                while i < length and text[i] != "\n":
                    i += 1
                continue
            if ch in "\"'`":
                quote = ch
                i += 1
                while i < length:
                    if text[i] == "\\" and quote != "`":
                        i += 2
                        continue
                    if text[i] == quote:
                        i += 1
                        break
                    i += 1
                continue
            if ch == "(":
                paren += 1
            elif ch == ")":
                paren -= 1
            elif ch == "{":
                brace += 1
            elif ch == "}":
                brace -= 1
            elif ch == "[":
                bracket += 1
            elif ch == "]":
                bracket -= 1
            yield i, ch, paren, brace, bracket
            i += 1


def _top_level_words(expr: str) -> list[tuple[int, str]]:
    """Identifier-like words that sit outside every bracket and string."""

    words: list[tuple[int, str]] = []
    consumed_until = -1
    for index, ch, paren, brace, bracket in _Scan(expr).positions():
        if index < consumed_until or paren or brace or bracket:
            continue
        if not _IDENT_START.match(ch):
            continue
        match = _IDENT.match(expr, index)
        if match is None:
            continue
        words.append((index, match.group(0)))
        consumed_until = match.end()
    return words


def expr_from_generator_url(url: str | None) -> str | None:
    """Return the alert's PromQL from a Prometheus graph link, or ``None``.

    **The address is never requested** (ADR 0009). It is an in-cluster host that
    a local workbench cannot reach anyway, and following a URL written by the
    monitored system would let upstream content decide where the backend sends
    traffic.

    Only ``g0.expr`` is read: Prometheus puts exactly one query there when it
    generates an alert link; ``g1``/``g2`` only appear when a human adds panes in
    the UI, which never happens on an alert.
    """

    if not url or len(url) > MAX_URL_LENGTH:
        return None
    try:
        query = urlsplit(url).query
        values = parse_qs(query, keep_blank_values=False)
    except ValueError:
        return None
    candidates = values.get("g0.expr") or []
    for candidate in candidates:
        expr = candidate.strip()
        if expr and len(expr) <= MAX_EXPR_LENGTH:
            return expr
    return None


def strip_comparison(expr: str | None) -> ThresholdSplit | None:
    """Tier 1: split `<sub-expression> <op> <number>`, keeping the direction.

    Returns ``None`` — meaning "fall through to tier 2" — whenever the shape is
    anything less obvious than that. Refusing is cheap; guessing is not.
    """

    if not expr:
        return None
    text = expr.strip()
    if not text or len(text) > MAX_EXPR_LENGTH:
        return None

    # A subquery's `[...:...]` changes evaluation semantics; do not rewrite it.
    if _contains_subquery(text):
        return None

    words = _top_level_words(text)
    lowered = {word.lower() for _, word in words}
    if lowered & _SET_OPERATORS:
        return None
    # `offset`/`@` shift the evaluation window; `bool` turns the comparison into
    # a 0/1 series rather than a filter. None of them survive a naive strip.
    if "offset" in lowered or "bool" in lowered:
        return None
    if _has_top_level_at(text):
        return None

    occurrences = _top_level_comparisons(text)
    if len(occurrences) != 1:
        return None

    start, op = occurrences[0]
    left = text[:start].strip()
    right = text[start + len(op) :].strip()
    if not left or not right:
        return None

    left_is_number = bool(_NUMBER.match(left))
    right_is_number = bool(_NUMBER.match(right))
    if left_is_number and right_is_number:
        # `1 > 0` has nothing to chart, and picking a side would be arbitrary.
        return None
    if right_is_number:
        expression, operator, literal = left, op, right
    elif left_is_number:
        expression, operator, literal = right, _REVERSED_OPERATOR[op], left
    else:
        return None

    try:
        threshold = float(literal)
    except ValueError:
        return None
    return ThresholdSplit(expression=expression, operator=operator, threshold=threshold)


def _contains_subquery(expr: str) -> bool:
    depth_start: int | None = None
    for index, ch, _paren, _brace, bracket in _Scan(expr).positions():
        if ch == "[":
            depth_start = index
        elif ch == "]":
            depth_start = None
        elif ch == ":" and depth_start is not None and bracket:
            return True
    return False


def _has_top_level_at(expr: str) -> bool:
    return any(
        ch == "@" and not paren and not brace and not bracket
        for _index, ch, paren, brace, bracket in _Scan(expr).positions()
    )


def _top_level_comparisons(expr: str) -> list[tuple[int, str]]:
    """Positions of comparison operators outside every bracket and string."""

    found: list[tuple[int, str]] = []
    skip_until = -1
    for index, ch, paren, brace, bracket in _Scan(expr).positions():
        if index < skip_until or paren or brace or bracket:
            continue
        if ch not in "<>=!":
            continue
        for op in _COMPARISON_OPS:
            if expr.startswith(op, index):
                # `=~` / `!~` are matchers and only ever live inside `{}`, which
                # this branch already excluded; `=` alone is not a comparison.
                if op == "==" or op == "!=" or len(op) == 2 or expr[index + 1] != "=":
                    found.append((index, op))
                    skip_until = index + len(op)
                break
    return found


def extract_metric_names(expr: str | None) -> list[str]:
    """Tier 2: metric names in the expression, in order of first appearance.

    Excludes function names (identifier immediately followed by ``(``), label
    names (inside ``{}`` or inside a `by`/`without`/`on`/... group), keywords,
    and anything inside a string, comment or range selector.
    """

    if not expr:
        return []
    text = expr.strip()
    if not text or len(text) > MAX_EXPR_LENGTH:
        return []

    names: list[str] = []
    seen: set[str] = set()
    # One entry per open paren: True when the group holds label names.
    group_is_label_list: list[bool] = []
    pending_label_group = False
    consumed_until = -1

    for index, ch, _paren, brace, bracket in _Scan(text).positions():
        if ch == "(":
            group_is_label_list.append(pending_label_group)
            pending_label_group = False
            continue
        if ch == ")":
            if group_is_label_list:
                group_is_label_list.pop()
            continue
        if index < consumed_until:
            continue
        if brace or bracket or not _IDENT_START.match(ch):
            continue

        match = _IDENT.match(text, index)
        if match is None:
            continue
        word = match.group(0)
        consumed_until = match.end()

        if group_is_label_list and group_is_label_list[-1]:
            continue  # a label name inside by(...) / on(...) / ...

        lowered = word.lower()
        if lowered in _LABEL_LIST_KEYWORDS:
            pending_label_group = True
            continue
        if lowered in _KEYWORDS:
            continue
        if _next_non_space(text, match.end()) == "(":
            continue  # function call
        if word not in seen:
            seen.add(word)
            names.append(word)
    return names


def _next_non_space(text: str, index: int) -> str | None:
    while index < len(text) and text[index].isspace():
        index += 1
    return text[index] if index < len(text) else None


_EQUALITY_MATCHER = re.compile(
    r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"((?:[^"\\\\]|\\\\.)*)"'
)


def labels_with_multiple_values(expr: str | None) -> set[str]:
    """Labels the expression itself pins to more than one value.

    These must **not** be re-pinned from the alert's own labels when tier 2
    rebuilds a selector. The alert carries the value that fired — for the user's
    ES rule, `color="yellow"` — and constraining the chart to it hides the very
    transition the reader is looking for: the rule watches `green` *and*
    `yellow`, so charting only `yellow` draws a flat line where the green→yellow
    moment used to be.

    Only `=` matchers count. `!=` and regex matchers do not pin a value, so they
    say nothing about what the expression is varying.
    """

    text = (expr or "").strip()
    if not text or len(text) > MAX_EXPR_LENGTH:
        return set()

    seen: dict[str, set[str]] = {}
    for block_text in _brace_contents(text):
        for name, value in _EQUALITY_MATCHER.findall(block_text):
            seen.setdefault(name, set()).add(value)
    return {name for name, values in seen.items() if len(values) > 1}


def _brace_contents(expr: str) -> list[str]:
    """Text inside each top-level `{...}`, which is where label matchers live."""

    contents: list[str] = []
    start: int | None = None
    for index, ch, _paren, brace, _bracket in _Scan(expr).positions():
        if ch == "{" and brace == 1:
            start = index + 1
        elif ch == "}" and brace == 0 and start is not None:
            contents.append(expr[start:index])
            start = None
    return contents


def parse_promql_duration(text: str) -> int:
    """Seconds in a PromQL duration such as `5m`, `1h30m`, `7d`; -1 if unparseable.

    Sub-second units floor to zero rather than raising — the only caller compares
    against a ceiling measured in hours.
    """

    candidate = (text or "").strip()
    if not candidate or not _DURATION_FULL.match(candidate):
        return -1
    total = 0.0
    for amount, unit in _DURATION_PART.findall(candidate):
        total += int(amount) * _DURATION_UNITS[unit]
    return int(total)


def assert_query_scope_safe(expr: str | None) -> None:
    """Refuse expressions whose own time semantics exceed the client budget (D37).

    The outer window bounds `start`/`end`/`step`; it says nothing about what the
    expression asks the store to scan internally. Three constructs are rejected
    outright rather than bounded, because there is no honest way to shrink them
    without answering a different question:

    - ``offset`` moves the window somewhere else entirely;
    - ``@`` pins evaluation to an arbitrary instant;
    - a subquery ``[range:step]`` multiplies the work by its own resolution.

    Plain range selectors are capped instead of banned — they are how every
    ordinary `rate()` is written.
    """

    text = (expr or "").strip()
    if not text:
        return
    if len(text) > MAX_EXPR_LENGTH:
        raise QueryScopeUnsafe("expression is too long to inspect")

    words = {word.lower() for _index, word in _all_words(text)}
    if "offset" in words:
        raise QueryScopeUnsafe("`offset` is not allowed: it moves the window")
    if _contains_char_outside_quotes(text, "@"):
        raise QueryScopeUnsafe("`@` is not allowed: it pins evaluation to an instant")
    if _contains_subquery(text):
        raise QueryScopeUnsafe("subqueries `[range:step]` are not allowed")

    for raw in _bracket_contents(text):
        seconds = parse_promql_duration(raw)
        if seconds < 0:
            continue  # not a duration; left alone
        if seconds > MAX_RANGE_SELECTOR_SECONDS:
            raise QueryScopeUnsafe(
                f"range selector [{raw}] exceeds the "
                f"{MAX_RANGE_SELECTOR_SECONDS // 3600}h ceiling"
            )


def _all_words(expr: str) -> list[tuple[int, str]]:
    """Identifier-like words at any paren depth, skipping strings and comments."""

    words: list[tuple[int, str]] = []
    consumed_until = -1
    for index, ch, _paren, brace, bracket in _Scan(expr).positions():
        if index < consumed_until or brace or bracket:
            continue
        if not _IDENT_START.match(ch):
            continue
        match = _IDENT.match(expr, index)
        if match is None:
            continue
        words.append((index, match.group(0)))
        consumed_until = match.end()
    return words


def _contains_char_outside_quotes(expr: str, target: str) -> bool:
    return any(ch == target for _index, ch, _p, _b, _k in _Scan(expr).positions())


def _bracket_contents(expr: str) -> list[str]:
    """Text inside each `[...]`, which is where range selectors live."""

    contents: list[str] = []
    start: int | None = None
    for index, ch, _paren, _brace, bracket in _Scan(expr).positions():
        if ch == "[" and bracket == 1:
            start = index + 1
        elif ch == "]" and bracket == 0 and start is not None:
            contents.append(expr[start:index])
            start = None
    return contents


def escape_label_value(value: str) -> str:
    """Escape a label value for a PromQL matcher.

    Alert label values are external text: they reach us from the monitored
    system and must not be able to close the matcher and append their own.
    """

    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def build_metric_query(
    metric: str,
    labels: Mapping[str, str] | None = None,
    *,
    is_counter: bool,
    allowed_label_names: Iterable[str] | None = None,
    exclude_label_names: Iterable[str] | None = None,
    rate_window: str = DEFAULT_RATE_WINDOW,
) -> str:
    """Tier 2's final query: a bare metric constrained by the alert's own labels.

    ``is_counter`` wraps the selector in ``rate()``. **This only ever applies to
    tier 2**, where the system composes the query itself. User templates carry
    their own fully written PromQL and must never be wrapped again — a template
    that already says ``rate(...)`` would become a syntax error, and
    ``kube_pod_container_status_restarts_total`` needs ``increase()`` rather than
    ``rate()`` to stay readable (design §5.1 / ADR 0009).
    """

    # D35: the metric's *real* label schema is the main path — a fixed allowlist
    # drops exporter-specific labels (the user's ES alert carries `color`, which
    # is exactly what says which colour fired). `IDENTITY_LABELS` remains only as
    # the fallback for when that endpoint is unavailable, and the caller is
    # expected to surface that case as a guess.
    names: Iterable[str]
    if allowed_label_names is None:
        names = IDENTITY_LABELS
    else:
        # Sorted so the query is stable regardless of set iteration order.
        names = sorted(allowed_label_names)
    excluded = set(exclude_label_names or ())
    selected = {
        name: labels[name]
        for name in names
        if name not in excluded
        and labels
        and name in labels
        and str(labels[name]).strip()
    }
    matchers = ",".join(
        f'{name}="{escape_label_value(value)}"' for name, value in selected.items()
    )
    selector = f"{metric}{{{matchers}}}" if matchers else metric
    if is_counter:
        return f"rate({selector}[{rate_window}])"
    return selector
