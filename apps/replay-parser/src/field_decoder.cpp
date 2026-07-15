#include "field_decoder.hpp"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>

namespace dota::demo {

namespace {

// -- Квантованный float (QuantizedFloatDecoder Source 2) ---------------------

// Флаги интерпретируются как беззнаковые (см. manta quantizedfloat.go) —
// схема иногда отдаёт encode_flags как отрицательное int32 (знаковое
// расширение верхних бит), но действительные флаги всегда лежат в bit 0..3.
constexpr uint32_t kQFRoundDown = 1u << 0;
constexpr uint32_t kQFRoundUp = 1u << 1;
constexpr uint32_t kQFEncodeZero = 1u << 2;
constexpr uint32_t kQFEncodeInt = 1u << 3;

// Точный порт dotabuff/manta quantizedfloat.go — расхождение в подсчёте
// бит (bitcount при ENCODE_INT) ломает синхронизацию всего потока, а
// расхождение в формуле значения (steps-1, не steps) даёт неверные числа
// при формально верной синхронизации.
struct QuantizedParams {
    uint32_t bits = 0;
    float low = 0, high = 1;
    uint32_t flags = 0;
    float offset = 0.0f;
    float high_low_mul = 0.0f;
    float dec_mul = 0.0f;
    bool no_scale = false;

    void validate_flags() {
        if (flags == 0) return;
        if ((low == 0.0f && (flags & kQFRoundDown)) ||
            (high == 0.0f && (flags & kQFRoundUp))) {
            flags &= ~kQFEncodeZero;
        }
        if (low == 0.0f && (flags & kQFEncodeZero)) {
            flags |= kQFRoundDown;
            flags &= ~kQFEncodeZero;
        }
        if (high == 0.0f && (flags & kQFEncodeZero)) {
            flags |= kQFRoundUp;
            flags &= ~kQFEncodeZero;
        }
        if (low > 0.0f || high < 0.0f) flags &= ~kQFEncodeZero;
        if (flags & kQFEncodeInt) {
            flags &= ~(kQFRoundUp | kQFRoundDown | kQFEncodeZero);
        }
    }

    void assign_multipliers(uint32_t steps) {
        float range = high - low;
        uint32_t hi = (bits == 32) ? 0xFFFFFFFEu : ((1u << bits) - 1u);
        float high_mul;
        if (std::fabs(double(range)) <= 0.0) {
            high_mul = float(hi);
        } else {
            high_mul = float(hi) / range;
        }
        if (double(high_mul) * double(range) > double(hi)) {
            static constexpr float kAdj[] = {0.9999f, 0.99f, 0.9f, 0.8f, 0.7f};
            for (float m : kAdj) {
                high_mul = float(hi) / range * m;
                if (double(high_mul) * double(range) <= double(hi)) break;
            }
        }
        high_low_mul = high_mul;
        dec_mul = 1.0f / float(steps > 0 ? steps - 1 : 1);
    }

    void init() {
        if (bits == 0 || bits >= 32) {
            no_scale = true;
            bits = 32;
            return;
        }
        validate_flags();

        uint32_t steps = 1u << bits;
        if (flags & kQFRoundDown) {
            float range = high - low;
            offset = range / float(steps);
            high -= offset;
        } else if (flags & kQFRoundUp) {
            float range = high - low;
            offset = range / float(steps);
            low += offset;
        }

        if (flags & kQFEncodeInt) {
            float delta = high - low;
            if (delta < 1.0f) delta = 1.0f;
            double delta_log2 = std::ceil(std::log2(double(delta)));
            uint32_t range2 = 1u << uint32_t(delta_log2);
            uint32_t bc = bits;
            while (!((1u << bc) > range2)) bc++;
            if (bc > bits) {
                bits = bc;
                steps = 1u << bits;
            }
            offset = float(range2) / float(steps);
        }

        assign_multipliers(steps);

        // Убрать ненужные флаги (manta: "Remove unessecary flags"). Если
        // квантование границы уже само возвращает границу без специального
        // «резервного» бита в потоке, movable — флаг был установлен только
        // для КОНСТРУКЦИИ high/low (уже сделано выше) и не должен больше
        // потреблять бит на decode(): иначе бит потока читается лишний раз
        // и все последующие поля десинхронизируются на 1 бит.
        if (flags & kQFRoundDown) {
            if (quantize(low) == low) flags &= ~kQFRoundDown;
        }
        if (flags & kQFRoundUp) {
            if (quantize(high) == high) flags &= ~kQFRoundUp;
        }
        if (flags & kQFEncodeZero) {
            if (quantize(0.0f) == 0.0f) flags &= ~kQFEncodeZero;
        }
    }

