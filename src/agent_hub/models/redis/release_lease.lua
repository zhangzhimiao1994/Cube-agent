local removed = redis.call('ZREM', KEYS[1], ARGV[1])
if redis.call('EXISTS', KEYS[1]) == 1 then
    redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return removed
