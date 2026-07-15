// demoinfo — CLI: сводка по файлу .dem (заголовок, матч-инфо, статистика кадров).
// Режим --deep дополнительно демультиплексирует внутренние сообщения
// DEM_Packet и загружает схему сущностей (ClassInfo + FlattenedSerializer).
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <set>
#include <memory>
#include <string>

#include "combat_log.hpp"
#include "fieldpath.hpp"
#include "entities.hpp"
#include "demo_reader.hpp"
#include "packet_demux.hpp"
#include "pb_lite.hpp"
#include "string_tables.hpp"

using dota::demo::DemoReader;

// Отладочный дампер структуры protobuf-сообщений неизвестного типа:
// печатает номера/типы/превью полей первых образцов.
static void probe_message(std::string_view payload, int depth = 0) {
    namespace pb = dota::pb;
    pb::Reader r(payload);
    pb::Field f;
    while (pb::next_field(r, f)) {
        std::printf("%*s#%u wt%u ", depth * 2 + 4, "", f.number, f.wire_type);
        if (f.wire_type == 2) {
            bool printable = !f.data.empty();
            for (char c : f.data.substr(0, 24))
                if (uint8_t(c) < 0x20 || uint8_t(c) > 0x7E) { printable = false; break; }
            if (printable) {
                std::printf("str \"%.*s\"%s\n", int(f.data.size() > 24 ? 24 : f.data.size()),
                            f.data.data(), f.data.size() > 24 ? "…" : "");
            } else {
                std::printf("bytes[%zu]\n", f.data.size());
                if (depth < 2 && f.data.size() < 200) probe_message(f.data, depth + 1);
            }
        } else if (f.wire_type == 5) {
            float fl;
            uint32_t b = uint32_t(f.varint);
            std::memcpy(&fl, &b, 4);
            std::printf("f32 %g\n", fl);
        } else {
            std::printf("varint %llu\n", (unsigned long long)f.varint);
        }
    }
}

static const char* winner_name(int64_t w) {
    if (w == 2) return "Radiant";
    if (w == 3) return "Dire";
    return "Unknown";
}

// JSON-эскейп строки: имена игроков произвольны (кавычки, backslash,
// управляющие символы), а сводка --summary читается машиной.
static std::string json_escape(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 8);
    for (unsigned char c : s) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof buf, "\\u%04x", c);
                    out += buf;
                } else {
                    out += char(c);
                }
        }
    }
    return out;
}

// Достать int64 из watched-поля сущности; def — если поля нет.
static int64_t watched_int(const dota::demo::Entity& e, const char* key,
                           int64_t def = 0) {
    auto it = e.watched.find(key);
    if (it == e.watched.end()) return def;
    if (auto* u = std::get_if<uint64_t>(&it->second)) return int64_t(*u);
    if (auto* s = std::get_if<int64_t>(&it->second)) return *s;
    return def;
}

