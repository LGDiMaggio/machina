"""Neutral, code-derived self-description of Machina.

:func:`describe` returns a deterministic :class:`Spine` snapshot of what the
framework can do (connectors x capabilities), how it is configured (schema
shape only), and where its extension seams are — derived entirely from code,
with **no** heavy optional dependency imported.

Public API::

    from machina.introspect import describe

    spine = describe()
    for connector in spine.connectors:
        ...
"""

from __future__ import annotations

from machina.introspect.core import (
    CapabilityInfo,
    ConnectorCapability,
    ConnectorInfo,
    ConventionSeam,
    Gaps,
    ProtocolSeam,
    SeamMethod,
    Seams,
    Spine,
    describe,
)

__all__ = [
    "CapabilityInfo",
    "ConnectorCapability",
    "ConnectorInfo",
    "ConventionSeam",
    "Gaps",
    "ProtocolSeam",
    "SeamMethod",
    "Seams",
    "Spine",
    "describe",
]
