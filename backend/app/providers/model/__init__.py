"""Outbound to a model service. The second class of external write (ADR 0010).

Parallel to `app/providers/` rather than inside it: the safety gate is the same
shape (typed kind, immutable after creation, DRAFT -> explicit successful test
-> ACTIVE, secret encrypted and never echoed) but routing an Incident to a model
is meaningless, so the kinds must not share an enum.
"""