static void deep_scan(DemoReader& reader, uint32_t probe_type, int probe_limit,
                      const char* events_path, const char* entities_path,
                      const char* economy_path) {
    bool want_entities = entities_path != nullptr || economy_path != nullptr;
    FILE* entities_out = entities_path ? std::fopen(entities_path, "w") : nullptr;
    FILE* economy_out = economy_path ? std::fopen(economy_path, "w") : nullptr;
    using dota::demo::InnerMsg;
    namespace demo = dota::demo;
    namespace pb = dota::pb;

    std::map<uint32_t, uint64_t> inner_hist;
    demo::StringTables tables;
    demo::ClassInfo class_info;
    demo::SendTables send_tables;
    uint64_t inner_total = 0;
    int probed = 0;

    // Сущности: создаётся после загрузки SendTables/ClassInfo.
    std::unique_ptr<demo::Entities> entities;
    bool entities_failed = false;
    uint64_t pos_samples = 0;

    // Combat log: агрегаты и лента убийств героев.
    std::map<int32_t, uint64_t> cl_hist;
    struct Kill { float t; std::string victim, killer, inflictor; };
    std::vector<Kill> hero_kills;
    FILE* events_out = events_path ? std::fopen(events_path, "w") : nullptr;

    auto on_inner = [&](const InnerMsg& m) {
        inner_hist[m.type]++;
        inner_total++;
        if (m.type == 44) tables.create(m.payload);
        else if (m.type == 45) tables.update(m.payload);
        else if (m.type == 55 && entities && !entities_failed) {
            if (!entities->on_packet_entities(m.payload)) {
                entities_failed = true;
                std::printf("  !! entity decode desync at packet %llu\n",
                            (unsigned long long)entities->packets_processed());
            }
        }
        else if (m.type == demo::kMsgCombatLogDataHLTV) {
            auto e = demo::parse_combat_log(m.payload,
                                            tables.by_name("CombatLogNames"));
            cl_hist[e.type]++;
            bool is_hero_death =
                e.type == int32_t(demo::CombatLogType::Death) &&
                e.is_target_hero && !e.is_target_illusion;
            if (is_hero_death) {
                hero_kills.push_back({e.timestamp, e.target_name,
                                      e.attacker_name, e.inflictor_name});
            }
            if (events_out) {
                std::fprintf(events_out,
                    "{\"type\":\"%s\",\"t\":%.2f,\"attacker\":\"%s\","
                    "\"target\":\"%s\",\"inflictor\":\"%s\",\"value\":%lld,"
                    "\"attacker_hero\":%d,\"target_hero\":%d}\n",
                    demo::combat_log_type_name(e.type), e.timestamp,
                    e.attacker_name.c_str(), e.target_name.c_str(),
                    e.inflictor_name.c_str(), (long long)e.value,
                    e.is_attacker_hero ? 1 : 0, e.is_target_hero ? 1 : 0);
            }
        }
        if (probe_type != 0 && m.type == probe_type && probed < probe_limit) {
            std::printf("---- probe msg type %u, sample %d, %zu bytes ----\n",
                        m.type, probed, m.payload.size());
            probe_message(m.payload);
            probed++;
        }
    };

    uint32_t last_sample_tick = 0;
    auto t0 = std::chrono::steady_clock::now();
    reader.scan([&](const demo::Frame& fr) {
        if (entities && !entities_failed && fr.tick != 0xFFFFFFFFu &&
            fr.tick >= last_sample_tick + 300) {
            last_sample_tick = fr.tick;
            entities->each_hero([&](const demo::Entity& e) {
                float x, y;
                if (demo::Entities::world_pos(e, x, y)) {
                    pos_samples++;
                    if (entities_out) {
                        std::fprintf(entities_out,
                            "{\"tick\":%u,\"class\":\"%s\",\"x\":%.1f,\"y\":%.1f}\n",
                            fr.tick, e.class_name.c_str(), x, y);
                    }
                }
            });
            if (economy_out) {
                for (const auto& [idx, e] : entities->all()) {
                    int team;
                    if (e.class_name == "CDOTA_DataRadiant") team = 2;
                    else if (e.class_name == "CDOTA_DataDire") team = 3;
                    else continue;
                    for (int slot = 0; slot < 5; slot++) {
                        char key[64];
                        auto field = [&](const char* name) {
                            std::snprintf(key, sizeof key,
                                          "m_vecDataTeam.%d.%s", slot, name);
                            return watched_int(e, key);
                        };
                        std::fprintf(economy_out,
                            "{\"tick\":%u,\"team\":%d,\"slot\":%d,"
                            "\"net_worth\":%lld,\"total_gold\":%lld,"
                            "\"total_xp\":%lld,\"lh\":%lld,\"dn\":%lld}\n",
                            fr.tick, team, slot,
                            (long long)field("m_iNetWorth"),
                            (long long)field("m_iTotalEarnedGold"),
                            (long long)field("m_iTotalEarnedXP"),
                            (long long)field("m_iLastHitCount"),
                            (long long)field("m_iDenyCount"));
                    }
                }
            }
        }
        switch (demo::Cmd(fr.cmd)) {
            case demo::Cmd::Packet:
            case demo::Cmd::SignonPacket:
                demo::demux_packet(fr.payload, on_inner);
                break;
            case demo::Cmd::FullPacket:
                break;  // снапшоты для перемотки; линейному чтению не нужны
            case demo::Cmd::ClassInfo:
                class_info = demo::parse_class_info(fr.payload);
                if (want_entities && !send_tables.serializers.empty()) {
                    entities = std::make_unique<demo::Entities>(
                        send_tables, class_info, tables);
                }
                break;
            case demo::Cmd::SendTables:
                send_tables = demo::parse_send_tables(fr.payload);
                break;
            default:
                break;
        }
    });
    auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                  std::chrono::steady_clock::now() - t0).count();

    std::printf("== Deep scan ==\n");
    std::printf("  inner_messages : %llu (time %lld ms)\n",
                (unsigned long long)inner_total, (long long)ms);
    std::printf("  classes        : %zu\n", class_info.classes.size());
    std::printf("  serializers    : %zu (fields %zu, symbols %zu)\n",
                send_tables.serializers.size(), send_tables.fields.size(),
                send_tables.symbols.size());
    std::printf("  string_tables  : %zu\n", tables.count());
    if (const auto* cl = tables.by_name("CombatLogNames")) {
        std::printf("  CombatLogNames : %zu entries; first 10:\n",
                    cl->entries.size());
        int shown = 0;
        for (const auto& [idx, e] : cl->entries) {
            if (shown++ >= 10) break;
            std::printf("    [%d] %s\n", idx, e.key.c_str());
        }
    }
    if (std::getenv("DUMP_HUFFMAN")) {
        demo::FieldPathDecoder fpd({true, true, true});
        auto codes = fpd.debug_codes();
        for (size_t i = 0; i < codes.size(); i++)
            std::printf("%zu\t%s\n", i, codes[i].c_str());
    }
    if (const char* dsn = std::getenv("DUMP_SER_NAME")) {
        std::printf("== All serializers named %s ==\n", dsn);
        for (size_t i = 0; i < send_tables.serializers.size(); i++) {
            const auto& s = send_tables.serializers[i];
            if (s.name == dsn) {
                std::printf("  #%zu v%d (%zu fields)\n", i, s.version, s.field_indexes.size());
            }
        }
    }
    if (const char* ds = std::getenv("DUMP_SER")) {
        size_t idx = size_t(std::atoi(ds));
        if (idx < send_tables.serializers.size()) {
            const auto& s = send_tables.serializers[idx];
            std::printf("== Dump serializer #%zu: %s (v%d, %zu fields) ==\n",
                        idx, s.name.c_str(), s.version, s.field_indexes.size());
            for (size_t i = 0; i < s.field_indexes.size(); i++) {
                const auto& f = send_tables.fields[size_t(s.field_indexes[i])];
                std::printf("  [%zu] %-30s type=%-30s ser=%d enc=%s bc=%d\n",
                            i, f.var_name.c_str(), f.var_type.c_str(),
                            f.field_serializer, f.encoder.c_str(), f.bit_count);
            }
        } else {
            std::printf("serializer #%zu out of range (%zu total)\n", idx,
                        send_tables.serializers.size());
        }
    }
    // Отладочный дамп схемы конкретного класса: DUMP_CLASS=CWorld
    if (const char* dc = std::getenv("DUMP_CLASS")) {
        auto it = send_tables.by_name.find(dc);
        if (it != send_tables.by_name.end()) {
            const auto& s = send_tables.serializers[it->second];
            std::printf("== Dump class %s (v%d, %zu fields) ==\n",
                        s.name.c_str(), s.version, s.field_indexes.size());
            for (size_t i = 0; i < s.field_indexes.size(); i++) {
                const auto& f = send_tables.fields[size_t(s.field_indexes[i])];
                std::printf("  [%zu] %-30s type=%-30s ser=%d enc=%s bc=%d model=%d elem=%s low=%g high=%g flags=%d\n",
                            i, f.var_name.c_str(), f.var_type.c_str(),
                            f.field_serializer, f.encoder.c_str(), f.bit_count,
                            int(f.model), f.element_type.c_str(), f.low_value, f.high_value, f.encode_flags);
            }
        } else {
            std::printf("class %s not found\n", dc);
        }
    }
    if (const auto* bl = tables.by_name("instancebaseline")) {
        std::printf("  instancebaseline: %zu entries (baseline bitstreams)\n",
                    bl->entries.size());
    }
    std::printf("  inner_by_type  :\n");
    for (const auto& [type, n] : inner_hist) {
        const char* name = demo::inner_msg_name(type);
        std::printf("    %-36s (%3u) %llu\n", name ? name : "?", type,
                    (unsigned long long)n);
    }
    // Sanity: сериализатор героя должен присутствовать в схеме.
    auto it = send_tables.by_name.find("CDOTA_Unit_Hero_Puck");
    if (it != send_tables.by_name.end()) {
        const auto& s = send_tables.serializers[it->second];
        std::printf("  sample class   : %s (fields %zu)\n", s.name.c_str(),
                    s.field_indexes.size());
    }

    if (!cl_hist.empty()) {
        uint64_t cl_total = 0;
        for (const auto& [t, n] : cl_hist) cl_total += n;
        std::printf("== Combat log ==\n");
        std::printf("  entries        : %llu\n", (unsigned long long)cl_total);
        for (const auto& [t, n] : cl_hist) {
            std::printf("    %-16s %llu\n", demo::combat_log_type_name(t),
                        (unsigned long long)n);
        }
        std::printf("  hero_kills     : %zu\n", hero_kills.size());
        size_t show = hero_kills.size() < 8 ? hero_kills.size() : 8;
        for (size_t i = 0; i < show; i++) {
            const auto& k = hero_kills[i];
            std::printf("    [%6.1fs] %s -> %s (%s)\n", k.t, k.killer.c_str(),
                        k.victim.c_str(), k.inflictor.c_str());
        }
    }
    if (events_out) {
        std::fclose(events_out);
        std::printf("  events written : %s\n", events_path);
    }
    if (entities) {
        std::printf("== Entities ==\n");
        std::printf("  huffman        : %s\n", entities->huffman_variant_name());
        std::printf("  packets        : %llu%s\n",
                    (unsigned long long)entities->packets_processed(),
                    entities_failed ? " (DESYNC)" : "");
        std::printf("  creates/updates: %llu / %llu\n",
                    (unsigned long long)entities->creates(),
                    (unsigned long long)entities->updates());
        std::printf("  live entities  : %zu\n", entities->all().size());
        std::printf("  pos_samples    : %llu\n", (unsigned long long)pos_samples);
        std::printf("  heroes (final) :\n");
        entities->each_hero([&](const demo::Entity& e) {
            float x, y;
            bool ok = demo::Entities::world_pos(e, x, y);
            std::printf("    %-34s idx=%d pos=(%.0f, %.0f)%s\n",
                        e.class_name.c_str(), e.index, ok ? x : 0.f,
                        ok ? y : 0.f, ok ? "" : " [no pos]");
        });
        std::printf("  net worth (final, per slot) :\n");
        for (const auto& [idx, e] : entities->all()) {
            if (e.class_name != "CDOTA_DataRadiant" &&
                e.class_name != "CDOTA_DataDire")
                continue;
            std::printf("    %s:", e.class_name.c_str());
            // m_vecDataTeam.NNNN.m_iNetWorth — по 5 слотов на команду.
            for (int slot = 0; slot < 5; slot++) {
                char key[64];
                std::snprintf(key, sizeof key, "m_vecDataTeam.%d.m_iNetWorth",
                              slot);
                auto it = e.watched.find(key);
                if (it == e.watched.end()) { std::printf(" -"); continue; }
                if (auto* u = std::get_if<uint64_t>(&it->second))
                    std::printf(" %llu", (unsigned long long)*u);
                else if (auto* s = std::get_if<int64_t>(&it->second))
                    std::printf(" %lld", (long long)*s);
                else
                    std::printf(" ?");
            }
            std::printf("\n");
        }
    }
    if (entities_out) {
        std::fclose(entities_out);
        std::printf("  positions written: %s\n", entities_path);
    }
    if (economy_out) {
        std::fclose(economy_out);
        std::printf("  economy written  : %s\n", economy_path);
    }
}

