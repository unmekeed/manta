#include "packet_demux.hpp"

#include <cstdlib>
#include <cstring>
#include <set>
#include <stdexcept>

#include "bit_reader.hpp"
#include "pb_lite.hpp"

namespace dota::demo {

const char* inner_msg_name(uint32_t type) {
    switch (type) {
        // NET_Messages
        case 4: return "net_Tick";
        case 6: return "net_SetConVar";
        case 7: return "net_SignonState";
        case 8: return "net_SpawnGroup_Load";
        // SVC_Messages
        case 40: return "svc_ServerInfo";
        case 41: return "svc_FlattenedSerializer";
        case 42: return "svc_ClassInfo";
        case 44: return "svc_CreateStringTable";
        case 45: return "svc_UpdateStringTable";
        case 47: return "svc_Sounds";
        case 48: return "svc_SetView";
        case 51: return "svc_ClearAllStringTables";
        case 55: return "svc_PacketEntities";
        case 62: return "svc_VoiceData";
        // EBaseGameEvents
        case 205: return "GE_Source1LegacyGameEventList";
        case 207: return "GE_Source1LegacyGameEvent";
        case 208: return "GE_SosStartSoundEvent";
        case 209: return "GE_SosStopSoundEvent";
        case 210: return "GE_SosSetSoundEventParams";
        case 212: return "GE_SosStopSoundEventHash";
        // EDotaUserMessages (частые)
        case 466: return "DOTA_UM_ChatEvent";
        case 467: return "DOTA_UM_CombatHeroPositions";
        case 471: return "DOTA_UM_CreateLinearProjectile";
        case 472: return "DOTA_UM_DestroyLinearProjectile";
        case 473: return "DOTA_UM_DodgeTrackingProjectiles";
        case 477: return "DOTA_UM_LocationPing";
        case 478: return "DOTA_UM_MapLine";
        case 481: return "DOTA_UM_MinimapEvent";
        case 483: return "DOTA_UM_OverheadEvent";
        case 485: return "DOTA_UM_SharedCooldown";
        case 486: return "DOTA_UM_SpectatorPlayerClick";
        case 488: return "DOTA_UM_UnitEvent";
        case 492: return "DOTA_UM_ItemPurchased";
        default: return nullptr;
    }
}

size_t demux_packet_raw(std::string_view data,
                        const std::function<void(const InnerMsg&)>& cb) {
    bits::BitReader br(data);
    size_t count = 0;
    std::vector<uint8_t> buf;
    // Хвост короче минимального сообщения (~2 байта) — просто паддинг.
    while (br.remaining_bits() >= 16) {
        uint32_t type = br.read_ubitvar();
        uint32_t size = br.read_varuint32();
        if (br.overflowed()) break;
        if (size > data.size()) {
            throw std::runtime_error("inner message size exceeds packet");
        }
        buf.resize(size);
        if (!br.read_bytes(buf.data(), size)) break;
        if (cb) {
            InnerMsg m;
            m.type = type;
            m.payload = {reinterpret_cast<const char*>(buf.data()), size};
            cb(m);
        }
        count++;
    }
    return count;
}

size_t demux_packet(std::string_view demo_packet_payload,
                    const std::function<void(const InnerMsg&)>& cb) {
    // CDemoPacket { bytes data = 3; }
    pb::Reader r(demo_packet_payload);
    pb::Field f;
    while (pb::next_field(r, f)) {
        if (f.number == 3 && f.wire_type == 2) {
            return demux_packet_raw(f.data, cb);
        }
    }
    return 0;
}

ClassInfo parse_class_info(std::string_view payload) {
    // CDemoClassInfo { repeated class_t classes = 1 {
    //   class_id = 1; network_name = 2; table_name = 3; } }
    ClassInfo out;
    pb::Reader r(payload);
    pb::Field f;
    while (pb::next_field(r, f)) {
        if (f.number != 1 || f.wire_type != 2) continue;
        pb::Reader cr(f.data);
        pb::Field cf;
        int32_t id = -1;
        std::string name;
        while (pb::next_field(cr, cf)) {
            if (cf.number == 1) id = int32_t(cf.varint);
            else if (cf.number == 2) name = std::string(cf.data);
        }
        if (id >= 0) out.classes[id] = std::move(name);
    }
    return out;
}

namespace {

float bits_to_float(uint64_t v) {
    uint32_t b = uint32_t(v);
    float out;
    std::memcpy(&out, &b, sizeof out);
    return out;
}

std::string sym(const std::vector<std::string>& symbols, uint64_t idx) {
    return idx < symbols.size() ? symbols[idx] : std::string();
}

// -- Определение модели поля (dotabuff/manta, sendtable.go) ------------------

// Типы, трактуемые как "указатель на структуру" даже без явного '*' в имени
// типа — портировано дословно из manta's pointerTypes.
const std::set<std::string>& pointer_types() {
    static const std::set<std::string> kSet = {
        "PhysicsRagdollPose_t", "CBodyComponent", "CEntityIdentity",
        "CPhysicsComponent", "CRenderComponent", "CDOTAGamerules",
        "CDOTAGameManager", "CDOTASpectatorGraphManager", "CPlayerLocalData",
        "CPlayer_CameraServices", "CDOTAGameRules",
    };
    return kSet;
}

// Разбор строки типа Source 2: базовый тип, generic-параметр (<...>),
// признак массива ([N] или [MACRO_NAME]), признак указателя (trailing '*').
struct ParsedType {
    std::string base;      // тип без generic/array/pointer обёрток
    std::string element;   // generic-параметр (для CUtlVector<T> — T)
    int array_flag = 0;    // >0, если было [N] или [MACRO] (точное N не важно)
    bool pointer = false;
};

ParsedType parse_var_type(const std::string& type) {
    ParsedType p;
    std::string s = type;
    if (!s.empty() && s.back() == '*') {
        p.pointer = true;
        s.pop_back();
    }
    auto lb = s.find('[');
    if (lb != std::string::npos) {
        std::string inside = s.substr(lb + 1);
        if (!inside.empty() && inside.back() == ']') inside.pop_back();
        int n = std::atoi(inside.c_str());
        p.array_flag = n > 0 ? n : (inside.empty() ? 0 : 1);
        s = s.substr(0, lb);
    }
    auto lt = s.find('<');
    if (lt != std::string::npos) {
        auto gt = s.rfind('>');
        if (gt != std::string::npos && gt > lt) {
            std::string inner = s.substr(lt + 1, gt - lt - 1);
            size_t b = inner.find_first_not_of(' ');
            size_t e = inner.find_last_not_of(' ');
            p.element = (b == std::string::npos) ? ""
                                                  : inner.substr(b, e - b + 1);
        }
        s = s.substr(0, lt);
        while (!s.empty() && s.back() == ' ') s.pop_back();
    }
    p.base = s;
    return p;
}

// Присвоить каждому полю модель обхода — портировано дословно из
// onCDemoSendTables (порядок проверок критичен: наличие field_serializer
// проверяется РАНЬШЕ вида типа).
void compute_field_models(SendTables& st) {
    for (auto& f : st.fields) {
        ParsedType pt = parse_var_type(f.var_type);
        if (f.field_serializer >= 0) {
            bool is_pointer_like = pt.pointer || pointer_types().count(pt.base) > 0;
            f.model = is_pointer_like ? FieldModel::FixedTable
                                      : FieldModel::VariableTable;
        } else if (pt.array_flag > 0 && pt.base != "char") {
            f.model = FieldModel::FixedArray;
            f.element_type = pt.base;
        } else if (pt.base == "CUtlVector" || pt.base == "CNetworkUtlVectorBase") {
            f.model = FieldModel::VariableArray;
            f.element_type = pt.element;
        } else {
            f.model = FieldModel::Simple;
            // Очищенный базовый тип (без [N]/generic-обёрток) — например
            // "char[128]" -> "char" — для корректного выбора decode_value.
            f.element_type = pt.base;
        }
    }
}

}  // namespace

SendTables parse_send_tables(std::string_view payload) {
    // CDemoSendTables { bytes data = 1 } — внутри: varint длина +
    // CSVCMsg_FlattenedSerializer.
    pb::Reader outer(payload);
    pb::Field f;
    std::string_view blob;
    while (pb::next_field(outer, f)) {
        if (f.number == 1 && f.wire_type == 2) blob = f.data;
    }
    if (blob.empty()) throw std::runtime_error("CDemoSendTables: no data field");

    pb::Reader lenr(blob);
    auto msg_len = lenr.varint();
    if (!msg_len) throw std::runtime_error("CDemoSendTables: bad inner length");
    auto msg = lenr.bytes(size_t(*msg_len));
    if (!msg) throw std::runtime_error("CDemoSendTables: truncated inner message");

    // CSVCMsg_FlattenedSerializer { serializers=1; symbols=2; fields=3 }
    SendTables st;
    struct RawField {
        uint64_t var_type_sym = 0, var_name_sym = 0, send_node_sym = 0;
        uint64_t field_serializer_name_sym = ~0ull, var_encoder_sym = ~0ull;
        int32_t field_serializer_version = 0;
        int32_t bit_count = 0, encode_flags = 0;
        float low_value = 0.0f, high_value = 0.0f;
    };
    std::vector<RawField> raw_fields;
    struct RawSer {
        uint64_t name_sym = 0;
        int32_t version = 0;
        std::vector<int32_t> field_indexes;
    };
    std::vector<RawSer> raw_sers;

    pb::Reader r(*msg);
    while (pb::next_field(r, f)) {
        switch (f.number) {
            case 2: st.symbols.emplace_back(f.data); break;
            case 3: {  // ProtoFlattenedSerializerField_t
                RawField rf;
                pb::Reader fr(f.data);
                pb::Field ff;
                while (pb::next_field(fr, ff)) {
                    switch (ff.number) {
                        case 1: rf.var_type_sym = ff.varint; break;
                        case 2: rf.var_name_sym = ff.varint; break;
                        case 3: rf.bit_count = int32_t(ff.varint); break;
                        case 4: rf.low_value = bits_to_float(ff.varint); break;
                        case 5: rf.high_value = bits_to_float(ff.varint); break;
                        case 6: rf.encode_flags = int32_t(ff.varint); break;
                        case 7: rf.field_serializer_name_sym = ff.varint; break;
                        case 8: rf.field_serializer_version = int32_t(ff.varint); break;
                        case 9: rf.send_node_sym = ff.varint; break;
                        case 10: rf.var_encoder_sym = ff.varint; break;
                        default: break;
                    }
                }
                raw_fields.push_back(rf);
                break;
            }
            case 1: {  // ProtoFlattenedSerializer_t
                RawSer rs;
                pb::Reader sr(f.data);
                pb::Field sf;
                while (pb::next_field(sr, sf)) {
                    switch (sf.number) {
                        case 1: rs.name_sym = sf.varint; break;
                        case 2: rs.version = int32_t(sf.varint); break;
                        case 3: rs.field_indexes.push_back(int32_t(sf.varint)); break;
                        default: break;
                    }
                }
                raw_sers.push_back(std::move(rs));
                break;
            }
            default: break;
        }
    }

    // Разрешение символов.
    st.fields.reserve(raw_fields.size());
    for (const auto& rf : raw_fields) {
        SerializerField sfld;
        sfld.var_name = sym(st.symbols, rf.var_name_sym);
        sfld.var_type = sym(st.symbols, rf.var_type_sym);
        sfld.send_node = sym(st.symbols, rf.send_node_sym);
        if (rf.var_encoder_sym != ~0ull)
            sfld.encoder = sym(st.symbols, rf.var_encoder_sym);
        sfld.bit_count = rf.bit_count;
        sfld.low_value = rf.low_value;
        sfld.high_value = rf.high_value;
        sfld.encode_flags = rf.encode_flags;
        st.fields.push_back(std::move(sfld));
    }
    st.serializers.reserve(raw_sers.size());
    for (const auto& rs : raw_sers) {
        Serializer s;
        s.name = sym(st.symbols, rs.name_sym);
        s.version = rs.version;
        s.field_indexes = rs.field_indexes;
        size_t idx = st.serializers.size();
        st.by_name_version[{s.name, s.version}] = idx;
        auto it = st.by_name.find(s.name);
        if (it == st.by_name.end() ||
            st.serializers[it->second].version < s.version) {
            st.by_name[s.name] = idx;
        }
        st.serializers.push_back(std::move(s));
    }

    // Второй проход: привязка вложенных сериализаторов к полям — строго
    // по паре (имя, версия): версии одного имени имеют разные наборы полей.
    for (size_t i = 0; i < raw_fields.size(); i++) {
        if (raw_fields[i].field_serializer_name_sym == ~0ull) continue;
        auto name = sym(st.symbols, raw_fields[i].field_serializer_name_sym);
        auto it = st.by_name_version.find(
            {name, raw_fields[i].field_serializer_version});
        if (it == st.by_name_version.end()) {
            auto fallback = st.by_name.find(name);
            if (fallback != st.by_name.end())
                st.fields[i].field_serializer = int32_t(fallback->second);
        } else {
            st.fields[i].field_serializer = int32_t(it->second);
        }
    }

    compute_field_models(st);
    return st;
}

}  // namespace dota::demo
