import flask
from wrapt import function_wrapper

from ddtrace import config
from ddtrace.contrib import trace_utils
from ddtrace.internal import core
from ddtrace.internal.constants import COMPONENT
from ddtrace.internal.logger import get_logger
from ddtrace.internal.utils.importlib import func_name
from ddtrace.trace import Pin


log = get_logger(__name__)


def wrap_view(instance, func, name=None, resource=None):
    return _wrap_call_with_pin_check(func, instance, name or func_name(func), resource=resource, do_dispatch=True)


def get_current_app():
    """Helper to get the flask.app.Flask from the current app context"""
    try:
        return flask.current_app
    except RuntimeError:
        # raised if current_app is None: https://github.com/pallets/flask/blob/2.1.3/src/flask/globals.py#L40
        pass
    return None


def _wrap_call(
    wrapped, pin, name, resource=None, signal=None, span_type=None, do_dispatch=False, args=None, kwargs=None
):
    args = args or []
    kwargs = kwargs or {}
    tags = {COMPONENT: config.flask.integration_name}
    if signal:
        tags["flask.signal"] = signal
    with core.context_with_data(
        "flask.call",
        span_name=name,
        pin=pin,
        resource=resource,
        service=trace_utils.int_service(pin, config.flask),
        span_type=span_type,
        tags=tags,
    ) as ctx, ctx.span:
        if do_dispatch:
            dispatch = core.dispatch_with_results("flask.wrapped_view", (kwargs,))

            # Appsec blocks the request
            result = dispatch.callbacks
            if result:
                callback_block = result.value
                if callback_block:
                    return callback_block()
            # IAST overrides the kwargs
            result = dispatch.check_kwargs
            if result:
                _kwargs = result.value
                if _kwargs:
                    for k in kwargs:
                        kwargs[k] = _kwargs[k]

        return wrapped(*args, **kwargs)


def _wrap_call_with_pin_check(func, instance, name, resource=None, signal=None, do_dispatch=False):
    @function_wrapper
    def patch_func(wrapped, _instance, args, kwargs):
        pin = Pin._find(wrapped, _instance, instance, get_current_app())
        if not pin or not pin.enabled():
            return wrapped(*args, **kwargs)
        return _wrap_call(
            wrapped, pin, name, resource=resource, signal=signal, do_dispatch=do_dispatch, args=args, kwargs=kwargs
        )

    return patch_func(func)


def wrap_function(instance, func, name=None, resource=None):
    return _wrap_call_with_pin_check(func, instance, name or func_name(func), resource=resource)


def simple_call_wrapper(name, span_type=None):
    @with_instance_pin
    def wrapper(pin, wrapped, instance, args, kwargs):
        return _wrap_call(wrapped, pin, name, span_type=span_type, args=args, kwargs=kwargs)

    return wrapper


def with_instance_pin(func):
    """Helper to wrap a function wrapper and ensure an enabled pin is available for the `instance`"""

    def wrapper(wrapped, instance, args, kwargs):
        pin = Pin._find(wrapped, instance, get_current_app())
        if not pin or not pin.enabled():
            return wrapped(*args, **kwargs)

        return func(pin, wrapped, instance, args, kwargs)

    return wrapper
