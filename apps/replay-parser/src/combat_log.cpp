#include "combat_log.hpp"

#include <cstring>

#include "pb_lite.hpp"

namespace dota::demo {

const char* combat_log_type_name(int32_t t) {
    switch (CombatLogType(t)) {
        case CombatLogType::Damage: return "DAMAGE";
        case CombatLogType::Heal: return "HEAL";
        case CombatLogType::ModifierAdd: return "MODIFIER_ADD";
        case CombatLogType::ModifierRemove: return "MODIFIER_REMOVE";
        case CombatLogType::Death: return "DEATH";
        case CombatLogType::Ability: return "ABILITY";
        case CombatLogType::Item: return "ITEM";
        case CombatLogType::Location: return "LOCATION";
        case CombatLogType::Gold: return "GOLD";
        case CombatLogType::GameState: return "GAME_STATE";
        case CombatLogType::XP: return "XP";
        case CombatLogType::Purchase: return "PURCHASE";
        case CombatLogType::Buyback: return "BUYBACK";
    }
    return "OTHER";
}

namespace {

std::string resolve(const StringTable* names, uint64_t idx) {
    if (names) {
        auto it = names->entries.find(int32_t(idx));
        if (it != names->entries.end()) return it->second.key;
    }
    return "#" + std::to_string(idx);
}

float f32(uint64_t bits) {
    float v;
    uint32_t b = uint32_t(bits);
    std::memcpy(&v, &b, sizeof v);
    return v;
}

}  // namespace

CombatLogEntry parse_combat_log(std::string_view payload,
                                const StringTable* names) {
    CombatLogEntry e;
    pb::Reader r(payload);
    pb::Field f;
    while (pb::next_field(r, f)) {
        switch (f.number) {
            case 1: e.type = int32_t(f.varint); break;
            case 2: e.target_name = resolve(names, f.varint); break;
            case 4: e.attacker_name = resolve(names, f.varint); break;
            case 6: e.inflictor_name = resolve(names, f.varint); break;
            case 7: e.is_attacker_illusion = f.varint != 0; break;
            case 8: e.is_attacker_hero = f.varint != 0; break;
            case 9: e.is_target_illusion = f.varint != 0; break;
            case 10: e.is_target_hero = f.varint != 0; break;
            case 13: e.value = int64_t(f.varint); break;
            case 14: e.health = int32_t(f.varint); break;
            case 15: e.timestamp = f32(f.varint); break;
            case 21: e.location_x = f32(f.varint); break;
            case 22: e.location_y = f32(f.varint); break;
            case 23: e.gold_reason = int32_t(f.varint); break;
            case 26: e.xp_reason = int32_t(f.varint); break;
            default: break;
        }
    }
    return e;
}

}  // namespace dota::demo