int main(int argc, char** argv) {
    bool deep = false;
    uint32_t probe_type = 0;
    const char* path = nullptr;
    const char* events_path = nullptr;
    const char* entities_path = nullptr;
    const char* economy_path = nullptr;
    const char* summary_path = nullptr;
    for (int i = 1; i < argc; i++) {
        if (std::strcmp(argv[i], "--deep") == 0) deep = true;
        else if (std::strcmp(argv[i], "--probe") == 0 && i + 1 < argc) {
            probe_type = uint32_t(std::atoi(argv[++i]));
            deep = true;
        }
        else if (std::strcmp(argv[i], "--events") == 0 && i + 1 < argc) {
            events_path = argv[++i];
            deep = true;
        }
        else if (std::strcmp(argv[i], "--entities") == 0 && i + 1 < argc) {
            entities_path = argv[++i];
            deep = true;
        }
        else if (std::strcmp(argv[i], "--economy") == 0 && i + 1 < argc) {
            economy_path = argv[++i];
            deep = true;
        }
        else if (std::strcmp(argv[i], "--summary") == 0 && i + 1 < argc) {
            summary_path = argv[++i];
        }
        else path = argv[i];
    }
    if (!path) {
        std::fprintf(stderr, "usage: %s [--deep] [--probe TYPE] [--events OUT.jsonl] [--entities OUT.jsonl] [--economy OUT.jsonl] [--summary OUT.json] <replay.dem>\n",
                     argv[0]);
        return 2;
    }
    try {
        DemoReader reader(path);

        auto header = reader.read_file_header();
        std::printf("== FileHeader ==\n");
        std::printf("  stamp            : %s\n", header.demo_file_stamp.c_str());
        std::printf("  map              : %s\n", header.map_name.c_str());
        std::printf("  server           : %s\n", header.server_name.c_str());
        std::printf("  network_protocol : %lld\n", (long long)header.network_protocol);
        std::printf("  build            : %lld\n", (long long)header.build_num);

        auto info = reader.read_file_info();
        std::printf("== FileInfo ==\n");
        std::printf("  match_id       : %llu\n", (unsigned long long)info.match_id);
        std::printf("  winner         : %s\n", winner_name(info.game_winner));
        std::printf("  game_mode      : %lld\n", (long long)info.game_mode);
        std::printf("  playback_time  : %.1f s (%lld ticks, %lld frames)\n",
                    info.playback_time_s, (long long)info.playback_ticks,
                    (long long)info.playback_frames);
        std::printf("  players        : %zu\n", info.players.size());
        for (const auto& p : info.players) {
            std::printf("    [team %lld] %-25s %s\n", (long long)p.game_team,
                        p.player_name.c_str(), p.hero_name.c_str());
        }

        if (summary_path) {
            FILE* sf = std::fopen(summary_path, "w");
            if (!sf) {
                std::fprintf(stderr, "error: cannot open %s\n", summary_path);
                return 1;
            }
            std::fprintf(sf,
                "{\"match_id\":%llu,\"winner\":\"%s\",\"game_mode\":%lld,"
                "\"playback_time_s\":%.1f,\"build\":%lld,\"players\":[",
                (unsigned long long)info.match_id, winner_name(info.game_winner),
                (long long)info.game_mode, info.playback_time_s,
                (long long)header.build_num);
            for (size_t i = 0; i < info.players.size(); i++) {
                const auto& p = info.players[i];
                std::fprintf(sf, "%s{\"team\":%lld,\"name\":\"%s\",\"hero\":\"%s\"}",
                             i ? "," : "", (long long)p.game_team,
                             json_escape(p.player_name).c_str(),
                             json_escape(p.hero_name).c_str());
            }
            std::fprintf(sf, "]}\n");
            std::fclose(sf);
            std::printf("  summary written: %s\n", summary_path);
        }

        auto t0 = std::chrono::steady_clock::now();
        auto st = reader.scan();
        auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                      std::chrono::steady_clock::now() - t0).count();

        std::printf("== Scan ==\n");
        std::printf("  file_size      : %.1f MiB\n", reader.file_size() / 1048576.0);
        std::printf("  frames         : %llu (%llu snappy-compressed)\n",
                    (unsigned long long)st.frames,
                    (unsigned long long)st.compressed_frames);
        std::printf("  payload        : %.1f MiB (decompressed %.1f MiB)\n",
                    st.payload_bytes / 1048576.0, st.decompressed_bytes / 1048576.0);
        std::printf("  last_tick      : %u\n", st.last_tick);
        std::printf("  scan_time      : %lld ms (%.1f MiB/s)\n", (long long)ms,
                    reader.file_size() / 1048576.0 / (ms / 1000.0));
        std::printf("  frames_by_cmd  :\n");
        for (const auto& [cmd, n] : st.frames_by_cmd) {
            std::printf("    %-24s %llu\n", dota::demo::cmd_name(cmd),
                        (unsigned long long)n);
        }
        if (deep) deep_scan(reader, probe_type, 3, events_path, entities_path,
                            economy_path);
        return 0;
    } catch (const std::exception& e) {
        std::fprintf(stderr, "error: %s\n", e.what());
        return 1;
    }
}
