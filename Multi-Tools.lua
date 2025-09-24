local ffi = require("ffi")
local vector = require("vector")
local easing = require("gamesense/easing")
local bit = require("bit")

-- Check if FFI is available
if not ffi then
    error("FFI library is not available. Model changer functionality requires LuaJIT with FFI support.", 0)
end

-- FFI definitions for model changer
ffi.cdef[[
    typedef struct 
    {
        void*   fnHandle;        
        char    szName[260];     
        int     nLoadFlags;      
        int     nServerCount;    
        int     type;            
        int     flags;           
        float  vecMins[3];       
        float  vecMaxs[3];       
        float   radius;          
        char    pad[0x1C];       
    }model_t;
    
    typedef int(__thiscall* get_model_index_t)(void*, const char*);
    typedef const model_t(__thiscall* find_or_load_model_t)(void*, const char*);
    typedef int(__thiscall* add_string_t)(void*, bool, const char*, int, const void*);
    typedef void*(__thiscall* find_table_t)(void*, const char*);
    typedef void(__thiscall* set_model_index_t)(void*, int);
    typedef int(__thiscall* precache_model_t)(void*, const char*, bool);
    typedef void*(__thiscall* get_client_entity_t)(void*, int);
]]

local client_screen_size, client_set_cvar, client_userid_to_entindex, client_exec, client_set_event_callback, client_create_interface, client_find_signature, client_reload_active_scripts, client_unset_event_callback, client_delay_call, globals_frametime, globals_realtime, math_fmod, math_floor, renderer_gradient, renderer_indicator, tonumber, ui_get, ui_new_checkbox, ui_new_slider, ui_new_combobox, ui_new_multiselect, ui_new_listbox, ui_set_callback, ui_set_visible, entity_get_local_player, entity_get_player_name, entity_is_enemy, entity_get_prop, ui_reference, ui_set, ui_new_color_picker, ui_is_menu_open, ui_menu_position, ui_menu_size, entity_get_player_weapon, entity_is_alive, ui_new_hotkey = 
    client.screen_size, client.set_cvar, client.userid_to_entindex, client.exec, client.set_event_callback, client.create_interface, client.find_signature, client.reload_active_scripts, client.unset_event_callback, client.delay_call, globals.frametime, globals.realtime, math.fmod, math.floor, renderer.gradient, renderer.indicator, tonumber, ui.get, ui.new_checkbox, ui.new_slider, ui.new_combobox, ui.new_multiselect, ui.new_listbox, ui.set_callback, ui.set_visible, entity.get_local_player, entity.get_player_name, entity.is_enemy, entity.get_prop, ui.reference, ui.set, ui.new_color_picker, ui.is_menu_open, ui.menu_position, ui.menu_size, entity.get_player_weapon, entity.is_alive, ui.new_hotkey

-- Master switch for enabling/disabling all functionality
local master_switch = ui_new_checkbox("LUA", "B", "Enable Multi-Tools")

-- Cache initial viewmodel values
local xO, yO, zO, fO = 0, 0, 0, 0
local function cache_viewmodel()
    xO = client.get_cvar("viewmodel_offset_x")
    yO = client.get_cvar("viewmodel_offset_y")
    zO = client.get_cvar("viewmodel_offset_z")
    fO = client.get_cvar("viewmodel_fov")
end
cache_viewmodel()

-- Thirdperson controls
local onionThirdperson = {
    collisionControl = ui_new_checkbox("LUA", "B", "Thirdperson collisions"),
    distanceControl = ui_new_slider("LUA", "B", "Thirdperson distance", 30, 200, 125)
}

-- Viewmodel controls
local viewmodelControls = {
    xSlider = ui_new_slider("LUA", "B", "Viewmodel X", -20, 20, xO),
    ySlider = ui_new_slider("LUA", "B", "Viewmodel Y", -100, 100, yO),
    zSlider = ui_new_slider("LUA", "B", "Viewmodel Z", -20, 20, zO),
    fovSlider = ui_new_slider("LUA", "B", "Viewmodel FOV", -60, 60, fO)
}

