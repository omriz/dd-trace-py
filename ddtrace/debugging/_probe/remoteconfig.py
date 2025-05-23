from itertools import count
import os
import time
from typing import Any
from typing import Callable
from typing import Dict
from typing import Iterable
from typing import Optional
from typing import Type

from ddtrace import config as tracer_config
from ddtrace.debugging._config import di_config
from ddtrace.debugging._probe.model import DEFAULT_CAPTURE_LIMITS
from ddtrace.debugging._probe.model import DEFAULT_PROBE_CONDITION_ERROR_RATE
from ddtrace.debugging._probe.model import DEFAULT_PROBE_RATE
from ddtrace.debugging._probe.model import DEFAULT_SNAPSHOT_PROBE_RATE
from ddtrace.debugging._probe.model import DEFAULT_TRIGGER_PROBE_RATE
from ddtrace.debugging._probe.model import CaptureLimits
from ddtrace.debugging._probe.model import ExpressionTemplateSegment
from ddtrace.debugging._probe.model import FunctionProbe
from ddtrace.debugging._probe.model import LineProbe
from ddtrace.debugging._probe.model import LiteralTemplateSegment
from ddtrace.debugging._probe.model import LogFunctionProbe
from ddtrace.debugging._probe.model import LogLineProbe
from ddtrace.debugging._probe.model import MetricFunctionProbe
from ddtrace.debugging._probe.model import MetricLineProbe
from ddtrace.debugging._probe.model import Probe
from ddtrace.debugging._probe.model import ProbeEvalTiming
from ddtrace.debugging._probe.model import ProbeType
from ddtrace.debugging._probe.model import SpanDecoration
from ddtrace.debugging._probe.model import SpanDecorationFunctionProbe
from ddtrace.debugging._probe.model import SpanDecorationLineProbe
from ddtrace.debugging._probe.model import SpanDecorationTag
from ddtrace.debugging._probe.model import SpanFunctionProbe
from ddtrace.debugging._probe.model import StringTemplate
from ddtrace.debugging._probe.model import TemplateSegment
from ddtrace.debugging._probe.model import TimingMixin
from ddtrace.debugging._probe.model import TriggerFunctionProbe
from ddtrace.debugging._probe.model import TriggerLineProbe
from ddtrace.debugging._probe.status import ProbeStatusLogger
from ddtrace.debugging._redaction import DDRedactedExpression
from ddtrace.internal.logger import get_logger
from ddtrace.internal.remoteconfig._connectors import PublisherSubscriberConnector
from ddtrace.internal.remoteconfig._publishers import RemoteConfigPublisher
from ddtrace.internal.remoteconfig._pubsub import PubSub
from ddtrace.internal.remoteconfig._subscribers import RemoteConfigSubscriber


log = get_logger(__name__)


def xlate_keys(d: Dict[str, Any], mapping: Dict[str, str]) -> Dict[str, Any]:
    return {mapping.get(k, k): v for k, v in d.items()}


def _compile_segment(segment: dict) -> TemplateSegment:
    if "str" in segment:
        return LiteralTemplateSegment(str_value=segment["str"])

    if "json" in segment:
        return ExpressionTemplateSegment(expr=DDRedactedExpression.compile(segment))

    msg = f"Invalid template segment: {segment}"
    raise ValueError(msg)


def _match_env_and_version(probe: Probe) -> bool:
    probe_version = probe.tags.get("version", None)
    probe_env = probe.tags.get("env", None)

    return (probe_version is None or probe_version == tracer_config.version) and (
        probe_env is None or probe_env == tracer_config.env
    )


def _filter_by_env_and_version(f: Callable[..., Iterable[Probe]]) -> Callable[..., Iterable[Probe]]:
    def _wrapper(*args: Any, **kwargs: Any) -> Iterable[Probe]:
        return [_ for _ in f(*args, **kwargs) if _match_env_and_version(_)]

    return _wrapper


class ProbeFactory(object):
    __line_class__: Optional[Type[LineProbe]] = None
    __function_class__: Optional[Type[FunctionProbe]] = None

    @classmethod
    def update_args(cls, args, attribs):
        raise NotImplementedError()

    @classmethod
    def build(cls, args: Dict[str, Any], attribs: Dict[str, Any]) -> Any:
        cls.update_args(args, attribs)

        where = attribs["where"]
        if where.get("sourceFile", None) is not None:
            if cls.__line_class__ is None:
                raise TypeError("Line probe type is not supported")

            args["source_file"] = where["sourceFile"]
            args["line"] = int(where["lines"][0])

            return cls.__line_class__(**args)

        if cls.__function_class__ is None:
            raise TypeError("Function probe type is not supported")

        args["module"] = where.get("type") or where["typeName"]
        args["func_qname"] = where.get("method") or where["methodName"]
        if issubclass(cls.__function_class__, TimingMixin):
            args["evaluate_at"] = ProbeEvalTiming[attribs.get("evaluateAt", "DEFAULT")]

        return cls.__function_class__(**args)


