import itertools
import re
from typing import Any  # noqa:F401
from typing import Dict  # noqa:F401
from typing import FrozenSet  # noqa:F401
from typing import List  # noqa:F401
from typing import Literal  # noqa:F401
from typing import Optional  # noqa:F401
from typing import Text  # noqa:F401
from typing import Tuple  # noqa:F401
from typing import cast  # noqa:F401
import urllib.parse

from ddtrace._trace._span_link import SpanLink
from ddtrace._trace.context import Context
from ddtrace._trace.span import Span  # noqa:F401
from ddtrace._trace.span import _get_64_highest_order_bits_as_hex
from ddtrace._trace.span import _get_64_lowest_order_bits_as_int
from ddtrace._trace.span import _MetaDictType
from ddtrace.appsec._constants import APPSEC
from ddtrace.internal import core
from ddtrace.settings._config import config
from ddtrace.settings.asm import config as asm_config

from ..constants import AUTO_KEEP
from ..constants import AUTO_REJECT
from ..constants import USER_KEEP
from ..internal._tagset import TagsetDecodeError
from ..internal._tagset import TagsetEncodeError
from ..internal._tagset import TagsetMaxSizeDecodeError
from ..internal._tagset import TagsetMaxSizeEncodeError
from ..internal._tagset import decode_tagset_string
from ..internal._tagset import encode_tagset_values
from ..internal.compat import ensure_text
from ..internal.constants import _PROPAGATION_BEHAVIOR_RESTART
from ..internal.constants import _PROPAGATION_STYLE_BAGGAGE
from ..internal.constants import _PROPAGATION_STYLE_W3C_TRACECONTEXT
from ..internal.constants import BAGGAGE_TAG_PREFIX
from ..internal.constants import DD_TRACE_BAGGAGE_MAX_BYTES
from ..internal.constants import DD_TRACE_BAGGAGE_MAX_ITEMS
from ..internal.constants import HIGHER_ORDER_TRACE_ID_BITS as _HIGHER_ORDER_TRACE_ID_BITS
from ..internal.constants import LAST_DD_PARENT_ID_KEY
from ..internal.constants import MAX_UINT_64BITS as _MAX_UINT_64BITS
from ..internal.constants import PROPAGATION_STYLE_B3_MULTI
from ..internal.constants import PROPAGATION_STYLE_B3_SINGLE
from ..internal.constants import PROPAGATION_STYLE_DATADOG
from ..internal.constants import W3C_TRACEPARENT_KEY
from ..internal.constants import W3C_TRACESTATE_KEY
from ..internal.logger import get_logger
from ..internal.sampling import SAMPLING_DECISION_TRACE_TAG_KEY
from ..internal.sampling import SamplingMechanism
from ..internal.sampling import validate_sampling_decision
from ..internal.utils.http import w3c_tracestate_add_p
from ._utils import get_wsgi_header


log = get_logger(__name__)


# HTTP headers one should set for distributed tracing.
# These are cross-language (eg: Python, Go and other implementations should honor these)
_HTTP_BAGGAGE_PREFIX: Literal["ot-baggage-"] = "ot-baggage-"
HTTP_HEADER_TRACE_ID: Literal["x-datadog-trace-id"] = "x-datadog-trace-id"
HTTP_HEADER_PARENT_ID: Literal["x-datadog-parent-id"] = "x-datadog-parent-id"
HTTP_HEADER_SAMPLING_PRIORITY: Literal["x-datadog-sampling-priority"] = "x-datadog-sampling-priority"
HTTP_HEADER_ORIGIN: Literal["x-datadog-origin"] = "x-datadog-origin"
_HTTP_HEADER_B3_SINGLE: Literal["b3"] = "b3"
_HTTP_HEADER_B3_TRACE_ID: Literal["x-b3-traceid"] = "x-b3-traceid"
_HTTP_HEADER_B3_SPAN_ID: Literal["x-b3-spanid"] = "x-b3-spanid"
_HTTP_HEADER_B3_SAMPLED: Literal["x-b3-sampled"] = "x-b3-sampled"
_HTTP_HEADER_B3_FLAGS: Literal["x-b3-flags"] = "x-b3-flags"
_HTTP_HEADER_TAGS: Literal["x-datadog-tags"] = "x-datadog-tags"
_HTTP_HEADER_TRACEPARENT: Literal["traceparent"] = "traceparent"
_HTTP_HEADER_TRACESTATE: Literal["tracestate"] = "tracestate"
_HTTP_HEADER_BAGGAGE: Literal["baggage"] = "baggage"


def _possible_header(header):
    # type: (str) -> FrozenSet[str]
    return frozenset([header, get_wsgi_header(header).lower()])


# Note that due to WSGI spec we have to also check for uppercased and prefixed
# versions of these headers
POSSIBLE_HTTP_HEADER_TRACE_IDS = _possible_header(HTTP_HEADER_TRACE_ID)
POSSIBLE_HTTP_HEADER_PARENT_IDS = _possible_header(HTTP_HEADER_PARENT_ID)
POSSIBLE_HTTP_HEADER_SAMPLING_PRIORITIES = _possible_header(HTTP_HEADER_SAMPLING_PRIORITY)
POSSIBLE_HTTP_HEADER_ORIGIN = _possible_header(HTTP_HEADER_ORIGIN)
_POSSIBLE_HTTP_HEADER_TAGS = frozenset([_HTTP_HEADER_TAGS, get_wsgi_header(_HTTP_HEADER_TAGS).lower()])
_POSSIBLE_HTTP_HEADER_B3_SINGLE_HEADER = _possible_header(_HTTP_HEADER_B3_SINGLE)
_POSSIBLE_HTTP_HEADER_B3_TRACE_IDS = _possible_header(_HTTP_HEADER_B3_TRACE_ID)
_POSSIBLE_HTTP_HEADER_B3_SPAN_IDS = _possible_header(_HTTP_HEADER_B3_SPAN_ID)
_POSSIBLE_HTTP_HEADER_B3_SAMPLEDS = _possible_header(_HTTP_HEADER_B3_SAMPLED)
_POSSIBLE_HTTP_HEADER_B3_FLAGS = _possible_header(_HTTP_HEADER_B3_FLAGS)
_POSSIBLE_HTTP_HEADER_TRACEPARENT = _possible_header(_HTTP_HEADER_TRACEPARENT)
_POSSIBLE_HTTP_HEADER_TRACESTATE = _possible_header(_HTTP_HEADER_TRACESTATE)
_POSSIBLE_HTTP_BAGGAGE_PREFIX = _possible_header(_HTTP_BAGGAGE_PREFIX)
_POSSIBLE_HTTP_BAGGAGE_HEADER = _possible_header(_HTTP_HEADER_BAGGAGE)


