local proposed_base = tonumber(ARGV[1])
local proposed_rpm = tonumber(ARGV[2])
local proposed_tpm = tonumber(ARGV[3])
local ttl_ms = tonumber(ARGV[4])
if proposed_base == nil or proposed_base < 1 or proposed_base ~= math.floor(proposed_base)
    or proposed_rpm == nil or proposed_rpm < 0 or proposed_rpm ~= math.floor(proposed_rpm)
    or proposed_tpm == nil or proposed_tpm < 0 or proposed_tpm ~= math.floor(proposed_tpm)
    or ttl_ms == nil or ttl_ms < 1 then
    return redis.error_reply('invalid model scope policy')
end

local function restrictive(existing, proposed)
    if existing == nil then return proposed end
    if existing == 0 then return proposed end
    if proposed == 0 then return existing end
    return math.min(existing, proposed)
end

local base = restrictive(tonumber(redis.call('HGET', KEYS[1], 'base')), proposed_base)
local rpm = restrictive(tonumber(redis.call('HGET', KEYS[1], 'rpm')), proposed_rpm)
local tpm = restrictive(tonumber(redis.call('HGET', KEYS[1], 'tpm')), proposed_tpm)
redis.call('HSET', KEYS[1], 'base', base, 'rpm', rpm, 'tpm', tpm)
redis.call('PEXPIRE', KEYS[1], ttl_ms)
return {base, rpm, tpm}
