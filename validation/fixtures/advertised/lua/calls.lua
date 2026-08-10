local types = require("lua.types")
local M = {}
function M.goldOrdered(value)
  types.goldFirst(value)
  types.goldSecond(value)
  return types.goldFirst(value)
end
return M