# https://www.w3.org/TR/trace-context/#traceparent-header-field-values
# Future proofing: The traceparent spec is additive, future traceparent versions may contain more than 4 values
# The regex below matches the version, trace id, span id, sample flag, and end-string/future values (if version>00)
_TRACEPARENT_HEX_REGEX = re.compile(
    r"""
     ^                  # Start of string
     ([a-f0-9]{2})-     # 2 character hex version
     ([a-f0-9]{32})-    # 32 character hex trace id
     ([a-f0-9]{16})-    # 16 character hex span id
     ([a-f0-9]{2})      # 2 character hex sample flag
     (-.+)?             # optional, start of any additional values
     $                  # end of string
     """,
    re.VERBOSE,
)


def _extract_header_value(possible_header_names, headers, default=None):
    # type: (FrozenSet[str], Dict[str, str], Optional[str]) -> Optional[str]
    for header in possible_header_names:
        if header in headers:
            return ensure_text(headers[header], errors="backslashreplace")

    return default


def _attach_baggage_to_context(headers: Dict[str, str], context: Context):
    if context is not None:
        for key, value in headers.items():
            for possible_prefix in _POSSIBLE_HTTP_BAGGAGE_PREFIX:
                if key.startswith(possible_prefix):
                    context.set_baggage_item(key[len(possible_prefix) :], value)


def _hex_id_to_dd_id(hex_id):
    # type: (str) -> int
    """Helper to convert hex ids into Datadog compatible ints."""
    return int(hex_id, 16)


_b3_id_to_dd_id = _hex_id_to_dd_id


def _dd_id_to_b3_id(dd_id):
    # type: (int) -> str
    """Helper to convert Datadog trace/span int ids into lower case hex values"""
    if dd_id > _MAX_UINT_64BITS:
        # b3 trace ids can have the length of 16 or 32 characters:
        # https://github.com/openzipkin/b3-propagation#traceid
        return "{:032x}".format(dd_id)
    return "{:016x}".format(dd_id)


