-- Delayed preview with live Whisper subtitles, for capture-hub.sh (PREVIEW_DELAY > 0).
--
-- The hub pipes live Matroska into mpv. Subtitles (capture/live_captions.py ->
-- /dev/shm/captions.json, wall-clock ms) are ready ~17 s after the speech, so mpv
-- buffers the pipe and plays `delay` seconds behind it, like the TV app's CC mode.
-- The wall-clock time of the frame on screen is
--   now - (demuxer-cache-time - time-pos)
-- (cache end = what the hub just sent), so subtitles stay in step through stalls.
-- Key words Gemini glossed are shown in blue with their English in green above them.

local mp = require "mp"
local utils = require "mp.utils"
local options = require "mp.options"

local o = { delay = 18, file = "/dev/shm/captions.json", viewer = "/dev/shm/captions-viewer" }
options.read_options(o, "captions")

local overlay = mp.create_osd_overlay("ass-events")   -- default canvas: 720 lines high
local subs, file_mtime, shown = {}, nil, nil
local held = true            -- we paused: waiting for the buffer to reach `delay`

local function lag()
    local cached, pos = mp.get_property_number("demuxer-cache-time"), mp.get_property_number("time-pos")
    if not cached or not pos then return nil end
    return cached - pos
end

local function load()
    local info = utils.file_info(o.file)
    if not info or info.mtime == file_mtime then return end
    local f = io.open(o.file, "r")
    if not f then return end
    local data = utils.parse_json(f:read("*a"))
    f:close()
    if data and data.captions then subs, file_mtime = data.captions, info.mtime end
end

local function touch_viewer()   -- live_captions.py only calls Gemini while someone watches
    local f = io.open(o.viewer, "w")
    if f then f:write("mpv\n"); f:close() end
end

local function esc(s)
    return (s:gsub("\\", "\\\\"):gsub("{", "\\{"):gsub("}", "\\}"))
end

-- Layout on a fixed 1280x720 canvas: an opaque light-gray box at the bottom; each subtitle
-- line is a row of word columns with the English of glossed words centred above them
-- (2 subtitle lines -> 4 rows), like the TV app. Column width = wider of word and gloss.
local W, H = 1280, 720
local FS, GFS = 40, 26               -- Spanish / English font sizes
local GAP, PADX, PADY, BOTTOM = 14, 28, 12, 28
local BOX, INK, KEY, EN = "D8D8D8", "141414", "004A8C", "1E6E00"   -- RGB
local function bgr(rgb) return rgb:sub(5, 6) .. rgb:sub(3, 4) .. rgb:sub(1, 2) end

overlay.res_x, overlay.res_y = W, H
local meter = mp.create_osd_overlay("ass-events")
meter.res_x, meter.res_y, meter.hidden, meter.compute_bounds = W, H, true, true
local widths = {}
local function width(text, fs, bold)  -- rendered width on the canvas (libass), cached
    local k = fs .. (bold and "b" or "") .. text
    if not widths[k] then
        meter.data = string.format("{\\an7\\pos(0,0)\\bord0\\shad0\\b%d\\fs%d}%s", bold and 1 or 0, fs, esc(text))
        local r = meter:update()
        widths[k] = (r and r.x1 and r.x1 - r.x0) or #text * fs * 0.5
    end
    return widths[k]
end

local function render(c)
    local gloss = {}
    for _, g in ipairs(c.gloss or {}) do
        if g.w and g.en then gloss[g.w:lower()] = g.en end
    end
    local rows, boxw = {}, 0
    for line in (c.text .. "\n"):gmatch("(.-)\n") do
        local cols, linew = {}, 0
        for word in line:gmatch("%S+") do
            local key = word:gsub("^[¿¡\"«(]+", ""):gsub("[.,;:!?\"»)…]+$", ""):lower()
            local en = gloss[key]
            local w = math.max(width(word, FS, true), en and width(en, GFS, false) or 0)
            cols[#cols + 1] = { word = word, en = en, w = w }
            linew = linew + w + (#cols > 1 and GAP or 0)
        end
        if #cols > 0 then rows[#rows + 1] = { cols = cols, w = linew }; boxw = math.max(boxw, linew) end
    end
    if #rows == 0 then return "" end
    local k = math.min(1, (W - 2 * PADX - 40) / boxw)       -- shrink very long lines to fit
    local fs, gfs, gap = FS * k, GFS * k, GAP * k
    local gh, sh = math.floor(gfs * 1.15), math.floor(fs * 1.2)     -- row heights
    local slots = math.max(#rows, 2)                           -- always 4 rows tall: no jumping
    local bw, bh = boxw * k + 2 * PADX, slots * (gh + sh) + 2 * PADY
    local bx, by = (W - bw) / 2, H - BOTTOM - bh
    local ev = { string.format("{\\an7\\pos(%d,%d)\\bord0\\shad0\\1c&H%s&\\1a&H00&\\p1}m 0 0 l %d 0 %d %d 0 %d{\\p0}",
                               bx, by, bgr(BOX), bw, bw, bh, bh) }
    local y = by + PADY + (slots - #rows) * (gh + sh)          -- short subtitles sit at the bottom
    for _, r in ipairs(rows) do
        local x = (W - r.w * k) / 2
        for _, col in ipairs(r.cols) do
            local cx = x + col.w * k / 2
            if col.en then
                ev[#ev + 1] = string.format("{\\an2\\pos(%.1f,%d)\\bord0\\shad0\\b0\\fs%.1f\\1c&H%s&}%s",
                                            cx, y + gh, gfs, bgr(EN), esc(col.en))
            end
            ev[#ev + 1] = string.format("{\\an2\\pos(%.1f,%d)\\bord0\\shad0\\b1\\fs%.1f\\1c&H%s&}%s",
                                        cx, y + gh + sh, fs, bgr(col.en and KEY or INK), esc(col.word))
            x = x + col.w * k + gap
        end
        y = y + gh + sh
    end
    return table.concat(ev, "\n")
end

-- Lua's os.time() has 1 s resolution: anchor mpv's monotonic clock to `date` (re-synced every 10 min).
local anchor, anchored_at = nil, -1e9
local function now_ms()
    local t = mp.get_time()
    if t - anchored_at > 600 then
        local r = mp.command_native({ name = "subprocess", args = { "date", "+%s%3N" },
                                      capture_stdout = true, playback_only = false })
        local w = r and tonumber(r.stdout)
        if w then anchor, anchored_at = w - t * 1000, mp.get_time() end
    end
    return anchor and (anchor + t * 1000) or os.time() * 1000
end

local function tick()
    local l = lag()
    if l then
        if held and l >= o.delay then
            held = false
            mp.set_property_bool("pause", false)
        elseif not held and l < o.delay - 2 then   -- input stalled: rebuild the buffer
            held = true
            mp.set_property_bool("pause", true)
        end
    end
    if held then
        overlay.data = string.format("{\\an9\\fs24\\bord2}Buffering for subtitles: %.0f / %d s", math.max(l or 0, 0), o.delay)
        overlay:update()
        shown = nil
        return
    end
    if not l then return end
    touch_viewer()
    load()
    local t = now_ms() - l * 1000            -- wall clock of the frame on screen
    local cur
    for _, c in ipairs(subs) do
        if c.start <= t and t <= c["end"] then cur = c end
    end
    local text = cur and render(cur) or ""
    if text ~= shown then
        overlay.data = text
        overlay:update()
        shown = text
    end
end

mp.set_property_bool("pause", true)
mp.add_periodic_timer(0.1, tick)