    // Точный порт quantize() из manta — используется только для проверки
    // «нужен ли флаг» выше (не для собственно кодирования, которого мы не
    // делаем); НЕ паникует на выходе за диапазон, а зажимает к границе,
    // т.к. на этапе очистки флагов low/high всегда валидны по построению.
    float quantize(float val) const {
        if (val < low) return low;
        if (val > high) return high;
        uint32_t i = uint32_t((val - low) * high_low_mul);
        return low + (high - low) * (float(i) * dec_mul);
    }

    float decode(bits::BitReader& r) const {
        if (no_scale) {
            uint32_t b = r.read_bits(32);
            float v;
            std::memcpy(&v, &b, sizeof v);
            return v;
        }
        if (std::getenv("QF_DEBUG")) {
            auto peek = r;
            std::fprintf(stderr, "[qf] entering decode at bit %llu, next 16 raw bits: ",
                        (unsigned long long)peek.pos_bits());
            for (int i = 0; i < 16; i++) std::fprintf(stderr, "%u", peek.read_bits(1));
            std::fprintf(stderr, " flags=%u bits=%u\n", flags, bits);
        }
        if ((flags & kQFRoundDown) && r.read_bool()) return low;
        if ((flags & kQFRoundUp) && r.read_bool()) return high;
        if ((flags & kQFEncodeZero) && r.read_bool()) return 0.0f;
        uint32_t u = r.read_bits(bits);
        if (std::getenv("QF_DEBUG")) {
            std::fprintf(stderr, "[qf] bits=%u u=%u low=%g high=%g dec_mul=%.8f flags=%u -> %g\n",
                         bits, u, low, high, dec_mul, flags,
                         low + (high - low) * float(u) * dec_mul);
        }
        return low + (high - low) * float(u) * dec_mul;
    }
};

float read_noscale_float(bits::BitReader& r) {
    uint32_t b = r.read_bits(32);
    float v;
    std::memcpy(&v, &b, sizeof v);
    return v;
}

// readCoord Source (целая часть 14 бит + дробь 5 бит).
float read_coord(bits::BitReader& r) {
    float value = 0;
    bool has_int = r.read_bool();
    bool has_frac = r.read_bool();
    if (!has_int && !has_frac) return 0;
    bool sign = r.read_bool();
    if (has_int) value += float(r.read_bits(14)) + 1;
    if (has_frac) value += float(r.read_bits(5)) * (1.0f / 32.0f);
    return sign ? -value : value;
}

// Нормализованный вектор (3D): 2 флага + 11-битные компоненты + знак Z.
void read_normal_vector(bits::BitReader& r) {
    bool has_x = r.read_bool();
    bool has_y = r.read_bool();
    auto read_norm = [&](bool) {
        bool sign = r.read_bool();
        uint32_t frac = r.read_bits(11);
        (void)sign;
        (void)frac;
    };
    if (has_x) read_norm(true);
    if (has_y) read_norm(true);
    r.read_bool();  // знак Z (Z восстанавливается из нормы)
}

int64_t zigzag(uint64_t v) { return int64_t(v >> 1) ^ -int64_t(v & 1); }

// -- Определение типа значения по базовому имени -----------------------------

ResolvedField::Kind base_kind(const std::string& b) {
    using K = ResolvedField::Kind;
    if (b == "bool") return K::Bool;
    if (b == "uint8" || b == "uint16" || b == "uint32" || b == "uint64" ||
        b == "Color" || b == "color32" || b == "CUtlStringToken" ||
        b == "HSequence" || b == "CEntityHandle" ||
        b == "CGameSceneNodeHandle" || b.rfind("CHandle<", 0) == 0 ||
        b.rfind("CStrongHandle<", 0) == 0 || b == "item_definition_index_t" ||
        b == "itemid_t" || b == "style_index_t" || b == "CEntityIndex")
        return K::VarUint;
    if (b == "int8" || b == "int16" || b == "int32" || b == "int64" ||
        b == "HeroID_t")
        return K::VarSint;
    if (b == "float32" || b == "CNetworkedQuantizedFloat" || b == "GameTime_t" ||
        b == "float")
        return K::Float;
    if (b == "Vector" || b == "VectorWS") return K::Vector3;
    if (b == "Vector2D") return K::Vector2;
    if (b == "Vector4D" || b == "Quaternion") return K::Vector4;
    if (b == "QAngle") return K::QAngle;
    if (b == "CUtlString" || b == "CUtlSymbolLarge" || b == "char")
        return K::String;
    // Перечисления и неизвестные скаляры кодируются varint.
    return K::VarUint;
}

}  // namespace

bool decode_value(bits::BitReader& r, const ResolvedField& f, FieldValue& out) {
    using K = ResolvedField::Kind;
    out = std::monostate{};

    auto read_float_one = [&]() -> float {
        if (f.coord) return read_coord(r);
        if (f.simtime) return float(r.read_varuint32()) * (1.0f / 30.0f);
        if (f.runetime) {  // manta runeTimeDecoder: 4 сырых бита как float-биты
            uint32_t b = r.read_bits(4);
            float v;
            std::memcpy(&v, &b, sizeof v);
            return v;
        }
        if (f.bit_count <= 0 || f.bit_count >= 32) return read_noscale_float(r);
        QuantizedParams q;
        q.bits = f.bit_count;
        q.low = f.low;
        q.high = f.high;
        q.flags = f.encode_flags;
        q.init();
        return q.decode(r);
    };

    switch (f.kind) {
        case K::Bool: out = r.read_bool(); break;
        case K::VarUint: out = r.read_varuint64(); break;
        case K::VarSint: out = zigzag(r.read_varuint64()); break;
        case K::Fixed64: {
            uint64_t lo = r.read_bits(32), hi = r.read_bits(32);
            out = (hi << 32) | lo;
            break;
        }
        case K::Float: out = read_float_one(); break;
        case K::Vector2: {
            float x = read_float_one();
            read_float_one();
            out = x;
            break;
        }
        case K::Vector3: {
            float x = read_float_one();
            read_float_one();
            read_float_one();
            out = x;
            break;
        }
        case K::Vector4: {
            float x = read_float_one();
            read_float_one(); read_float_one(); read_float_one();
            out = x;
            break;
        }
        case K::QAngle: {
            if (f.qangle_precise) {
                bool hx = r.read_bool(), hy = r.read_bool(), hz = r.read_bool();
                if (hx) r.read_bits(20);
                if (hy) r.read_bits(20);
                if (hz) r.read_bits(20);
            } else if (f.bit_count != 0) {
                r.read_bits(uint32_t(f.bit_count));
                r.read_bits(uint32_t(f.bit_count));
                r.read_bits(uint32_t(f.bit_count));
            } else {
                bool hx = r.read_bool(), hy = r.read_bool(), hz = r.read_bool();
                if (hx) read_coord(r);
                if (hy) read_coord(r);
                if (hz) read_coord(r);
            }
            out = 0.0f;
            break;
        }
        case K::NormalVec: read_normal_vector(r); out = 0.0f; break;
        case K::String: {
            std::string s;
            for (int i = 0; i < 4096; i++) {
                uint8_t c = uint8_t(r.read_bits(8));
                if (c == 0 || r.overflowed()) break;
                s.push_back(char(c));
            }
            out = std::move(s);
            break;
        }
        case K::ArrayCount: out = uint64_t(r.read_varuint32()); break;
        case K::PointerMarker: out = r.read_bool(); break;
        case K::Unknown: return false;
    }
    return !r.overflowed();
}

namespace {

// Заполнить параметры декодирования из СОБСТВЕННЫХ атрибутов поля
// (bit_count/low/high/encoder) — применимо к Simple и к элементам
// FixedArray; НЕ применимо к элементам VariableArray (Гл. 5.1 — там
// используется декодер по умолчанию для базового типа, без квантования).
void apply_field_context(const SerializerField& f, ResolvedField& rf) {
    using K = ResolvedField::Kind;
    rf.bit_count = f.bit_count;
    rf.low = f.low_value;
    rf.high = f.high_value;
    rf.encode_flags = f.encode_flags;

    const std::string& bt = f.element_type;

    // Приоритет декодера в manta (findDecoder): fieldTypeFactories по базовому
    // типу проверяется РАНЬШЕ fieldTypeDecoders. Фабрики есть у float32
    // (floatFactory: encoder coord/simtime/runetime, иначе noscale/quantized
    // по bit_count), у CNetworkedQuantizedFloat (ВСЕГДА quantized, энкодер
    // игнорируется) и у векторов Vector/VectorWS/Vector2D/Vector4D/Quaternion
    // (vectorFactory: каждая компонента через floatFactory, т.е. энкодер
    // уважается покомпонентно). Типы из fieldTypeDecoders энкодер и параметры
    // квантования игнорируют всегда: GameTime_t — безусловно noscale.
    if (rf.kind == K::Vector3 && f.encoder == "normal") {
        rf.kind = K::NormalVec;  // vectorFactory: 3-вектор с encoder="normal"
        return;
    }
    if (bt == "GameTime_t") {
        rf.bit_count = 0;  // noscaleDecoder независимо от bit_count схемы
        return;
    }
    if (bt == "CNetworkedQuantizedFloat") return;

    bool float_like = bt == "float32" || rf.kind == K::Vector2 ||
                      rf.kind == K::Vector3 || rf.kind == K::Vector4;
    if (float_like) {
        rf.coord = f.encoder == "coord";
        // Патч схемы (manta field_patch.go, применяется для всех билдов):
        // поля с этими именами принудительно получают encoder="simtime".
        rf.simtime = f.encoder == "simtime" ||
                     f.var_name == "m_flSimulationTime" ||
                     f.var_name == "m_flAnimTime";
        // runetime-патч только при сентинельных границах ±FLT_MAX (иначе
        // поле кодируется обычным квантованным float).
        rf.runetime = f.encoder == "runetime" ||
                      (f.var_name == "m_flRuneTime" &&
                       f.low_value == -std::numeric_limits<float>::max() &&
                       f.high_value == std::numeric_limits<float>::max());
    }
    if (f.encoder == "fixed64" && rf.kind == K::VarUint) {
        rf.kind = K::Fixed64;
    }
    if (rf.kind == K::QAngle) {
        if (f.encoder == "qangle_pitch_yaw") {
            rf.kind = K::Vector2;  // pitch+yaw (bit_count бит или noscale), roll нет
        } else if (f.encoder == "qangle_precise") {
            rf.qangle_precise = true;  // 3 флага + по 20 бит на компоненту
        }
    }
}

bool resolve_in_serializer(const SendTables& st, size_t ser_idx,
                           const FieldPath& fp, int32_t pos, std::string& name,
                           ResolvedField& out);

// pos — индекс СЛЕДУЮЩЕЙ непотреблённой компоненты пути (после того как
// поле f уже было выбрано компонентой fp.path[pos-1] родительским
// сериализатором). Модель обхода портирована из dotabuff/manta field.go
// (getFieldForFieldPath/getDecoderForFieldPath) — см. Гл. 5.1.
bool resolve_field(const SendTables& st, const SerializerField& f,
                   const FieldPath& fp, int32_t pos, std::string& name,
                   ResolvedField& out) {
    using K = ResolvedField::Kind;
    switch (f.model) {
        case FieldModel::Simple:
            out.kind = base_kind(f.element_type);
            apply_field_context(f, out);
            return true;

        case FieldModel::FixedArray:
            // Валидные компоненты кодируют индекс элемента, но декодер один
            // и тот же независимо от глубины остатка пути (manta: getDecoder
            // всегда возвращает f.decoder для FixedArray).
            if (fp.last >= pos) {
                name += '.';
                name += std::to_string(fp.path[size_t(pos)]);
            }
            out.kind = base_kind(f.element_type);
            apply_field_context(f, out);
            return true;

        case FieldModel::VariableArray:
            if (fp.last < pos) {
                out.kind = K::ArrayCount;  // изменение длины массива
                return true;
            }
            if (fp.last == pos) {
                // Элемент массива скаляров: декодер по умолчанию для
                // базового типа БЕЗ параметров квантования поля-массива.
                name += '.';
                name += std::to_string(fp.path[size_t(pos)]);
                out.kind = base_kind(f.element_type);
                return true;
            }
            return false;  // глубже некуда — массив скаляров, не структур

        case FieldModel::FixedTable:
            if (fp.last < pos) {
                out.kind = K::PointerMarker;  // создание/удаление указателя
                return true;
            }
            if (f.field_serializer < 0) return false;
            // Не массив — то же pos продолжает выбор поля во вложенном
            // сериализаторе (без отдельного индекса элемента).
            return resolve_in_serializer(st, size_t(f.field_serializer), fp,
                                         pos, name, out);

        case FieldModel::VariableTable:
            if (fp.last <= pos) {
                out.kind = K::ArrayCount;  // изменение длины массива структур
                return true;
            }
            if (f.field_serializer < 0) return false;
            name += '.';
            name += std::to_string(fp.path[size_t(pos)]);
            return resolve_in_serializer(st, size_t(f.field_serializer), fp,
                                         pos + 1, name, out);
    }
    return false;
}

bool resolve_in_serializer(const SendTables& st, size_t ser_idx,
                           const FieldPath& fp, int32_t pos, std::string& name,
                           ResolvedField& out) {
    if (ser_idx >= st.serializers.size()) return false;
    const auto& ser = st.serializers[ser_idx];
    if (pos < 0 || pos > fp.last) return false;
    int32_t comp = fp.path[size_t(pos)];
    if (comp < 0 || size_t(comp) >= ser.field_indexes.size()) return false;
    const auto& f = st.fields[size_t(ser.field_indexes[size_t(comp)])];
    if (!name.empty()) name += '.';
    name += f.var_name;
    return resolve_field(st, f, fp, pos + 1, name, out);
}

}  // namespace

bool FieldResolver::resolve(size_t ser_idx, const FieldPath& fp,
                            ResolvedField& out) const {
    uint64_t key = fp.key();
    auto& ser_cache = cache_[ser_idx];
    auto it = ser_cache.find(key);
    if (it != ser_cache.end()) {
        out = it->second;
        return out.kind != ResolvedField::Kind::Unknown;
    }

    ResolvedField rf;
    std::string name;
    bool ok = resolve_in_serializer(st_, ser_idx, fp, 0, name, rf);
    rf.kind = ok ? rf.kind : ResolvedField::Kind::Unknown;
    rf.full_name = std::move(name);
    ser_cache[key] = rf;
    out = rf;
    return ok;
}

}  // namespace dota::demo
