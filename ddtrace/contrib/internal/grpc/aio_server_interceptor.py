import inspect
from typing import Any  # noqa:F401
from typing import Awaitable  # noqa:F401
from typing import Callable  # noqa:F401
from typing import Iterable  # noqa:F401
from typing import Union  # noqa:F401

import grpc
from grpc import aio
from grpc.aio._typing import RequestIterableType
from grpc.aio._typing import RequestType
from grpc.aio._typing import ResponseIterableType
from grpc.aio._typing import ResponseType
import wrapt

from ddtrace import config
from ddtrace.constants import _ANALYTICS_SAMPLE_RATE_KEY
from ddtrace.constants import _SPAN_MEASURED_KEY
from ddtrace.constants import ERROR_MSG
from ddtrace.constants import ERROR_TYPE
from ddtrace.constants import SPAN_KIND
from ddtrace.contrib import trace_utils
from ddtrace.contrib.internal.grpc import constants
from ddtrace.contrib.internal.grpc.utils import set_grpc_method_meta
from ddtrace.ext import SpanKind
from ddtrace.ext import SpanTypes
from ddtrace.internal.compat import to_unicode
from ddtrace.internal.constants import COMPONENT
from ddtrace.internal.schema import schematize_url_operation
from ddtrace.internal.schema.span_attribute_schema import SpanDirection
from ddtrace.trace import Pin  # noqa:F401
from ddtrace.trace import Span  # noqa:F401


Continuation = Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler]]


# Used to get a status code from integer
# as `grpc._cython.cygrpc._ServicerContext.code()` returns an integer.
_INT2CODE = {s.value[0]: s for s in grpc.StatusCode}


def _is_coroutine_handler(handler):
    # type: (grpc.RpcMethodHandler) -> bool
    if not handler.request_streaming and not handler.response_streaming:
        return inspect.iscoroutinefunction(handler.unary_unary)
    elif not handler.request_streaming and handler.response_streaming:
        return inspect.iscoroutinefunction(handler.unary_stream)
    elif handler.request_streaming and not handler.response_streaming:
        return inspect.iscoroutinefunction(handler.stream_unary)
    else:
        return inspect.iscoroutinefunction(handler.stream_stream)


def _is_async_gen_handler(handler):
    # type: (grpc.RpcMethodHandler) -> bool
    if not handler.response_streaming:
        return False
    if handler.request_streaming:
        return inspect.isasyncgenfunction(handler.stream_stream)
    else:
        return inspect.isasyncgenfunction(handler.unary_stream)


def create_aio_server_interceptor(pin):
    # type: (Pin) -> _ServerInterceptor
    async def interceptor_function(
        continuation,  # type: Continuation
        handler_call_details,  # type: grpc.HandlerCallDetails
    ):
        # type: (...) -> Union[TracedRpcMethodHandlerType, None]
        rpc_method_handler = await continuation(handler_call_details)

        # continuation returns an RpcMethodHandler instance if the RPC is
        # considered serviced, or None otherwise
        # https://grpc.github.io/grpc/python/grpc.html#grpc.ServerInterceptor.intercept_service

        if rpc_method_handler is None:
            return None

        # Since streaming response RPC can be either a coroutine or an async generator, we're checking either here.
        if _is_coroutine_handler(rpc_method_handler):
            return _TracedCoroRpcMethodHandler(pin, handler_call_details, rpc_method_handler)
        elif _is_async_gen_handler(rpc_method_handler):
            return _TracedAsyncGenRpcMethodHandler(pin, handler_call_details, rpc_method_handler)
        else:
            return _TracedRpcMethodHandler(pin, handler_call_details, rpc_method_handler)

    return _ServerInterceptor(interceptor_function)


def _handle_server_exception(
    servicer_context,  # type: Union[None, grpc.ServicerContext]
    span,  # type: Span
):
    # type: (...) -> None
    span.error = 1
    if servicer_context is None:
        return
    if hasattr(servicer_context, "details"):
        span.set_tag_str(ERROR_MSG, to_unicode(servicer_context.details()))
    if hasattr(servicer_context, "code") and servicer_context.code() != 0 and servicer_context.code() in _INT2CODE:
        span.set_tag_str(ERROR_TYPE, to_unicode(_INT2CODE[servicer_context.code()]))


