#!/usr/bin/env python3

import logging
import os
import sys

from ddtrace.ext import SpanTypes
from ddtrace.trace import tracer


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
stream_handler = logging.StreamHandler(sys.stdout)
logger.addHandler(stream_handler)

from tests.appsec.iast.fixtures.integration.print_str import print_str  # noqa: E402


def main():
    with tracer.trace("main", span_type=SpanTypes.WEB):
        print_str()


if __name__ == "__main__":
    iast_enabled = bool(os.environ.get("DD_IAST_ENABLED", "") == "true")
    logger.info("configuring IAST to %s", iast_enabled)
    tracer.configure(iast_enabled=iast_enabled)
    main()
