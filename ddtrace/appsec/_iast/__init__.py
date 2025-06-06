"""IAST (Interactive Application Security Testing) analyzes code for security vulnerabilities.

To add new vulnerabilities analyzers (Taint sink) we should update `IAST_PATCH` in
`ddtrace/appsec/iast/_patch_modules.py`

Create new file with the same name: `ddtrace/appsec/iast/taint_sinks/[my_new_vulnerability].py`

Then, implement the `patch()` function and its wrappers.

In order to have the better performance, the Overhead control engine (OCE) helps us to control the overhead of our
wrapped functions. We should create a class that inherit from `ddtrace.appsec._iast.taint_sinks._base.VulnerabilityBase`
and register with `ddtrace.appsec._iast.oce`.

@oce.register
class MyVulnerability(VulnerabilityBase):
    vulnerability_type = "MyVulnerability"
    evidence_type = "kind_of_Vulnerability"

Before that, we should decorate our wrappers with `wrap` method and
report the vulnerabilities with `report` method. OCE will manage the number of requests, number of vulnerabilities
to reduce the overhead.

@WeakHash.wrap
def wrapped_function(wrapped, instance, args, kwargs):
    WeakHash.report(
        evidence_value=evidence,
    )
    return wrapped(*args, **kwargs)
"""  # noqa: RST201, RST213, RST210

import inspect
import os
import sys
import types

from ddtrace.internal.logger import get_logger
from ddtrace.internal.module import ModuleWatchdog
from ddtrace.settings.asm import config as asm_config

from ._listener import iast_listen
from ._overhead_control_engine import oce


log = get_logger(__name__)

_IAST_TO_BE_LOADED = True
_iast_propagation_enabled = False


def ddtrace_iast_flask_patch():
    """
    Patch the code inside the Flask main app source code file (typically "app.py") so
    Runtime Code Analysis (IAST) works also for the functions and methods defined inside it.
    This must be called on the top level or inside the `if __name__ == "__main__"`
    and must be before the `app.run()` call. It also requires `DD_IAST_ENABLED` to be
    activated.
    """
    if not asm_config._iast_enabled:
        return

    from ._ast.ast_patching import astpatch_module

    module_name = inspect.currentframe().f_back.f_globals["__name__"]
    module = sys.modules[module_name]
    try:
        module_path, patched_ast = astpatch_module(module)
    except Exception:
        log.debug("Unexpected exception while AST patching", exc_info=True)
        return

    if not patched_ast:
        log.debug("Main flask module not patched, probably it was not needed")
        return

    compiled_code = compile(patched_ast, module_path, "exec")
    # creating a new module environment to execute the patched code from scratch
    new_module = types.ModuleType(module_name)
    module.__dict__.clear()
    module.__dict__.update(new_module.__dict__)
    # executing the compiled code in the new module environment
    exec(compiled_code, module.__dict__)  # nosec B102


def enable_iast_propagation():
    """Add IAST AST patching in the ModuleWatchdog"""
    # DEV: These imports are here to avoid _ast.ast_patching import in the top level
    # because they are slow and affect serverless startup time
    from ddtrace.appsec._iast._ast.ast_patching import _should_iast_patch
    from ddtrace.appsec._iast._loader import _exec_iast_patched_module

    global _iast_propagation_enabled
    if _iast_propagation_enabled:
        return

    log.debug("iast::instrumentation::starting IAST")
    ModuleWatchdog.register_pre_exec_module_hook(_should_iast_patch, _exec_iast_patched_module)
    _iast_propagation_enabled = True


def _iast_pytest_activation():
    global _iast_propagation_enabled
    if _iast_propagation_enabled:
        return
    os.environ["DD_IAST_ENABLED"] = os.environ.get("DD_IAST_ENABLED") or "1"
    os.environ["_DD_IAST_USE_ROOT_SPAN"] = os.environ.get("_DD_IAST_USE_ROOT_SPAN") or "true"
    os.environ["DD_IAST_REQUEST_SAMPLING"] = os.environ.get("DD_IAST_REQUEST_SAMPLING") or "100.0"
    os.environ["_DD_APPSEC_DEDUPLICATION_ENABLED"] = os.environ.get("_DD_APPSEC_DEDUPLICATION_ENABLED") or "false"
    os.environ["DD_IAST_VULNERABILITIES_PER_REQUEST"] = os.environ.get("DD_IAST_VULNERABILITIES_PER_REQUEST") or "1000"
    os.environ["DD_IAST_MAX_CONCURRENT_REQUESTS"] = os.environ.get("DD_IAST_MAX_CONCURRENT_REQUESTS") or "1000"

    asm_config._iast_request_sampling = 100.0
    asm_config._deduplication_enabled = False
    asm_config._iast_max_vulnerabilities_per_requests = 1000
    asm_config._iast_max_concurrent_requests = 1000
    enable_iast_propagation()
    oce.reconfigure()


def disable_iast_propagation():
    """Remove IAST AST patching from the ModuleWatchdog. Only for testing proposes"""
    # DEV: These imports are here to avoid _ast.ast_patching import in the top level
    # because they are slow and affect serverless startup time
    from ddtrace.appsec._iast._ast.ast_patching import _should_iast_patch
    from ddtrace.appsec._iast._loader import _exec_iast_patched_module

    global _iast_propagation_enabled
    if not _iast_propagation_enabled:
        return
    try:
        ModuleWatchdog.remove_pre_exec_module_hook(_should_iast_patch, _exec_iast_patched_module)
    except KeyError:
        log.warning("IAST is already disabled and it's not in the ModuleWatchdog")
    _iast_propagation_enabled = False


__all__ = [
    "ddtrace_iast_flask_patch",
    "enable_iast_propagation",
    "disable_iast_propagation",
]


def load_iast():
    """Lazily load the iast module listeners."""
    global _IAST_TO_BE_LOADED
    if _IAST_TO_BE_LOADED:
        iast_listen()
        _IAST_TO_BE_LOADED = False
