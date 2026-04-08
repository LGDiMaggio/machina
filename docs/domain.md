# Domain Model Reference

Machina's domain model is the backbone of the framework. Every connector
normalizes external data into these entities, and every agent reasons in
these terms. The model is **aligned with ISO 14224** — the international
standard for reliability and maintenance data collection — so teams already
using ISO-aligned CMMS tooling can interoperate without custom field mapping.

The entities are [pydantic v2](https://docs.pydantic.dev/) models with
validators. Fields carrying ISO 14224 codes (`iso_14224_code`,
`equipment_class_code`, `failure_impact`) are **optional** and live alongside
Machina's own catalog identifiers — you can adopt the ISO alignment
incrementally, one field at a time.

## Core Entities

### Asset

::: machina.domain.asset.Asset

::: machina.domain.asset.AssetType

::: machina.domain.asset.Criticality

### WorkOrder

::: machina.domain.work_order.WorkOrder

::: machina.domain.work_order.WorkOrderType

::: machina.domain.work_order.WorkOrderStatus

::: machina.domain.work_order.Priority

::: machina.domain.work_order.FailureImpact

::: machina.domain.work_order.SparePartRequirement

### FailureMode

::: machina.domain.failure_mode.FailureMode

### SparePart

::: machina.domain.spare_part.SparePart

### Alarm

::: machina.domain.alarm.Alarm

::: machina.domain.alarm.Severity

### MaintenancePlan

::: machina.domain.maintenance_plan.MaintenancePlan

::: machina.domain.maintenance_plan.Interval

### Plant

::: machina.domain.plant.Plant

## Domain Services

### FailureAnalyzer

::: machina.domain.services.failure_analyzer.FailureAnalyzer

### WorkOrderFactory

::: machina.domain.services.work_order_factory.WorkOrderFactory

### MaintenanceScheduler

::: machina.domain.services.maintenance_scheduler.MaintenanceScheduler
