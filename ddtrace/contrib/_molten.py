"""
The molten web framework is automatically traced by ``ddtrace``::

    import ddtrace.auto
    from molten import App, Route

    def hello(name: str, age: int) -> str:
        return f'Hello {age} year old named {name}!'
    app = App(routes=[Route('/hello/{name}/{age}', hello)])


You may also enable molten tracing automatically via ``ddtrace-run``::

    ddtrace-run python app.py


Configuration
~~~~~~~~~~~~~

.. py:data:: ddtrace.config.molten['distributed_tracing']

   Whether to parse distributed tracing headers from requests received by your Molten app.

   Default: ``True``

.. py:data:: ddtrace.config.molten['service_name']

   The service name reported for your Molten app.

   Can also be configured via the ``DD_SERVICE`` or ``DD_MOLTEN_SERVICE`` environment variables.

   Default: ``'molten'``

:ref:`All HTTP tags <http-tagging>` are supported for this integration.

"""
