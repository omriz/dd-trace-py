import itertools
import math
import os
import typing as t

from ddtrace import config as core_config
from ddtrace.ext.git import COMMIT_SHA
from ddtrace.ext.git import MAIN_PACKAGE
from ddtrace.ext.git import REPOSITORY_URL
from ddtrace.internal import compat
from ddtrace.internal import gitmetadata
from ddtrace.internal.logger import get_logger
from ddtrace.internal.telemetry import report_configuration
from ddtrace.internal.utils.formats import parse_tags_str
from ddtrace.settings._core import DDConfig


logger = get_logger(__name__)


# Stash the reason why a transitive dependency failed to load; since we try to load things safely in order to guide
# configuration, these errors won't bubble up naturally.  All of these components should use the same pattern
# in order to guarantee uniformity.
ddup_failure_msg = ""
stack_v2_failure_msg = ""


def _derive_default_heap_sample_size(heap_config, default_heap_sample_size=1024 * 1024):
    # type: (ProfilingConfigHeap, int) -> int
    heap_sample_size = heap_config._sample_size
    if heap_sample_size is not None:
        return heap_sample_size

    if not heap_config.enabled:
        return 0

    try:
        from ddtrace.vendor import psutil

        total_mem = psutil.swap_memory().total + psutil.virtual_memory().total
    except Exception:
        logger.warning(
            "Unable to get total memory available, using default value of %d KB",
            default_heap_sample_size / 1024,
            exc_info=True,
        )
        return default_heap_sample_size

    # This is TRACEBACK_ARRAY_MAX_COUNT
    max_samples = 2**16

    return int(max(math.ceil(total_mem / max_samples), default_heap_sample_size))


def _check_for_ddup_available():
    global ddup_failure_msg
    ddup_is_available = False
    try:
        from ddtrace.internal.datadog.profiling import ddup

        ddup_is_available = ddup.is_available
        ddup_failure_msg = ddup.failure_msg
    except Exception:
        pass  # nosec
    return ddup_is_available


def _check_for_stack_v2_available():
    global stack_v2_failure_msg
    stack_v2_is_available = False

    # stack_v2 will use libdd; in order to prevent two separate collectors from running, it then needs to force
    # libdd to be enabled as well; that means it depends on the libdd interface (ddup)
    if not _check_for_ddup_available():
        return False

    try:
        from ddtrace.internal.datadog.profiling import stack_v2

        stack_v2_is_available = stack_v2.is_available
        stack_v2_failure_msg = stack_v2.failure_msg
    except Exception:
        pass  # nosec
    return stack_v2_is_available


def _is_libdd_required(config):
    # This function consolidates the logic for force-enabling the libdd uploader.  Otherwise this will get enabled in
    # a bunch of separate places and it'll be tough to manage.
    # v2 requires libdd because it communicates over a pure-native channel
    # libdd... requires libdd
    # injected environments _cannot_ deploy protobuf, so they must use libdd
    # timeline requires libdd
    return (
        config.stack.v2_enabled
        or config.export._libdd_enabled
        or config._injected
        or config.timeline_enabled
        or config.pytorch.enabled
    )


# This value indicates whether or not profiling is _loaded_ in an injected environment. It does not by itself
# indicate whether profiling was enabled.
_profiling_injected = False


def _parse_profiling_enabled(raw: str) -> bool:
    global _profiling_injected

    # Before we do anything else, check the tracer configuration
    _profiling_injected = core_config._lib_was_injected

    # Try to derive two bits of information
    # - Are we injected (DD_INJECTION_ENABLED set) (almost certainly already populated correctly by core_config)
    # - Is profiling enabled ("profiler" in the list)
    if os.environ.get("DD_INJECTION_ENABLED") is not None:
        _profiling_injected = True
        for tok in os.environ.get("DD_INJECTION_ENABLED", "").split(","):
            if tok.strip().lower() == "profiler":
                return True

    # This is the normal check
    raw_lc = raw.lower()
    if raw_lc in ("1", "true", "yes", "on"):
        return True

    # In addition to everything else, we have to check for the `auto` value of `DD_PROFILING_ENABLED`.
    # This value simultaneously enables the profiler and indicates the environment is injected.
    if raw_lc == "auto":
        _profiling_injected = True
        return True

    # If it wasn't enabled, then disable it
    return False


def _check_for_injected():
    global _profiling_injected
    return _profiling_injected


def _update_git_metadata_tags(tags):
    """
    Update profiler tags with git metadata
    """
    # clean tags, because values will be combined and inserted back in the same way as for tracer
    gitmetadata.clean_tags(tags)
    repository_url, commit_sha, main_package = gitmetadata.get_git_tags()
    if repository_url:
        tags[REPOSITORY_URL] = repository_url
    if commit_sha:
        tags[COMMIT_SHA] = commit_sha
    if main_package:
        tags[MAIN_PACKAGE] = main_package
    return tags


