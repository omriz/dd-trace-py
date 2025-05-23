from enum import Enum

from ddtrace.internal.logger import get_logger
from ddtrace.internal.telemetry import telemetry_writer
from ddtrace.internal.telemetry.constants import TELEMETRY_NAMESPACE


log = get_logger(__name__)


class ENDPOINT(str, Enum):
    TEST_CYCLE = "test_cycle"
    CODE_COVERAGE = "code_coverage"


class ENDPOINT_PAYLOAD_TELEMETRY(str, Enum):
    BYTES = "endpoint_payload.bytes"
    REQUESTS_COUNT = "endpoint_payload.requests"
    REQUESTS_MS = "endpoint_payload.requests_ms"
    REQUESTS_ERRORS = "endpoint_payload.requests_errors"
    EVENTS_COUNT = "endpoint_payload.events_count"
    EVENTS_SERIALIZATION_MS = "endpoint_payload.events_serialization_ms"


class REQUEST_ERROR_TYPE(str, Enum):
    TIMEOUT = "timeout"
    NETWORK = "network"
    STATUS_CODE = "status_code"


def record_endpoint_payload_bytes(endpoint: ENDPOINT, nbytes: int) -> None:
    log.debug("Recording endpoint payload bytes: %s, %s", endpoint, nbytes)
    tags = (("endpoint", endpoint.value),)
    telemetry_writer.add_distribution_metric(
        TELEMETRY_NAMESPACE.CIVISIBILITY, ENDPOINT_PAYLOAD_TELEMETRY.BYTES.value, nbytes, tags
    )


def record_endpoint_payload_request(endpoint: ENDPOINT) -> None:
    log.debug("Recording endpoint payload request: %s", endpoint)
    tags = (("endpoint", endpoint.value),)
    telemetry_writer.add_count_metric(
        TELEMETRY_NAMESPACE.CIVISIBILITY, ENDPOINT_PAYLOAD_TELEMETRY.REQUESTS_COUNT.value, 1, tags
    )


def record_endpoint_payload_request_time(endpoint: ENDPOINT, seconds: float) -> None:
    log.debug("Recording endpoint payload request time: %s, %s seconds", endpoint, seconds)
    tags = (("endpoint", endpoint.value),)
    telemetry_writer.add_distribution_metric(
        TELEMETRY_NAMESPACE.CIVISIBILITY, ENDPOINT_PAYLOAD_TELEMETRY.REQUESTS_MS.value, seconds * 1000, tags
    )


def record_endpoint_payload_request_error(endpoint: ENDPOINT, error_type: REQUEST_ERROR_TYPE) -> None:
    log.debug("Recording endpoint payload request error: %s, %s", endpoint, error_type)
    tags = (("endpoint", endpoint.value), ("error_type", error_type))
    telemetry_writer.add_count_metric(
        TELEMETRY_NAMESPACE.CIVISIBILITY, ENDPOINT_PAYLOAD_TELEMETRY.REQUESTS_ERRORS.value, 1, tags
    )


def record_endpoint_payload_events_count(endpoint: ENDPOINT, count: int) -> None:
    log.debug("Recording endpoint payload events count: %s, %s", endpoint, count)
    tags = (("endpoint", endpoint.value),)
    telemetry_writer.add_distribution_metric(
        TELEMETRY_NAMESPACE.CIVISIBILITY, ENDPOINT_PAYLOAD_TELEMETRY.EVENTS_COUNT.value, count, tags
    )


def record_endpoint_payload_events_serialization_time(endpoint: ENDPOINT, seconds: float) -> None:
    log.debug("Recording endpoint payload serialization time: %s, %s seconds", endpoint, seconds)
    tags = (("endpoint", endpoint.value),)
    telemetry_writer.add_distribution_metric(
        TELEMETRY_NAMESPACE.CIVISIBILITY, ENDPOINT_PAYLOAD_TELEMETRY.EVENTS_SERIALIZATION_MS.value, seconds * 1000, tags
    )
