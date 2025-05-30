from google.protobuf.descriptor import FieldDescriptor

from ddtrace.ext import schema as SCHEMA_TAGS
from ddtrace.internal.datastreams import data_streams_processor
from ddtrace.internal.datastreams.schemas.schema_builder import SchemaBuilder
from ddtrace.internal.datastreams.schemas.schema_iterator import SchemaIterator

# from google._upb._message import Descriptor
from ddtrace.trace import Span


class SchemaExtractor(SchemaIterator):
    SERIALIZATION = "serialization"
    DESERIALIZATION = "deserialization"
    PROTOBUF = "protobuf"

    def __init__(self, schema):
        self.schema = schema

    @staticmethod
    def extract_property(
        field: FieldDescriptor, schema_name: str, field_name: str, builder: SchemaBuilder, depth: int
    ) -> int:
        array = False
        type_ = None
        format_ = None
        description = None
        ref = None
        enum_values = None

        type_format = SchemaExtractor.get_type_and_format(field.type)
        type_ = type_format[0]
        format_ = type_format[1]

        if type_ is None and format_ == "Message type":
            format_ = None
            ref = "#/components/schemas/" + field.message_type.name
            if not SchemaExtractor.extract_schema(field.message_type, builder, depth):
                return False
        elif format_ == "enum":
            enum_values = [value.name for value in field.enum_type.values]
        return builder.add_property(schema_name, field_name, array, type_, description, ref, format_, enum_values)

    @staticmethod
    def extract_schema(schema, builder: SchemaBuilder, depth: int) -> bool:
        depth += 1
        schema_name = schema.name
        if not builder.should_extract_schema(schema_name, depth):
            return False
        try:
            for field_name, field_desc in schema.fields_by_name.items():
                if not SchemaExtractor.extract_property(field_desc, schema_name, field_name, builder, depth):
                    return False
        except Exception:
            return False
        return True

    @staticmethod
    def extract_schemas(descriptor) -> bool:
        return data_streams_processor().get_schema(descriptor.name, SchemaExtractor(descriptor))

    def iterate_over_schema(self, builder: SchemaBuilder):
        self.extract_schema(self.schema, builder, 0)

    @staticmethod
    def attach_schema_on_span(descriptor, span: Span, operation: str):
        if descriptor is None or span is None:
            return

        span.set_tag(SCHEMA_TAGS.SCHEMA_TYPE, SchemaExtractor.PROTOBUF)
        span.set_tag(SCHEMA_TAGS.SCHEMA_NAME, descriptor.name)
        span.set_tag(SCHEMA_TAGS.SCHEMA_OPERATION, operation)

        if not data_streams_processor().can_sample_schema(operation):
            return

        prio = span.context.sampling_priority
        if prio is None or prio <= 0:
            return

        weight = data_streams_processor().try_sample_schema(operation)
        if weight == 0:
            return

        schema_data = SchemaExtractor.extract_schemas(descriptor)

        span.set_tag(SCHEMA_TAGS.SCHEMA_DEFINITION, schema_data.definition)
        span.set_metric(SCHEMA_TAGS.SCHEMA_WEIGHT, weight)
        span.set_tag(SCHEMA_TAGS.SCHEMA_ID, schema_data.id)

    @staticmethod
    def get_type_and_format(type_: int) -> tuple:
        type_format_mapping = {
            1: ("number", "double"),
            2: ("number", "float"),
            3: ("integer", "int64"),
            4: ("integer", "uint64"),
            5: ("integer", "int32"),
            6: ("integer", "fixed64"),
            7: ("integer", "fixed32"),
            8: ("boolean", None),
            9: ("string", None),
            10: ("object", "Group type"),  # Group types are deprecated
            11: (None, "Message type"),  # Placeholder for handling messages
            12: ("string", "byte"),
            13: ("integer", "uint32"),
            14: ("string", "enum"),
            15: ("integer", "sfixed32"),
            16: ("integer", "sfixed64"),
            17: ("integer", "int32"),
            18: ("integer", "int64"),
        }

        # Default values for unknown types
        return type_format_mapping.get(type_, ("string", None))