-- Auto-buy controls
local primary_weapons = {
    {name='-', command=""},
    {name='AWP', command="buy awp; "},
    {name='Auto-Sniper', command="buy scar20; buy g3sg1; "},
    {name='Scout', command="buy ssg08; "},
    {name='Shotgun', command="buy mag7; buy sawedoff "}
}
local secondary_weapons = {
    {name='-', command=""},
    {name='R8 Revolver / Deagle', command="buy deagle; "},
    {name='Dual Berettas', command="buy elite; "},
    {name='FN57 / Tec9 / CZ75-Auto', command="buy fn57; "}
}
local gear_weapons = {
    {name='Kevlar', command="buy vest; "},
    {name='Helmet', command="buy vesthelm; "},
    {name='Defuse Kit', command="buy defuser; "},
    {name='Grenade', command="buy hegrenade; "},
    {name='Molotov', command="buy incgrenade; "},
    {name='Smoke', command="buy smokegrenade; "},
    {name='Flashbang (x2)', command="buy flashbang; "},
    {name='Taser', command="buy taser; "},
}
local buybot_enabled = ui_new_checkbox("LUA", "B", "Auto-Buy")
local buybot_primary = ui_new_combobox("LUA", "B", "Auto-Buy: Primary", (function() local names = {} for i=1, #primary_weapons do names[i] = primary_weapons[i].name end return names end)())
local buybot_pistol = ui_new_combobox("LUA", "B", "Auto-Buy: Secondary", (function() local names = {} for i=1, #secondary_weapons do names[i] = secondary_weapons[i].name end return names end)())
local buybot_gear = ui_new_multiselect("LUA", "B", "Auto-Buy: Gear", (function() local names = {} for i=1, #gear_weapons do names[i] = gear_weapons[i].name end return names end)())

-- TrashTalk functionality
local trashtalk_enable = ui_new_checkbox("LUA", "B", "Trashtalk Enable")
local deathtalk_enable = ui_new_checkbox("LUA", "B", "DeathTalk")
local player_name_add = ui_new_checkbox("LUA", "B", "Player Name Added")
local missed_logs_chat = ui_new_checkbox("LUA", "B", "Missed Logs Chat")

local phrases = {
    "Не плохой легит Хуecос.",
    "bля чёт изи",
    "Ага.. Угу.. Понял! С0си дальше",
    "Блин как так ты заглотнул по горло...",
    "1. 2.. 3.. Пoшёл Нахyй",
    "Сорри отходил а ты умер ок...",
    "Изи только не плач уёбoк",
    "Zhristmas Бля ёбeт лучше чем твой отчим тебя",
    "На тебе тренболона чтоб не плакал",
    "Ой как так ты сын шлюxи",
    "Опа а что это у тебя в жoпе!? О это не мой хyй!",
    "Ваще пиздeц ты умeр как твоя мать в туалете.",
    "Оу а что же это мой 30 сантиметровый члeн у тебя во рту L0L",
    "Опять я тебя изнасилoвaл...",
    "О привет сын шлюxи ты опять сюда зашёл чтоб тебе весь сервер oбкoнчaл!?",
    "Хочешь CFG как у меня!? Сoси xуй чмo.",
    "Сосал? Можешь не отвечать я и так всё о тебе знаю X_X",
    "Ой как так упустил Орешник на Львов... Плачем...",
    "Что маме? Что папе? А... хотя они же умeрли Круто :)",
    "Как дела твоя мама ещё не родила!?",
    "ты из Godeless? А так вот почему ты попосал :)",
    "Ой чит не забустил :(",
    "Я разчлeню твою семью и на ихних лбах набью Z | :)",
    "Ты опять отсoсал ну впринципе как и обычно.",
    "Да ты lucky ты умер от руки своего короля :)",
    "Как ты умер я же +W...",
    "Z | Za Nashuh 🇷🇺 Z Slava Putinu | 🇷🇺 🇷🇺 🇷🇺 | 1 |",
    "О ты умер круто как раз передашь привет своей маме 😅",
    "Сколько лет сколько зим я не видел таких травозил :(",
    "Ахуеть как так 9/0 ну ты мощ.",
    "Что Такое сладкий, мать больше не дефает?",
    "Привет как дела) СOсал?",
    "Ты знаешь почему ты Тупой? Вот и я не знаю.",
    "Ты думал как поступить? Но ты всё равно умер(",
    "Блин чёт я забустился от твоей мамаши)",
}

local death_phrases = {
    "Блин твоя мать перестала сoсать и я умер(",
    "Сyка меня твоя сестра трахает не мешай.",
    "Iq? или ты на свою мать надеешся? Ответ очивиден)",
    "Я на +W а ты потеешь NicoNico. Не стыдно? ... Умный?)",
    "Челси тебе норм на дефах дегать? Тебе свою мать не жалко?",
    "Передай привет своей маме от Зексуса Ёбаря)",
    "Сyка как так чит не дал убить вымирающий вид дoлбaеба(",
    "Ну ФУ!!! воняет со рта твоей мамаши 0_0",
    "Ну блин я умер от ДЦП обидно(",
    "А блин забыл я же по легиту.",
    "А я Ragebot оффнул забыл сорри)",
    "О нет меня убил нищий даун... Я сейчас заплачу(",
}

-- Jumpscout functionality
local air_strafe = ui_reference("MISC", "Movement", "Air strafe")
local jumpscout_enable = ui_new_checkbox("LUA", "B", "Jumpscout")

--- Custom model bossas functionality
local t_player_models = {
    ["cirno"] = "models/player/custom_player/nuclearsilo/touhou/cirno/cirno.mdl",
    ["tm_jungle"] = "models/player/custom_player/legacy/tm_jungle_marxist.mdl",
    ["bananana"] = "models/player/custom_player/caleon1/peely/peely.mdl",
    ["deadpool"] = "models/player/custom_player/kuristaja/deadpool/deadpool.mdl",
    ["t_cyber"] = "models/player/custom_player/FunMod/t_cyber.mdl",
    ["PLAYERMODEL"] = "models/player/custom_player/eminem/big_head/ctm_idf.mdl",
    ["CT FBI PLAYERMODEL"] = "models/player/custom_player/eminem/big_head/ctm_fbi.mdl",
    ["PIRATE PLAYERMODEL"] = "models/player/custom_player/eminem/big_head/tm_pirate.mdl",
    ["Ct urban"] = "models/player/custom_player/FunMod/ct_urban.mdl",
    ["Клоун"] = "models/player/custom_player/legacy/gxp/bioshock/clown/clown_v1.mdl",
    ["townman_v1"] = "models/player/custom_player/legacy/gxp/bioshock/man1/townman_v1.mdl",
    ["Rapture Combat Guy 01"] = "models/player/custom_player/legacy/gxp/bioshock/combat1/combat_v1.mdl",
	["boy_v1"] = "models/player/custom_player/legacy/gxp/bioshock/baguette_boy/boy_v1.mdl",
	["Трубочка"] = "models/player/custom_player/nf/milk_carton/milk_carton.mdl",
	["Miyu"] = "models/player/custom_player/kuristaja/cso2/miyu_schoolgirl/miyu.mdl",
	["Natalie"] = "models/player/custom_player/kuristaja/cso2/natalie/natalie.mdl",
	["mister"] = "models/player/custom_player/luffaren/mister_muscle.mdl",
	["chadthundercock"] = "models/player/custom_player/luffaren/chadthundercock.mdl",
	
}

local ct_player_models = {
 ["cirno"] = "models/player/custom_player/nuclearsilo/touhou/cirno/cirno.mdl",
    ["tm_jungle"] = "models/player/custom_player/legacy/tm_jungle_marxist.mdl",
    ["bananana"] = "models/player/custom_player/caleon1/peely/peely.mdl",
    ["deadpool"] = "models/player/custom_player/kuristaja/deadpool/deadpool.mdl",
    ["t_cyber"] = "models/player/custom_player/FunMod/t_cyber.mdl",
    ["PLAYERMODEL"] = "models/player/custom_player/eminem/big_head/ctm_idf.mdl",
    ["CT FBI PLAYERMODEL"] = "models/player/custom_player/eminem/big_head/ctm_fbi.mdl",
    ["PIRATE PLAYERMODEL"] = "models/player/custom_player/eminem/big_head/tm_pirate.mdl",
    ["Ct urban"] = "models/player/custom_player/FunMod/ct_urban.mdl",
    ["Клоун"] = "models/player/custom_player/legacy/gxp/bioshock/clown/clown_v1.mdl",
    ["townman_v1"] = "models/player/custom_player/legacy/gxp/bioshock/man1/townman_v1.mdl",
    ["Rapture Combat Guy 01"] = "models/player/custom_player/legacy/gxp/bioshock/combat1/combat_v1.mdl",
	["boy_v1"] = "models/player/custom_player/legacy/gxp/bioshock/baguette_boy/boy_v1.mdl",
	["Трубочка"] = "models/player/custom_player/nf/milk_carton/milk_carton.mdl",
	["Miyu"] = "models/player/custom_player/kuristaja/cso2/miyu_schoolgirl/miyu.mdl",
	["Natalie"] = "models/player/custom_player/kuristaja/cso2/natalie/natalie.mdl",
	["mister"] = "models/player/custom_player/luffaren/mister_muscle.mdl",
	["chadthundercock"] = "models/player/custom_player/luffaren/chadthundercock.mdl",
	
}

local model_names = {}
local model_names_ct = {}

for k,v in pairs(t_player_models) do
    table.insert(model_names, k)
end

for k,v in pairs(ct_player_models) do
    table.insert(model_names_ct, k)
end

local model_check = ui_new_multiselect('LUA', 'B', 'Agent changer', {"CT", "T"})
local localplayer_model_ct = ui_new_combobox("LUA", "B", "CT Model", model_names_ct)
local localplayer_model_t = ui_new_combobox("LUA", "B", "T Model", model_names)

-- Hit chance override functionality
local hitchance = {
    hc_in_air = ui_new_checkbox('LUA', 'B', 'Hit chance in air'),
    hit_chance_in_air = ui_new_slider('LUA', 'B', '\ninairhc', 0, 100, 50, true, '%'),
    hit_chance_ovr = ui_new_slider('LUA', 'B', 'Hit chance override', 0, 100, 50, true, '%'),
    hc_ovr_key = ui_new_hotkey('LUA', 'B', 'Hit chance override', false)
}
local hc_ref = ui_reference('RAGE', 'Aimbot', 'Minimum hit chance')
ui_set_visible(hc_ref, false)

local class_ptr = ffi.typeof("void***")
local rawientitylist = client_create_interface("client_panorama.dll", "VClientEntityList003") or error("VClientEntityList003 wasnt found", 2)
local ientitylist = ffi.cast(class_ptr, rawientitylist) or error("rawientitylist is nil", 2)
local get_client_entity = ffi.cast("get_client_entity_t", ientitylist[0][3]) or error("get_client_entity is nil", 2)

local rawivmodelinfo = client_create_interface("engine.dll", "VModelInfoClient004") or error("VModelInfoClient004 wasnt found", 2)
local ivmodelinfo = ffi.cast(class_ptr, rawivmodelinfo) or error("rawivmodelinfo is nil", 2)
local get_model_index = ffi.cast("get_model_index_t", ivmodelinfo[0][2]) or error("get_model_info is nil", 2)
local find_or_load_model = ffi.cast("find_or_load_model_t", ivmodelinfo[0][39]) or error("find_or_load_model is nil", 2)

local rawnetworkstringtablecontainer = client_create_interface("engine.dll", "VEngineClientStringTable001") or error("VEngineClientStringTable001 wasnt found", 2)
local networkstringtablecontainer = ffi.cast(class_ptr, rawnetworkstringtablecontainer) or error("rawnetworkstringtablecontainer is nil", 2)
local find_table = ffi.cast("find_table_t", networkstringtablecontainer[0][3]) or error("find_table is nil", 2)

-- Scope overlay functionality
local scope_overlay = ui_reference('VISUALS', 'Effects', 'Remove scope overlay')
local scope_lines = ui_new_checkbox('LUA', 'B', 'Custom scope lines')
local scope_lines_color = ui_new_color_picker('LUA', 'B', '\n scope_lines_color_picker', 0, 0, 0, 255)
local scope_lines_initial_pos = ui_new_slider('LUA', 'B', '\n scope_lines_initial_pos', 0, 500, 190)
local scope_lines_offset = ui_new_slider('LUA', 'B', '\n scope_lines_offset', 0, 500, 15)
local scope_lines_fade = ui_new_slider('LUA', 'B', 'Fade animation speed', 3, 20, 12, true, 'fr', 1, { [3] = 'Off' })
local m_alpha = 0
local clamp = function(v, min, max) local num = v; num = num < min and min or num; num = num > max and max or num; return num end

local function contains(tab, val)
    for i = 1, #tab do
        if tab[i] == val then
            return true
        end
    end
    return false
end

local function precache_model(modelname)
    local rawprecache_table = find_table(networkstringtablecontainer, "modelprecache") or error("couldnt find modelprecache", 2)
    if rawprecache_table then 
        local precache_table = ffi.cast(class_ptr, rawprecache_table) or error("couldnt cast precache_table", 2)
        if precache_table then 
            local add_string = ffi.cast("add_string_t", precache_table[0][8]) or error("add_string is nil", 2)
            find_or_load_model(ivmodelinfo, modelname)
            local idx = add_string(precache_table, false, modelname, -1, nil)
            if idx == -1 then 
                return false
            end
        end
    end
    return true
end

local function set_model_index(entity, idx)
    local raw_entity = get_client_entity(ientitylist, entity)
    if raw_entity then 
        local gce_entity = ffi.cast(class_ptr, raw_entity)
        local a_set_model_index = ffi.cast("set_model_index_t", gce_entity[0][75])
        if a_set_model_index == nil then 
            error("set_model_index is nil")
        end
        a_set_model_index(gce_entity, idx)
    end
end

local function change_model(ent, model)
    if model:len() > 5 then 
        if precache_model(model) == false then 
            error("invalid model", 2)
        end
        local idx = get_model_index(ivmodelinfo, model)
        if idx == -1 then 
            return
        end
        set_model_index(ent, idx)
    end
end

-- Menu glow functionality
local function lerp_color(c1, c2, t)
    local r = math_floor(c1[1] * (1 - t) + c2[1] * t + 0.5)
    local g = math_floor(c1[2] * (1 - t) + c2[2] * t + 0.5)
    local b = math_floor(c1[3] * (1 - t) + c2[3] * t + 0.5)
    local a = math_floor(c1[4] * (1 - t) + c2[4] * t + 0.5)
    return { r, g, b, a }
end

local function lerp(start, vend, time)
    return start + (vend - start) * time
end

local function rect_gradient(x, y, w, h, precision, t_left, t_right, b_left, b_right)
    if w < h then
        for i = 1, w, precision do
            local t = (i - 1 + 0.5) / (w - 1 + 0.5)
            local clr = lerp_color(t_left, t_right, t)
            local clr2 = lerp_color(b_left, b_right, t)
            renderer_gradient(x + i - 1, y, precision, h, clr[1], clr[2], clr[3], clr[4], clr2[1], clr2[2], clr2[3], clr2[4], false)
        end
    else
        for i = 1, h, precision do 
            local t = (i - 1 + 0.5) / (h - 1 + 0.5)
            local clr = lerp_color(t_left, b_left, t)
            local clr2 = lerp_color(t_right, b_right, t)
            renderer_gradient(x, y + i - 1, w, precision, clr[1], clr[2], clr[3], clr[4], clr2[1], clr2[2], clr2[3], clr2[4], true)
        end
    end
end

local function hsv2rgb(h, s, v, a)
    local r, g, b
    local i = math_floor(h * 6)
    local f = h * 6 - i
    local p = v * (1 - s)
    local q = v * (1 - f * s)
    local t = v * (1 - (1 - f) * s)
    i = i % 6
    if i == 0 then r, g, b = v, t, p
    elseif i == 1 then r, g, b = q, v, p
    elseif i == 2 then r, g, b = p, v, t
    elseif i == 3 then r, g, b = p, q, v
    elseif i == 4 then r, g, b = t, p, v
    elseif i == 5 then r, g, b = v, p, q
    end
    return r * 255, g * 255, b * 255, a * 255
end

local function __rgb(angle, rgb_split_ratio)
    local r, g, b, a = hsv2rgb(angle, 1, 1, 1)
    r = r * rgb_split_ratio
    g = g * rgb_split_ratio
    b = b * rgb_split_ratio
    return r, g, b, a
end

local menu_glow = {
    style = ui_new_combobox("LUA", "B", "\aD0D0D0FF \aEEEEEEFFTHENKMOD\a6CC312FFclub\aEEEEEEFF.\aFFCC00FFru\aD0D0D0FF \aFFFFFFFF Menu Glow", { "-", "gamesense.pub", "Manual" }),
    [1] = ui_new_color_picker("LUA", "B", "Glow Color 1", 255, 255, 255, 255),
    [2] = ui_new_color_picker("LUA", "B", "Glow Color 2", 255, 255, 255, 255),
    [3] = ui_new_color_picker("LUA", "B", "Glow Color 3", 255, 255, 255, 255),
    [4] = ui_new_color_picker("LUA", "B", "Glow Color 4", 255, 255, 255, 255),
    thickness = ui_new_slider("LUA", "B", "Glow Thickness", 1, 1000, 5, true, "px"),
    speed = ui_new_slider("LUA", "B", "Glow Speed", 1, 10, 5, true, "f"),
}

local menu_key = ui_reference("Misc", "Settings", "Menu key")
local menu_open = ui_is_menu_open()
local glow_style
local t = { 0, 0, 0, 0 } -- transparent color
local rgb_offset = { 0, 0.25, 0.5, 0.75 }
local fade_alpha = menu_open and 1 or 0
local key_down = false

local function visual_fix(element)
    glow_style = ui_get(element)
    for i = 1, 4 do
        ui_set_visible(menu_glow[i], glow_style == "Manual" and ui_get(master_switch))
    end
    ui_set_visible(menu_glow.thickness, glow_style ~= "-" and ui_get(master_switch))
    ui_set_visible(menu_glow.speed, glow_style == "gamesense.pub" and ui_get(master_switch))
end
ui_set_callback(menu_glow.style, visual_fix)
visual_fix(menu_glow.style)

-- Thirdperson functionality
local function thirdpersonValues()
    if not ui_get(master_switch) then return end
    if ui_get(onionThirdperson.collisionControl) then
        cvar.cam_collision:set_int(1)
    else
        cvar.cam_collision:set_int(0)
    end
    cvar.c_mindistance:set_int(ui_get(onionThirdperson.distanceControl))
    cvar.c_maxdistance:set_int(ui_get(onionThirdperson.distanceControl))
end

-- Auto-buy functionality
local function get_command(table, name)
    for i = 1, #table do
        if table[i].name == name then return table[i].command end
    end
end

local function run_buybot(e)
    if not ui_get(master_switch) then return end
    local userid = e.userid
    if userid and client_userid_to_entindex(userid) ~= entity_get_local_player() then return end
    if not ui_get(buybot_enabled) then return end
    local primary = ui_get(buybot_primary)
    local pistol = ui_get(buybot_pistol)
    local gear = ui_get(buybot_gear)
    local commands = {}
    table.insert(commands, get_command(primary_weapons, primary))
    table.insert(commands, get_command(secondary_weapons, pistol))
    for i = 1, #gear do
        table.insert(commands, get_command(gear_weapons, gear[i]))
    end
    table.insert(commands, "use weapon_knife;")
    client_exec(table.concat(commands, ""))
end

-- TrashTalk functionality
local function on_player_death(e)
    if not ui_get(master_switch) then return end
    local victim_userid, attacker_userid = e.userid, e.attacker
    if victim_userid == nil or attacker_userid == nil then return end
    local victim_entindex = client_userid_to_entindex(victim_userid)
    local attacker_entindex = client_userid_to_entindex(attacker_userid)
    local local_player = entity_get_local_player()
    if attacker_entindex == local_player and entity_is_enemy(victim_entindex) then
        if ui_get(trashtalk_enable) then
            local victim_name = entity_get_player_name(victim_entindex)
            local random_phrase = phrases[math.random(#phrases)]
            if ui_get(player_name_add) then
                random_phrase = " | " .. victim_name .. " | " .. random_phrase
            end
            client_exec("say " .. random_phrase)
        end
    end
    if victim_entindex == local_player and ui_get(deathtalk_enable) then
        client_exec("say " .. death_phrases[math.random(#death_phrases)])
    end
end

local function on_miss_log(e)
    if not ui_get(master_switch) then return end
    if not ui_get(missed_logs_chat) then return end
    local target = e.target
    if target == nil then return end
    local target_name = entity_get_player_name(target)
    local messages = {
        "Ой, мисснул " .. target_name .. ", но ты все равно отсосал!",
        "Как так, не попал в " .. target_name .. ", Pred Error похyй!",
        "Боже, " .. target_name .. ", тебе повезло, что чит не забустил!",
        "Ну что, " .. target_name .. ", наслаждаешься своей удачей?",
        "Мда, мисснул в " .. target_name .. "_Dayn :(.",
        "Ага, понял, мисснул в вымирающий вид Дoлбaеба | " .. target_name .. " |",
        "Чё, как дела? Тебя мать спасла, радуйся, что я миссаю в такое ничтожество, " .. target_name .. "_SunDyru",
        "Блин, мисснул в пустоту, " .. target_name .. "_pidor",
        "Ты слишком везучий, и я даже знаю почему... Мать дефает | " .. target_name .. " |",
        "| " .. target_name .. "_Jhurnuy | О боже, какой же ты нищий, даже слишком.",
        "Хаха, мисснул в говняный сервер. Ну и конечно членом в твою мать | " .. target_name .. " |"
    }
    client_exec("say " .. messages[math.random(#messages)])
end

-- Jumpscout functionality
local function on_setup_command(c)
    if not ui_get(master_switch) then return end
    if ui_get(jumpscout_enable) then
        local vel_x, vel_y = entity_get_prop(entity_get_local_player(), "m_vecVelocity")
        local vel = math.sqrt(vel_x^2 + vel_y^2)
        ui_set(air_strafe, not (c.in_jump and (vel < 10)) or ui.is_menu_open())
    end
    -- Hit chance override
    local lp = entity_get_local_player()
    if lp == nil or not entity_is_alive(lp) then return end
    local flags = entity_get_prop(lp, 'm_fFlags')
    local in_air = bit.band(flags, 1) ~= 1
    if in_air and ui_get(hitchance.hc_in_air) then
        ui_set(hc_ref, ui_get(hitchance.hit_chance_in_air))
    end
    if ui_get(hitchance.hc_ovr_key) then
        ui_set(hc_ref, ui_get(hitchance.hit_chance_ovr))
    end
end

-- Model changer functionality
local function on_pre_render()
    if not ui_get(master_switch) then return end
    local me = entity_get_local_player()
    if me == nil then return end
    local team = entity_get_prop(me, 'm_iTeamNum') 
    if (team == 2 and contains(ui_get(model_check), 'T') ) or (team == 3 and contains(ui_get(model_check), 'CT') ) then
        change_model(me, team == 2 and t_player_models[ui_get(localplayer_model_t)] or ct_player_models[ui_get(localplayer_model_ct)] )
    end
end

-- Force aspect ratio functionality
local screen_width, screen_height, aspect_ratio_reference
local function set_aspect_ratio(aspect_ratio_multiplier)
    local screen_width, screen_height = client_screen_size()
    local aspectratio_value = (screen_width * aspect_ratio_multiplier) / screen_height
    if aspect_ratio_multiplier == 1 then aspectratio_value = 0 end
    client_set_cvar("r_aspectratio", tonumber(aspectratio_value))
end

local function gcd(m, n)
    while m ~= 0 do m, n = math_fmod(n, m), m end
    return n
end

local function on_aspect_ratio_changed()
    if not ui_get(master_switch) then return end
    local aspect_ratio = ui_get(aspect_ratio_reference) * 0.01
    aspect_ratio = 2 - aspect_ratio
    set_aspect_ratio(aspect_ratio)
end

local function setup_aspect_ratio(screen_width_temp, screen_height_temp)
    screen_width, screen_height = screen_width_temp, screen_height_temp
    local aspect_ratio_table = {}
    for i = 1, 200 do
        local i2 = (200 - i) * 0.01
        local divisor = gcd(screen_width * i2, screen_height)
        if screen_width * i2 / divisor < 100 or i2 == 1 then
            aspect_ratio_table[i] = screen_width * i2 / divisor .. ":" .. screen_height / divisor
        end
    end
    if aspect_ratio_reference then
        ui_set_visible(aspect_ratio_reference, false)
        ui_set_callback(aspect_ratio_reference, function() end)
    end
    aspect_ratio_reference = ui_new_slider("LUA", "B", "Force aspect ratio", 0, 199, 100, true, "%", 1, aspect_ratio_table)
    ui_set_callback(aspect_ratio_reference, on_aspect_ratio_changed)
end

-- Scope overlay paint functionality
local function scope_paint()
    if not ui_get(master_switch) or not ui_get(scope_lines) then 
        ui_set(scope_overlay, true)
        m_alpha = 0
        return 
    end
    ui_set(scope_overlay, false)

    local width, height = client_screen_size()
    local offset, initial_position, speed, color =
        ui_get(scope_lines_offset) * height / 1080, 
        ui_get(scope_lines_initial_pos) * height / 1080, 
        ui_get(scope_lines_fade), { ui_get(scope_lines_color) }

    local me = entity_get_local_player()
    local wpn = entity_get_player_weapon(me)

    local scope_level = entity_get_prop(wpn, 'm_zoomLevel')
    local scoped = entity_get_prop(me, 'm_bIsScoped') == 1
    local resume_zoom = entity_get_prop(me, 'm_bResumeZoom') == 1

    local is_valid = entity_is_alive(me) and wpn ~= nil and scope_level ~= nil
    local act = is_valid and scope_level > 0 and scoped and not resume_zoom

    local FT = speed > 3 and globals_frametime() * speed or 1
    local alpha = easing.linear(m_alpha, 0, 1, 1)

    renderer_gradient(width/2 - initial_position + 2, height / 2, initial_position - offset, 1, color[1], color[2], color[3], 0, color[1], color[2], color[3], alpha*color[4], true)
    renderer_gradient(width/2 + offset, height / 2, initial_position - offset, 1, color[1], color[2], color[3], alpha*color[4], color[1], color[2], color[3], 0, true)
    renderer_gradient(width / 2, height/2 - initial_position + 2, 1, initial_position - offset, color[1], color[2], color[3], 0, color[1], color[2], color[3], alpha*color[4], false)
    renderer_gradient(width / 2, height/2 + offset, 1, initial_position - offset, color[1], color[2], color[3], alpha*color[4], color[1], color[2], color[3], 0, false)
    
    m_alpha = clamp(m_alpha + (act and FT or -FT), 0, 1)
end

-- UI visibility and callback setup
local function update_ui_visibility()
    local enabled = ui_get(master_switch)
    ui_set_visible(onionThirdperson.collisionControl, enabled)
    ui_set_visible(onionThirdperson.distanceControl, enabled)
    ui_set_visible(viewmodelControls.xSlider, enabled)
    ui_set_visible(viewmodelControls.ySlider, enabled)
    ui_set_visible(viewmodelControls.zSlider, enabled)
    ui_set_visible(viewmodelControls.fovSlider, enabled)
    ui_set_visible(buybot_enabled, enabled)
    ui_set_visible(buybot_primary, enabled and ui_get(buybot_enabled))
    ui_set_visible(buybot_pistol, enabled and ui_get(buybot_enabled))
    ui_set_visible(buybot_gear, enabled and ui_get(buybot_enabled))
    ui_set_visible(trashtalk_enable, enabled)
    ui_set_visible(deathtalk_enable, enabled)
    ui_set_visible(player_name_add, enabled)
    ui_set_visible(missed_logs_chat, enabled)
    ui_set_visible(jumpscout_enable, enabled)
    ui_set_visible(aspect_ratio_reference, enabled)
    ui_set_visible(model_check, enabled)
    ui_set_visible(localplayer_model_t, enabled and contains(ui_get(model_check), 'T'))
    ui_set_visible(localplayer_model_ct, enabled and contains(ui_get(model_check), 'CT'))
    ui_set_visible(menu_glow.style, enabled)
    ui_set_visible(menu_glow.thickness, enabled and glow_style ~= "-")
    ui_set_visible(menu_glow.speed, enabled and glow_style == "gamesense.pub")
    ui_set_visible(scope_lines, enabled)
    ui_set_visible(scope_lines_color, enabled and ui_get(scope_lines))
    ui_set_visible(scope_lines_initial_pos, enabled and ui_get(scope_lines))
    ui_set_visible(scope_lines_offset, enabled and ui_get(scope_lines))
    ui_set_visible(scope_lines_fade, enabled and ui_get(scope_lines))
    ui_set_visible(scope_overlay, enabled and not ui_get(scope_lines))
    ui_set_visible(hitchance.hc_in_air, enabled)
    ui_set_visible(hitchance.hit_chance_in_air, enabled and ui_get(hitchance.hc_in_air))
    ui_set_visible(hitchance.hit_chance_ovr, enabled)
    ui_set_visible(hitchance.hc_ovr_key, enabled)
    for i = 1, 4 do
        ui_set_visible(menu_glow[i], enabled and glow_style == "Manual")
    end
end

local function hitchance_ui_callback(self)
    ui_set_visible(hitchance.hit_chance_in_air, ui_get(self) and ui_get(master_switch))
end

ui_set_callback(master_switch, update_ui_visibility)
ui_set_callback(onionThirdperson.collisionControl, thirdpersonValues)
ui_set_callback(onionThirdperson.distanceControl, thirdpersonValues)
ui_set_callback(buybot_enabled, function()
    local enabled = ui_get(buybot_enabled) and ui_get(master_switch)
    ui_set_visible(buybot_primary, enabled)
    ui_set_visible(buybot_pistol, enabled)
    ui_set_visible(buybot_gear, enabled)
end)
ui_set_callback(model_check, function()
    local enabled = ui_get(master_switch)
    ui_set_visible(localplayer_model_t, enabled and contains(ui_get(model_check), 'T'))
    ui_set_visible(localplayer_model_ct, enabled and contains(ui_get(model_check), 'CT'))
end)
ui_set_callback(menu_glow.style, visual_fix)
ui_set_callback(scope_lines, function(c)
    local enabled = ui_get(c) and ui_get(master_switch)
    ui_set_visible(scope_lines_color, enabled)
    ui_set_visible(scope_lines_initial_pos, enabled)
    ui_set_visible(scope_lines_offset, enabled)
    ui_set_visible(scope_lines_fade, enabled)
    ui_set_visible(scope_overlay, not enabled)
end)
ui_set_callback(hitchance.hc_in_air, hitchance_ui_callback)

-- Event callbacks
client_set_event_callback("player_death", on_player_death)
client_set_event_callback("aim_miss", on_miss_log)
client_set_event_callback("player_spawn", run_buybot)
client_set_event_callback("setup_command", on_setup_command)
client_set_event_callback("pre_render", on_pre_render)
client_set_event_callback("player_connect_full", function(event)
    if not ui_get(master_switch) then return end
    if client_userid_to_entindex(event.userid) ~= entity_get_local_player() then return end
end)
client_set_event_callback("paint_ui", function()
    if ui_get(master_switch) and ui_get(scope_lines) then
        ui_set(scope_overlay, true)
    end
end)
client_set_event_callback("paint", function(ctx)
    if not ui_get(master_switch) then return end
    
    -- Original paint functionality
    local screen_width_temp, screen_height_temp = client_screen_size()
    if screen_width_temp ~= screen_width or screen_height_temp ~= screen_height then
        setup_aspect_ratio(screen_width_temp, screen_height_temp)
    end
    local viewmodel_fov = fO + ui_get(viewmodelControls.fovSlider)
    local x = ui_get(viewmodelControls.xSlider)
    local y = ui_get(viewmodelControls.ySlider)
    local z = ui_get(viewmodelControls.zSlider)
    client_set_cvar("viewmodel_offset_x", x)
    client_set_cvar("viewmodel_offset_y", y)
    client_set_cvar("viewmodel_offset_z", z)
    client_set_cvar("viewmodel_fov", viewmodel_fov)

    -- Menu glow functionality
    if glow_style ~= "-" then
        local p = vector(ui_menu_position())
        local s = vector(ui_menu_size())
        local m_key = ui_get(menu_key)
        if not key_down and m_key then
            key_down = true
            menu_open = not menu_open
            client_delay_call(0.3, function() menu_open = ui_is_menu_open() end)
        end
        if not m_key then
            key_down = false
        end
        fade_alpha = lerp(fade_alpha, (menu_open and 1 or 0), globals_frametime() * 20)
        if fade_alpha >= 0.05 then
            local rgb_split_ratio = 1
            local time = globals_realtime() * (ui_get(menu_glow.speed) / 10) % 1
            local thickness = ui_get(menu_glow.thickness) + 1
            local c = {}
            if glow_style == "Manual" then
                for i = 1, 4 do
                    c[i] = { ui_get(menu_glow[i]) }
                end
            else
                for i = 1, 4 do
                    c[i] = { __rgb((time - rgb_offset[i]) % 1, rgb_split_ratio) }
                end
            end
            c[1][4] = c[1][4] * fade_alpha
            c[2][4] = c[2][4] * fade_alpha
            c[3][4] = c[3][4] * fade_alpha
            c[4][4] = c[4][4] * fade_alpha
            local precs = 1
            rect_gradient(p.x, p.y - thickness, s.x, thickness, precs, t, t, c[1], c[2])
            rect_gradient(p.x, p.y + s.y, s.x, thickness, precs, c[4], c[3], t, t)
            rect_gradient(p.x - thickness , p.y, thickness, s.y , precs, t, c[1], t, c[4])
            rect_gradient(p.x + s.x, p.y, thickness, s.y, precs, c[2], t, c[3], t)
            rect_gradient(p.x - thickness  , p.y - thickness   , thickness, thickness, precs, t, t, t, c[1])
            rect_gradient(p.x - thickness  , p.y + s.y         , thickness, thickness, precs, t, c[4], t, t)
            rect_gradient(p.x + s.x        , p.y - thickness   , thickness, thickness, precs, t, t, c[2], t)
            rect_gradient(p.x + s.x        , p.y + s.y         , thickness, thickness, precs, c[3], t, t, t)
        end
    end

    -- Scope overlay paint
    scope_paint()

    -- Hit chance indicator
    if ui_get(hitchance.hc_ovr_key) then
        renderer_indicator(255, 255, 255, 255, "H1TCHANCE")
    end
end)
client_set_event_callback("shutdown", function()
    ui_set_visible(hc_ref, true)
end)

-- Initial setup
thirdpersonValues()
setup_aspect_ratio(client_screen_size())
ui_set_visible(buybot_primary, false)
ui_set_visible(buybot_pistol, false)
ui_set_visible(buybot_gear, false)
ui_set_visible(model_check, false)
ui_set_visible(localplayer_model_t, false)
ui_set_visible(localplayer_model_ct, false)
ui_set_visible(menu_glow.style, false)
ui_set_visible(menu_glow.thickness, false)
ui_set_visible(menu_glow.speed, false)
ui_set_visible(scope_lines, false)
ui_set_visible(scope_lines_color, false)
ui_set_visible(scope_lines_initial_pos, false)
ui_set_visible(scope_lines_offset, false)
ui_set_visible(scope_lines_fade, false)
ui_set_visible(hitchance.hc_in_air, false)
ui_set_visible(hitchance.hit_chance_in_air, false)
ui_set_visible(hitchance.hit_chance_ovr, false)
ui_set_visible(hitchance.hc_ovr_key, false)
for i = 1, 4 do
    ui_set_visible(menu_glow[i], false)
end
update_ui_visibility()