class LogProbeFactory(ProbeFactory):
    __line_class__ = LogLineProbe
    __function_class__ = LogFunctionProbe

    @classmethod
    def update_args(cls, args, attribs):
        take_snapshot = attribs.get("captureSnapshot", False)

        rate = DEFAULT_SNAPSHOT_PROBE_RATE if take_snapshot else DEFAULT_PROBE_RATE
        sampling = attribs.get("sampling")
        if sampling is not None:
            rate = sampling.get("snapshotsPerSecond", rate)

        args.update(
            condition=DDRedactedExpression.compile(attribs["when"]) if "when" in attribs else None,
            rate=rate,
            limits=CaptureLimits(
                **xlate_keys(
                    attribs["capture"],
                    {
                        "maxReferenceDepth": "max_level",
                        "maxCollectionSize": "max_size",
                        "maxLength": "max_len",
                        "maxFieldCount": "max_fields",
                    },
                )
            )
            if "capture" in attribs
            else DEFAULT_CAPTURE_LIMITS,
            condition_error_rate=DEFAULT_PROBE_CONDITION_ERROR_RATE,  # TODO: should we take rate limit out of Probe?
            take_snapshot=take_snapshot,
            template=attribs.get("template"),
            segments=[_compile_segment(segment) for segment in attribs.get("segments", [])],
        )


class MetricProbeFactory(ProbeFactory):
    __line_class__ = MetricLineProbe
    __function_class__ = MetricFunctionProbe

    @classmethod
    def update_args(cls, args, attribs):
        # adding probe_id to probe-tags so it would be recorded as a metric tag
        args["tags"]["debugger.probeid"] = args["probe_id"]

        args.update(
            condition=DDRedactedExpression.compile(attribs["when"]) if "when" in attribs else None,
            name=attribs["metricName"],
            kind=attribs["kind"],
            condition_error_rate=DEFAULT_PROBE_CONDITION_ERROR_RATE,  # TODO: should we take rate limit out of Probe?
            value=DDRedactedExpression.compile(attribs["value"]) if "value" in attribs else None,
        )


class SpanProbeFactory(ProbeFactory):
    __function_class__ = SpanFunctionProbe

    @classmethod
    def update_args(cls, args, attribs):
        args.update(
            condition=DDRedactedExpression.compile(attribs["when"]) if "when" in attribs else None,
            condition_error_rate=DEFAULT_PROBE_CONDITION_ERROR_RATE,  # TODO: should we take rate limit out of Probe?
        )


class SpanDecorationProbeFactory(ProbeFactory):
    __line_class__ = SpanDecorationLineProbe
    __function_class__ = SpanDecorationFunctionProbe

    @classmethod
    def update_args(cls, args, attribs):
        args.update(
            target_span=attribs["targetSpan"],
            decorations=[
                SpanDecoration(
                    when=DDRedactedExpression.compile(d["when"]) if "when" in d else None,
                    tags=[
                        SpanDecorationTag(
                            name=t["name"],
                            value=StringTemplate(
                                template=t["value"].get("template"),
                                segments=[_compile_segment(segment) for segment in t["value"].get("segments", [])],
                            ),
                        )
                        for t in d.get("tags", [])
                    ],
                )
                for d in attribs["decorations"]
            ],
        )


class TriggerProbeFactory(ProbeFactory):
    __line_class__ = TriggerLineProbe
    __function_class__ = TriggerFunctionProbe

    @classmethod
    def update_args(cls, args, attribs):
        args.update(
            rate=attribs.get("sampling", {}).get("cooldownInSeconds", DEFAULT_TRIGGER_PROBE_RATE),
            session_id=attribs["session_id"],
            level=int(attribs["level"]),
            condition=DDRedactedExpression.compile(attribs["when"]) if "when" in attribs else None,
            condition_error_rate=DEFAULT_PROBE_CONDITION_ERROR_RATE,
        )


class InvalidProbeConfiguration(ValueError):
    pass


PROBE_FACTORY = {
    ProbeType.LOG_PROBE: LogProbeFactory,
    ProbeType.METRIC_PROBE: MetricProbeFactory,
    ProbeType.SPAN_PROBE: SpanProbeFactory,
    ProbeType.SPAN_DECORATION_PROBE: SpanDecorationProbeFactory,
    ProbeType.TRIGGER_PROBE: TriggerProbeFactory,
}


