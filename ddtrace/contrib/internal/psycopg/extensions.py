"""
Tracing utilities for the psycopg2 potgres client library.
"""
import functools

import wrapt

from ddtrace import config
from ddtrace.constants import _SPAN_MEASURED_KEY
from ddtrace.constants import SPAN_KIND
from ddtrace.ext import SpanKind
from ddtrace.ext import SpanTypes
from ddtrace.ext import db
from ddtrace.ext import net
from ddtrace.internal.constants import COMPONENT
from ddtrace.internal.schema import schematize_database_operation


def get_psycopg2_extensions(psycopg_module):
    class TracedCursor(psycopg_module.extensions.cursor):
        """Wrapper around cursor creating one span per query"""

        def __init__(self, *args, **kwargs):
            self._datadog_tracer = kwargs.pop("datadog_tracer", None)
            self._datadog_service = kwargs.pop("datadog_service", None)
            self._datadog_tags = kwargs.pop("datadog_tags", None)
            super(TracedCursor, self).__init__(*args, **kwargs)

        def execute(self, query, vars=None):  # noqa: A002
            """just wrap the cursor execution in a span"""
            if not self._datadog_tracer:
                return psycopg_module.extensions.cursor.execute(self, query, vars)

            with self._datadog_tracer.trace(
                schematize_database_operation("postgres.query", database_provider="postgresql"),
                service=self._datadog_service,
                span_type=SpanTypes.SQL,
            ) as s:
                s.set_tag_str(COMPONENT, config.psycopg.integration_name)
                s.set_tag_str(db.SYSTEM, config.psycopg.dbms_name)

                # set span.kind to the type of operation being performed
                s.set_tag_str(SPAN_KIND, SpanKind.CLIENT)

                s.set_tag(_SPAN_MEASURED_KEY)
                if s.context.sampling_priority is None or s.context.sampling_priority <= 0:
                    return super(TracedCursor, self).execute(query, vars)

                s.resource = query
                s.set_tags(self._datadog_tags)
                try:
                    return super(TracedCursor, self).execute(query, vars)
                finally:
                    s.set_metric(db.ROWCOUNT, self.rowcount)

        def callproc(self, procname, vars=None):  # noqa: A002
            """just wrap the execution in a span"""
            return psycopg_module.extensions.cursor.callproc(self, procname, vars)

    class TracedConnection(psycopg_module.extensions.connection):
        """Wrapper around psycopg2 for tracing"""

        def __init__(self, *args, **kwargs):
            self._datadog_tracer = kwargs.pop("datadog_tracer", None)
            self._datadog_service = kwargs.pop("datadog_service", None)

            super(TracedConnection, self).__init__(*args, **kwargs)

            # add metadata (from the connection, string, etc)
            dsn = psycopg_module.extensions.parse_dsn(self.dsn)
            self._datadog_tags = {
                net.TARGET_HOST: dsn.get("host"),
                net.TARGET_PORT: dsn.get("port"),
                net.SERVER_ADDRESS: dsn.get("host"),
                db.NAME: dsn.get("dbname"),
                db.USER: dsn.get("user"),
                db.SYSTEM: config.psycopg.dbms_name,
                "db.application": dsn.get("application_name"),
            }

            self._datadog_cursor_class = functools.partial(
                TracedCursor,
                datadog_tracer=self._datadog_tracer,
                datadog_service=self._datadog_service,
                datadog_tags=self._datadog_tags,
            )

        def cursor(self, *args, **kwargs):
            """register our custom cursor factory"""
            kwargs.setdefault("cursor_factory", self._datadog_cursor_class)
            return super(TracedConnection, self).cursor(*args, **kwargs)

    # extension hooks
    _extensions = [
        (
            psycopg_module.extensions.register_type,
            psycopg_module.extensions,
            "register_type",
            _extensions_register_type,
        ),
        (psycopg_module._psycopg.register_type, psycopg_module._psycopg, "register_type", _extensions_register_type),
        (psycopg_module.extensions.adapt, psycopg_module.extensions, "adapt", _extensions_adapt),
    ]

    # `_json` attribute is only available for psycopg >= 2.5
    if getattr(psycopg_module, "_json", None):
        _extensions += [
            (psycopg_module._json.register_type, psycopg_module._json, "register_type", _extensions_register_type),
        ]

    # `quote_ident` attribute is only available for psycopg >= 2.7
    if getattr(psycopg_module, "extensions", None) and getattr(psycopg_module.extensions, "quote_ident", None):
        _extensions += [
            (psycopg_module.extensions.quote_ident, psycopg_module.extensions, "quote_ident", _extensions_quote_ident),
        ]

    return _extensions


def _extensions_register_type(func, _, args, kwargs):
    def _unroll_args(obj, scope=None):
        return obj, scope

    obj, scope = _unroll_args(*args, **kwargs)

    # register_type performs a c-level check of the object
    # type so we must be sure to pass in the actual db connection
    if scope and isinstance(scope, wrapt.ObjectProxy):
        scope = scope.__wrapped__

    return func(obj, scope) if scope else func(obj)


def _extensions_quote_ident(func, _, args, kwargs):
    def _unroll_args(obj, scope=None):
        return obj, scope

    obj, scope = _unroll_args(*args, **kwargs)

    # register_type performs a c-level check of the object
    # type so we must be sure to pass in the actual db connection
    if scope and isinstance(scope, wrapt.ObjectProxy):
        scope = scope.__wrapped__

    return func(obj, scope) if scope else func(obj)


def _extensions_adapt(func, _, args, kwargs):
    adapt = func(*args, **kwargs)
    if hasattr(adapt, "prepare"):
        return AdapterWrapper(adapt)
    return adapt


class AdapterWrapper(wrapt.ObjectProxy):
    def prepare(self, *args, **kwargs):
        func = self.__wrapped__.prepare
        if not args:
            return func(*args, **kwargs)
        conn = args[0]

        # prepare performs a c-level check of the object type so
        # we must be sure to pass in the actual db connection
        if isinstance(conn, wrapt.ObjectProxy):
            conn = conn.__wrapped__

        return func(conn, *args[1:], **kwargs)


def _patch_extensions(_extensions):
    # we must patch extensions all the time (it's pretty harmless) so split
    # from global patching of connections. must be idempotent.
    for _, module, func, wrapper in _extensions:
        if not hasattr(module, func) or isinstance(getattr(module, func), wrapt.ObjectProxy):
            continue
        wrapt.wrap_function_wrapper(module, func, wrapper)


def _unpatch_extensions(_extensions):
    for original, module, func, _ in _extensions:
        setattr(module, func, original)
