local now_parts = redis.call('TIME')
local now_ms = (tonumber(now_parts[1]) * 1000) + math.floor(tonumber(now_parts[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms)
if redis.call('ZSCORE', KEYS[1], ARGV[1]) == false then
    return 0
end
local expires_ms = now_ms + tonumber(ARGV[2])
redis.call('ZADD', KEYS[1], expires_ms, ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[3])
return expires_ms