async def _wrap_aio_stream_response(
    behavior: Callable[[Union[RequestIterableType, RequestType], aio.ServicerContext], ResponseIterableType],
    request_or_iterator: Union[RequestIterableType, RequestType],
    servicer_context: aio.ServicerContext,
    span: Span,
) -> ResponseIterableType:
    try:
        call = behavior(request_or_iterator, servicer_context)
        async for response in call:
            yield response
    except Exception:
        span.set_traceback()
        _handle_server_exception(servicer_context, span)
        raise
    finally:
        span.finish()


async def _wrap_aio_unary_response(
    behavior: Callable[[Union[RequestIterableType, RequestType], aio.ServicerContext], Awaitable[ResponseType]],
    request_or_iterator: Union[RequestIterableType, RequestType],
    servicer_context: aio.ServicerContext,
    span: Span,
) -> ResponseType:
    try:
        return await behavior(request_or_iterator, servicer_context)
    except Exception:
        span.set_traceback()
        _handle_server_exception(servicer_context, span)
        raise
    finally:
        span.finish()


def _wrap_stream_response(
    behavior,  # type: Callable[[Any, grpc.ServicerContext], Iterable[Any]]
    request_or_iterator,  # type: Any
    servicer_context,  # type: grpc.ServicerContext
    span,  # type: Span
):
    # type: (...) -> Iterable[Any]
    try:
        for response in behavior(request_or_iterator, servicer_context):
            yield response
    except Exception:
        span.set_traceback()
        _handle_server_exception(servicer_context, span)
        raise
    finally:
        span.finish()


def _wrap_unary_response(
    behavior,  # type: Callable[[Any, grpc.ServicerContext], Any]
    request_or_iterator,  # type: Any
    servicer_context,  # type: grpc.ServicerContext
    span,  # type: Span
):
    # type: (...) -> Any
    try:
        return behavior(request_or_iterator, servicer_context)
    except Exception:
        span.set_traceback()
        _handle_server_exception(servicer_context, span)
        raise
    finally:
        span.finish()


def _create_span(pin, method, invocation_metadata, method_kind):
    # type: (Pin, str, grpc.HandlerCallDetails, str) -> Span
    tracer = pin.tracer
    trace_utils.activate_distributed_headers(
        tracer, int_config=config.grpc_aio_server, request_headers=dict(invocation_metadata)
    )

    span = tracer.trace(
        schematize_url_operation("grpc", protocol="grpc", direction=SpanDirection.INBOUND),
        span_type=SpanTypes.GRPC,
        service=trace_utils.int_service(pin, config.grpc_aio_server),
        resource=method,
    )

    span.set_tag_str(COMPONENT, config.grpc_aio_server.integration_name)

    # set span.kind to the type of operation being performed
    span.set_tag_str(SPAN_KIND, SpanKind.SERVER)

    span.set_tag(_SPAN_MEASURED_KEY)

    set_grpc_method_meta(span, method, method_kind)
    span.set_tag_str(constants.GRPC_SPAN_KIND_KEY, constants.GRPC_SPAN_KIND_VALUE_SERVER)

    sample_rate = config.grpc_aio_server.get_analytics_sample_rate()
    if sample_rate is not None:
        span.set_tag(_ANALYTICS_SAMPLE_RATE_KEY, sample_rate)

    if pin.tags:
        span.set_tags(pin.tags)

    return span


class _TracedCoroRpcMethodHandler(wrapt.ObjectProxy):
    def __init__(self, pin, handler_call_details, wrapped):
        # type: (Pin, grpc.HandlerCallDetails, grpc.RpcMethodHandler) -> None
        super(_TracedCoroRpcMethodHandler, self).__init__(wrapped)
        self._pin = pin
        self.method = handler_call_details.method

    async def unary_unary(self, request: RequestType, context: aio.ServicerContext) -> ResponseType:
        span = _create_span(self._pin, self.method, context.invocation_metadata(), constants.GRPC_METHOD_KIND_UNARY)
        return await _wrap_aio_unary_response(self.__wrapped__.unary_unary, request, context, span)

    async def unary_stream(self, request: RequestType, context: aio.ServicerContext) -> ResponseType:
        span = _create_span(
            self._pin,
            self.method,
            context.invocation_metadata(),
            constants.GRPC_METHOD_KIND_SERVER_STREAMING,
        )
        return await _wrap_aio_unary_response(self.__wrapped__.unary_stream, request, context, span)

    async def stream_unary(self, request_iterator: RequestIterableType, context: aio.ServicerContext) -> ResponseType:
        span = _create_span(
            self._pin,
            self.method,
            context.invocation_metadata(),
            constants.GRPC_METHOD_KIND_CLIENT_STREAMING,
        )
        return await _wrap_aio_unary_response(self.__wrapped__.stream_unary, request_iterator, context, span)

    async def stream_stream(self, request_iterator: RequestIterableType, context: aio.ServicerContext) -> ResponseType:
        span = _create_span(
            self._pin,
            self.method,
            context.invocation_metadata(),
            constants.GRPC_METHOD_KIND_BIDI_STREAMING,
        )
        return await _wrap_aio_unary_response(self.__wrapped__.stream_stream, request_iterator, context, span)


