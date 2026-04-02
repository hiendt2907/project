# Redis Streams operations

## Consumer groups

Use `XREADGROUP` with `XACK` for at-least-once processing. Pending entries can be listed with `XPENDING`.

## Lab note

Keep `num_ctx` bounded for LLM workers per deployment policy.
