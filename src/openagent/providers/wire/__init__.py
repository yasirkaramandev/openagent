"""Protocol serializers, one per wire format (spec §7).

A provider names *who* is being talked to; a :class:`~openagent.core.models.Protocol` names *how*.
Only the how decides how a request is serialized and how a stream is read, so these modules are
keyed by protocol and configured by a :class:`~..compat.profiles_v2.CompatibilityProfile` — not one
module per vendor. Nine v0.2 providers share five wires here; a tenth provider that speaks one of
them adds a profile entry and no code.
"""
