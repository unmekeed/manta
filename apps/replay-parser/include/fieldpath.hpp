// fieldpath — декодер путей полей Source 2 (Гл. 5.1).
// Пути к полям сущности кодируются последовательностью из 40 операций,
// сжатых huffman-кодом с фиксированными весами; операции модифицируют
// текущий путь (стек до 7 уровней) до маркера FieldPathEncodeFinish.
#pragma once

#include <array>
#include <cstdint>
#include <functional>
#include <vector>

#include "bit_reader.hpp"

namespace dota::demo {

struct FieldPath {
    std::array<int32_t, 7> path{{-1, 0, 0, 0, 0, 0, 0}};
    int32_t last = 0;

    bool operator==(const FieldPath& o) const {
        if (last != o.last) return false;
        for (int32_t i = 0; i <= last; i++)
            if (path[i] != o.path[i]) return false;
        return true;
    }
    // Компактный ключ для кэшей (6 бит на компонент достаточно на практике).
    uint64_t key() const {
        uint64_t k = uint64_t(last);
        for (int32_t i = 0; i <= last; i++)
            k = (k << 8) | uint64_t(uint8_t(path[i] & 0xFF));
        return k;
    }
};

// Дерево huffman строится с параметрами tie-break, которые невозможно
// восстановить из спецификации формата однозначно; корректная комбинация
// подбирается автоматически на первом svc_PacketEntities (см. entities.cpp).
struct HuffmanVariant {
    bool zero_weight_as_one;   // веса 0 → 1 при построении
    bool tie_prefer_later;     // при равном весе раньше выходит поздний op
    bool bit_means_right;      // 1-бит идёт в right (иначе в left)
};

class FieldPathDecoder {
  public:
    explicit FieldPathDecoder(HuffmanVariant v);

    // Прочитать все пути до FieldPathEncodeFinish; false при desync/overflow.
    bool read_paths(bits::BitReader& r, std::vector<FieldPath>& out) const;

    // Отладка: код каждого op'а как строка "0"/"1" (см. ENT_DEBUG tooling).
    std::vector<std::string> debug_codes() const;

    static constexpr int kNumOps = 40;

  private:
    // Плоское дерево: node[i] = {left, right}; листы < kNumOps, внутренние ≥ 40.
    struct Node { int16_t left = -1, right = -1; };
    std::vector<Node> nodes_;
    int root_ = -1;
    bool bit_right_ = true;

    bool apply_op(int op, bits::BitReader& r, FieldPath& fp) const;
};

}  // namespace dota::demo
