import json
from typing import Any
from typing import Dict

import botocore.exceptions

from ddtrace import config
from ddtrace.contrib.internal.botocore.constants import BOTOCORE_STEPFUNCTIONS_INPUT_KEY
from ddtrace.contrib.internal.trace_utils import ext_service
from ddtrace.ext import SpanTypes
from ddtrace.internal import core
from ddtrace.internal.logger import get_logger
from ddtrace.internal.schema import SpanDirection
from ddtrace.internal.schema import schematize_cloud_messaging_operation
from ddtrace.internal.schema import schematize_service_name


log = get_logger(__name__)


def update_stepfunction_input(ctx: core.ExecutionContext, params: Any) -> None:
    if "input" not in params or params["input"] is None:
        return

    input_obj = params["input"]

    if isinstance(input_obj, str):
        try:
            input_obj = json.loads(params["input"])
        except ValueError:
            log.warning("Input is not a valid JSON string")
            return

    if not isinstance(input_obj, dict) or "_datadog" in input_obj:
        return

    input_obj["_datadog"] = {}
    core.dispatch("botocore.stepfunctions.update_input", [ctx, None, None, input_obj, None])
    updated_input_obj = ctx.get_item(BOTOCORE_STEPFUNCTIONS_INPUT_KEY)
    if updated_input_obj:
        input_json_str = json.dumps(updated_input_obj)
        params["input"] = input_json_str


def patched_stepfunction_api_call(original_func, instance, args, kwargs: Dict, function_vars: Dict):
    params = function_vars.get("params")
    trace_operation = function_vars.get("trace_operation")
    pin = function_vars.get("pin")
    endpoint_name = function_vars.get("endpoint_name")
    operation = function_vars.get("operation")

    is_start_execution_call = endpoint_name == "states" and operation in {"StartExecution", "StartSyncExecution"}
    should_update_input = args and config.botocore["distributed_tracing"] and is_start_execution_call
    if should_update_input:
        call_name = schematize_cloud_messaging_operation(
            trace_operation,
            cloud_provider="aws",
            cloud_service="stepfunctions",
            direction=SpanDirection.OUTBOUND,
        )
    else:
        call_name = trace_operation

    with core.context_with_data(
        "botocore.patched_stepfunctions_api_call",
        span_name=call_name,
        service=schematize_service_name("{}.{}".format(ext_service(pin, int_config=config.botocore), endpoint_name)),
        span_type=SpanTypes.HTTP,
        span_key="patched_stepfunctions_api_call",
        instance=instance,
        args=args,
        params=params,
        endpoint_name=endpoint_name,
        operation=operation,
        pin=pin,
    ) as ctx, ctx.span:
        core.dispatch("botocore.patched_stepfunctions_api_call.started", [ctx])

        if should_update_input:
            update_stepfunction_input(ctx, params)

        try:
            return original_func(*args, **kwargs)
        except botocore.exceptions.ClientError as e:
            core.dispatch(
                "botocore.patched_stepfunctions_api_call.exception",
                [
                    ctx,
                    e.response,
                    botocore.exceptions.ClientError,
                    config.botocore.operations[ctx.span.resource].is_error_code,
                ],
            )
            raise