class _DatadogMultiHeader:
    """Helper class for injecting/extract Datadog multi header format

    Headers:

      - ``x-datadog-trace-id`` the context trace id as a uint64 integer
      - ``x-datadog-parent-id`` the context current span id as a uint64 integer
      - ``x-datadog-sampling-priority`` integer representing the sampling decision.
        ``<= 0`` (Reject) or ``> 1`` (Keep)
      - ``x-datadog-origin`` optional name of origin Datadog product which initiated the request
      - ``x-datadog-tags`` optional tracer tags

    Restrictions:

      - Trace tag key-value pairs in ``x-datadog-tags`` are extracted from incoming requests.
      - Only trace tags with keys prefixed with ``_dd.p.`` are propagated.
      - The trace tag keys must be printable ASCII characters excluding space, comma, and equals.
      - The trace tag values must be printable ASCII characters excluding comma. Leading and
        trailing spaces are trimmed.
    """

    _X_DATADOG_TAGS_EXTRACT_REJECT = frozenset(["_dd.p.upstream_services"])

    @staticmethod
    def _is_valid_datadog_trace_tag_key(key):
        return key.startswith("_dd.p.")

    @staticmethod
    def _get_tags_value(headers):
        # type: (Dict[str, str]) -> Optional[str]
        return _extract_header_value(
            _POSSIBLE_HTTP_HEADER_TAGS,
            headers,
            default="",
        )

    @staticmethod
    def _extract_meta(tags_value):
        # Do not fail if the tags are malformed
        try:
            meta = {
                k: v
                for (k, v) in decode_tagset_string(tags_value).items()
                if (
                    k not in _DatadogMultiHeader._X_DATADOG_TAGS_EXTRACT_REJECT
                    and _DatadogMultiHeader._is_valid_datadog_trace_tag_key(k)
                )
            }
        except TagsetMaxSizeDecodeError:
            meta = {
                "_dd.propagation_error": "extract_max_size",
            }
            log.warning("failed to decode x-datadog-tags", exc_info=True)
        except TagsetDecodeError:
            meta = {
                "_dd.propagation_error": "decoding_error",
            }
            log.debug("failed to decode x-datadog-tags: %r", tags_value, exc_info=True)
        return meta

    @staticmethod
    def _put_together_trace_id(trace_id_hob_hex: str, low_64_bits: int) -> int:
        # combine highest and lowest order hex values to create a 128 bit trace_id
        return int(trace_id_hob_hex + "{:016x}".format(low_64_bits), 16)

    @staticmethod
    def _higher_order_is_valid(upper_64_bits: str) -> bool:
        try:
            if len(upper_64_bits) != 16 or not (int(upper_64_bits, 16) or (upper_64_bits.islower())):
                raise ValueError
        except ValueError:
            return False

        return True

    @staticmethod
    def _inject(span_context, headers):
        # type: (Context, Dict[str, str]) -> None
        if span_context.trace_id is None or span_context.span_id is None:
            log.debug("tried to inject invalid context %r", span_context)
            return

        # When apm tracing is not enabled, only distributed traces with the `_dd.p.ts` tag
        # are propagated. If the tag is not present, we should not propagate downstream.
        if not asm_config._apm_tracing_enabled and (APPSEC.PROPAGATION_HEADER not in span_context._meta):
            return

        if span_context.trace_id > _MAX_UINT_64BITS:
            # set lower order 64 bits in `x-datadog-trace-id` header. For backwards compatibility these
            # bits should be converted to a base 10 integer.
            headers[HTTP_HEADER_TRACE_ID] = str(_get_64_lowest_order_bits_as_int(span_context.trace_id))
            # set higher order 64 bits in `_dd.p.tid` to propagate the full 128 bit trace id.
            # Note - The higher order bits must be encoded in hex
            span_context._meta[_HIGHER_ORDER_TRACE_ID_BITS] = _get_64_highest_order_bits_as_hex(span_context.trace_id)
        else:
            headers[HTTP_HEADER_TRACE_ID] = str(span_context.trace_id)

        headers[HTTP_HEADER_PARENT_ID] = str(span_context.span_id)
        sampling_priority = span_context.sampling_priority
        # Propagate priority only if defined
        if sampling_priority is not None:
            headers[HTTP_HEADER_SAMPLING_PRIORITY] = str(span_context.sampling_priority)
        # Propagate origin only if defined
        if span_context.dd_origin is not None:
            headers[HTTP_HEADER_ORIGIN] = ensure_text(span_context.dd_origin)

        if not config._x_datadog_tags_enabled:
            span_context._meta["_dd.propagation_error"] = "disabled"
            return

        # Do not try to encode tags if we have already tried and received an error
        if "_dd.propagation_error" in span_context._meta:
            return

        # Only propagate trace tags which means ignoring the _dd.origin
        tags_to_encode = {
            # DEV: Context._meta is a _MetaDictType but we need Dict[str, str]
            ensure_text(k): ensure_text(v)
            for k, v in span_context._meta.items()
            if _DatadogMultiHeader._is_valid_datadog_trace_tag_key(k)
        }  # type: Dict[Text, Text]

        if tags_to_encode:
            try:
                headers[_HTTP_HEADER_TAGS] = encode_tagset_values(
                    tags_to_encode, max_size=config._x_datadog_tags_max_length
                )

            except TagsetMaxSizeEncodeError:
                # We hit the max size allowed, add a tag to the context to indicate this happened
                span_context._meta["_dd.propagation_error"] = "inject_max_size"
                log.warning("failed to encode x-datadog-tags", exc_info=True)
            except TagsetEncodeError:
                # We hit an encoding error, add a tag to the context to indicate this happened
                span_context._meta["_dd.propagation_error"] = "encoding_error"
                log.warning("failed to encode x-datadog-tags", exc_info=True)

    @staticmethod
    def _extract(headers):
        # type: (Dict[str, str]) -> Optional[Context]
        trace_id_str = _extract_header_value(POSSIBLE_HTTP_HEADER_TRACE_IDS, headers)
        if trace_id_str is None:
            return None
        try:
            trace_id = int(trace_id_str)
        except ValueError:
            trace_id = 0

        if trace_id <= 0 or trace_id > _MAX_UINT_64BITS:
            log.warning(
                "Invalid trace id: %r. `x-datadog-trace-id` must be greater than zero and less than 2**64", trace_id_str
            )
            return None

        parent_span_id = _extract_header_value(
            POSSIBLE_HTTP_HEADER_PARENT_IDS,
            headers,
            default="0",
        )
        sampling_priority = _extract_header_value(
            POSSIBLE_HTTP_HEADER_SAMPLING_PRIORITIES,
            headers,
        )
        origin = _extract_header_value(
            POSSIBLE_HTTP_HEADER_ORIGIN,
            headers,
        )

        meta = None

        tags_value = _DatadogMultiHeader._get_tags_value(headers)
        if tags_value:
            meta = _DatadogMultiHeader._extract_meta(tags_value)

        # When 128 bit trace ids are propagated the 64 lowest order bits are set in the `x-datadog-trace-id`
        # header. The 64 highest order bits are encoded in base 16 and store in the `_dd.p.tid` tag.
        # Here we reconstruct the full 128 bit trace_id if 128-bit trace id generation is enabled.
        if meta and _HIGHER_ORDER_TRACE_ID_BITS in meta:
            trace_id_hob_hex = meta[_HIGHER_ORDER_TRACE_ID_BITS]
            if _DatadogMultiHeader._higher_order_is_valid(trace_id_hob_hex):
                if config._128_bit_trace_id_enabled:
                    trace_id = _DatadogMultiHeader._put_together_trace_id(trace_id_hob_hex, trace_id)
            else:
                meta["_dd.propagation_error"] = "malformed_tid {}".format(trace_id_hob_hex)
                del meta[_HIGHER_ORDER_TRACE_ID_BITS]
                log.warning("malformed_tid: %s. Failed to decode trace id from http headers", trace_id_hob_hex)

        if not meta:
            meta = {}

        if not meta.get(SAMPLING_DECISION_TRACE_TAG_KEY):
            meta[SAMPLING_DECISION_TRACE_TAG_KEY] = f"-{SamplingMechanism.LOCAL_USER_TRACE_SAMPLING_RULE}"

        # Try to parse values into their expected types
        try:
            if sampling_priority is not None:
                sampling_priority = int(sampling_priority)  # type: ignore[assignment]

            if meta:
                meta = validate_sampling_decision(meta)

            if not asm_config._apm_tracing_enabled:
                # When apm tracing is not enabled, only distributed traces with the `_dd.p.ts` tag
                # are propagated downstream, however we need 1 trace per minute sent to the backend, so
                # we unset sampling priority so the rate limiter decides.
                if not meta or APPSEC.PROPAGATION_HEADER not in meta:
                    sampling_priority = None
                # If the trace has appsec propagation tag, the default priority is user keep
                elif meta and APPSEC.PROPAGATION_HEADER in meta:
                    sampling_priority = 2  # type: ignore[assignment]

            return Context(
                # DEV: Do not allow `0` for trace id or span id, use None instead
                trace_id=trace_id or None,
                span_id=int(parent_span_id) or None,  # type: ignore[arg-type]
                sampling_priority=sampling_priority,  # type: ignore[arg-type]
                dd_origin=origin,
                # DEV: This cast is needed because of the type requirements of
                # span tags and trace tags which are currently implemented using
                # the same type internally (_MetaDictType).
                meta=cast(_MetaDictType, meta),
            )
        except (TypeError, ValueError):
            log.debug(
                (
                    "received invalid x-datadog-* headers, "
                    "trace-id: %r, parent-id: %r, priority: %r, origin: %r, tags:%r"
                ),
                trace_id,
                parent_span_id,
                sampling_priority,
                origin,
                tags_value,
            )
        return None


