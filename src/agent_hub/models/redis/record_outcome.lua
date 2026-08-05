local now_parts = redis.call('TIME')
local now_ms = (tonumber(now_parts[1]) * 1000) + math.floor(tonumber(now_parts[2]) / 1000)
local base_limit = tonumber(redis.call('HGET', KEYS[3], 'base'))
local status = tonumber(ARGV[1])
local succeeded = tonumber(ARGV[2])
local latency_ms = tonumber(ARGV[3])
local cooldown_ms = tonumber(ARGV[4])
local latency_threshold_ms = tonumber(ARGV[5])
local window = tonumber(ARGV[6])
local state_ttl_ms = tonumber(ARGV[7])
if base_limit == nil or base_limit < 1 or base_limit ~= math.floor(base_limit)
    or status == nil or status ~= math.floor(status)
    or (status ~= 0 and (status < 100 or status > 599))
    or (succeeded ~= 0 and succeeded ~= 1)
    or latency_ms == nil or latency_ms < 0
    or cooldown_ms == nil or cooldown_ms < 1
    or latency_threshold_ms == nil or latency_threshold_ms < 1
    or window == nil or window < 1 or window ~= math.floor(window)
    or state_ttl_ms == nil or state_ttl_ms < 1 then
    return redis.error_reply('invalid model health outcome')
end
redis.call('PEXPIRE', KEYS[3], state_ttl_ms)

redis.call('RPUSH', KEYS[2], latency_ms)
redis.call('LTRIM', KEYS[2], -window, -1)
redis.call('PEXPIRE', KEYS[2], state_ttl_ms)
local samples = redis.call('LRANGE', KEYS[2], 0, -1)
local numeric = {}
for index, value in ipairs(samples) do numeric[index] = tonumber(value) end
table.sort(numeric)
local p95_index = math.max(1, math.ceil(#numeric * 0.95))
local p95 = numeric[p95_index] or 0

local current = tonumber(redis.call('HGET', KEYS[1], 'effective')) or base_limit
current = math.max(1, math.min(current, base_limit))
local cooldown_until = tonumber(redis.call('HGET', KEYS[1], 'cooldown_until_ms')) or 0
local unhealthy = status == 429 or status == 503 or p95 > latency_threshold_ms
if unhealthy then
    current = math.max(1, math.floor(current / 2))
    cooldown_until = math.max(cooldown_until, now_ms + cooldown_ms)
elseif succeeded == 1 and now_ms >= cooldown_until and current < base_limit then
    current = current + 1
end
redis.call('HSET', KEYS[1], 'effective', current, 'cooldown_until_ms', cooldown_until)
redis.call('PEXPIRE', KEYS[1], state_ttl_ms)
return {current, p95, cooldown_until}
