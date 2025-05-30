import subprocess
import sys

import pytest
import requests
from requests import Session
from requests.exceptions import InvalidURL
from requests.exceptions import MissingSchema

from ddtrace import config
from ddtrace.constants import ERROR_MSG
from ddtrace.constants import ERROR_STACK
from ddtrace.constants import ERROR_TYPE
from ddtrace.contrib.internal.requests.connection import _extract_hostname_and_path
from ddtrace.contrib.internal.requests.connection import _extract_query_string
from ddtrace.contrib.internal.requests.patch import patch
from ddtrace.contrib.internal.requests.patch import unpatch
from ddtrace.ext import http
from ddtrace.internal.schema import DEFAULT_SPAN_SERVICE_NAME
from ddtrace.trace import Pin
from tests.opentracer.utils import init_tracer
from tests.utils import TracerTestCase
from tests.utils import assert_is_measured
from tests.utils import assert_span_http_status_code
from tests.utils import override_global_tracer


HOST_AND_PORT = "localhost:8001"
SOCKET = HOST_AND_PORT.split(":")[0]
URL_200 = "http://{}/status/200".format(HOST_AND_PORT)
URL_500 = "http://{}/status/500".format(HOST_AND_PORT)
URL_AUTH_200 = "http://user:pass@{}/status/200".format(HOST_AND_PORT)


class BaseRequestTestCase(object):
    """Create a traced Session, patching during the setUp and
    unpatching after the tearDown
    """

    def setUp(self):
        super(BaseRequestTestCase, self).setUp()

        patch()
        self.session = Session()
        self.session.datadog_tracer = self.tracer

    def tearDown(self):
        unpatch()

        super(BaseRequestTestCase, self).tearDown()