class _B3MultiHeader:
    """Helper class to inject/extract B3 Multi-Headers

    https://github.com/openzipkin/b3-propagation/blob/3e54cda11620a773d53c7f64d2ebb10d3a01794c/README.md#multiple-headers

    Example::

        X-B3-TraceId: 80f198ee56343ba864fe8b2a57d3eff7
        X-B3-ParentSpanId: 05e3ac9a4f6e3b90
        X-B3-SpanId: e457b5a2e4d86bd1
        X-B3-Sampled: 1


    Headers:

      - ``X-B3-TraceId`` header is encoded as 32 or 16 lower-hex characters.
      - ``X-B3-SpanId`` header is encoded as 16 lower-hex characters.
      - ``X-B3-Sampled`` header value of ``0`` means Deny, ``1`` means Accept, and absent means to defer.
      - ``X-B3-Flags`` header is used to set ``1`` meaning Debug or an Accept.

    Restrictions:

      - ``X-B3-Sampled`` and ``X-B3-Flags`` should never both be set

    Implementation details:

      - Sampling priority gets encoded as:
        - ``sampling_priority <= 0`` -> ``X-B3-Sampled: 0``
        - ``sampling_priority == 1`` -> ``X-B3-Sampled: 1``
        - ``sampling_priority > 1`` -> ``X-B3-Flags: 1``
      - Sampling priority gets decoded as:
        - ``X-B3-Sampled: 0`` -> ``sampling_priority = 0``
        - ``X-B3-Sampled: 1`` -> ``sampling_priority = 1``
        - ``X-B3-Flags: 1`` -> ``sampling_priority = 2``
      - ``X-B3-TraceId`` is not required, will use ``None`` when not present
      - ``X-B3-SpanId`` is not required, will use ``None`` when not present
    """

    @staticmethod
    def _inject(span_context, headers):
        # type: (Context, Dict[str, str]) -> None
        if span_context.trace_id is None or span_context.span_id is None:
            log.debug("tried to inject invalid context %r", span_context)
            return

        headers[_HTTP_HEADER_B3_TRACE_ID] = _dd_id_to_b3_id(span_context.trace_id)
        headers[_HTTP_HEADER_B3_SPAN_ID] = _dd_id_to_b3_id(span_context.span_id)
        sampling_priority = span_context.sampling_priority
        # Propagate priority only if defined
        if sampling_priority is not None:
            if sampling_priority <= 0:
                headers[_HTTP_HEADER_B3_SAMPLED] = "0"
            elif sampling_priority == 1:
                headers[_HTTP_HEADER_B3_SAMPLED] = "1"
            elif sampling_priority > 1:
                headers[_HTTP_HEADER_B3_FLAGS] = "1"

    @staticmethod
    def _extract(headers):
        # type: (Dict[str, str]) -> Optional[Context]
        trace_id_val = _extract_header_value(
            _POSSIBLE_HTTP_HEADER_B3_TRACE_IDS,
            headers,
        )
        if trace_id_val is None:
            return None

        span_id_val = _extract_header_value(
            _POSSIBLE_HTTP_HEADER_B3_SPAN_IDS,
            headers,
        )
        sampled = _extract_header_value(
            _POSSIBLE_HTTP_HEADER_B3_SAMPLEDS,
            headers,
        )
        flags = _extract_header_value(
            _POSSIBLE_HTTP_HEADER_B3_FLAGS,
            headers,
        )

        # Try to parse values into their expected types
        try:
            # DEV: We are allowed to have only x-b3-sampled/flags
            # DEV: Do not allow `0` for trace id or span id, use None instead
            trace_id = None
            span_id = None
            if trace_id_val is not None:
                trace_id = _b3_id_to_dd_id(trace_id_val) or None
            if span_id_val is not None:
                span_id = _b3_id_to_dd_id(span_id_val) or None

            sampling_priority = None
            if sampled is not None:
                if sampled == "0":
                    sampling_priority = AUTO_REJECT
                elif sampled == "1":
                    sampling_priority = AUTO_KEEP
            if flags == "1":
                sampling_priority = USER_KEEP

            return Context(
                trace_id=trace_id,
                span_id=span_id,
                sampling_priority=sampling_priority,
            )
        except (TypeError, ValueError):
            log.debug(
                "received invalid x-b3-* headers, trace-id: %r, span-id: %r, sampled: %r, flags: %r",
                trace_id_val,
                span_id_val,
                sampled,
                flags,
            )
        return None


