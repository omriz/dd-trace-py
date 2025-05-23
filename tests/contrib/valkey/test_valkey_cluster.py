# -*- coding: utf-8 -*-
import valkey

from ddtrace.contrib.internal.valkey.patch import patch
from ddtrace.contrib.internal.valkey.patch import unpatch
from ddtrace.internal.schema import DEFAULT_SPAN_SERVICE_NAME
from ddtrace.trace import Pin
from tests.contrib.config import VALKEY_CLUSTER_CONFIG
from tests.utils import DummyTracer
from tests.utils import TracerTestCase
from tests.utils import assert_is_measured


class TestValkeyClusterPatch(TracerTestCase):
    TEST_HOST = VALKEY_CLUSTER_CONFIG["host"]
    TEST_PORTS = VALKEY_CLUSTER_CONFIG["ports"]

    def _get_test_client(self):
        startup_nodes = [valkey.cluster.ClusterNode(self.TEST_HOST, int(port)) for port in self.TEST_PORTS.split(",")]
        return valkey.cluster.ValkeyCluster(startup_nodes=startup_nodes)

    def setUp(self):
        super(TestValkeyClusterPatch, self).setUp()
        patch()
        r = self._get_test_client()
        r.flushall()
        Pin._override(r, tracer=self.tracer)
        self.r = r

    def tearDown(self):
        unpatch()
        super(TestValkeyClusterPatch, self).tearDown()

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v1"))
    def test_span_service_name_v1(self):
        us = self.r.get("cheese")
        assert us is None
        spans = self.get_spans()
        span = spans[0]
        assert span.service == DEFAULT_SPAN_SERVICE_NAME

    def test_basics(self):
        us = self.r.get("cheese")
        assert us is None
        spans = self.get_spans()
        assert len(spans) == 1
        span = spans[0]
        assert_is_measured(span)
        assert span.service == "valkey"
        assert span.name == "valkey.command"
        assert span.span_type == "valkey"
        assert span.error == 0
        assert span.get_tag("valkey.raw_command") == "GET cheese"
        assert span.get_tag("component") == "valkey"
        assert span.get_tag("db.system") == "valkey"
        assert span.get_metric("valkey.args_length") == 2
        assert span.resource == "GET"

    def test_unicode(self):
        us = self.r.get("😐")
        assert us is None
        spans = self.get_spans()
        assert len(spans) == 1
        span = spans[0]
        assert_is_measured(span)
        assert span.service == "valkey"
        assert span.name == "valkey.command"
        assert span.span_type == "valkey"
        assert span.error == 0
        assert span.get_tag("valkey.raw_command") == "GET 😐"
        assert span.get_tag("component") == "valkey"
        assert span.get_tag("db.system") == "valkey"
        assert span.get_metric("valkey.args_length") == 2
        assert span.resource == "GET"

    def test_pipeline(self):
        with self.r.pipeline(transaction=False) as p:
            p.set("blah", 32)
            p.rpush("foo", "éé")
            p.hgetall("xxx")
            p.execute()

        spans = self.get_spans()
        assert len(spans) == 1
        span = spans[0]
        assert_is_measured(span)
        assert span.service == "valkey"
        assert span.name == "valkey.command"
        assert span.resource == "SET\nRPUSH\nHGETALL"
        assert span.span_type == "valkey"
        assert span.error == 0
        assert span.get_tag("valkey.raw_command") == "SET blah 32\nRPUSH foo éé\nHGETALL xxx"
        assert span.get_tag("component") == "valkey"
        assert span.get_metric("valkey.pipeline_length") == 3

    def test_patch_unpatch(self):
        tracer = DummyTracer()

        # Test patch idempotence
        patch()
        patch()

        r = self._get_test_client()
        Pin.get_from(r)._clone(tracer=tracer).onto(r)
        r.get("key")

        spans = tracer.pop()
        assert spans, spans
        assert len(spans) == 1

        # Test unpatch
        unpatch()

        r = self._get_test_client()
        r.get("key")

        spans = tracer.pop()
        assert not spans, spans

        # Test patch again
        patch()

        r = self._get_test_client()
        Pin.get_from(r)._clone(tracer=tracer).onto(r)
        r.get("key")

        spans = tracer.pop()
        assert spans, spans
        assert len(spans) == 1

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_SERVICE="mysvc", DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v0"))
    def test_user_specified_service_v0(self):
        """
        When a user specifies a service for the app
            The valkeycluster integration should not use it.
        """
        # Ensure that the service name was configured
        from ddtrace import config

        assert config.service == "mysvc"

        r = self._get_test_client()
        Pin.get_from(r)._clone(tracer=self.tracer).onto(r)
        r.get("key")

        spans = self.get_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.service != "mysvc"

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_SERVICE="mysvc", DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v1"))
    def test_user_specified_service_v1(self):
        """
        When a user specifies a service for the app
            The valkeycluster integration should use it.
        """
        # Ensure that the service name was configured
        from ddtrace import config

        assert config.service == "mysvc"

        r = self._get_test_client()
        Pin.get_from(r)._clone(tracer=self.tracer).onto(r)
        r.get("key")

        spans = self.get_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.service == "mysvc"

    @TracerTestCase.run_in_subprocess(
        env_overrides=dict(DD_VALKEY_SERVICE="myvalkeycluster", DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v0")
    )
    def test_env_user_specified_valkeycluster_service_v0(self):
        self.r.get("cheese")
        span = self.get_spans()[0]
        assert span.service == "myvalkeycluster", span.service

    @TracerTestCase.run_in_subprocess(
        env_overrides=dict(DD_VALKEY_SERVICE="myvalkeycluster", DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v1")
    )
    def test_env_user_specified_valkeycluster_service_v1(self):
        self.r.get("cheese")
        span = self.get_spans()[0]
        assert span.service == "myvalkeycluster", span.service

    @TracerTestCase.run_in_subprocess(
        env_overrides=dict(
            DD_SERVICE="app-svc", DD_VALKEY_SERVICE="myvalkeycluster", DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v0"
        )
    )
    def test_service_precedence_v0(self):
        self.r.get("cheese")
        span = self.get_spans()[0]
        assert span.service == "myvalkeycluster"

        self.reset()

    @TracerTestCase.run_in_subprocess(
        env_overrides=dict(
            DD_SERVICE="app-svc", DD_VALKEY_SERVICE="myvalkeycluster", DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v1"
        )
    )
    def test_service_precedence_v1(self):
        self.r.get("cheese")
        span = self.get_spans()[0]
        assert span.service == "myvalkeycluster"

        self.reset()
