"""What we ask a model when it investigates one alert (ADR 0011).

The wording here is doing one job above all others: **stopping the model from
collapsing onto the first plausible signal and then arguing for it.** Grafana's
own write-up named that as the failure mode of this exact product shape, and
with "one alert plus one curve" as the input it is close to inevitable — there
is a spike on the chart, so the spike becomes the answer.

The structure is what prevents it, not the politeness. Every claim has to be a
hypothesis with a verdict, and a verdict has to cite facts by id. A model that
cannot support a hunch has to mark it `BLOCKED` and say what is missing, which
is exactly the sentence a free-text answer would have quietly dropped.

Two things the wording must never be trusted for, because code enforces them:
the fact ids are checked against the snapshot after the fact, and the verdict
rules (`DISPROVEN` carries no recommendations, mitigation needs `SUPPORTED`) are
pydantic validators. The prompt makes a good answer likely; the validators make
a bad one impossible (D24).
"""

from __future__ import annotations

from typing import Any

from app.services.investigation_contract import structured_response_format

#: Bump when the instructions change in a way that could change answers.
#: Recorded on every run so "the results got worse" is a question with an answer.
INVESTIGATION_VERSION = "investigation/v1"

INVESTIGATION_PROMPT = """\
You are helping an on-call engineer understand one firing alert on their own
cluster. Answer in the same language as the alert's annotations.

You are given three kinds of evidence, each item carrying a `fact_id`:
- the alert itself: labels and annotations, as the monitoring system wrote them;
- metric facts: statistics computed from real curves around the alert window;
- handling history: what this same alert group did before, and how the user
  resolved it.

Write hypotheses, not a narrative. For each one:
- `statement` is a specific claim about *this* alert, not a general observation.
- `verdict` is one of:
    SUPPORTED  - the evidence backs it
    SYMPTOM    - real, but an effect of something else rather than the cause
    DISPROVEN  - you checked and the evidence contradicts it
    BLOCKED    - plausible, but the evidence here cannot settle it
- cite `fact_id`s. A statement with no cited fact will be rejected.
- `missing_evidence` on a BLOCKED hypothesis says what would settle it.

**A hypothesis you disproved is worth keeping.** "It is not a memory problem,
I checked" saves the reader the same dead end, and it is the first thing a
free-text answer drops.

Recommendations:
- `NEXT_CHECK` - something to look at, phrased so the reader knows where to look.
- `MITIGATION_CANDIDATE` - an action that might resolve it. Only on SUPPORTED.
  The user executes it; you never do. Prefer the reversible option, and say what
  it would cost if the hypothesis turns out to be wrong.

Do not restate the annotation back to the reader; they wrote it or they have
already read it. Do not claim a root cause: correlated metrics and past handling
cannot establish causation, and overclaiming is the failure this structure
exists to prevent. If the history says the user repeatedly closed this as a
false alarm, that is evidence about the alert rule, and saying so is useful.

Prefer two hypotheses you can support over five you cannot.\
"""


def investigation_response_format() -> dict[str, Any]:
    """The strict schema, shared with the channel test so both prove the same shape."""
    return structured_response_format()


__all__ = [
    "INVESTIGATION_PROMPT",
    "INVESTIGATION_VERSION",
    "investigation_response_format",
]