class _B3SingleHeader:
    """Helper class to inject/extract B3

    https://github.com/openzipkin/b3-propagation/blob/3e54cda11620a773d53c7f64d2ebb10d3a01794c/README.md#single-header

    Format::

        b3={TraceId}-{SpanId}-{SamplingState}-{ParentSpanId}

    Example::

        b3: 80f198ee56343ba864fe8b2a57d3eff7-e457b5a2e4d86bd1-1-05e3ac9a4f6e3b90


    Values:

      - ``TraceId`` header is encoded as 32 or 16 lower-hex characters.
      - ``SpanId`` header is encoded as 16 lower-hex characters.
      - ``SamplingState`` header value of ``0`` means Deny, ``1`` means Accept, and ``d`` means Debug
      - ``ParentSpanId`` header is not used/ignored if sent

    Restrictions:

      - ``ParentSpanId`` value is ignored/not used

    Implementation details:

      - Sampling priority gets encoded as:
        - ``sampling_priority <= 0`` -> ``SamplingState: 0``
        - ``sampling_priority == 1`` -> ``SamplingState: 1``
        - ``sampling_priority > 1`` -> ``SamplingState: d``
      - Sampling priority gets decoded as:
        - ``SamplingState: 0`` -> ``sampling_priority = 0``
        - ``SamplingState: 1`` -> ``sampling_priority = 1``
        - ``SamplingState: d`` -> ``sampling_priority = 2``
      - ``TraceId`` is not required, will use ``None`` when not present
      - ``SpanId`` is not required, will use ``None`` when not present
    """

    @staticmethod
    def _inject(span_context, headers):
        # type: (Context, Dict[str, str]) -> None
        if span_context.trace_id is None or span_context.span_id is None:
            log.debug("tried to inject invalid context %r", span_context)
            return

        single_header = "{}-{}".format(_dd_id_to_b3_id(span_context.trace_id), _dd_id_to_b3_id(span_context.span_id))
        sampling_priority = span_context.sampling_priority
        if sampling_priority is not None:
            if sampling_priority <= 0:
                single_header += "-0"
            elif sampling_priority == 1:
                single_header += "-1"
            elif sampling_priority > 1:
                single_header += "-d"
        headers[_HTTP_HEADER_B3_SINGLE] = single_header

    @staticmethod
    def _extract(headers):
        # type: (Dict[str, str]) -> Optional[Context]
        single_header = _extract_header_value(_POSSIBLE_HTTP_HEADER_B3_SINGLE_HEADER, headers)
        if not single_header:
            return None

        trace_id = None
        span_id = None
        sampled = None

        parts = single_header.split("-")
        trace_id_val = None
        span_id_val = None

        # Only SamplingState is provided
        if len(parts) == 1:
            (sampled,) = parts

        # Only TraceId and SpanId are provided
        elif len(parts) == 2:
            trace_id_val, span_id_val = parts

        # Full header, ignore any ParentSpanId present
        elif len(parts) >= 3:
            trace_id_val, span_id_val, sampled = parts[:3]

        # Try to parse values into their expected types
        try:
            # DEV: We are allowed to have only x-b3-sampled/flags
            # DEV: Do not allow `0` for trace id or span id, use None instead
            if trace_id_val is not None:
                trace_id = _b3_id_to_dd_id(trace_id_val) or None
            if span_id_val is not None:
                span_id = _b3_id_to_dd_id(span_id_val) or None

            sampling_priority = None
            if sampled is not None:
                if sampled == "0":
                    sampling_priority = AUTO_REJECT
                elif sampled == "1":
                    sampling_priority = AUTO_KEEP
                elif sampled == "d":
                    sampling_priority = USER_KEEP

            return Context(
                trace_id=trace_id,
                span_id=span_id,
                sampling_priority=sampling_priority,
            )
        except (TypeError, ValueError):
            log.debug(
                "received invalid b3 header, b3: %r",
                single_header,
            )
        return None


