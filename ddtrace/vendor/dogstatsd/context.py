# Unless explicitly stated otherwise all files in this repository are licensed under the BSD-3-Clause License.
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2015-Present Datadog, Inc
# stdlib
from functools import wraps
from inspect import iscoroutinefunction

try:
    from time import monotonic  # type: ignore[attr-defined]
except ImportError:
    from time import time as monotonic

# datadog
from .context_async import _get_wrapped_co


class TimedContextManagerDecorator(object):
    """
    A context manager and a decorator which will report the elapsed time in
    the context OR in a function call.
    """

    def __init__(self, statsd, metric=None, tags=None, sample_rate=1, use_ms=None):
        self.statsd = statsd
        self.timing_func = statsd.timing
        self.metric = metric
        self.tags = tags
        self.sample_rate = sample_rate
        self.use_ms = use_ms
        self.elapsed = None

    def __call__(self, func):
        """
        Decorator which returns the elapsed time of the function call.

        Default to the function name if metric was not provided.
        """
        if not self.metric:
            self.metric = "%s.%s" % (func.__module__, func.__name__)

        # Coroutines
        if iscoroutinefunction(func):
            return _get_wrapped_co(self, func)

        # Others
        @wraps(func)
        def wrapped(*args, **kwargs):
            start = monotonic()
            try:
                return func(*args, **kwargs)
            finally:
                self._send(start)

        return wrapped

    def __enter__(self):
        if not self.metric:
            raise TypeError("Cannot used timed without a metric!")
        self._start = monotonic()
        return self

    def __exit__(self, type, value, traceback):
        # Report the elapsed time of the context manager.
        self._send(self._start)

    def _send(self, start):
        elapsed = monotonic() - start
        use_ms = self.use_ms if self.use_ms is not None else self.statsd.use_ms
        elapsed = int(round(1000 * elapsed)) if use_ms else elapsed
        self.timing_func(self.metric, elapsed, self.tags, self.sample_rate)
        self.elapsed = elapsed

    def start(self):
        self.__enter__()

    def stop(self):
        self.__exit__(None, None, None)


class DistributedContextManagerDecorator(TimedContextManagerDecorator):
    """
    A context manager and a decorator which will report the elapsed time in
    the context OR in a function call using the custom distribution metric.
    """

    def __init__(self, statsd, metric=None, tags=None, sample_rate=1, use_ms=None):
        super(DistributedContextManagerDecorator, self).__init__(statsd, metric, tags, sample_rate, use_ms)
        self.timing_func = statsd.distribution
