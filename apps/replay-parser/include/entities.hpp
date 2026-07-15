// entities — состояние сетевых сущностей: создание из instancebaseline,
// delta-обновления из svc_PacketEntities, извлечение наблюдаемых полей
// (позиции m_cellX/m_vecX, экономика m_iNetWorth) — Гл. 5.1/5.3.
#pragma once

#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <string>
#include <unordered_map>

#include "field_decoder.hpp"
#include "fieldpath.hpp"
#include "packet_demux.hpp"
#include "string_tables.hpp"

namespace dota::demo {

struct Entity {
    int32_t index = -1;
    int32_t class_id = -1;
    size_t serializer = SIZE_MAX;
    std::string class_name;
    bool active = true;
    // Материализуются только наблюдаемые поля (см. is_watched_field).
    std::unordered_map<std::string, FieldValue> watched;
};

class Entities {
  public:
    Entities(const SendTables& st, const ClassInfo& ci,
             const StringTables& tables);

    // Обработать svc_PacketEntities. На первом вызове автоматически
    // подбирается вариант huffman-дерева field paths (4 кандидата) —
    // выбирается тот, что декодирует сообщение без рассинхронизации.
    // Возвращает false при неустранимом desync.
    bool on_packet_entities(std::string_view msg_payload);

    const std::map<int32_t, Entity>& all() const { return entities_; }
    uint64_t packets_processed() const { return packets_; }
    uint64_t creates() const { return creates_; }
    uint64_t updates() const { return updates_; }
    const char* huffman_variant_name() const;

    // Обойти живые сущности-герои.
    void each_hero(const std::function<void(const Entity&)>& cb) const;

    // Мировые координаты из наблюдаемых полей (cell*128 + vec - 16384).
    static bool world_pos(const Entity& e, float& x, float& y);

  private:
    bool decode_with(const FieldPathDecoder& fpd, std::string_view entity_data,
                     int32_t updated, bool strict);
    bool decode_entity_fields(const FieldPathDecoder& fpd, bits::BitReader& r,
                              Entity& e, bool strict);
    bool apply_baseline(const FieldPathDecoder& fpd, Entity& e, bool strict);

    const SendTables& st_;
    const ClassInfo& ci_;
    const StringTables& tables_;
    FieldResolver resolver_;
    std::unique_ptr<FieldPathDecoder> fpd_;
    int variant_ = -1;
    uint32_t class_id_bits_ = 0;
    std::map<int32_t, Entity> entities_;
    uint64_t packets_ = 0, creates_ = 0, updates_ = 0;
    std::vector<FieldPath> paths_scratch_;
};

}  // namespace dota::demo