class _TraceContext:
    """Helper class to inject/extract W3C Trace Context
    https://www.w3.org/TR/trace-context/
    Overview:
      - ``traceparent`` header describes the position of the incoming request in its
        trace graph in a portable, fixed-length format. Its design focuses on
        fast parsing. Every tracing tool MUST properly set traceparent even when
        it only relies on vendor-specific information in tracestate
      - ``tracestate`` header extends traceparent with vendor-specific data represented
        by a set of name/value pairs. Storing information in tracestate is
        optional.

    The format for ``traceparent`` is::
      HEXDIGLC        = DIGIT / "a" / "b" / "c" / "d" / "e" / "f"
      value           = version "-" version-format
      version         = 2HEXDIGLC
      version-format  = trace-id "-" parent-id "-" trace-flags
      trace-id        = 32HEXDIGLC
      parent-id       = 16HEXDIGLC
      trace-flags     = 2HEXDIGLC

    Example value of HTTP ``traceparent`` header::
        value = 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
        base16(version) = 00
        base16(trace-id) = 4bf92f3577b34da6a3ce929d0e0e4736
        base16(parent-id) = 00f067aa0ba902b7
        base16(trace-flags) = 01  // sampled

    The format for ``tracestate`` is key value pairs with each entry limited to 256 characters.
    An example of the ``dd`` list member we would add is::
    "dd=s:2;o:rum;t.dm:-4;t.usr.id:baz64"

    Implementation details:
      - Datadog Trace and Span IDs are 64-bit unsigned integers.
      - The W3C Trace Context Trace ID is a 16-byte hexadecimal string.
      - If the incoming traceparent is invalid we DO NOT use the tracecontext headers.
        Otherwise, the trace-id value is set to the hex-encoded value of the trace-id.
        If the trace-id is a 64-bit value (i.e. a Datadog trace-id),
        then the upper half of the hex-encoded value will be all zeroes.

      - The tracestate header will have one list member added to it, ``dd``, which contains
        values that would be in x-datadog-tags as well as those needed for propagation information.
        The keys to the ``dd`` values have been shortened as follows to save space:
        ``sampling_priority`` = ``s``
        ``origin`` = ``o``
        ``_dd.p.`` prefix = ``t.``
    """

    @staticmethod
    def decode_tag_val(tag_val):
        # type str -> str
        return tag_val.replace("~", "=")

    @staticmethod
    def _get_traceparent_values(tp):
        # type: (str) -> Tuple[int, int, Literal[0,1]]
        """If there is no traceparent, or if the traceparent value is invalid raise a ValueError.
        Otherwise we extract the trace-id, span-id, and sampling priority from the
        traceparent header.
        """
        valid_tp_values = _TRACEPARENT_HEX_REGEX.match(tp.strip())
        if valid_tp_values is None:
            raise ValueError("Invalid traceparent version: %s" % tp)

        (
            version,
            trace_id_hex,
            span_id_hex,
            trace_flags_hex,
            future_vals,
        ) = valid_tp_values.groups()  # type: Tuple[str, str, str, str, Optional[str]]

        if version == "ff":
            # https://www.w3.org/TR/trace-context/#version
            raise ValueError("ff is an invalid traceparent version: %s" % tp)
        elif version != "00":
            # currently 00 is the only version format, but if future versions come up we may need to add changes
            log.warning("unsupported traceparent version:%r, still attempting to parse", version)
        elif version == "00" and future_vals is not None:
            raise ValueError("Traceparents with the version `00` should contain 4 values delimited by a dash: %s" % tp)

        trace_id = _hex_id_to_dd_id(trace_id_hex)
        span_id = _hex_id_to_dd_id(span_id_hex)

        # All 0s are invalid values
        if trace_id == 0:
            raise ValueError("0 value for trace_id is invalid")
        if span_id == 0:
            raise ValueError("0 value for span_id is invalid")

        trace_flags = _hex_id_to_dd_id(trace_flags_hex)
        # there's currently only one trace flag, which denotes sampling priority
        # was set to keep "01" or drop "00"
        # trace flags is a bit field: https://www.w3.org/TR/trace-context/#trace-flags
        # if statement is required to cast traceflags to a Literal
        sampling_priority = 1 if trace_flags & 0x1 else 0  # type: Literal[0, 1]

        return trace_id, span_id, sampling_priority

    @staticmethod
    def _get_tracestate_values(ts_l):
        # type: (List[str]) -> Tuple[Optional[int], Dict[str, str], Optional[str], Optional[str]]

        # tracestate list parsing example: ["dd=s:2;o:rum;t.dm:-4;t.usr.id:baz64","congo=t61rcWkgMzE"]
        # -> 2, {"_dd.p.dm":"-4","_dd.p.usr.id":"baz64"}, "rum"

        dd = None
        for list_mem in ts_l:
            if list_mem.startswith("dd="):
                # cut out dd= before turning into dict
                list_mem = list_mem[3:]
                # since tags can have a value with a :, we need to only split on the first instance of :
                dd = dict(item.split(":", 1) for item in list_mem.split(";"))

        # parse out values
        if dd:
            sampling_priority_ts = dd.get("s")
            if sampling_priority_ts is not None:
                sampling_priority_ts_int = int(sampling_priority_ts)
            else:
                sampling_priority_ts_int = None

            origin = dd.get("o")
            if origin:
                # we encode "=" as "~" in tracestate so need to decode here
                origin = _TraceContext.decode_tag_val(origin)

            # Get last datadog parent id, this field is used to reconnect traces with missing spans
            lpid = dd.get("p")

            # need to convert from t. to _dd.p.
            other_propagated_tags = {
                "_dd.p.%s" % k[2:]: _TraceContext.decode_tag_val(v) for (k, v) in dd.items() if k.startswith("t.")
            }

            return sampling_priority_ts_int, other_propagated_tags, origin, lpid
        else:
            return None, {}, None, None

    @staticmethod
    def _get_sampling_priority(
        traceparent_sampled: int, tracestate_sampling_priority: Optional[int], origin: Optional[str] = None
    ):
        """
        When the traceparent sampled flag is set, the Datadog sampling priority is either
        1 or a positive value of sampling priority if propagated in tracestate.

        When the traceparent sampled flag is not set, the Datadog sampling priority is either
        0 or a negative value of sampling priority if propagated in tracestate.

        When origin is "rum" and there is no sampling priority propagated in tracestate, the above rules do not apply.
        """
        from_rum_wo_priority = not tracestate_sampling_priority and origin == "rum"

        if (
            not from_rum_wo_priority
            and traceparent_sampled == 0
            and (not tracestate_sampling_priority or tracestate_sampling_priority >= 0)
        ):
            sampling_priority = 0
        elif (
            not from_rum_wo_priority
            and traceparent_sampled == 1
            and (not tracestate_sampling_priority or tracestate_sampling_priority < 0)
        ):
            sampling_priority = 1
        else:
            # The two other options provided for clarity:
            # elif traceparent_sampled == 1 and tracestate_sampling_priority > 0:
            # elif traceparent_sampled == 0 and tracestate_sampling_priority <= 0:
            sampling_priority = tracestate_sampling_priority  # type: ignore

        return sampling_priority

    @staticmethod
    def _extract(headers):
        # type: (Dict[str, str]) -> Optional[Context]

        try:
            tp = _extract_header_value(_POSSIBLE_HTTP_HEADER_TRACEPARENT, headers)
            if tp is None:
                log.debug("no traceparent header")
                return None
            trace_id, span_id, trace_flag = _TraceContext._get_traceparent_values(tp)
        except (ValueError, AssertionError):
            log.exception("received invalid w3c traceparent: %s ", tp)
            return None

        meta = {W3C_TRACEPARENT_KEY: tp}  # type: _MetaDictType

        ts = _extract_header_value(_POSSIBLE_HTTP_HEADER_TRACESTATE, headers)
        return _TraceContext._get_context(trace_id, span_id, trace_flag, ts, meta)

    @staticmethod
    def _get_context(trace_id, span_id, trace_flag, ts, meta=None):
        # type: (int, int, Literal[0,1], Optional[str], Optional[_MetaDictType]) -> Context
        if meta is None:
            meta = {}
        origin = None
        sampling_priority = trace_flag  # type: int
        if ts:
            # whitespace is allowed, but whitespace to start or end values should be trimmed
            # e.g. "foo=1 \t , \t bar=2, \t baz=3" -> "foo=1,bar=2,baz=3"
            ts_l = [member.strip() for member in ts.split(",")]
            ts = ",".join(ts_l)
            # the value MUST contain only ASCII characters in the
            # range of 0x20 to 0x7E
            if re.search(r"[^\x20-\x7E]+", ts):
                log.debug("received invalid tracestate header: %r", ts)
            else:
                # store tracestate so we keep other vendor data for injection, even if dd ends up being invalid
                meta[W3C_TRACESTATE_KEY] = ts
                try:
                    tracestate_values = _TraceContext._get_tracestate_values(ts_l)
                except (TypeError, ValueError):
                    log.debug("received invalid dd header value in tracestate: %r ", ts)
                    tracestate_values = None

                if tracestate_values:
                    sampling_priority_ts, other_propagated_tags, origin, lpid = tracestate_values
                    meta.update(other_propagated_tags.items())
                    if lpid:
                        meta[LAST_DD_PARENT_ID_KEY] = lpid

                    sampling_priority = _TraceContext._get_sampling_priority(trace_flag, sampling_priority_ts, origin)
                else:
                    log.debug("no dd list member in tracestate from incoming request: %r", ts)

        return Context(
            trace_id=trace_id,
            span_id=span_id,
            sampling_priority=sampling_priority,
            dd_origin=origin,
            meta=meta,
        )

    @staticmethod
    def _inject(span_context, headers):
        # type: (Context, Dict[str, str]) -> None
        tp = span_context._traceparent
        if tp:
            headers[_HTTP_HEADER_TRACEPARENT] = tp
            if span_context._is_remote is False:
                # Datadog Span is active, so the current span_id is the last datadog span_id
                headers[_HTTP_HEADER_TRACESTATE] = w3c_tracestate_add_p(
                    span_context._tracestate, span_context.span_id or 0
                )
            elif LAST_DD_PARENT_ID_KEY in span_context._meta:
                # Datadog Span is not active, propagate the last datadog span_id
                span_id = int(span_context._meta[LAST_DD_PARENT_ID_KEY], 16)
                headers[_HTTP_HEADER_TRACESTATE] = w3c_tracestate_add_p(span_context._tracestate, span_id)
            else:
                headers[_HTTP_HEADER_TRACESTATE] = span_context._tracestate


