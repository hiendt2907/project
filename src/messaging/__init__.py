"""Kafka event bus (replaces Redis Streams for ingress, DLQ, audits, diagnostics)."""

from messaging.kafka_bus import KafkaBus, create_producer, decode_kafka_value_to_fields, kafka_msg_id

__all__ = ["KafkaBus", "create_producer", "decode_kafka_value_to_fields", "kafka_msg_id"]
