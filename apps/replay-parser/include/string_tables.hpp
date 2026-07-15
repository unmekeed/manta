// string_tables — декодер сетевых string tables Source 2 (Гл. 5.1).
// Таблицы (CombatLogNames, instancebaseline, EntityNames, ...) передаются
// в svc_CreateStringTable/svc_UpdateStringTable; записи упакованы битовым
// потоком с историей последних 32 ключей и опциональным snappy user data.
#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace dota::demo {

struct StringTableEntry {
    std::string key;
    std::string value;  // user data (может быть пустым)
};

struct StringTable {
    int32_t table_id = -1;
    std::string name;
    bool user_data_fixed_size = false;
    int32_t user_data_size_bits = 0;
    int32_t flags = 0;
    bool using_varint_bitcounts = false;
    std::map<int32_t, StringTableEntry> entries;  // index → entry
};

class StringTables {
  public:
    // svc_CreateStringTable: зарегистрировать таблицу и декодировать записи.
    // Возвращает ссылку на созданную таблицу.
    StringTable& create(std::string_view msg_payload);

    // svc_UpdateStringTable: применить дельту к существующей таблице.
    void update(std::string_view msg_payload);

    const StringTable* by_name(const std::string& name) const;
    const StringTable* by_id(int32_t id) const;
    size_t count() const { return tables_.size(); }

  private:
    void decode_entries(StringTable& t, std::string_view data,
                        int32_t num_entries);

    std::vector<StringTable> tables_;             // id = позиция
    std::map<std::string, size_t> name_index_;
};

}  // namespace dota::demo
