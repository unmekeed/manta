// BitReader — little-endian битовый ридер потока Source 2.
// Биты потребляются от младшего к старшему внутри каждого байта; после
// не выровненных чтений байтовые операции (varint, bytes) продолжаются
// с текущей битовой позиции.
#pragma once

#include <cstdint>
#include <cstring>
#include <optional>
#include <string_view>

namespace dota::bits {

class BitReader {
  public:
    explicit BitReader(std::string_view data)
        : data_(reinterpret_cast<const uint8_t*>(data.data())),
          size_bits_(data.size() * 8) {}

    uint64_t pos_bits() const { return pos_; }
    uint64_t remaining_bits() const { return size_bits_ - pos_; }
    bool overflowed() const { return overflow_; }

    // Прочитать n бит (0..32) как LE-число. При выходе за границу
    // выставляет overflow и возвращает 0.
    uint32_t read_bits(uint32_t n) {
        if (n == 0) return 0;
        if (n > 32 || pos_ + n > size_bits_) {
            overflow_ = true;
            pos_ = size_bits_;
            return 0;
        }
        uint32_t out = 0;
        uint32_t got = 0;
        while (got < n) {
            uint64_t byte_i = (pos_ + got) >> 3;
            uint32_t bit_i = (pos_ + got) & 7;
            uint32_t take = 8 - bit_i;
            if (take > n - got) take = n - got;
            uint32_t chunk = (data_[byte_i] >> bit_i) & ((1u << take) - 1);
            out |= chunk << got;
            got += take;
        }
        pos_ += n;
        return out;
    }

    bool read_bool() { return read_bits(1) != 0; }

    // ubitvar Source 2: 6 бит, старшие 2 бита выбирают расширение.
    uint32_t read_ubitvar() {
        uint32_t v = read_bits(6);
        switch (v & 0x30) {
            case 0x10: v = (v & 0x0F) | (read_bits(4) << 4); break;
            case 0x20: v = (v & 0x0F) | (read_bits(8) << 4); break;
            case 0x30: v = (v & 0x0F) | (read_bits(28) << 4); break;
            default: break;
        }
        return v;
    }

    // Base-128 varint поверх битового потока (байты могут быть не выровнены).
    uint32_t read_varuint32() {
        uint32_t v = 0;
        int shift = 0;
        while (shift < 35) {
            uint32_t b = read_bits(8);
            if (overflow_) return 0;
            v |= (b & 0x7F) << shift;
            if (!(b & 0x80)) return v;
            shift += 7;
        }
        overflow_ = true;
        return 0;
    }

    uint64_t read_varuint64() {
        uint64_t v = 0;
        int shift = 0;
        while (shift < 70) {
            uint64_t b = read_bits(8);
            if (overflow_) return 0;
            v |= (b & 0x7F) << shift;
            if (!(b & 0x80)) return v;
            shift += 7;
        }
        overflow_ = true;
        return 0;
    }

    // Скопировать n байт (позиция может быть не выровнена по байту).
    bool read_bytes(uint8_t* dst, size_t n) {
        if (pos_ + n * 8 > size_bits_) {
            overflow_ = true;
            pos_ = size_bits_;
            return false;
        }
        if ((pos_ & 7) == 0) {  // быстрый путь: выровнено
            std::memcpy(dst, data_ + (pos_ >> 3), n);
            pos_ += n * 8;
            return true;
        }
        for (size_t i = 0; i < n; i++) dst[i] = uint8_t(read_bits(8));
        return !overflow_;
    }

  private:
    const uint8_t* data_;
    uint64_t size_bits_;
    uint64_t pos_ = 0;
    bool overflow_ = false;
};

}  // namespace dota::bits
