"""The filter values the CMMS connectors' ``read_spare_parts`` accepts."""

from __future__ import annotations

from machina.exceptions import ConnectorError


def spare_part_filters(asset_id: object, sku: object) -> tuple[str, str]:
    """Return ``read_spare_parts``' filters as text, refusing one that is not an ID.

    The empty string, the default, is no filter. Any other value is a filter
    the connector must apply or refuse, so it must be an ID: a non-blank
    string, or a whole number, which is matched as its text (an asset number
    from a workflow event or model JSON). ``None`` (what a workflow step
    passes when its ``{trigger.asset_id}`` placeholder does not resolve), a
    blank string, a boolean, a float or a container is refused: read as no
    filter, it would pass the whole inventory off as the asset's parts.

    Args:
        asset_id: The caller's asset filter.
        sku: The caller's sku filter.

    Returns:
        The two filters as text, ``""`` for one that is not given.

    Raises:
        ConnectorError: If a filter is not an ID. The message names the
            filter, never its value.
    """
    return _filter_text("asset_id", asset_id), _filter_text("sku", sku)


def _filter_text(name: str, value: object) -> str:
    if isinstance(value, str) and (value == "" or value.strip()):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    raise ConnectorError(
        f"read_spare_parts: the {name} filter is not an ID; give a non-blank {name}, "
        f"or leave it out for no {name} filter"
    )
