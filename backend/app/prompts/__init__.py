"""Prompts as versioned artifacts, not string literals in the middle of a service.

Three things this buys, each of which was missing:

- **A version to record.** ADR 0011's D48 requires an investigation snapshot to
  carry the prompt version it was produced under; a literal buried in a service
  module has no version to carry, so the requirement was unimplementable as
  written and quietly went unbuilt.
- **Somewhere to test.** A prompt is the part of this system most likely to
  change and least likely to have anything checking it. Here it is data, so a
  test can assert the invariants that the surrounding code depends on -- notably
  that the instructions and the JSON schema agree about the field names.
- **A diff worth reading.** "Changed a string" and "changed the contract the
  model answers under" look identical in a patch until the prompt has a version
  and a home.

Every prompt here is paired with the **strict** JSON schema its answer must
satisfy. Structure is enforced by the schema and re-checked by code (D24);
the wording exists to make a correct answer likely, never to make it safe.
"""

from app.prompts.investigation import (
    INVESTIGATION_PROMPT,
    INVESTIGATION_VERSION,
    investigation_response_format,
)
__all__ = [
    "INVESTIGATION_PROMPT",
    "INVESTIGATION_VERSION",
    "investigation_response_format",
]
