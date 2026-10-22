"""``read_spare_parts`` filter values: no filter, an ID, or a refusal."""

from __future__ import annotations

import pytest

from machina.connectors.cmms.filters import spare_part_filters
from machina.exceptions import ConnectorError, ConnectorUnsupportedFilterError


def test_the_empty_string_is_no_filter() -> None:
    assert spare_part_filters("", "") == ("", "")


@pytest.mark.parametrize(
    ("value", "text"),
    [("P-201", "P-201"), (" P-201 ", " P-201 "), (11430, "11430"), (0, "0")],
    ids=["text", "padded-text", "number", "zero"],
)
def test_an_id_is_returned_as_text(value: object, text: str) -> None:
    """A whole number (from a workflow event or model JSON) is matched as its text."""
    assert spare_part_filters(value, "") == (text, "")
    assert spare_part_filters("", value) == ("", text)


@pytest.mark.parametrize("name", ["asset_id", "sku"])
@pytest.mark.parametrize(
    "value",
    [None, " ", "\t\n", True, False, 1.5, [], ["P-201"], {}, {"id": "P-201"}],
    ids=[
        "none",
        "blank",
        "whitespace",
        "true",
        "false",
        "float",
        "empty-list",
        "list",
        "empty-dict",
        "dict",
    ],
)
def test_a_value_that_is_not_an_id_is_refused(name: str, value: object) -> None:
    """Reading it as no filter would list the whole inventory as the asset's parts."""
    filters: dict[str, object] = {"asset_id": "", "sku": ""}
    filters[name] = value

    with pytest.raises(ConnectorError) as exc_info:
        spare_part_filters(**filters)

    # Not a refused filter: the caller sent a value no CMMS can match.
    assert not isinstance(exc_info.value, ConnectorUnsupportedFilterError)
    assert str(exc_info.value) == (
        f"read_spare_parts: the {name} filter is not an ID; give a non-blank {name}, "
        f"or leave it out for no {name} filter"
    )