class _BaggageHeader:
    """Helper class to inject/extract Baggage Headers"""

    SAFE_CHARACTERS_KEY = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!#$%&'*+-.^_`|~"
    SAFE_CHARACTERS_VALUE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!#$%&'()*+-./:<>?@[]^_`{|}~"

    @staticmethod
    def _encode_key(key: str) -> str:
        return urllib.parse.quote(str(key).strip(), safe=_BaggageHeader.SAFE_CHARACTERS_KEY)

    @staticmethod
    def _encode_value(value: str) -> str:
        return urllib.parse.quote(str(value).strip(), safe=_BaggageHeader.SAFE_CHARACTERS_VALUE)

    @staticmethod
    def _inject(span_context: Context, headers: Dict[str, str]) -> None:
        baggage_items = span_context._baggage.items()
        if not baggage_items:
            return

        try:
            if len(baggage_items) > DD_TRACE_BAGGAGE_MAX_ITEMS:
                log.warning("Baggage item limit exceeded, dropping excess items")
                baggage_items = itertools.islice(baggage_items, DD_TRACE_BAGGAGE_MAX_ITEMS)  # type: ignore

            encoded_items: List[str] = []
            total_size = 0
            for key, value in baggage_items:
                item = f"{_BaggageHeader._encode_key(key)}={_BaggageHeader._encode_value(value)}"
                item_size = len(item.encode("utf-8")) + (1 if encoded_items else 0)  # +1 for comma if not first item
                if total_size + item_size > DD_TRACE_BAGGAGE_MAX_BYTES:
                    log.warning("Baggage header size exceeded, dropping excess items")
                    break  # stop adding items when size limit is reached
                encoded_items.append(item)
                total_size += item_size

            header_value = ",".join(encoded_items)
            headers[_HTTP_HEADER_BAGGAGE] = header_value

        except Exception:
            log.warning("Failed to encode and inject baggage header")

    @staticmethod
    def _extract(headers: Dict[str, str]) -> Context:
        header_value = _extract_header_value(_POSSIBLE_HTTP_BAGGAGE_HEADER, headers)

        if not header_value:
            return Context(baggage={})

        baggage = {}
        baggages = header_value.split(",")
        for key_value in baggages:
            if "=" not in key_value:
                return Context(baggage={})
            key, value = key_value.split("=", 1)
            key = urllib.parse.unquote(key.strip())
            value = urllib.parse.unquote(value.strip())
            if not key or not value:
                return Context(baggage={})
            baggage[key] = value

        return Context(baggage=baggage)


_PROP_STYLES = {
    PROPAGATION_STYLE_DATADOG: _DatadogMultiHeader,
    PROPAGATION_STYLE_B3_MULTI: _B3MultiHeader,
    PROPAGATION_STYLE_B3_SINGLE: _B3SingleHeader,
    _PROPAGATION_STYLE_W3C_TRACECONTEXT: _TraceContext,
    _PROPAGATION_STYLE_BAGGAGE: _BaggageHeader,
}


