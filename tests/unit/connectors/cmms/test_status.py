"""Tests for the work-order status rules shared by the vendor CMMS connectors."""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from machina.connectors.cmms.mappers.maximo import MAXIMO_STATUS_MAP
from machina.connectors.cmms.mappers.sap_pm import SAP_STATUS_MAP
from machina.connectors.cmms.mappers.upkeep import UPKEEP_STATUS_MAP
from machina.connectors.cmms.status import status_filter, status_not_applied
from machina.domain.work_order import WorkOrderStatus
from machina.exceptions import ConnectorError


class TestStatusFilter:
    @pytest.mark.parametrize("status", list(WorkOrderStatus))
    def test_the_enum_and_its_value_in_any_case_are_machina_statuses(
        self, status: WorkOrderStatus
    ) -> None:
        """LLM and MCP clients send the value as text, not the enum."""
        assert status_filter(status) is status
        assert status_filter(status.value) is status
        assert status_filter(status.value.upper()) is status
        assert status_filter(status.value.title()) is status

    @pytest.mark.parametrize("code", ["REL", "TECO", "WMATL", "onHold", "complete", ""])
    def test_other_text_is_a_vendor_code(self, code: str) -> None:
        assert status_filter(code) is None

    def test_no_vendor_code_the_mappers_know_reads_as_a_machina_status(self) -> None:
        """Otherwise a vendor code would be reverse-mapped instead of sent."""
        codes = {*SAP_STATUS_MAP, *MAXIMO_STATUS_MAP, *UPKEEP_STATUS_MAP}
        assert [code for code in codes if status_filter(code) is not None] == []


class TestStatusNotApplied:
    def test_the_error_says_the_status_was_not_applied_and_is_logged(self) -> None:
        with capture_logs() as logs:
            error = status_not_applied("MaximoConnector", "WO-1", "closed", "assigned")
        assert isinstance(error, ConnectorError)
        assert str(error) == (
            "The CMMS accepted the update of work order WO-1, but its status reads back as "
            "'assigned', not 'closed': the status change was not applied. Read the work "
            "order before repeating the update."
        )
        assert logs == [
            {
                "event": "work_order_status_not_applied",
                "log_level": "warning",
                "connector": "MaximoConnector",
                "operation": "update_work_order",
                "work_order_id": "WO-1",
                "wanted_status": "closed",
                "actual_status": "assigned",
            }
        ]
