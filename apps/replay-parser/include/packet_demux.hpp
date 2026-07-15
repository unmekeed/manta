// packet_demux — разбор внутреннего слоя DEM_Packet (Гл. 5.1):
// CDemoPacket.data(3) содержит битовый поток сообщений
// `ubitvar msg_type | varint size | size байт`.
// Здесь же — загрузка схемы сущностей: CDemoClassInfo и
// CSVCMsg_FlattenedSerializer из CDemoSendTables.
#pragma once

#include <cstdint>
#include <functional>
#include <map>
#include <string>
#include <string_view>
#include <vector>

namespace dota::demo {

// Известные ID внутренних сообщений (net/svc/user messages Source 2).
const char* inner_msg_name(uint32_t type);

// Внутреннее сообщение пакета. payload указывает в буфер вызывающего.
struct InnerMsg {
    uint32_t type = 0;
    std::string_view payload;
};

// Разобрать CDemoPacket: выделить поле data(3) и пройти битовый поток.
// Возвращает число сообщений; cb вызывается для каждого.
size_t demux_packet(std::string_view demo_packet_payload,
                    const std::function<void(const InnerMsg&)>& cb);

// Как demux_packet, но payload — уже "голый" битовый поток (поле data).
size_t demux_packet_raw(std::string_view data,
                        const std::function<void(const InnerMsg&)>& cb);

// -- Схема сущностей ---------------------------------------------------------

// CDemoClassInfo: соответствие class_id → сетевое имя класса.
struct ClassInfo {
    std::map<int32_t, std::string> classes;  // id → network_name
};
ClassInfo parse_class_info(std::string_view payload);

// Модель обхода поля при разрешении field path (портировано из dotabuff/manta,
// sendtable.go: onCDemoSendTables). Определяется НЕ регэкспом по строке типа,
// а точным правилом Valve: наличие вложенного сериализатора имеет приоритет
// над видом типа.
enum class FieldModel {
    Simple,          // скаляр — decode напрямую
    FixedArray,      // T[N] или T[MACRO] — статический массив скаляров
    FixedTable,      // указатель на структуру (CBodyComponent и т.п.) — не массив
    VariableArray,   // CUtlVector<T>/CNetworkUtlVectorBase<T> — массив скаляров
    VariableTable,   // вложенный сериализатор, НЕ являющийся pointer-type — массив структур
};

// Поле сериализатора (ProtoFlattenedSerializerField_t, разрешённые символы).
struct SerializerField {
    std::string var_name;
    std::string var_type;
    std::string encoder;        // var_encoder_sym (пусто, если нет)
    std::string send_node;
    int32_t bit_count = 0;
    float low_value = 0.0f;
    float high_value = 0.0f;
    int32_t encode_flags = 0;
    int32_t field_serializer = -1;  // индекс вложенного сериализатора, -1 = нет

    // Вычисляются один раз после парсинга схемы (compute_field_models).
    FieldModel model = FieldModel::Simple;
    std::string element_type;   // тип элемента для FixedArray/VariableArray
};

// Сериализатор класса (ProtoFlattenedSerializer_t).
struct Serializer {
    std::string name;
    int32_t version = 0;
    std::vector<int32_t> field_indexes;  // индексы в SendTables::fields
};

// Полная схема из CDemoSendTables.
struct SendTables {
    std::vector<std::string> symbols;
    std::vector<SerializerField> fields;
    std::vector<Serializer> serializers;
    // имя → индекс сериализатора максимальной версии (привязка классов)
    std::map<std::string, size_t> by_name;
    // (имя, версия) → индекс (привязка вложенных полей)
    std::map<std::pair<std::string, int32_t>, size_t> by_name_version;
};
SendTables parse_send_tables(std::string_view payload);

}  // namespace dota::demo
