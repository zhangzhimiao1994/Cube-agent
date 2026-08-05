local now_parts = redis.call('TIME')
local now_ms = (tonumber(now_parts[1]) * 1000) + math.floor(tonumber(now_parts[2]) / 1000)
local lease_ttl_ms = tonumber(ARGV[2])
local base_limit = tonumber(ARGV[3])
local rpm = tonumber(ARGV[4])
local tpm = tonumber(ARGV[5])
local estimated_tokens = tonumber(ARGV[6])
local state_ttl_ms = tonumber(ARGV[7])

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms)
local active = redis.call('ZCARD', KEYS[1])
local effective = tonumber(redis.call('HGET', KEYS[4], 'effective')) or base_limit
effective = math.max(1, math.min(effective, base_limit))
redis.call('HSET', KEYS[4], 'effective', effective)
redis.call('PEXPIRE', KEYS[4], state_ttl_ms)
if active >= effective then
    return {0, active, effective, 1}
end

local function available_tokens(key, limit, cost)
    if limit <= 0 then
        return {true, 0, now_ms}
    end
    local tokens = tonumber(redis.call('HGET', key, 'tokens'))
    local last_ms = tonumber(redis.call('HGET', key, 'last_ms'))
    if tokens == nil or last_ms == nil then
        tokens = limit
        last_ms = now_ms
    else
        local elapsed = math.max(0, now_ms - last_ms)
        tokens = math.min(limit, tokens + (elapsed * limit / 60000))
    end
    return {tokens + 0.000000001 >= cost, tokens, now_ms}
end

local rpm_state = available_tokens(KEYS[2], rpm, 1)
local tpm_state = available_tokens(KEYS[3], tpm, estimated_tokens)
if not rpm_state[1] or not tpm_state[1] then
    return {0, active, effective, 2}
end

local function spend(key, limit, state, cost)
    if limit > 0 then
        redis.call('HSET', key, 'tokens', state[2] - cost, 'last_ms', state[3])
        redis.call('PEXPIRE', key, 120000)
    end
end

spend(KEYS[2], rpm, rpm_state, 1)
spend(KEYS[3], tpm, tpm_state, estimated_tokens)
local expires_ms = now_ms + lease_ttl_ms
redis.call('ZADD', KEYS[1], expires_ms, ARGV[1])
redis.call('PEXPIRE', KEYS[1], state_ttl_ms)
return {1, active + 1, effective, expires_ms}