def build_probe(attribs: Dict[str, Any]) -> Probe:
    """
    Create a new Probe instance.
    """
    try:
        _type = attribs["type"]
        _id = attribs["id"]
    except KeyError as e:
        raise InvalidProbeConfiguration("Invalid probe attributes: %s" % e)

    args = dict(
        probe_id=_id,
        version=attribs.get("version", 0),
        tags=dict(_.split(":", 1) for _ in attribs.get("tags", [])),
    )

    factory = PROBE_FACTORY.get(_type)
    if factory is not None:
        return factory.build(args, attribs)

    raise InvalidProbeConfiguration("Unsupported probe type: %s" % _type)


@_filter_by_env_and_version
def get_probes(config: dict, status_logger: ProbeStatusLogger) -> Iterable[Probe]:
    try:
        return [build_probe(config)]
    except InvalidProbeConfiguration:
        raise
    except Exception as e:
        status_logger.error(
            probe=Probe(probe_id=config["id"], version=config.get("version", 0), tags={}),
            error=(type(e).__name__, str(e)),
        )
        return []


class ProbePollerEvent(object):
    NEW_PROBES = 0
    DELETED_PROBES = 1
    MODIFIED_PROBES = 2
    STATUS_UPDATE = 3


ProbePollerEventType = int


class DebuggerRemoteConfigSubscriber(RemoteConfigSubscriber):
    """Probe configuration adapter for the RCM client.

    This adapter turns configuration events from the RCM client into probe
    events that can be handled easily by the debugger.
    """

    def __init__(self, data_connector, callback, name, status_logger):
        super().__init__(data_connector, None, name)
        self._configs: Dict[str, Dict[str, Probe]] = {}
        self._status_timestamp_sequence = count(
            time.time() + di_config.diagnostics_interval, di_config.diagnostics_interval
        )
        self._status_timestamp = next(self._status_timestamp_sequence)
        self._status_logger = status_logger
        self._debugger_callback = callback

    def _exec_callback(self, data, test_tracer=None):
        # Check if it is time to re-emit probe status messages.
        # DEV: We use the periodic signal from the remote config client worker
        # thread to avoid having to spawn a separate thread for this.
        if time.time() > self._status_timestamp:
            self._send_status_update()
            self._status_timestamp = next(self._status_timestamp_sequence)

        if data:
            log.debug("[%s][P: %s] Dynamic Instrumentation Updated", os.getpid(), os.getppid())
            for payload in data:
                if payload.metadata is None:
                    log.debug(
                        "[%s][P: %s] Dynamic Instrumentation: no RCM metadata for configuration; skipping",
                        os.getpid(),
                        os.getppid(),
                    )
                    continue
                self._update_probes_for_config(payload.metadata.id, payload.content)

        # Flush any probe status messages that might have been generated
        self._status_logger.flush()

    def _send_status_update(self):
        log.debug(
            "[%s][P: %s] Dynamic Instrumentation: emitting probe status log messages",
            os.getpid(),
            os.getppid(),
        )

        self._debugger_callback(ProbePollerEvent.STATUS_UPDATE, [])

    def _dispatch_probe_events(self, prev_probes: Dict[str, Probe], next_probes: Dict[str, Probe]) -> None:
        new_probes = [p for _, p in next_probes.items() if _ not in prev_probes]
        deleted_probes = [p for _, p in prev_probes.items() if _ not in next_probes]
        modified_probes = [p for _, p in next_probes.items() if _ in prev_probes and p != prev_probes[_]]

        if deleted_probes:
            self._debugger_callback(ProbePollerEvent.DELETED_PROBES, deleted_probes)
        if modified_probes:
            self._debugger_callback(ProbePollerEvent.MODIFIED_PROBES, modified_probes)
        if new_probes:
            self._debugger_callback(ProbePollerEvent.NEW_PROBES, new_probes)

    def _update_probes_for_config(self, config_id: str, config: Any) -> None:
        prev_probes: Dict[str, Probe] = self._configs.get(config_id, {})
        next_probes: Dict[str, Probe] = (
            {probe.probe_id: probe for probe in get_probes(config, self._status_logger)}
            if config not in (None, False)
            else {}
        )
        log.debug("[%s][P: %s] Dynamic Instrumentation, dispatch probe events", os.getpid(), os.getppid())
        self._dispatch_probe_events(prev_probes, next_probes)

        if next_probes:
            self._configs[config_id] = next_probes
        else:
            self._configs.pop(config_id, None)


class ProbeRCAdapter(PubSub):
    __publisher_class__ = RemoteConfigPublisher
    __subscriber_class__ = DebuggerRemoteConfigSubscriber
    __shared_data__ = PublisherSubscriberConnector()

    def __init__(self, _preprocess_results, callback, status_logger):
        self._publisher = self.__publisher_class__(self.__shared_data__, _preprocess_results)
        self._subscriber = self.__subscriber_class__(self.__shared_data__, callback, "DEBUGGER", status_logger)
