from dataclasses import dataclass
from dataclasses import field
from itertools import chain
import sys
from types import FrameType
from types import FunctionType
from types import ModuleType
from typing import Any
from typing import Dict
from typing import Mapping
from typing import Optional
from typing import cast

from ddtrace.debugging._expressions import DDExpressionEvaluationError
from ddtrace.debugging._probe.model import DEFAULT_CAPTURE_LIMITS
from ddtrace.debugging._probe.model import CaptureLimits
from ddtrace.debugging._probe.model import FunctionLocationMixin
from ddtrace.debugging._probe.model import LineLocationMixin
from ddtrace.debugging._probe.model import LiteralTemplateSegment
from ddtrace.debugging._probe.model import LogFunctionProbe
from ddtrace.debugging._probe.model import LogLineProbe
from ddtrace.debugging._probe.model import LogProbeMixin
from ddtrace.debugging._probe.model import TemplateSegment
from ddtrace.debugging._redaction import REDACTED_PLACEHOLDER
from ddtrace.debugging._redaction import DDRedactedExpressionError
from ddtrace.debugging._safety import get_args
from ddtrace.debugging._safety import get_globals
from ddtrace.debugging._safety import get_locals
from ddtrace.debugging._signal import utils
from ddtrace.debugging._signal.log import LogSignal
from ddtrace.debugging._signal.model import EvaluationError
from ddtrace.debugging._signal.model import probe_to_signal
from ddtrace.debugging._signal.utils import serialize
from ddtrace.internal.compat import ExcInfoType
from ddtrace.internal.utils.time import HourGlass


CAPTURE_TIME_BUDGET = 0.2  # seconds


_NOTSET = object()


EXCLUDE_GLOBAL_TYPES = (ModuleType, type, FunctionType)


def _capture_context(
    frame: FrameType,
    throwable: ExcInfoType,
    retval: Any = _NOTSET,
    limits: CaptureLimits = DEFAULT_CAPTURE_LIMITS,
) -> Dict[str, Any]:
    with HourGlass(duration=CAPTURE_TIME_BUDGET) as hg:

        def timeout(_):
            return not hg.trickling()

        arguments = get_args(frame)
        _locals = get_locals(frame)
        _globals = ((n, v) for n, v in get_globals(frame) if not isinstance(v, EXCLUDE_GLOBAL_TYPES))

        _, exc, _ = throwable
        if exc is not None:
            _locals = chain(_locals, [("@exception", exc)])
        elif retval is not _NOTSET:
            _locals = chain(_locals, [("@return", retval)])

        return {
            "arguments": utils.capture_pairs(
                arguments, limits.max_level, limits.max_len, limits.max_size, limits.max_fields, timeout
            )
            if arguments
            else {},
            "locals": utils.capture_pairs(
                _locals, limits.max_level, limits.max_len, limits.max_size, limits.max_fields, timeout
            )
            if _locals
            else {},
            "staticFields": utils.capture_pairs(
                _globals, limits.max_level, limits.max_len, limits.max_size, limits.max_fields, timeout
            )
            if _globals
            else {},
            "throwable": utils.capture_exc_info(throwable),
        }


_EMPTY_CAPTURED_CONTEXT: Dict[str, Any] = {"arguments": {}, "locals": {}, "staticFields": {}, "throwable": None}


@dataclass
class Snapshot(LogSignal):
    """Raw snapshot.

    Used to collect the minimum amount of information from a firing probe.
    """

    entry_capture: Optional[dict] = field(default=None)
    return_capture: Optional[dict] = field(default=None)
    line_capture: Optional[dict] = field(default=None)
    _stack: Optional[list] = field(default=None)
    _message: Optional[str] = field(default=None)
    duration: Optional[int] = field(default=None)  # nanoseconds

    def _eval_segment(self, segment: TemplateSegment, _locals: Mapping[str, Any]) -> str:
        probe = cast(LogProbeMixin, self.probe)
        capture = probe.limits
        try:
            if isinstance(segment, LiteralTemplateSegment):
                return segment.eval(_locals)
            return serialize(
                segment.eval(_locals),
                level=capture.max_level,
                maxsize=capture.max_size,
                maxlen=capture.max_len,
                maxfields=capture.max_fields,
            )
        except DDExpressionEvaluationError as e:
            self.errors.append(EvaluationError(expr=e.dsl, message=e.error))
            return REDACTED_PLACEHOLDER if isinstance(e.__cause__, DDRedactedExpressionError) else "ERROR"

    def _eval_message(self, _locals: Mapping[str, Any]) -> None:
        probe = cast(LogProbeMixin, self.probe)
        self._message = "".join([self._eval_segment(s, _locals) for s in probe.segments])

    def _do(self, retval, exc_info, scope):
        probe = cast(LogProbeMixin, self.probe)
        frame = self.frame

        self._eval_message(scope)

        self._stack = utils.capture_stack(self.frame)

        return _capture_context(frame, exc_info, retval=retval, limits=probe.limits) if probe.take_snapshot else None

    def enter(self, scope: Mapping[str, Any]) -> None:
        self.entry_capture = self._do(_NOTSET, (None, None, None), scope)

    def exit(self, retval, exc_info, duration, scope) -> None:
        self.duration = duration
        self.return_capture = self._do(retval, exc_info, scope)

        # Fix the line number of the top frame. This might have been mangled by
        # the instrumented exception handling of function probes.
        assert self._stack is not None  # nosec B101
        tb = exc_info[2]
        while tb is not None:
            frame = tb.tb_frame
            if frame == self.frame:
                self._stack[0]["lineNumber"] = tb.tb_lineno
                break
            tb = tb.tb_next

    def line(self, scope) -> None:
        self.line_capture = self._do(_NOTSET, sys.exc_info(), scope)

    @property
    def message(self) -> Optional[str]:
        return self._message

    def has_message(self) -> bool:
        return self._message is not None or bool(self.errors)

    @property
    def data(self):
        probe = self.probe

        captures = {}
        if isinstance(probe, LogProbeMixin) and probe.take_snapshot:
            if isinstance(probe, LineLocationMixin):
                captures = {"lines": {str(probe.line): self.line_capture or _EMPTY_CAPTURED_CONTEXT}}
            elif isinstance(probe, FunctionLocationMixin):
                captures = {
                    "entry": self.entry_capture or _EMPTY_CAPTURED_CONTEXT,
                    "return": self.return_capture or _EMPTY_CAPTURED_CONTEXT,
                }

        return {
            "stack": self._stack,
            "captures": captures,
            "duration": self.duration,
        }


@probe_to_signal.register
def _(probe: LogFunctionProbe, frame, thread, trace_context, meter):
    return Snapshot(probe=probe, frame=frame, thread=thread, trace_context=trace_context)


@probe_to_signal.register
def _(probe: LogLineProbe, frame, thread, trace_context, meter):
    return Snapshot(probe=probe, frame=frame, thread=thread, trace_context=trace_context)
