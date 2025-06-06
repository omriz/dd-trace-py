import typing as t

from ddtrace.contrib.internal.pytest._utils import _get_pytest_version_tuple


if _get_pytest_version_tuple() >= (7, 0, 0):
    from pytest import CallInfo as pytest_CallInfo  # noqa: F401
    from pytest import Config as pytest_Config  # noqa: F401
    from pytest import TestReport as pytest_TestReport  # noqa: F401
else:
    from _pytest.config import Config as pytest_Config  # noqa: F401
    from _pytest.reports import TestReport as pytest_TestReport  # noqa: F401
    from _pytest.runner import CallInfo as pytest_CallInfo  # noqa: F401

_pytest_report_teststatus_return_type = t.Optional[t.Tuple[str, str, t.Tuple[str, t.Mapping[str, bool]]]]

if _get_pytest_version_tuple() >= (7, 4, 0):
    from _pytest.tmpdir import tmppath_result_key  # noqa: F401
else:
    tmppath_result_key = None
