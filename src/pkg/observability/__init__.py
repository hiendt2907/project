"""Observability helpers shared between gateway and workers.

Modules here MUST stay dependency-light (stdlib + redis client passed in) so the
gateway image — which only packages src/pkg/{autonomy,reasoning,rag,observability}
— can import them without pulling in workers/executor/prober.
"""
