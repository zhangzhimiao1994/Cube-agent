redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('HDEL', KEYS[2], ARGV[1])
local latest = redis.call('ZREVRANGE', KEYS[1], 0, 0, 'WITHSCORES')
if #latest == 0 then
    redis.call('DEL', KEYS[1], KEYS[2])
else
    redis.call('PEXPIREAT', KEYS[1], latest[2])
    redis.call('PEXPIREAT', KEYS[2], latest[2])
end
return 1
