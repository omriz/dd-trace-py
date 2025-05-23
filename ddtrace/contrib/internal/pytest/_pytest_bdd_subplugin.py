"""Provides functionality to support the pytest-bdd plugin as part of the ddtrace integration

NOTE: This replaces the previous ddtrace.pytest_bdd plugin.

This plugin mainly modifies the names of the test, its suite, and parameters. It does not, however modify the tests'
suite from the perspective of Test Visibility data.

The plugin is only instantiated and added if the pytest-bdd plugin itself is installed and enabled, because the hook
implementations will cause errors unless the hookspecs are added by the original plugin.
"""
from pathlib import Path
import sys

import pytest

from ddtrace.contrib.internal.pytest._utils import _get_test_id_from_item
from ddtrace.contrib.internal.pytest_bdd._plugin import _extract_span
from ddtrace.contrib.internal.pytest_bdd._plugin import _get_step_func_args_json
from ddtrace.contrib.internal.pytest_bdd._plugin import _store_span
from ddtrace.contrib.internal.pytest_bdd.constants import FRAMEWORK
from ddtrace.contrib.internal.pytest_bdd.constants import STEP_KIND
from ddtrace.contrib.internal.pytest_bdd.patch import get_version
from ddtrace.ext import test
from ddtrace.internal.logger import get_logger
from ddtrace.internal.test_visibility.api import InternalTest
from ddtrace.internal.test_visibility.api import InternalTestSession


log = get_logger(__name__)


def _get_workspace_relative_path(feature_path_str: str) -> Path:
    feature_path = Path(feature_path_str).resolve()
    workspace_path = InternalTestSession.get_workspace_path()
    if workspace_path:
        try:
            return feature_path.relative_to(workspace_path)
        except ValueError:  # noqa: E722
            log.debug("Feature path %s is not relative to workspace path %s", feature_path, workspace_path)
    return feature_path


class _PytestBddSubPlugin:
    def __init__(self):
        self.framework_version = get_version()

    @staticmethod
    @pytest.hookimpl(tryfirst=True)
    def pytest_bdd_before_scenario(request, feature, scenario):
        test_id = _get_test_id_from_item(request.node)
        feature_path = _get_workspace_relative_path(scenario.feature.filename)
        codeowners = InternalTestSession.get_path_codeowners(feature_path)

        InternalTest.overwrite_attributes(
            test_id, name=scenario.name, suite_name=str(feature_path), codeowners=codeowners
        )

    @pytest.hookimpl(tryfirst=True)
    def pytest_bdd_before_step(self, request, feature, scenario, step, step_func):
        feature_test_id = _get_test_id_from_item(request.node)

        feature_span = InternalTest.get_span(feature_test_id)

        tracer = InternalTestSession.get_tracer()
        if tracer is None:
            return

        span = tracer.start_span(
            step.type,
            resource=step.name,
            span_type=STEP_KIND,
            child_of=feature_span,
            activate=True,
        )
        span.set_tag_str("component", "pytest_bdd")

        span.set_tag(test.FRAMEWORK, FRAMEWORK)
        span.set_tag(test.FRAMEWORK_VERSION, self.framework_version)

        feature_path = _get_workspace_relative_path(scenario.feature.filename)

        span.set_tag(test.FILE, str(feature_path))
        span.set_tag(test.CODEOWNERS, InternalTestSession.get_path_codeowners(feature_path))

        _store_span(step_func, span)

    @staticmethod
    @pytest.hookimpl(trylast=True)
    def pytest_bdd_after_step(request, feature, scenario, step, step_func, step_func_args):
        span = _extract_span(step_func)
        if span is not None:
            step_func_args_json = _get_step_func_args_json(step, step_func, step_func_args)
            if step_func_args:
                span.set_tag(test.PARAMETERS, step_func_args_json)
            span.finish()

    @staticmethod
    def pytest_bdd_step_error(request, feature, scenario, step, step_func, step_func_args, exception):
        span = _extract_span(step_func)
        if span is not None:
            if hasattr(exception, "__traceback__"):
                tb = exception.__traceback__
            else:
                # PY2 compatibility workaround
                _, _, tb = sys.exc_info()
            step_func_args_json = _get_step_func_args_json(step, step_func, step_func_args)
            if step_func_args:
                span.set_tag(test.PARAMETERS, step_func_args_json)
            span.set_exc_info(type(exception), exception, tb)
            span.finish()