class TestRequests(BaseRequestTestCase, TracerTestCase):
    def test_resource_path(self):
        out = self.session.get(URL_200)
        assert out.status_code == 200
        spans = self.pop_spans()
        assert len(spans) == 1
        s = spans[0]
        assert s.get_tag("http.url") == URL_200
        assert s.get_tag("component") == "requests"
        assert s.get_tag("span.kind") == "client"
        assert s.get_tag("out.host") == SOCKET

    def test_tracer_disabled(self):
        # ensure all valid combinations of args / kwargs work
        self.tracer.enabled = False
        out = self.session.get(URL_200)
        assert out.status_code == 200
        spans = self.pop_spans()
        assert len(spans) == 0

    def test_args_kwargs(self):
        # ensure all valid combinations of args / kwargs work
        url = URL_200
        method = "GET"
        inputs = [
            ([], {"method": method, "url": url}),
            ([method], {"url": url}),
            ([method, url], {}),
        ]

        for args, kwargs in inputs:
            # ensure a traced request works with these args
            out = self.session.request(*args, **kwargs)
            assert out.status_code == 200
            # validation
            spans = self.pop_spans()
            assert len(spans) == 1
            s = spans[0]
            assert s.get_tag(http.METHOD) == "GET"
            assert s.get_tag("component") == "requests"
            assert s.get_tag("span.kind") == "client"
            assert s.get_tag("out.host") == SOCKET
            assert_span_http_status_code(s, 200)

    def test_untraced_request(self):
        # ensure the unpatch removes tracing
        unpatch()
        untraced = Session()

        out = untraced.get(URL_200)
        assert out.status_code == 200
        # validation
        spans = self.pop_spans()
        assert len(spans) == 0

    def test_double_patch(self):
        # ensure that double patch doesn't duplicate instrumentation
        patch()
        session = Session()
        session.datadog_tracer = self.tracer

        out = session.get(URL_200)
        assert out.status_code == 200
        spans = self.pop_spans()
        assert len(spans) == 1

    def test_200(self):
        out = self.session.get(URL_200)
        assert out.status_code == 200
        # validation
        spans = self.pop_spans()
        assert len(spans) == 1
        s = spans[0]

        assert_is_measured(s)
        assert s.get_tag(http.METHOD) == "GET"
        assert s.get_tag("component") == "requests"
        assert s.get_tag("span.kind") == "client"
        assert s.get_tag("out.host") == SOCKET
        assert_span_http_status_code(s, 200)
        assert s.error == 0
        assert s.span_type == "http"
        assert http.QUERY_STRING not in s.get_tags()
        assert s.resource == "GET /status/200"

    def test_auth_200(self):
        self.session.get(URL_AUTH_200)
        spans = self.pop_spans()
        assert len(spans) == 1
        s = spans[0]
        assert s.get_tag(http.URL) == URL_200
        assert s.resource == "GET /status/200"

    def test_200_send(self):
        # when calling send directly
        req = requests.Request(url=URL_200, method="GET")
        req = self.session.prepare_request(req)

        out = self.session.send(req)
        assert out.status_code == 200
        # validation
        spans = self.pop_spans()
        assert len(spans) == 1
        s = spans[0]

        assert_is_measured(s)
        assert s.get_tag(http.METHOD) == "GET"
        assert s.get_tag("component") == "requests"
        assert s.get_tag("span.kind") == "client"
        assert s.get_tag("out.host") == SOCKET
        assert_span_http_status_code(s, 200)
        assert s.error == 0
        assert s.span_type == "http"
        assert s.resource == "GET /status/200"

    def test_200_query_string(self):
        # ensure query string is removed before adding url to metadata
        query_string = "key=value&key2=value2"
        with self.override_http_config("requests", dict(trace_query_string=True)):
            out = self.session.get(URL_200 + "?" + query_string)
        assert out.status_code == 200
        # validation
        spans = self.pop_spans()
        assert len(spans) == 1
        s = spans[0]

        assert_is_measured(s)
        assert s.get_tag(http.METHOD) == "GET"
        assert_span_http_status_code(s, 200)
        assert s.get_tag(http.URL) == URL_200 + "?" + query_string
        assert s.error == 0
        assert s.span_type == "http"
        assert s.get_tag(http.QUERY_STRING) == query_string
        assert s.get_tag("component") == "requests"
        assert s.get_tag("span.kind") == "client"
        assert s.get_tag("out.host") == SOCKET
        assert s.resource == "GET /status/200"

    def test_requests_module_200(self):
        # ensure the requests API is instrumented even without
        # using a `Session` directly
        with override_global_tracer(self.tracer):
            out = requests.get(URL_200)
            assert out.status_code == 200
            # validation
            spans = self.pop_spans()
            assert len(spans) == 1
            s = spans[0]

            assert_is_measured(s)
            assert s.get_tag(http.METHOD) == "GET"
            assert s.get_tag("component") == "requests"
            assert s.get_tag("span.kind") == "client"
            assert s.get_tag("out.host") == SOCKET
            assert_span_http_status_code(s, 200)
            assert s.error == 0
            assert s.span_type == "http"
            assert s.resource == "GET /status/200"

    def test_post_500(self):
        out = self.session.post(URL_500)
        # validation
        assert out.status_code >= 500
        spans = self.pop_spans()
        assert len(spans) == 1
        s = spans[0]

        assert_is_measured(s)
        assert s.get_tag(http.METHOD) == "POST"
        assert s.get_tag("component") == "requests"
        assert s.get_tag("span.kind") == "client"
        assert s.get_tag("out.host") == SOCKET
        assert_span_http_status_code(s, 500)
        assert s.error == 1
        assert s.resource == "POST /status/500"

    def test_non_existant_url(self):
        try:
            self.session.get("http://doesnotexist.google.com")
        except Exception:
            pass
        else:
            assert 0, "expected error"

        spans = self.pop_spans()
        assert len(spans) == 1
        s = spans[0]

        assert_is_measured(s)
        assert s.get_tag(http.METHOD) == "GET"
        assert s.get_tag("component") == "requests"
        assert s.get_tag("span.kind") == "client"
        assert s.get_tag("out.host") == "doesnotexist.google.com"
        assert s.error == 1
        assert (
            "HTTPConnectionPool(host='doesnotexist.google.com', port=80): Max retries exceeded with url: /"
            in s.get_tag(ERROR_MSG)
        )
        assert s.get_tag(ERROR_STACK)
        assert "requests.exception" in s.get_tag(ERROR_TYPE)

    def test_500(self):
        out = self.session.get(URL_500)
        assert out.status_code >= 500

        spans = self.pop_spans()
        assert len(spans) == 1
        s = spans[0]

        assert_is_measured(s)
        assert s.get_tag(http.METHOD) == "GET"
        assert s.get_tag("component") == "requests"
        assert s.get_tag("span.kind") == "client"
        assert s.get_tag("out.host") == SOCKET
        assert_span_http_status_code(s, 500)
        assert s.error == 1
        assert s.resource == "GET /status/500"

    def test_default_service_name(self):
        # ensure a default service name is set
        out = self.session.get(URL_200)
        assert out.status_code == 200

        spans = self.pop_spans()
        assert len(spans) == 1
        s = spans[0]

        assert s.service == "requests"
        assert s.resource == "GET /status/200"

    def test_user_set_service_name(self):
        # ensure a service name set by the user has precedence
        cfg = Pin._get_config(self.session)
        cfg["service_name"] = "clients"
        out = self.session.get(URL_200)
        assert out.status_code == 200
        spans = self.pop_spans()
        assert len(spans) == 1
        s = spans[0]
        assert s.service == "clients"

    def test_user_set_service(self):
        # ensure a service name set by the user has precedence
        cfg = Pin._get_config(self.session)
        cfg["service"] = "clients"
        out = self.session.get(URL_200)
        assert out.status_code == 200
        spans = self.pop_spans()
        assert len(spans) == 1
        s = spans[0]
        assert s.service == "clients"

    def test_parent_service_name_precedence(self):
        # span should not inherit the parent's service name
        # as all outbound requests should go to a difference service
        with self.tracer.trace("parent.span", service="web"):
            out = self.session.get(URL_200)
            assert out.status_code == 200

        spans = self.pop_spans()
        assert len(spans) == 2
        s = spans[1]

        assert s.name == "requests.request"
        assert s.service == "requests"

    def test_parent_without_service_name(self):
        # ensure the default value is used if the parent
        # doesn't have a service
        with self.tracer.trace("parent.span"):
            out = self.session.get(URL_200)
            assert out.status_code == 200

        spans = self.pop_spans()
        assert len(spans) == 2
        s = spans[1]

        assert s.name == "requests.request"
        assert s.service == "requests"
        assert s.resource == "GET /status/200"

    def test_user_service_name_precedence(self):
        # ensure the user service name takes precedence over
        # the parent Span
        cfg = Pin._get_config(self.session)
        cfg["service_name"] = "clients"
        with self.tracer.trace("parent.span", service="web"):
            out = self.session.get(URL_200)
            assert out.status_code == 200

        spans = self.pop_spans()
        assert len(spans) == 2
        s = spans[1]

        assert s.name == "requests.request"
        assert s.service == "clients"

    def test_split_by_domain_with_ampersat(self):
        # Regression test for: https://github.com/DataDog/dd-trace-py/issues/4062
        # ensure a service name is generated by the domain name
        cfg = Pin._get_config(self.session)
        cfg["split_by_domain"] = True
        # domain name should take precedence over monkey_service
        cfg["service_name"] = "monkey_service"
        url = URL_200 + "?email=monkey_monkey@zoo_mail.ca"

        out = self.session.get(url)
        assert out.status_code == 200

        spans = self.pop_spans()
        assert len(spans) == 1
        s = spans[0]

        assert s.get_tag("out.host") == SOCKET
        assert s.service == HOST_AND_PORT

    def test_split_by_domain(self):
        # ensure a service name is generated by the domain name
        # of the ongoing call
        cfg = Pin._get_config(self.session)
        cfg["split_by_domain"] = True
        out = self.session.get(URL_200)
        assert out.status_code == 200

        spans = self.pop_spans()
        assert len(spans) == 1
        s = spans[0]

        assert s.get_tag("out.host") == SOCKET
        assert s.service == HOST_AND_PORT
        assert s.resource == "GET /status/200"

    def test_split_by_domain_precedence(self):
        # ensure the split by domain has precedence all the time
        cfg = Pin._get_config(self.session)
        cfg["split_by_domain"] = True
        cfg["service_name"] = "intake"
        out = self.session.get(URL_200)
        assert out.status_code == 200

        spans = self.pop_spans()
        assert len(spans) == 1
        s = spans[0]

        assert s.get_tag("out.host") == SOCKET
        assert s.service == HOST_AND_PORT
        assert s.resource == "GET /status/200"

    def test_split_by_domain_wrong(self):
        # ensure the split by domain doesn't crash in case of a wrong URL;
        # in that case, no spans are created
        cfg = Pin._get_config(self.session)
        cfg["split_by_domain"] = True
        with pytest.raises((MissingSchema, InvalidURL)):
            self.session.get("http:/some>thing")

        # We are wrapping `requests.Session.send` and this error gets thrown before that function
        spans = self.pop_spans()
        assert len(spans) == 0

    def test_split_by_domain_remove_auth_in_url(self):
        # ensure that auth details are stripped from URL
        cfg = Pin._get_config(self.session)
        cfg["split_by_domain"] = True
        out = self.session.get(f"http://user:pass@{HOST_AND_PORT}")
        assert out.status_code == 200

        spans = self.pop_spans()
        assert len(spans) == 1
        s = spans[0]

        assert s.get_tag("out.host") == SOCKET
        assert s.service == HOST_AND_PORT

    def test_split_by_domain_includes_port(self):
        # ensure that port is included if present in URL
        cfg = Pin._get_config(self.session)
        cfg["split_by_domain"] = True
        out = self.session.get(f"http://{HOST_AND_PORT}")
        assert out.status_code == 200

        spans = self.pop_spans()
        assert len(spans) == 1
        s = spans[0]

        assert s.get_tag("out.host") == SOCKET
        assert s.service == HOST_AND_PORT

    def test_split_by_domain_includes_port_path(self):
        # ensure that port is included if present in URL but not path
        cfg = Pin._get_config(self.session)
        cfg["split_by_domain"] = True
        out = self.session.get(f"http://{HOST_AND_PORT}/anything/v1/foo")
        assert out.status_code == 200

        spans = self.pop_spans()
        assert len(spans) == 1
        s = spans[0]

        assert s.get_tag("out.host") == SOCKET
        assert s.service == HOST_AND_PORT

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_REQUESTS_SERVICE="override"))
    def test_global_config_service_env_precedence(self):
        out = self.session.get(URL_200)
        assert out.status_code == 200
        spans = self.pop_spans()
        assert spans[0].service == "override"

        cfg = Pin._get_config(self.session)
        cfg["service"] = "override2"
        out = self.session.get(URL_200)
        assert out.status_code == 200
        spans = self.pop_spans()
        assert spans[0].service == "override2"

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_REQUESTS_SERVICE="override"))
    def test_global_config_service_env(self):
        out = self.session.get(URL_200)
        assert out.status_code == 200
        spans = self.pop_spans()
        assert spans[0].service == "override"

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_SERVICE="mysvc"))
    def test_schematization_service_name_default(self):
        out = self.session.get(URL_200)
        assert out.status_code == 200
        spans = self.pop_spans()
        assert spans[0].service == "requests"

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_SERVICE="mysvc", DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v0"))
    def test_schematization_service_name_v0(self):
        out = self.session.get(URL_200)
        assert out.status_code == 200
        spans = self.pop_spans()
        assert spans[0].service == "requests"

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_SERVICE="mysvc", DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v1"))
    def test_schematization_service_name_v1(self):
        out = self.session.get(URL_200)
        assert out.status_code == 200
        spans = self.pop_spans()
        assert spans[0].service == "mysvc"

    @TracerTestCase.run_in_subprocess()
    def test_schematization_unspecified_service_name_default(self):
        out = self.session.get(URL_200)
        assert out.status_code == 200
        spans = self.pop_spans()
        assert spans[0].service == "requests"

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v0"))
    def test_schematization_unspecified_service_name_v0(self):
        out = self.session.get(URL_200)
        assert out.status_code == 200
        spans = self.pop_spans()
        assert spans[0].service == "requests"

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v1"))
    def test_schematization_unspecified_service_name_v1(self):
        out = self.session.get(URL_200)
        assert out.status_code == 200
        spans = self.pop_spans()
        assert spans[0].service == DEFAULT_SPAN_SERVICE_NAME

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v0"))
    def test_schematization_operation_name_v0(self):
        out = self.session.get(URL_200)
        assert out.status_code == 200
        spans = self.pop_spans()
        assert spans[0].name == "requests.request"

    @TracerTestCase.run_in_subprocess(env_overrides=dict(DD_TRACE_SPAN_ATTRIBUTE_SCHEMA="v1"))
    def test_schematization_operation_name_v1(self):
        out = self.session.get(URL_200)
        assert out.status_code == 200
        spans = self.pop_spans()
        assert spans[0].name == "http.client.request"

    def test_global_config_service(self):
        with self.override_config("requests", dict(service="override")):
            out = self.session.get(URL_200)
        assert out.status_code == 200
        spans = self.pop_spans()
        assert spans[0].service == "override"

    def test_200_ot(self):
        """OpenTracing version of test_200."""

        ot_tracer = init_tracer("requests_svc", self.tracer)

        with ot_tracer.start_active_span("requests_get"):
            out = self.session.get(URL_200)
            assert out.status_code == 200

        # validation
        spans = self.pop_spans()
        assert len(spans) == 2

        ot_span, dd_span = spans

        # confirm the parenting
        assert ot_span.parent_id is None
        assert dd_span.parent_id == ot_span.span_id

        assert ot_span.name == "requests_get"
        assert ot_span.service == "requests_svc"

        assert_is_measured(dd_span)
        assert dd_span.get_tag(http.METHOD) == "GET"
        assert_span_http_status_code(dd_span, 200)
        assert dd_span.error == 0
        assert dd_span.span_type == "http"
        assert dd_span.resource == "GET /status/200"

    def test_request_and_response_headers(self):
        # Disabled when not configured
        self.session.get(URL_200, headers={"my-header": "my_value"})
        spans = self.pop_spans()
        assert len(spans) == 1
        s = spans[0]
        assert s.get_tag("http.request.headers.my-header") is None
        assert s.get_tag("http.response.headers.access-control-allow-origin") is None

        # Enabled when explicitly configured
        with self.override_config("requests", {}):
            config.requests.http.trace_headers(["my-header"])
            self.session.get(URL_200, headers={"my-header": "my_value"})
            spans = self.pop_spans()
        assert len(spans) == 1
        s = spans[0]
        assert s.get_tag("http.request.headers.my-header") == "my_value"


