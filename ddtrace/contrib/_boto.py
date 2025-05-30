"""
Boto integration will trace all AWS calls made via boto2.

Enabling
~~~~~~~~

The boto integration is enabled automatically when using
:ref:`ddtrace-run<ddtracerun>` or :ref:`import ddtrace.auto<ddtraceauto>`.

Or use :func:`patch()<ddtrace.patch>` to manually enable the integration::

    from ddtrace import patch
    patch(boto=True)

Configuration
~~~~~~~~~~~~~

.. py:data:: ddtrace.config.boto['tag_no_params']

    This opts out of the default behavior of collecting a narrow set of API
    parameters as span tags.

    To not collect any API parameters, ``ddtrace.config.boto.tag_no_params =
    True`` or by setting the environment variable ``DD_AWS_TAG_NO_PARAMS=true``.


    Default: ``False``

"""
