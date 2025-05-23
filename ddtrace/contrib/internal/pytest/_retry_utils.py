from contextlib import contextmanager
from dataclasses import dataclass
import typing as t

import _pytest
from _pytest.logging import caplog_handler_key
from _pytest.logging import caplog_records_key
from _pytest.runner import CallInfo
import pytest

from ddtrace.contrib.internal.pytest._types import pytest_TestReport
from ddtrace.contrib.internal.pytest._types import tmppath_result_key
from ddtrace.contrib.internal.pytest._utils import _TestOutcome
from ddtrace.ext.test_visibility.api import TestExcInfo
from ddtrace.ext.test_visibility.api import TestStatus
from ddtrace.internal import core


@dataclass(frozen=True)
class RetryOutcomes:
    PASSED: str
    FAILED: str
    SKIPPED: str
    XFAIL: str
    XPASS: str


def get_retry_num(nodeid: str) -> t.Optional[int]:
    with core.context_with_data(f"dd-pytest-retry-{nodeid}") as ctx:
        return ctx.get_item("retry_num")


@contextmanager
def set_retry_num(nodeid: str, retry_num: int):
    with core.context_with_data(f"dd-pytest-retry-{nodeid}") as ctx:
        ctx.set_item("retry_num", retry_num)
        yield


def _get_retry_attempt_string(nodeid) -> str:
    retry_number = get_retry_num(nodeid)
    return "ATTEMPT {} ".format(retry_number) if retry_number is not None else "INITIAL ATTEMPT "


def _get_outcome_from_retry(
    item: pytest.Item,
    outcomes: RetryOutcomes,
) -> _TestOutcome:
    _outcome_status: t.Optional[TestStatus] = None
    _outcome_skip_reason: t.Optional[str] = None
    _outcome_exc_info: t.Optional[TestExcInfo] = None

    # _initrequest() needs to be called first because the test has already executed once
    item._initrequest()

    # Reset output capture across retries.
    item._report_sections = []

    # Setup
    setup_call, setup_report = _retry_run_when(item, "setup", outcomes)
    if setup_report.outcome == outcomes.FAILED:
        _outcome_status = TestStatus.FAIL
        if setup_call.excinfo is not None:
            _outcome_exc_info = TestExcInfo(setup_call.excinfo.type, setup_call.excinfo.value, setup_call.excinfo.tb)
            item.stash[caplog_records_key] = {}
            item.stash[caplog_handler_key] = {}
            if tmppath_result_key is not None:
                item.stash[tmppath_result_key] = {}
    if setup_report.outcome == outcomes.SKIPPED:
        _outcome_status = TestStatus.SKIP

    # Call
    if setup_report.outcome == outcomes.PASSED:
        call_call, call_report = _retry_run_when(item, "call", outcomes)
        if call_report.outcome == outcomes.FAILED:
            _outcome_status = TestStatus.FAIL
            if call_call.excinfo is not None:
                _outcome_exc_info = TestExcInfo(call_call.excinfo.type, call_call.excinfo.value, call_call.excinfo.tb)
                item.stash[caplog_records_key] = {}
                item.stash[caplog_handler_key] = {}
                if tmppath_result_key is not None:
                    item.stash[tmppath_result_key] = {}
        elif call_report.outcome == outcomes.SKIPPED:
            _outcome_status = TestStatus.SKIP
        elif call_report.outcome == outcomes.PASSED:
            _outcome_status = TestStatus.PASS
    # Teardown does not happen if setup skipped
    if not setup_report.skipped:
        teardown_call, teardown_report = _retry_run_when(item, "teardown", outcomes)
        # Only override the outcome if the teardown failed, otherwise defer to either setup or call outcome
        if teardown_report.outcome == outcomes.FAILED:
            _outcome_status = TestStatus.FAIL
            if teardown_call.excinfo is not None:
                _outcome_exc_info = TestExcInfo(
                    teardown_call.excinfo.type, teardown_call.excinfo.value, teardown_call.excinfo.tb
                )
                item.stash[caplog_records_key] = {}
                item.stash[caplog_handler_key] = {}
                if tmppath_result_key is not None:
                    item.stash[tmppath_result_key] = {}

    item._initrequest()

    return _TestOutcome(status=_outcome_status, skip_reason=_outcome_skip_reason, exc_info=_outcome_exc_info)


def _retry_run_when(item, when, outcomes: RetryOutcomes) -> t.Tuple[CallInfo, _pytest.reports.TestReport]:
    hooks = {
        "setup": item.ihook.pytest_runtest_setup,
        "call": item.ihook.pytest_runtest_call,
        "teardown": item.ihook.pytest_runtest_teardown,
    }
    hook = hooks[when]
    # NOTE: we use nextitem=item here to make sure that logs don't generate a new line
    if when == "teardown":
        call = CallInfo.from_call(
            lambda: hook(item=item, nextitem=pytest.Class.from_parent(item.session, name="forced_teardown")), when=when
        )
    else:
        call = CallInfo.from_call(lambda: hook(item=item), when=when)
    report = item.ihook.pytest_runtest_makereport(item=item, call=call)
    if report.outcome == "passed":
        report.outcome = outcomes.PASSED
    elif report.outcome == "failed" or report.outcome == "error":
        report.outcome = outcomes.FAILED
    elif report.outcome == "skipped":
        report.outcome = outcomes.SKIPPED
    # Only log for actual test calls, or failures
    if when == "call" or "passed" not in report.outcome:
        item.ihook.pytest_runtest_logreport(report=report)
    return call, report


class RetryTestReport(pytest_TestReport):
    """
    A RetryTestReport behaves just like a normal pytest TestReport, except that the the failed/passed/skipped
    properties are aware of retry final states (dd_efd_final_*, etc). This affects the test counts in JUnit XML output,
    for instance.

    The object should be initialized with the `longrepr` of the _initial_ test attempt. A `longrepr` set to `None` means
    the initial attempt either succeeded (which means it was already counted by pytest) or was quarantined (which means
    we should not count it at all), so we don't need to count it here.
    """

    @property
    def failed(self):
        if self.longrepr is None:
            return False
        return "final_failed" in self.outcome

    @property
    def passed(self):
        if self.longrepr is None:
            return False
        return "final_passed" in self.outcome or "final_flaky" in self.outcome

    @property
    def skipped(self):
        if self.longrepr is None:
            return False
        return "final_skipped" in self.outcome
