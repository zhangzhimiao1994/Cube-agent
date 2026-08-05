redis.call('ZREM', KEYS[2], ARGV[1])
for index = 3, 6 do redis.call('HDEL', KEYS[index], ARGV[1]) end

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

local latest = redis.call('ZREVRANGE', KEYS[2], 0, 0, 'WITHSCORES')
if #latest == 0 then
    for index = 1, 7 do redis.call('DEL', KEYS[index]) end
    return 1
end

local previous = tonumber(redis.call('HGET', KEYS[1], 'base'))
local base = minimum(KEYS[3], false)
redis.call('HSET', KEYS[1], 'base', base, 'rpm', minimum(KEYS[4], true), 'tpm', minimum(KEYS[5], true))
local health = tonumber(redis.call('HGET', KEYS[7], 'effective'))
if health == nil or previous == nil or health == previous then
    redis.call('HSET', KEYS[7], 'effective', base)
elseif health > base then
    redis.call('HSET', KEYS[7], 'effective', base)
end
for index = 1, 7 do redis.call('PEXPIREAT', KEYS[index], latest[2]) end
return 1
