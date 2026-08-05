local now_parts = redis.call('TIME')
local now = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
local base = tonumber(ARGV[2])
local rpm = tonumber(ARGV[3])
local tpm = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
local max_owners = tonumber(ARGV[6])
if base == nil or base < 1 or base ~= math.floor(base)
    or rpm == nil or rpm < 0 or rpm ~= math.floor(rpm)
    or tpm == nil or tpm < 0 or tpm ~= math.floor(tpm)
    or ttl == nil or ttl < 1 or max_owners == nil or max_owners < 1
    or max_owners ~= math.floor(max_owners) then
    return redis.error_reply('invalid model scope policy')
end

local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now)
for _, owner in ipairs(expired) do
    redis.call('HDEL', KEYS[3], owner)
    redis.call('HDEL', KEYS[4], owner)
    redis.call('HDEL', KEYS[5], owner)
    redis.call('HDEL', KEYS[6], owner)
end
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)

local active_count = redis.call('ZCARD', KEYS[2])
for index = 3, 6 do
    if redis.call('HLEN', KEYS[index]) ~= active_count then
        return redis.error_reply('invalid model scope policy owner state')
    end
end

local existing = redis.call('ZSCORE', KEYS[2], ARGV[1])
local count = active_count
if existing == false and count >= max_owners then
    return redis.error_reply('too many model scope policy owners')
end
local expires = now + ttl
redis.call('ZADD', KEYS[2], expires, ARGV[1])
redis.call('HSET', KEYS[3], ARGV[1], base)
redis.call('HSET', KEYS[4], ARGV[1], rpm)
redis.call('HSET', KEYS[5], ARGV[1], tpm)
redis.call('HSET', KEYS[6], ARGV[1], '1')

local function minimum(key, zero_unlimited)
    local values = redis.call('HVALS', key)
    local result = nil
    for _, raw in ipairs(values) do
        local value = tonumber(raw)
        if not zero_unlimited or value ~= 0 then
            if result == nil or value < result then result = value end
        end
    end
    if result == nil then return 0 end
    return result
end

local effective_base = minimum(KEYS[3], false)
local effective_rpm = minimum(KEYS[4], true)
local effective_tpm = minimum(KEYS[5], true)
redis.call('HSET', KEYS[1], 'base', effective_base, 'rpm', effective_rpm, 'tpm', effective_tpm)

local health = tonumber(redis.call('HGET', KEYS[7], 'effective'))
if health == nil then
    redis.call('HSET', KEYS[7], 'effective', effective_base)
elseif health > effective_base then
    redis.call('HSET', KEYS[7], 'effective', effective_base)
end

local latest = redis.call('ZREVRANGE', KEYS[2], 0, 0, 'WITHSCORES')
for index = 1, 7 do redis.call('PEXPIREAT', KEYS[index], latest[2]) end
return {effective_base, effective_rpm, effective_tpm, existing == false and 1 or 0, redis.call('ZCARD', KEYS[2])}