def _enrich_tags(tags) -> t.Dict[str, str]:
    tags = {
        k: compat.ensure_text(v, "utf-8")
        for k, v in itertools.chain(
            _update_git_metadata_tags(parse_tags_str(os.environ.get("DD_TAGS"))).items(),
            tags.items(),
        )
    }

    return tags


class ProfilingConfig(DDConfig):
    __prefix__ = "dd.profiling"

    # Note that the parser here has a side-effect, since SSI has changed the once-truthy value of the envvar to
    # truthy + "auto", which has a special meaning.
    enabled = DDConfig.v(
        bool,
        "enabled",
        parser=_parse_profiling_enabled,
        default=False,
        help_type="Boolean",
        help="Enable Datadog profiling when using ``ddtrace-run``",
    )

    agentless = DDConfig.v(
        bool,
        "agentless",
        default=False,
        help_type="Boolean",
        help="",
    )

    code_provenance = DDConfig.v(
        bool,
        "enable_code_provenance",
        default=True,
        help_type="Boolean",
        help="Whether to enable code provenance",
    )

    endpoint_collection = DDConfig.v(
        bool,
        "endpoint_collection_enabled",
        default=True,
        help_type="Boolean",
        help="Whether to enable the endpoint data collection in profiles",
    )

    output_pprof = DDConfig.v(
        t.Optional[str],
        "output_pprof",
        default=None,
        help_type="String",
        help="",
    )

    max_events = DDConfig.v(
        int,
        "max_events",
        default=16384,
        help_type="Integer",
        help="",
    )

    upload_interval = DDConfig.v(
        float,
        "upload_interval",
        default=60.0,
        help_type="Float",
        help="The interval in seconds to wait before flushing out recorded events",
    )

    capture_pct = DDConfig.v(
        float,
        "capture_pct",
        default=1.0,
        help_type="Float",
        help="The percentage of events that should be captured (e.g. memory "
        "allocation). Greater values reduce the program execution speed. Must be "
        "greater than 0 lesser or equal to 100",
    )

    max_frames = DDConfig.v(
        int,
        "max_frames",
        default=64,
        help_type="Integer",
        help="The maximum number of frames to capture in stack execution tracing",
    )

    ignore_profiler = DDConfig.v(
        bool,
        "ignore_profiler",
        default=False,
        help_type="Boolean",
        help="**Deprecated**: whether to ignore the profiler in the generated data",
    )

    max_time_usage_pct = DDConfig.v(
        float,
        "max_time_usage_pct",
        default=1.0,
        help_type="Float",
        help="The percentage of maximum time the stack profiler can use when computing "
        "statistics. Must be greater than 0 and lesser or equal to 100",
    )

    api_timeout = DDConfig.v(
        float,
        "api_timeout",
        default=10.0,
        help_type="Float",
        help="The timeout in seconds before dropping events if the HTTP API does not reply",
    )

    timeline_enabled = DDConfig.v(
        bool,
        "timeline_enabled",
        default=False,
        help_type="Boolean",
        help="Whether to add timestamp information to captured samples.  Adds a small amount of "
        "overhead to the profiler, but enables the use of the Timeline view in the UI.",
    )

    tags = DDConfig.v(
        dict,
        "tags",
        parser=parse_tags_str,
        default={},
        help_type="Mapping",
        help="The tags to apply to uploaded profile. Must be a list in the ``key1:value,key2:value2`` format",
    )

    enable_asserts = DDConfig.v(
        bool,
        "enable_asserts",
        default=False,
        help_type="Boolean",
        help="Whether to enable debug assertions in the profiler code",
    )

    _force_legacy_exporter = DDConfig.v(
        bool,
        "_force_legacy_exporter",
        default=False,
        help_type="Boolean",
        help="Exclusively used in testing environments to force the use of the legacy exporter. This parameter is "
        "not for general use and will be removed in the near future.",
    )

    sample_pool_capacity = DDConfig.v(
        int,
        "sample_pool_capacity",
        default=4,
        help_type="Integer",
        help="The number of Sample objects to keep in the pool for reuse. "
        "Increasing this can reduce the overhead from frequently allocating "
        "and deallocating Sample objects.",
    )


class ProfilingConfigStack(DDConfig):
    __item__ = __prefix__ = "stack"

    enabled = DDConfig.v(
        bool,
        "enabled",
        default=True,
        help_type="Boolean",
        help="Whether to enable the stack profiler",
    )

    _v2_enabled = DDConfig.v(
        bool,
        "v2_enabled",
        default=True,
        help_type="Boolean",
        help="Whether to enable the v2 stack profiler. Also enables the libdatadog collector.",
    )

    # V2 can't be enabled if stack collection is disabled or if pre-requisites are not met
    v2_enabled = DDConfig.d(bool, lambda c: _check_for_stack_v2_available() and c._v2_enabled and c.enabled)

    v2_adaptive_sampling = DDConfig.v(
        bool,
        "v2.adaptive_sampling.enabled",
        default=True,
        help_type="Boolean",
        private=True,
    )


