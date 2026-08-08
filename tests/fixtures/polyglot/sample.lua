local M = {}

local function helper(x) return x * 2 end

function M.quote(id) return helper(id) end

function M:reset() self.cache = {} end

return M
