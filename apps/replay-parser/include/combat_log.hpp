// combat_log — разбор CMsgDOTACombatLogEntry (msg id 554,
// DOTA_UM_CombatLogDataHLTV) с разрешением имён через string table
// CombatLogNames. Источник событий DAMAGE/HEAL/KILL/PURCHASE для
// таблицы ClickHouse ReplayEvents (Гл. 4.4, Гл. 5.3).
#pragma once

#include <cstdint>
#include <string>
#include <string_view>

#include "string_tables.hpp"

namespace dota::demo {

constexpr uint32_t kMsgCombatLogDataHLTV = 554;

// DOTA_COMBATLOG_TYPES (dota_shared_enums.proto).
enum class CombatLogType : int32_t {
    Damage = 0,
    Heal = 1,
    ModifierAdd = 2,
    ModifierRemove = 3,
    Death = 4,
    Ability = 5,
    Item = 6,
    Location = 7,
    Gold = 8,
    GameState = 9,
    XP = 10,
    Purchase = 11,
    Buyback = 12,
};

const char* combat_log_type_name(int32_t t);

struct CombatLogEntry {
    int32_t type = -1;
    // Индексы в CombatLogNames (сырые) и разрешённые имена.
    std::string target_name;
    std::string attacker_name;
    std::string inflictor_name;   // способность/предмет
    bool is_attacker_hero = false;
    bool is_target_hero = false;
    bool is_attacker_illusion = false;
    bool is_target_illusion = false;
    int64_t value = 0;            // урон/золото/ID предмета
    int32_t health = 0;
    float timestamp = 0.0f;       // игровые секунды
    float location_x = 0.0f;
    float location_y = 0.0f;
    int32_t gold_reason = -1;
    int32_t xp_reason = -1;
};

// Разобрать сообщение; имена резолвятся по текущему состоянию таблицы
// CombatLogNames (nullptr — оставить индексы нерезолвленными в виде "#N").
CombatLogEntry parse_combat_log(std::string_view payload,
                                const StringTable* names);

}  // namespace dota::demo
