#include "string_tables.hpp"

#include <snappy.h>

#include <stdexcept>

#include "bit_reader.hpp"
#include "pb_lite.hpp"

namespace dota::demo {

namespace {

constexpr size_t kKeyHistorySize = 32;

std::string read_cstring(bits::BitReader& r) {
    std::string s;
    while (!r.overflowed()) {
        uint8_t c = uint8_t(r.read_bits(8));
        if (c == 0) break;
        s.push_back(char(c));
    }
    return s;
}

}  // namespace

void StringTables::decode_entries(StringTable& t, std::string_view data,
                                  int32_t num_entries) {
    bits::BitReader r(data);
    int32_t index = -1;
    std::vector<std::string> history;
    history.reserve(kKeyHistorySize);

    for (int32_t i = 0; i < num_entries && !r.overflowed(); i++) {
        // Индекс записи: инкремент или явное значение.
        if (r.read_bool()) {
            index++;
        } else {
            index = int32_t(r.read_varuint32()) + 1;
        }

        std::string key;
        bool has_key = r.read_bool();
        if (has_key) {
            if (r.read_bool()) {  // ключ с историей: prefix из окна + хвост
                uint32_t pos = r.read_bits(5);
                uint32_t size = r.read_bits(5);
                if (pos >= history.size()) {
                    key = read_cstring(r);
                } else {
                    const std::string& prev = history[pos];
                    if (size > prev.size()) {
                        key = prev + read_cstring(r);
                    } else {
                        key = prev.substr(0, size) + read_cstring(r);
                    }
                }
            } else {
                key = read_cstring(r);
            }
            if (history.size() >= kKeyHistorySize) {
                history.erase(history.begin());
            }
            history.push_back(key);
        }

        std::string value;
        bool has_value = r.read_bool();
        if (has_value) {
            bool is_compressed = false;
            uint32_t byte_size = 0;
            if (t.user_data_fixed_size) {
                // фиксированный размер в битах
                uint32_t bit_size = uint32_t(t.user_data_size_bits);
                value.resize((bit_size + 7) / 8);
                for (uint32_t b = 0; b < bit_size; b += 8) {
                    uint32_t take = bit_size - b < 8 ? bit_size - b : 8;
                    value[b / 8] = char(r.read_bits(take));
                }
            } else {
                if (t.flags & 0x1) is_compressed = r.read_bool();
                byte_size = t.using_varint_bitcounts ? r.read_ubitvar()
                                                     : r.read_bits(17);
                value.resize(byte_size);
                if (byte_size > 0 &&
                    !r.read_bytes(reinterpret_cast<uint8_t*>(value.data()),
                                  byte_size)) {
                    break;
                }
                if (is_compressed) {
                    std::string plain;
                    if (!snappy::Uncompress(value.data(), value.size(), &plain)) {
                        throw std::runtime_error(
                            "string table '" + t.name + "': bad snappy user data");
                    }
                    value = std::move(plain);
                }
            }
        }

        auto& e = t.entries[index];
        if (has_key) e.key = std::move(key);
        if (has_value) e.value = std::move(value);
    }
}

StringTable& StringTables::create(std::string_view msg_payload) {
    // CSVCMsg_CreateStringTable { name=1; num_entries=2; user_data_fixed_size=3;
    //   user_data_size=4; user_data_size_bits=5; flags=6; string_data=7;
    //   uncompressed_size=8; data_compressed=9; using_varint_bitcounts=10 }
    StringTable t;
    t.table_id = int32_t(tables_.size());

    int32_t num_entries = 0;
    bool data_compressed = false;
    std::string_view string_data;

    pb::Reader r(msg_payload);
    pb::Field f;
    while (pb::next_field(r, f)) {
        switch (f.number) {
            case 1: t.name = std::string(f.data); break;
            case 2: num_entries = int32_t(f.varint); break;
            case 3: t.user_data_fixed_size = f.varint != 0; break;
            case 5: t.user_data_size_bits = int32_t(f.varint); break;
            case 6: t.flags = int32_t(f.varint); break;
            case 7: string_data = f.data; break;
            case 9: data_compressed = f.varint != 0; break;
            case 10: t.using_varint_bitcounts = f.varint != 0; break;
            default: break;
        }
    }

    std::string plain;
    if (data_compressed && !string_data.empty()) {
        if (!snappy::Uncompress(string_data.data(), string_data.size(), &plain)) {
            throw std::runtime_error("string table '" + t.name +
                                     "': bad snappy string_data");
        }
        string_data = plain;
    }
    if (!string_data.empty()) decode_entries(t, string_data, num_entries);

    name_index_[t.name] = tables_.size();
    tables_.push_back(std::move(t));
    return tables_.back();
}

void StringTables::update(std::string_view msg_payload) {
    // CSVCMsg_UpdateStringTable { table_id=1; num_changed_entries=2; string_data=3 }
    int32_t table_id = -1;
    int32_t changed = 0;
    std::string_view data;

    pb::Reader r(msg_payload);
    pb::Field f;
    while (pb::next_field(r, f)) {
        switch (f.number) {
            case 1: table_id = int32_t(f.varint); break;
            case 2: changed = int32_t(f.varint); break;
            case 3: data = f.data; break;
            default: break;
        }
    }
    if (table_id < 0 || size_t(table_id) >= tables_.size()) return;
    decode_entries(tables_[size_t(table_id)], data, changed);
}

const StringTable* StringTables::by_name(const std::string& name) const {
    auto it = name_index_.find(name);
    return it == name_index_.end() ? nullptr : &tables_[it->second];
}

const StringTable* StringTables::by_id(int32_t id) const {
    return (id >= 0 && size_t(id) < tables_.size()) ? &tables_[size_t(id)]
                                                    : nullptr;
}

}  // namespace dota::demo
