"""Python port of the brain-go Smart-SIEM graph correlation engine.

Scope: the Kafka-transport + graph-correlator path that was actually deployed
(`finguard/brain-go:siem-v2-corr`, BRAIN_TRANSPORT=kafka + CORR_GRAPH_ENABLED).
The legacy Redis-stream transport and the single-key correlator were never
deployed on Omni and are intentionally NOT ported.

Contract parity (must not drift without a schema bump):
- input:  ``omni-siem-raw``   — incident envelope (transport/kafka.go decode)
- output: ``omni-siem-incidents`` — passthrough metadata envelope
- output: ``omni-siem-chains``    — CorrelationChain envelope consumed by
  ``services.analyst.chain_consumer.ChainConsumer``

SECURITY INVARIANT (INV_DATA_RESIDENCY, non-negotiable): correlation reads only parsed
metadata fields and the already-normalized message string; entity extraction is
allowlist-only so raw VM log content can never travel through a chain.
"""
