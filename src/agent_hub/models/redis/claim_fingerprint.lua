local now_parts = redis.call('TIME')
local now = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
local ttl = tonumber(ARGV[3])
if ttl == nil or ttl < 1 then return redis.error_reply('invalid fingerprint ttl') end

local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now)
for _, owner in ipairs(expired) do redis.call('HDEL', KEYS[2], owner) end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)

local scopes = redis.call('HVALS', KEYS[2])
for _, scope in ipairs(scopes) do
    if scope ~= ARGV[2] then return 0 end
end

local expires = now + ttl
redis.call('ZADD', KEYS[1], expires, ARGV[1])
redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
local latest = redis.call('ZREVRANGE', KEYS[1], 0, 0, 'WITHSCORES')
if #latest == 2 then
    redis.call('PEXPIREAT', KEYS[1], latest[2])
    redis.call('PEXPIREAT', KEYS[2], latest[2])
end
return 1
