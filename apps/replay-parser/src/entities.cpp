#include "entities.hpp"

#include <cmath>
#include <cstring>

#include "pb_lite.hpp"

#include <cstdio>
#include <cstdlib>

namespace dota::demo {

namespace {

// Наблюдаемые поля: позиции и экономика (Гл. 5.1, таблицы извлечения).
bool is_watched_field(const std::string& name) {
    auto ends_with = [&](const char* s) {
        size_t n = std::strlen(s);
        return name.size() >= n &&
               name.compare(name.size() - n, n, s) == 0;
    };
    return ends_with("m_cellX") || ends_with("m_cellY") ||
           ends_with("m_vecX") || ends_with("m_vecY") ||
           ends_with("m_iNetWorth") || ends_with("m_iTotalEarnedGold") ||
           ends_with("m_iTotalEarnedXP") || ends_with("m_iLastHitCount") ||
           ends_with("m_iDenyCount") ||
           ends_with("m_iCurrentLevel") || ends_with("m_iHealth");
}

bool is_watched_class(const std::string& cls) {
    return cls.rfind("CDOTA_Unit_Hero_", 0) == 0 ||
           cls == "CDOTA_DataRadiant" || cls == "CDOTA_DataDire" ||
           cls == "CDOTA_PlayerResource";
}

constexpr HuffmanVariant kVariants[8] = {
    {true, true, true},  {true, false, true},
    {false, true, true}, {false, false, true},
    {true, true, false},  {true, false, false},
    {false, true, false}, {false, false, false},
};
const char* kVariantNames[8] = {
    "zero1/tie-later/R", "zero1/tie-earlier/R",
    "zero0/tie-later/R", "zero0/tie-earlier/R",
    "zero1/tie-later/L", "zero1/tie-earlier/L",
    "zero0/tie-later/L", "zero0/tie-earlier/L",
};

}  // namespace

Entities::Entities(const SendTables& st, const ClassInfo& ci,
                   const StringTables& tables)
    : st_(st), ci_(ci), tables_(tables), resolver_(st) {
    uint32_t n = uint32_t(ci.classes.size());
    class_id_bits_ = 1;
    while ((1u << class_id_bits_) < n) class_id_bits_++;
}

const char* Entities::huffman_variant_name() const {
    return variant_ >= 0 ? kVariantNames[variant_] : "not-selected";
}

bool Entities::decode_entity_fields(const FieldPathDecoder& fpd,
                                    bits::BitReader& r, Entity& e,
                                    bool strict) {
    static const bool dbg = std::getenv("ENT_DEBUG") != nullptr;
    paths_scratch_.clear();
    if (!fpd.read_paths(r, paths_scratch_)) {
        if (dbg) std::fprintf(stderr, "    [dbg] read_paths failed for %s (paths so far %zu)\n",
                              e.class_name.c_str(), paths_scratch_.size());
        return false;
    }

    bool watch = is_watched_class(e.class_name);
    for (const auto& fp : paths_scratch_) {
        ResolvedField rf;
        if (!resolver_.resolve(e.serializer, fp, rf)) {
            if (dbg) {
                std::fprintf(stderr, "    [dbg] resolve failed %s path=[", e.class_name.c_str());
                for (int i = 0; i <= fp.last; i++) std::fprintf(stderr, "%d ", fp.path[i]);
                std::fprintf(stderr, "]\n");
            }
            return false;  // путь не существует в схеме → desync
        }
        static const char* dbg2 = std::getenv("ENT_DEBUG");
        static const char* raw_peek = std::getenv("RAW_PEEK");
        uint64_t bit_before = r.pos_bits();
        if (raw_peek && rf.full_name == raw_peek) {
            auto peek = r;
            std::fprintf(stderr, "[raw] %s bit_count=%d low=%g high=%g flags=%d @%llu: ",
                        rf.full_name.c_str(), rf.bit_count, rf.low, rf.high, rf.encode_flags,
                        (unsigned long long)bit_before);
            for (int i = 0; i < 24; i++) std::fprintf(stderr, "%u", peek.read_bits(1));
            std::fprintf(stderr, "\n");
        }
        FieldValue v;
        if (!decode_value(r, rf, v)) {
            if (dbg) std::fprintf(stderr, "    [dbg] decode_value failed %s.%s kind=%d\n",
                                  e.class_name.c_str(), rf.full_name.c_str(), int(rf.kind));
            return false;
        }
        if (dbg2 && dbg2[0] == '2') {
            char valbuf[64] = "";
            if (auto* b = std::get_if<bool>(&v)) snprintf(valbuf, 64, "%d", *b);
            else if (auto* u = std::get_if<uint64_t>(&v)) snprintf(valbuf, 64, "%llu", (unsigned long long)*u);
            else if (auto* si = std::get_if<int64_t>(&v)) snprintf(valbuf, 64, "%lld", (long long)*si);
            else if (auto* fl = std::get_if<float>(&v)) snprintf(valbuf, 64, "%.3f", *fl);
            else if (auto* st2 = std::get_if<std::string>(&v)) snprintf(valbuf, 64, "'%.40s'", st2->c_str());
            std::fprintf(stderr, "      [f] %-46s k=%d @%llu+%llu = %s (left %llu)\n",
                         rf.full_name.c_str(), int(rf.kind),
                         (unsigned long long)bit_before,
                         (unsigned long long)(r.pos_bits() - bit_before),
                         valbuf, (unsigned long long)r.remaining_bits());
        }
        if (strict && r.overflowed()) return false;
        if (watch && is_watched_field(rf.full_name)) {
            e.watched[rf.full_name] = v;
        }
    }
    return !r.overflowed();
}

bool Entities::apply_baseline(const FieldPathDecoder& fpd, Entity& e,
                              bool strict) {
    const auto* bl = tables_.by_name("instancebaseline");
    if (!bl) return true;
    for (const auto& [idx, entry] : bl->entries) {
        if (entry.key == std::to_string(e.class_id) && !entry.value.empty()) {
            static const bool dbg = std::getenv("ENT_DEBUG") != nullptr;
            if (dbg) std::fprintf(stderr, "    [baseline begin %s, %zu bytes]\n",
                                  e.class_name.c_str(), entry.value.size());
            bits::BitReader br(entry.value);
            bool ok = decode_entity_fields(fpd, br, e, strict);
            if (dbg) std::fprintf(stderr, "    [baseline end ok=%d left=%llu]\n",
                                  ok, (unsigned long long)br.remaining_bits());
            return ok || !strict;
        }
    }
    static const bool dbg = std::getenv("ENT_DEBUG") != nullptr;
    if (dbg) std::fprintf(stderr, "    [no baseline for class %d %s]\n",
                          e.class_id, e.class_name.c_str());
    return true;
}

bool Entities::decode_with(const FieldPathDecoder& fpd,
                           std::string_view entity_data, int32_t updated,
                           bool strict) {
    bits::BitReader r(entity_data);
    int32_t index = -1;

    static const bool dbg = std::getenv("ENT_DEBUG") != nullptr;
    for (int32_t i = 0; i < updated; i++) {
        index += int32_t(r.read_ubitvar()) + 1;
        uint32_t cmd = r.read_bits(2);
        if (r.overflowed()) {
            if (dbg) std::fprintf(stderr,
                "  [dbg] header overflow at ent %d/%d (remaining_before was near 0)\n",
                i, updated);
            return false;
        }
        if (dbg) std::fprintf(stderr, "  [dbg] ent %d/%d idx=%d cmd=%u remaining=%llu\n",
                              i, updated, index, cmd, (unsigned long long)r.remaining_bits());

        if ((cmd & 0x01) == 0) {
            if (cmd & 0x02) {  // create
                uint32_t class_id = r.read_bits(class_id_bits_);
                uint32_t serial = r.read_bits(17);
                uint64_t hdr_bit = r.pos_bits();
                uint32_t unknown = r.read_varuint32();     // unknown (Source 2)
                if (dbg) std::fprintf(stderr,
                    "    [hdr] class=%u serial=%u unknown=%u (varint %llu bits)\n",
                    class_id, serial, unknown,
                    (unsigned long long)(r.pos_bits() - hdr_bit));
                auto cit = ci_.classes.find(int32_t(class_id));
                if (cit == ci_.classes.end()) {
                    static const bool dbg2 = std::getenv("ENT_DEBUG") != nullptr;
                    if (dbg2) std::fprintf(stderr, "    [dbg] unknown class_id %u\n", class_id);
                    return false;
                }
                auto sit = st_.by_name.find(cit->second);
                if (sit == st_.by_name.end()) {
                    if (dbg) std::fprintf(stderr, "    [dbg] no serializer for class %s\n",
                                          cit->second.c_str());
                    return false;
                }

                Entity e;
                e.index = index;
                e.class_id = int32_t(class_id);
                e.class_name = cit->second;
                e.serializer = sit->second;
                if (dbg) {
                    std::fprintf(stderr, "    [dbg] create %s\n", e.class_name.c_str());
                    if (e.class_name == "CDOTA_PlayerResource") {
                        std::fprintf(stderr, "    [raw stream bits @%llu]: ",
                                     (unsigned long long)r.pos_bits());
                        auto save = r;
                        for (int b = 0; b < 64; b++) std::fprintf(stderr, "%u", save.read_bits(1));
                        std::fprintf(stderr, "\n");
                    }
                }
                if (!apply_baseline(fpd, e, strict)) return false;
                if (dbg) std::fprintf(stderr, "    [live delta begin @%llu]\n",
                                      (unsigned long long)r.pos_bits());
                if (!decode_entity_fields(fpd, r, e, strict)) return false;
                if (dbg) std::fprintf(stderr, "    [live delta end @%llu]\n",
                                      (unsigned long long)r.pos_bits());
                entities_[index] = std::move(e);
                creates_++;
            } else {  // delta-update
                auto it = entities_.find(index);
                if (it == entities_.end()) {
                    if (dbg) std::fprintf(stderr, "    [dbg] delta-update unknown idx=%d\n", index);
                    return false;
                }
                if (dbg) std::fprintf(stderr, "    [dbg] update %s idx=%d\n",
                                      it->second.class_name.c_str(), index);
                if (!decode_entity_fields(fpd, r, it->second, strict))
                    return false;
                it->second.active = true;
                updates_++;
            }
        } else {
            if (cmd & 0x02) entities_.erase(index);  // delete
            else {
                auto it = entities_.find(index);     // leave PVS
                if (it != entities_.end()) it->second.active = false;
            }
        }
    }
    // Хвост — только паддинг.
    static const bool dbg_final = std::getenv("ENT_DEBUG") != nullptr;
    if (dbg_final) {
        std::fprintf(stderr, "  [dbg] decode_with done: overflow=%d remaining=%llu updated=%d\n",
                     r.overflowed(), (unsigned long long)r.remaining_bits(), updated);
    }
    return !r.overflowed() && r.remaining_bits() < 8;
}

bool Entities::on_packet_entities(std::string_view msg_payload) {
    // CSVCMsg_PacketEntities { max_entries=1; updated_entries=2; is_delta=3;
    //   update_baseline=4; baseline=5; delta_from=6; entity_data=7 }
    int32_t updated = 0;
    std::string_view entity_data;
    pb::Reader r(msg_payload);
    pb::Field f;
    while (pb::next_field(r, f)) {
        switch (f.number) {
            case 2: updated = int32_t(f.varint); break;
            case 7: entity_data = f.data; break;
            default: break;
        }
    }
    if (entity_data.empty()) return true;

    if (!fpd_) {
        // Автоподбор huffman-варианта на первом сообщении: пробуем каждый
        // кандидат на копии состояния; выигрывает полный чистый декод.
        for (int v = 0; v < 8; v++) {
            FieldPathDecoder cand{kVariants[v]};
            auto backup = entities_;
            auto c0 = creates_; auto u0 = updates_;
            if (decode_with(cand, entity_data, updated, true)) {
                fpd_ = std::make_unique<FieldPathDecoder>(kVariants[v]);
                variant_ = v;
                packets_++;
                return true;
            }
            entities_ = std::move(backup);
            creates_ = c0; updates_ = u0;
        }
        return false;
    }

    packets_++;
    return decode_with(*fpd_, entity_data, updated, false);
}

void Entities::each_hero(const std::function<void(const Entity&)>& cb) const {
    for (const auto& [idx, e] : entities_) {
        if (e.active && e.class_name.rfind("CDOTA_Unit_Hero_", 0) == 0) cb(e);
    }
}

bool Entities::world_pos(const Entity& e, float& x, float& y) {
    auto get = [&](const char* suffix, float& out) {
        for (const auto& [name, v] : e.watched) {
            size_t n = std::strlen(suffix);
            if (name.size() >= n &&
                name.compare(name.size() - n, n, suffix) == 0) {
                if (auto* u = std::get_if<uint64_t>(&v)) { out = float(*u); return true; }
                if (auto* fl = std::get_if<float>(&v)) { out = *fl; return true; }
                if (auto* i = std::get_if<int64_t>(&v)) { out = float(*i); return true; }
            }
        }
        return false;
    };
    float cell_x = 0, cell_y = 0, vec_x = 0, vec_y = 0;
    if (!get("m_cellX", cell_x) || !get("m_cellY", cell_y)) return false;
    get("m_vecX", vec_x);
    get("m_vecY", vec_y);
    x = cell_x * 128.0f + vec_x - 16384.0f;
    y = cell_y * 128.0f + vec_y - 16384.0f;
    return true;
}

}  // namespace dota::demo