class HTTPPropagator(object):
    """A HTTP Propagator using HTTP headers as carrier. Injects and Extracts headers
    according to the propagation style set by ddtrace configurations.
    """

    @staticmethod
    def _extract_configured_contexts_avail(normalized_headers: Dict[str, str]) -> Tuple[List[Context], List[str]]:
        contexts = []
        styles_w_ctx = []
        if config._propagation_style_extract is not None:
            for prop_style in config._propagation_style_extract:
                # baggage is handled separately
                if prop_style == _PROPAGATION_STYLE_BAGGAGE:
                    continue
                propagator = _PROP_STYLES[prop_style]
                context = propagator._extract(normalized_headers)  # type: ignore
                if context:
                    contexts.append(context)
                    styles_w_ctx.append(prop_style)
        return contexts, styles_w_ctx

    @staticmethod
    def _context_to_span_link(context: Context, style: str, reason: str) -> Optional[SpanLink]:
        # encoding expects at least trace_id and span_id
        if context.span_id and context.trace_id:
            return SpanLink(
                context.trace_id,
                context.span_id,
                flags=1 if context.sampling_priority and context.sampling_priority > 0 else 0,
                tracestate=(
                    context._meta.get(W3C_TRACESTATE_KEY, "") if style == _PROPAGATION_STYLE_W3C_TRACECONTEXT else None
                ),
                attributes={
                    "reason": reason,
                    "context_headers": style,
                },
            )
        return None

    @staticmethod
    def _resolve_contexts(contexts, styles_w_ctx, normalized_headers):
        primary_context = contexts[0]
        links = []

        for i, context in enumerate(contexts[1:], 1):
            style_w_ctx = styles_w_ctx[i]
            # encoding expects at least trace_id and span_id
            if context.trace_id and context.trace_id != primary_context.trace_id:
                link = HTTPPropagator._context_to_span_link(
                    context,
                    style_w_ctx,
                    "terminated_context",
                )
                if link:
                    links.append(link)
            # if trace_id matches and the propagation style is tracecontext
            # add the tracestate to the primary context
            elif style_w_ctx == _PROPAGATION_STYLE_W3C_TRACECONTEXT:
                # extract and add the raw ts value to the primary_context
                ts = _extract_header_value(_POSSIBLE_HTTP_HEADER_TRACESTATE, normalized_headers)
                if ts:
                    primary_context._meta[W3C_TRACESTATE_KEY] = ts
                if primary_context.trace_id == context.trace_id and primary_context.span_id != context.span_id:
                    dd_context = None
                    if PROPAGATION_STYLE_DATADOG in styles_w_ctx:
                        dd_context = contexts[styles_w_ctx.index(PROPAGATION_STYLE_DATADOG)]
                    if LAST_DD_PARENT_ID_KEY in context._meta:
                        # tracecontext headers contain a p value, ensure this value is sent to backend
                        primary_context._meta[LAST_DD_PARENT_ID_KEY] = context._meta[LAST_DD_PARENT_ID_KEY]
                    elif dd_context:
                        # if p value is not present in tracestate, use the parent id from the datadog headers
                        primary_context._meta[LAST_DD_PARENT_ID_KEY] = "{:016x}".format(dd_context.span_id)
                    # the span_id in tracecontext takes precedence over the first extracted propagation style
                    primary_context.span_id = context.span_id

        primary_context._span_links = links
        return primary_context

    @staticmethod
    def inject(span_context, headers, non_active_span=None):
        # type: (Context, Dict[str, str], Optional[Span]) -> None
        """Inject Context attributes that have to be propagated as HTTP headers.

        Here is an example using `requests`::

            import requests

            from ddtrace.propagation.http import HTTPPropagator

            def parent_call():
                with tracer.trace('parent_span') as span:
                    headers = {}
                    HTTPPropagator.inject(span.context, headers)
                    url = '<some RPC endpoint>'
                    r = requests.get(url, headers=headers)

        :param Context span_context: Span context to propagate.
        :param dict headers: HTTP headers to extend with tracing attributes.
        :param Span non_active_span: Only to be used if injecting a non-active span.
        """
        core.dispatch("http.span_inject", (span_context, headers))
        if not config._propagation_style_inject:
            return
        if non_active_span is not None and non_active_span.context is not span_context:
            log.error(
                "span_context and non_active_span.context are not the same, but should be. non_active_span.context "
                "will be used to generate distributed tracing headers. span_context: {}, non_active_span.context: {}",
                span_context,
                non_active_span.context,
            )

            span_context = non_active_span.context

        if core.tracer and hasattr(core.tracer, "sample"):
            root_span: Optional[Span] = None
            if non_active_span is not None:
                root_span = non_active_span._local_root
            else:
                root_span = core.tracer.current_root_span()

            if root_span is not None and root_span.context.sampling_priority is None:
                core.tracer.sample(root_span)
        else:
            log.error("ddtrace.tracer.sample is not available, unable to sample span.")

        # baggage should be injected regardless of existing span or trace id
        if _PROPAGATION_STYLE_BAGGAGE in config._propagation_style_inject:
            _BaggageHeader._inject(span_context, headers)

        # Not a valid context to propagate
        if span_context.trace_id is None or span_context.span_id is None:
            log.debug("tried to inject invalid context %r", span_context)
            return

        if config._propagation_http_baggage_enabled is True and span_context._baggage is not None:
            for key in span_context._baggage:
                headers[_HTTP_BAGGAGE_PREFIX + key] = span_context._baggage[key]

        if PROPAGATION_STYLE_DATADOG in config._propagation_style_inject:
            _DatadogMultiHeader._inject(span_context, headers)
        if PROPAGATION_STYLE_B3_MULTI in config._propagation_style_inject:
            _B3MultiHeader._inject(span_context, headers)
        if PROPAGATION_STYLE_B3_SINGLE in config._propagation_style_inject:
            _B3SingleHeader._inject(span_context, headers)
        if _PROPAGATION_STYLE_W3C_TRACECONTEXT in config._propagation_style_inject:
            _TraceContext._inject(span_context, headers)

    @staticmethod
    def extract(headers):
        """Extract a Context from HTTP headers into a new Context.
        For tracecontext propagation we extract tracestate headers for
        propagation even if another propagation style is specified before tracecontext,
        so as to always propagate other vendor's tracestate values by default.
        This is skipped if the tracer is configured to take the first style it matches.

        Here is an example from a web endpoint::

            from ddtrace.propagation.http import HTTPPropagator

            def my_controller(url, headers):
                context = HTTPPropagator.extract(headers)
                if context:
                    tracer.context_provider.activate(context)

                with tracer.trace('my_controller') as span:
                    span.set_tag('http.url', url)

        :param dict headers: HTTP headers to extract tracing attributes.
        :return: New `Context` with propagated attributes.
        """
        context = Context()
        if not headers or not config._propagation_style_extract:
            return context
        try:
            style = ""
            normalized_headers = {name.lower(): v for name, v in headers.items()}
            # tracer configured to extract first only
            if config._propagation_extract_first:
                # loop through the extract propagation styles specified in order, return whatever context we get first
                for prop_style in config._propagation_style_extract:
                    propagator = _PROP_STYLES[prop_style]
                    context = propagator._extract(normalized_headers)
                    style = prop_style
                    if config._propagation_http_baggage_enabled is True:
                        _attach_baggage_to_context(normalized_headers, context)
                    break

            # loop through all extract propagation styles
            else:
                contexts, styles_w_ctx = HTTPPropagator._extract_configured_contexts_avail(normalized_headers)
                # check that styles_w_ctx is not empty
                if styles_w_ctx:
                    style = styles_w_ctx[0]

                if contexts:
                    context = HTTPPropagator._resolve_contexts(contexts, styles_w_ctx, normalized_headers)
                    if config._propagation_http_baggage_enabled is True:
                        _attach_baggage_to_context(normalized_headers, context)

            # baggage headers are handled separately from the other propagation styles
            if _PROPAGATION_STYLE_BAGGAGE in config._propagation_style_extract:
                baggage_context = _BaggageHeader._extract(normalized_headers)
                if baggage_context._baggage != {}:
                    if context:
                        context._baggage = baggage_context.get_all_baggage_items()
                    else:
                        context = baggage_context

                    if config._baggage_tag_keys:
                        raw_keys = [k.strip() for k in config._baggage_tag_keys if k.strip()]
                        # wildcard: tag all baggage keys
                        if "*" in raw_keys:
                            tag_keys = baggage_context.get_all_baggage_items().keys()
                        else:
                            tag_keys = raw_keys

                        for stripped_key in tag_keys:
                            if (value := baggage_context.get_baggage_item(stripped_key)) is not None:
                                prefixed_key = BAGGAGE_TAG_PREFIX + stripped_key
                                if prefixed_key not in context._meta:
                                    context._meta[prefixed_key] = value

            if config._propagation_behavior_extract == _PROPAGATION_BEHAVIOR_RESTART:
                link = HTTPPropagator._context_to_span_link(context, style, "propagation_behavior_extract")
                context = Context(baggage=context.get_all_baggage_items(), span_links=[link] if link else [])

            return context

        except Exception:
            log.debug("error while extracting context propagation headers", exc_info=True)
        return Context()
