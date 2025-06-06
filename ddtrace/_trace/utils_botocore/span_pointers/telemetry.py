from ddtrace._trace.telemetry import record_span_pointer_calculation as base_record_span_pointer_calculation
from ddtrace._trace.telemetry import record_span_pointer_calculation_issue as base_record_span_pointer_calculation_issue


_CONTEXT = "botocore"


def record_span_pointer_calculation(span_pointer_count: int) -> None:
    base_record_span_pointer_calculation(
        context=_CONTEXT,
        span_pointer_count=span_pointer_count,
    )


def record_span_pointer_calculation_issue(operation: str, issue_tag: str) -> None:
    base_record_span_pointer_calculation_issue(
        context=_CONTEXT, additional_tags=(("operation", operation), ("issue", issue_tag))
    )
