import sys


try:
    sys.modules["sqlite3"] = __import__("pysqlite3")
except ImportError:
    pass

import sqlite3
import time
from typing import TYPE_CHECKING  # noqa:F401

import pytest

import ddtrace
from ddtrace.constants import ERROR_MSG
from ddtrace.constants import ERROR_STACK
from ddtrace.constants import ERROR_TYPE
from ddtrace.contrib.internal.sqlite3.patch import TracedSQLiteCursor
from ddtrace.contrib.internal.sqlite3.patch import patch
from ddtrace.contrib.internal.sqlite3.patch import unpatch
from ddtrace.internal.schema import DEFAULT_SPAN_SERVICE_NAME
from ddtrace.trace import Pin
from tests.opentracer.utils import init_tracer
from tests.utils import TracerTestCase
from tests.utils import assert_is_measured
from tests.utils import assert_is_not_measured


if TYPE_CHECKING:  # pragma: no cover
    from typing import Generator  # noqa:F401


@pytest.fixture
def patched_conn():
    # type: () -> Generator[sqlite3.Cursor, None, None]
    patch()
    conn = sqlite3.connect(":memory:")
    yield conn
    unpatch()


class TestSQLite(TracerTestCase):
    def setUp(self):
        super(TestSQLite, self).setUp()
        patch()

    def tearDown(self):
        unpatch()
        super(TestSQLite, self).tearDown()

    def test_service_info(self):
        backup_tracer = ddtrace.tracer
        ddtrace.tracer = self.tracer

        sqlite3.connect(":memory:")

        ddtrace.tracer = backup_tracer

    def test_sqlite(self):
        # ensure we can trace multiple services without stomping
        services = ["db", "another"]
        for service in services:
            db = sqlite3.connect(":memory:")
            pin = Pin.get_from(db)
            assert pin
            pin._clone(service=service, tracer=self.tracer).onto(db)

            # Ensure we can run a query and it's correctly traced
            q = "select * from sqlite_master"
            start = time.time()
            cursor = db.execute(q)
            self.assertIsInstance(cursor, TracedSQLiteCursor)
            rows = cursor.fetchall()
            end = time.time()
            assert not rows
            self.assert_structure(
                dict(name="sqlite.query", span_type="sql", resource=q, service=service, error=0),
            )
            root = self.get_root_span()
            assert_is_measured(root)
            self.assertIsNone(root.get_tag("sql.query"))
            self.assertEqual(root.get_tag("component"), "sqlite")
            self.assertEqual(root.get_tag("span.kind"), "client")
            self.assertEqual(root.get_tag("db.system"), "sqlite")
            assert start <= root.start <= end
            assert root.duration <= end - start
            self.reset()

            # run a query with an error and ensure all is well
            q = "select * from some_non_existant_table"
            with pytest.raises(sqlite3.OperationalError):
                db.execute(q)

            self.assert_structure(
                dict(name="sqlite.query", span_type="sql", resource=q, service=service, error=1),
            )
            root = self.get_root_span()
            assert_is_measured(root)
            self.assertIsNone(root.get_tag("sql.query"))
            self.assertEqual(root.get_tag("component"), "sqlite")
            self.assertEqual(root.get_tag("span.kind"), "client")
            self.assertEqual(root.get_tag("db.system"), "sqlite")
            self.assertIsNotNone(root.get_tag(ERROR_STACK))
            self.assertIn("OperationalError", root.get_tag(ERROR_TYPE))
            self.assertIn("no such table", root.get_tag(ERROR_MSG))
            self.reset()

    def test_sqlite_fetchall_is_traced(self):
        q = "select * from sqlite_master"

        # Not traced by default
        connection = self._given_a_traced_connection(self.tracer)
        cursor = connection.execute(q)
        cursor.fetchall()
        self.assert_structure(dict(name="sqlite.query", resource=q))
        self.reset()

        with self.override_config("sqlite", dict(trace_fetch_methods=True)):
            connection = self._given_a_traced_connection(self.tracer)
            cursor = connection.execute(q)
            cursor.fetchall()

            # We have two spans side by side
            query_span, fetchall_span = self.get_root_spans()

            # Assert query
            query_span.assert_structure(dict(name="sqlite.query", resource=q))
            assert_is_measured(query_span)

            # Assert fetchall
            fetchall_span.assert_structure(dict(name="sqlite.query.fetchall", resource=q, span_type="sql", error=0))
            assert_is_not_measured(fetchall_span)
            self.assertIsNone(fetchall_span.get_tag("sql.query"))

    def test_sqlite_fetchone_is_traced(self):
        q = "select * from sqlite_master"

        # Not traced by default
        connection = self._given_a_traced_connection(self.tracer)
        cursor = connection.execute(q)
        cursor.fetchone()
        self.assert_structure(dict(name="sqlite.query", resource=q))
        self.reset()

        with self.override_config("sqlite", dict(trace_fetch_methods=True)):
            connection = self._given_a_traced_connection(self.tracer)
            cursor = connection.execute(q)
            cursor.fetchone()

            # We have two spans side by side
            query_span, fetchone_span = self.get_root_spans()

            # Assert query
            assert_is_measured(query_span)
            self.assertEqual(query_span.get_tag("db.system"), "sqlite")
            query_span.assert_structure(dict(name="sqlite.query", resource=q))

            # Assert fetchone
            assert_is_not_measured(fetchone_span)
            fetchone_span.assert_structure(
                dict(
                    name="sqlite.query.fetchone",
                    resource=q,
                    span_type="sql",
                    error=0,
                ),
            )
            self.assertEqual(fetchone_span.get_tag("db.system"), "sqlite")
            self.assertIsNone(fetchone_span.get_tag("sql.query"))

    def test_sqlite_fetchmany_is_traced(self):
        q = "select * from sqlite_master"

        # Not traced by default
        connection = self._given_a_traced_connection(self.tracer)
        cursor = connection.execute(q)
        cursor.fetchmany(123)
        self.assert_structure(dict(name="sqlite.query", resource=q))
        self.reset()

        with self.override_config("sqlite", dict(trace_fetch_methods=True)):
            connection = self._given_a_traced_connection(self.tracer)
            cursor = connection.execute(q)
            cursor.fetchmany(123)

            # We have two spans side by side
            query_span, fetchmany_span = self.get_root_spans()

            # Assert query
            assert_is_measured(query_span)
            query_span.assert_structure(dict(name="sqlite.query", resource=q))
            self.assertEqual(query_span.get_tag("db.system"), "sqlite")

            # Assert fetchmany
            assert_is_not_measured(fetchmany_span)
            fetchmany_span.assert_structure(
                dict(
                    name="sqlite.query.fetchmany",
                    resource=q,
                    span_type="sql",
                    error=0,
                    metrics={"db.fetch.size": 123},
                ),
            )
            self.assertIsNone(fetchmany_span.get_tag("sql.query"))
            self.assertEqual(fetchmany_span.get_tag("db.system"), "sqlite")

    def test_sqlite_ot(self):
        """Ensure sqlite works with the opentracer."""
        ot_tracer = init_tracer("sqlite_svc", self.tracer)

        # Ensure we can run a query and it's correctly traced
        q = "select * from sqlite_master"
        with ot_tracer.start_active_span("sqlite_op"):
            db = sqlite3.connect(":memory:")
            pin = Pin.get_from(db)
            assert pin
            pin._clone(tracer=self.tracer).onto(db)
            cursor = db.execute(q)
            rows = cursor.fetchall()
        assert not rows

        self.assert_structure(
            dict(name="sqlite_op", service="sqlite_svc"),
            (dict(name="sqlite.query", service="sqlite", span_type="sql", resource=q, error=0),),
        )
        assert_is_measured(self.get_spans()[1])
        self.reset()

        with self.override_config("sqlite", dict(trace_fetch_methods=True)):
            with ot_tracer.start_active_span("sqlite_op"):
                db = sqlite3.connect(":memory:")
                pin = Pin.get_from(db)
                assert pin
                pin._clone(tracer=self.tracer).onto(db)
                cursor = db.execute(q)
                rows = cursor.fetchall()
                assert not rows

            self.assert_structure(
                dict(name="sqlite_op", service="sqlite_svc"),
                (
                    dict(name="sqlite.query", span_type="sql", resource=q, error=0),
                    dict(name="sqlite.query.fetchall", span_type="sql", resource=q, error=0),
                ),
            )
            assert_is_measured(self.get_spans()[1])

    def test_commit(self):
        connection = self._given_a_traced_connection(self.tracer)
        connection.commit()
        self.assertEqual(len(self.spans), 1)
        span = self.spans[0]
        self.assertEqual(span.service, "sqlite")
        self.assertEqual(span.name, "sqlite.connection.commit")

    def test_rollback(self):
        connection = self._given_a_traced_connection(self.tracer)
        connection.rollback()
        self.assert_structure(
            dict(name="sqlite.connection.rollback", service="sqlite"),
        )

    def test_patch_unpatch(self):
        # Test patch idempotence
        patch()
        patch()

        db = sqlite3.connect(":memory:")
        pin = Pin.get_from(db)
        assert pin
        pin._clone(tracer=self.tracer).onto(db)
        db.cursor().execute("select 'blah'").fetchall()

        self.assert_structure(
            dict(name="sqlite.query"),
        )
        self.reset()

        # Test unpatch
        unpatch()

        db = sqlite3.connect(":memory:")
        db.cursor().execute("select 'blah'").fetchall()

        self.assert_has_no_spans()

        # Test patch again
        patch()

        db = sqlite3.connect(":memory:")
        pin = Pin.get_from(db)
        assert pin
        pin._clone(tracer=self.tracer).onto(db)
        db.cursor().execute("select 'blah'").fetchall()

        self.assert_structure(
            dict(name="sqlite.query"),
        )

    def _given_a_traced_connection(self, tracer):
        db = sqlite3.connect(":memory:")
        Pin.get_from(db)._clone(tracer=tracer).onto(db)
        return db

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_SERVICE="mysvc", DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v0"))
    def test_app_service_v0(self):
        """
        v0: When a user specifies a service for the app
            The sqlite3 integration should not use it.
        """
        # Ensure that the service name was configured
        from ddtrace import config

        assert config.service == "mysvc"

        q = "select * from sqlite_master"
        connection = self._given_a_traced_connection(self.tracer)
        cursor = connection.execute(q)
        cursor.fetchall()

        spans = self.get_spans()

        self.assertEqual(len(spans), 1)
        span = spans[0]
        assert span.service != "mysvc"

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_SERVICE="mysvc", DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v1"))
    def test_app_service_v1(self):
        """
        v1: When a user specifies a service for the app
            The sqlite3 integration should use it.
        """
        # Ensure that the service name was configured
        from ddtrace import config

        assert config.service == "mysvc"

        q = "select * from sqlite_master"
        connection = self._given_a_traced_connection(self.tracer)
        cursor = connection.execute(q)
        cursor.fetchall()

        spans = self.get_spans()

        self.assertEqual(len(spans), 1)
        span = spans[0]
        assert span.service == "mysvc"

    @TracerTestCase.run_in_subprocess(
        env_overrides=dict(DD_SQLITE_SERVICE="my-svc", DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v0")
    )
    def test_user_specified_service_v0(self):
        q = "select * from sqlite_master"
        connection = self._given_a_traced_connection(self.tracer)
        cursor = connection.execute(q)
        cursor.fetchall()

        spans = self.get_spans()

        self.assertEqual(len(spans), 1)
        span = spans[0]
        assert span.service == "my-svc"

    @TracerTestCase.run_in_subprocess(
        env_overrides=dict(DD_SQLITE_SERVICE="my-svc", DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v1")
    )
    def test_user_specified_service_v1(self):
        q = "select * from sqlite_master"
        connection = self._given_a_traced_connection(self.tracer)
        cursor = connection.execute(q)
        cursor.fetchall()

        spans = self.get_spans()

        self.assertEqual(len(spans), 1)
        span = spans[0]
        assert span.service == "my-svc"

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v1"))
    def test_unspecified_service_v1(self):
        q = "select * from sqlite_master"
        connection = self._given_a_traced_connection(self.tracer)
        cursor = connection.execute(q)
        cursor.fetchall()

        spans = self.get_spans()

        self.assertEqual(len(spans), 1)
        span = spans[0]
        assert span.service == DEFAULT_SPAN_SERVICE_NAME

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v0"))
    def test_span_name_v0_schema(self):
        q = "select * from sqlite_master"
        connection = self._given_a_traced_connection(self.tracer)
        cursor = connection.execute(q)
        cursor.fetchall()

        spans = self.get_spans()

        self.assertEqual(len(spans), 1)
        span = spans[0]
        assert span.name == "sqlite.query"

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v1"))
    def test_span_name_v1_schema(self):
        q = "select * from sqlite_master"
        connection = self._given_a_traced_connection(self.tracer)
        cursor = connection.execute(q)
        cursor.fetchall()

        spans = self.get_spans()

        self.assertEqual(len(spans), 1)
        span = spans[0]
        assert span.name == "sqlite.query"

    def test_context_manager(self):
        conn = self._given_a_traced_connection(self.tracer)
        with conn as conn2:
            cursor = conn2.execute("select * from sqlite_master")
            cursor.fetchall()
            cursor.fetchall()
            spans = self.get_spans()
            assert len(spans) == 1


def test_iterator_usage(patched_conn):
    """Ensure sqlite3 patched cursors can be used as iterators."""
    rows = next(patched_conn.execute("select 1"))
    assert len(rows) == 1


def test_backup(patched_conn):
    """Ensure sqlite3 patched connections backup function can be used"""
    destination = sqlite3.connect(":memory:")

    with destination:
        patched_conn.backup(destination, pages=1)
