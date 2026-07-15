// pb_lite — минимальный ридер wire-формата Protocol Buffers.
// Достаточен для разбора служебных сообщений демо (CDemoFileHeader,
// CDemoFileInfo) без кодогенерации protoc; полноценные .proto-контракты
// подключаются на этапе EntityDecoder.
#pragma once

#include <cstdint>
#include <optional>
#include <string_view>

namespace dota::pb {

// Курсор по байтам сообщения.
struct Reader {
    const uint8_t* p;
    const uint8_t* end;

    explicit Reader(std::string_view data)
        : p(reinterpret_cast<const uint8_t*>(data.data())),
          end(p + data.size()) {}

    bool eof() const { return p >= end; }

    // Base-128 varint (до 64 бит). nullopt при обрыве данных.
    std::optional<uint64_t> varint() {
        uint64_t v = 0;
        int shift = 0;
        while (p < end && shift < 64) {
            uint8_t b = *p++;
            v |= uint64_t(b & 0x7F) << shift;
            if (!(b & 0x80)) return v;
            shift += 7;
        }
        return std::nullopt;
    }

    std::optional<uint32_t> fixed32() {
        if (end - p < 4) return std::nullopt;
        uint32_t v = uint32_t(p[0]) | uint32_t(p[1]) << 8 |
                     uint32_t(p[2]) << 16 | uint32_t(p[3]) << 24;
        p += 4;
        return v;
    }

    std::optional<std::string_view> bytes(size_t n) {
        if (size_t(end - p) < n) return std::nullopt;
        std::string_view sv(reinterpret_cast<const char*>(p), n);
        p += n;
        return sv;
    }
};

// Одно поле сообщения: номер, wire-тип и значение.
struct Field {
    uint32_t number = 0;
    uint32_t wire_type = 0;   // 0=varint, 1=fixed64, 2=len-delimited, 5=fixed32
    uint64_t varint = 0;      // для типов 0/1/5
    std::string_view data;    // для типа 2
};

// Прочитать следующее поле; false — конец сообщения или ошибка формата.
inline bool next_field(Reader& r, Field& f) {
    if (r.eof()) return false;
    auto tag = r.varint();
    if (!tag) return false;
    f.number = uint32_t(*tag >> 3);
    f.wire_type = uint32_t(*tag & 0x7);
    switch (f.wire_type) {
        case 0: {
            auto v = r.varint();
            if (!v) return false;
            f.varint = *v;
            return true;
        }
        case 1: {
            auto lo = r.fixed32(), hi = r.fixed32();
            if (!lo || !hi) return false;
            f.varint = uint64_t(*hi) << 32 | *lo;
            return true;
        }
        case 2: {
            auto len = r.varint();
            if (!len) return false;
            auto b = r.bytes(size_t(*len));
            if (!b) return false;
            f.data = *b;
            return true;
        }
        case 5: {
            auto v = r.fixed32();
            if (!v) return false;
            f.varint = *v;
            return true;
        }
        default:
            return false;  // группы (3/4) в Source 2 не используются
    }
}

}  // namespace dota::pb
