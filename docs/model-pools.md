# Model Pools

Model pools support multiple API providers and quota scopes.

Examples:

- One account quota with configured concurrency `8`: use target utilization below `100%`, for example `0.8`.
- Serial quota: configure concurrency `1`.
- Multiple keys in one provider account: use one shared quota scope so capacity is not double-counted.
- Four independent quota scopes: configure four serial slots, one per scope.
- Saturation policy: queue first, then fallback after timeout.

DeepSeek-style APIs can serve concurrent requests from one API key; configure queue and rate limits so the system does not run the quota to exhaustion.