class ProfilingConfigLock(DDConfig):
    __item__ = __prefix__ = "lock"

    enabled = DDConfig.v(
        bool,
        "enabled",
        default=True,
        help_type="Boolean",
        help="Whether to enable the lock profiler",
    )

    name_inspect_dir = DDConfig.v(
        bool,
        "name_inspect_dir",
        default=True,
        help_type="Boolean",
        help="Whether to inspect the ``dir()`` of local and global variables to find the name of the lock. "
        "With this enabled, the profiler finds the name of locks that are attributes of an object.",
    )


class ProfilingConfigMemory(DDConfig):
    __item__ = __prefix__ = "memory"

    enabled = DDConfig.v(
        bool,
        "enabled",
        default=True,
        help_type="Boolean",
        help="Whether to enable the memory profiler",
    )

    events_buffer = DDConfig.v(
        int,
        "events_buffer",
        default=16,
        help_type="Integer",
        help="",
    )


class ProfilingConfigHeap(DDConfig):
    __item__ = __prefix__ = "heap"

    enabled = DDConfig.v(
        bool,
        "enabled",
        default=True,
        help_type="Boolean",
        help="Whether to enable the heap memory profiler",
    )

    _sample_size = DDConfig.v(
        t.Optional[int],
        "sample_size",
        default=None,
        help_type="Integer",
        help="",
    )
    sample_size = DDConfig.d(int, _derive_default_heap_sample_size)


class ProfilingConfigPytorch(DDConfig):
    __item__ = __prefix__ = "pytorch"

    enabled = DDConfig.v(
        bool,
        "enabled",
        default=False,
        help_type="Boolean",
        help="Whether to enable the PyTorch profiler",
    )

    events_limit = DDConfig.v(
        int,
        "events_limit",
        default=1_000_000,
        help_type="Integer",
        help="How many events the PyTorch profiler records each collection",
    )


class ProfilingConfigExport(DDConfig):
    __item__ = __prefix__ = "export"

    _libdd_enabled = DDConfig.v(
        bool,
        "libdd_enabled",
        default=False,
        help_type="Boolean",
        help="Enables collection and export using a native exporter.  Can fallback to the pure-Python exporter.",
    )


# Include all the sub-configs
ProfilingConfig.include(ProfilingConfigStack, namespace="stack")
ProfilingConfig.include(ProfilingConfigLock, namespace="lock")
ProfilingConfig.include(ProfilingConfigMemory, namespace="memory")
ProfilingConfig.include(ProfilingConfigHeap, namespace="heap")
ProfilingConfig.include(ProfilingConfigPytorch, namespace="pytorch")
ProfilingConfig.include(ProfilingConfigExport, namespace="export")

config = ProfilingConfig()
report_configuration(config)

# If during processing we discover that the configuration was injected, we need to do a few things
# - Mark it as such
# - Force libdd to be enabled, disabling the profiler otherwise the service might crash
#   (this is done in the _is_libdd_required function)
config._injected = _check_for_injected()

# Force the enablement of libdd if the user requested a feature which requires it; otherwise the user has to manage
# configuration too intentionally and we'll need to change the API too much over time.
config.export.libdd_enabled = _is_libdd_required(config)

# AFTER checking for libdd enablement, we process the override (_force_legacy_exporter), which will disable libdd.
# This is done because we currently test in an injected posture, but the new exporter doesn't have the same
# introspection capabilities as the legacy one.
if config._force_legacy_exporter:
    config.export.libdd_enabled = False

# Certain features depend on libdd being available.  If it isn't for some reason, those features cannot be enabled.
if config.stack.v2_enabled and not config.export.libdd_enabled:
    msg = ddup_failure_msg or "libdd not available"
    logger.warning("The v2 stack profiler cannot be used (%s)", msg)
    config.stack.v2_enabled = False

# Loading stack_v2 can fail for similar reasons
if config.stack.v2_enabled and not _check_for_stack_v2_available():
    msg = stack_v2_failure_msg or "stack_v2 not available"
    logger.warning("The v2 stack profiler cannot be used (%s)", msg)
    config.stack.v2_enabled = False

# Enrich tags with git metadata and DD_TAGS
config.tags = _enrich_tags(config.tags)


def config_str(config):
    configured_features = []
    if config.stack.enabled:
        if config.stack.v2_enabled:
            configured_features.append("stack_v2")
        else:
            configured_features.append("stack")
    if config.lock.enabled:
        configured_features.append("lock")
    if config.memory.enabled:
        configured_features.append("mem")
    if config.heap.sample_size > 0:
        configured_features.append("heap")
    if config.pytorch.enabled:
        configured_features.append("pytorch")
    if config.export.libdd_enabled:
        configured_features.append("exp_dd")
    else:
        configured_features.append("exp_py")
    configured_features.append("CAP" + str(config.capture_pct))
    configured_features.append("MAXF" + str(config.max_frames))
    return "_".join(configured_features)
