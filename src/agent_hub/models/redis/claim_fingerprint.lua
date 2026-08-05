local existing = redis.call('GET', KEYS[1])
if existing == false then
    redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2], 'NX')
    return 1
end
if existing ~= ARGV[1] then return 0 end
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return 1