def test_traced_session_no_patch_all(tmpdir):
    f = tmpdir.join("test.py")
    f.write(
        """
import mock
import ddtrace
from ddtrace.contrib.requests import TracedSession

# disable tracer writing to agent
# FIXME: Remove use of this internal attribute of Tracer to disable writer
ddtrace.tracer._span_aggregator.writer.flush_queue = mock.Mock(return_value=None)

session = TracedSession()
session.get("http://httpbin.org/status/200")
""".lstrip()
    )
    p = subprocess.Popen(
        [sys.executable, "test.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(tmpdir),
    )
    p.wait()
    assert p.stderr.read() == b""
    assert p.stdout.read() == b""
    assert p.returncode == 0


@pytest.mark.parametrize(
    "uri,hostname,path",
    [
        ("http://localhost:8080", "localhost:8080", ""),
        ("http://localhost:8080/", "localhost:8080", "/"),
        ("http://localhost", "localhost", ""),
        ("http://localhost/", "localhost", "/"),
        ("http://asd:wwefwf@localhost:8080", "localhost:8080", ""),
        ("http://asd:wwefwf@localhost:8080/", "localhost:8080", "/"),
        ("http://asd:wwefwf@localhost:8080/path", "localhost:8080", "/path"),
        ("http://asd:wwefwf@localhost:8080/path?query", "localhost:8080", "/path"),
        ("http://asd:wwefwf@localhost:8080/path?query#fragment", "localhost:8080", "/path"),
        ("http://asd:wwefwf@localhost:8080/path#frag?ment", "localhost:8080", "/path"),
        ("http://localhost:8080/path#frag?ment", "localhost:8080", "/path"),
        ("http://localhost/path#frag?ment", "localhost", "/path"),
    ],
)
def test_extract_hostname(uri, hostname, path):
    assert _extract_hostname_and_path(uri) == (hostname, path)


def test_extract_hostname_invalid_port():
    assert _extract_hostname_and_path("http://localhost:-1/") == ("localhost:?", "/")


@pytest.mark.parametrize(
    "uri,qs",
    [
        ("http://localhost:8080", None),
        ("http://localhost", None),
        ("http://asd:wwefwf@localhost:8080", None),
        ("http://asd:wwefwf@localhost:8080/path", None),
        ("http://asd:wwefwf@localhost:8080/path?query", "query"),
        ("http://asd:wwefwf@localhost:8080/path?query#fragment", "query"),
        ("http://asd:wwefwf@localhost:8080/path#frag?ment", None),
        ("http://localhost:8080/path#frag?ment", None),
        ("http://localhost/path#frag?ment", None),
        ("http://localhost?query", "query"),
    ],
)
def test_extract_query_string(uri, qs):
    assert _extract_query_string(uri) == qs