class _TracedAsyncGenRpcMethodHandler(wrapt.ObjectProxy):
    def __init__(self, pin, handler_call_details, wrapped):
        # type: (Pin, grpc.HandlerCallDetails, grpc.RpcMethodHandler) -> None
        super(_TracedAsyncGenRpcMethodHandler, self).__init__(wrapped)
        self._pin = pin
        self.method = handler_call_details.method

    async def unary_stream(self, request: RequestType, context: aio.ServicerContext) -> ResponseIterableType:
        span = _create_span(
            self._pin,
            self.method,
            context.invocation_metadata(),
            constants.GRPC_METHOD_KIND_SERVER_STREAMING,
        )
        async for response in _wrap_aio_stream_response(self.__wrapped__.unary_stream, request, context, span):
            yield response

    async def stream_stream(
        self, request_iterator: RequestIterableType, context: aio.ServicerContext
    ) -> ResponseIterableType:
        span = _create_span(
            self._pin,
            self.method,
            context.invocation_metadata(),
            constants.GRPC_METHOD_KIND_BIDI_STREAMING,
        )
        async for response in _wrap_aio_stream_response(
            self.__wrapped__.stream_stream, request_iterator, context, span
        ):
            yield response


class _TracedRpcMethodHandler(wrapt.ObjectProxy):
    def __init__(self, pin, handler_call_details, wrapped):
        # type: (Pin, grpc.HandlerCallDetails, grpc.RpcMethodHandler) -> None
        super(_TracedRpcMethodHandler, self).__init__(wrapped)
        self._pin = pin
        self.method = handler_call_details.method

    def unary_unary(self, request, context):
        # type: (Any, grpc.ServicerContext) -> Any
        span = _create_span(self._pin, self.method, context.invocation_metadata(), constants.GRPC_METHOD_KIND_UNARY)
        return _wrap_unary_response(self.__wrapped__.unary_unary, request, context, span)

    def unary_stream(self, request, context):
        # type: (Any, grpc.ServicerContext) -> Iterable[Any]
        span = _create_span(
            self._pin,
            self.method,
            context.invocation_metadata(),
            constants.GRPC_METHOD_KIND_SERVER_STREAMING,
        )
        for response in _wrap_stream_response(self.__wrapped__.unary_stream, request, context, span):
            yield response

    def stream_unary(self, request_iterator, context):
        # type: (Iterable[Any], grpc.ServicerContext) -> Any
        span = _create_span(
            self._pin,
            self.method,
            context.invocation_metadata(),
            constants.GRPC_METHOD_KIND_CLIENT_STREAMING,
        )
        return _wrap_unary_response(self.__wrapped__.stream_unary, request_iterator, context, span)

    def stream_stream(self, request_iterator, context):
        # type: (Iterable[Any], grpc.ServicerContext) -> Iterable[Any]
        span = _create_span(
            self._pin,
            self.method,
            context.invocation_metadata(),
            constants.GRPC_METHOD_KIND_BIDI_STREAMING,
        )
        for response in _wrap_stream_response(self.__wrapped__.stream_stream, request_iterator, context, span):
            yield response


TracedRpcMethodHandlerType = Union[
    _TracedAsyncGenRpcMethodHandler, _TracedCoroRpcMethodHandler, _TracedRpcMethodHandler
]


class _ServerInterceptor(aio.ServerInterceptor):
    def __init__(self, interceptor_function):
        self._fn = interceptor_function

    async def intercept_service(
        self,
        continuation,  # type: Continuation
        handler_call_details,  # type: grpc.HandlerCallDetails
    ):
        # type: (...) -> Union[TracedRpcMethodHandlerType, None]
        return await self._fn(continuation, handler_call_details)
