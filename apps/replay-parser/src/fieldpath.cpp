#include "fieldpath.hpp"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <queue>
#include <string>

namespace dota::demo {

namespace {

// ubitvar варианта field path: 2/4/10/17/31 бит по флагам.
uint32_t read_ubitvar_fp(bits::BitReader& r) {
    if (r.read_bool()) return r.read_bits(2);
    if (r.read_bool()) return r.read_bits(4);
    if (r.read_bool()) return r.read_bits(10);
    if (r.read_bool()) return r.read_bits(17);
    return r.read_bits(31);
}

int32_t read_var_sint32(bits::BitReader& r) {
    uint32_t v = r.read_varuint32();
    return int32_t(v >> 1) ^ -int32_t(v & 1);  // zigzag
}

// Фиксированные веса 40 операций кодирования путей (частоты Valve).
struct OpDef {
    const char* name;
    int weight;
};
constexpr OpDef kOps[FieldPathDecoder::kNumOps] = {
    {"PlusOne", 36271},
    {"PlusTwo", 10334},
    {"PlusThree", 1375},
    {"PlusFour", 646},
    {"PlusN", 4128},
    {"PushOneLeftDeltaZeroRightZero", 35},
    {"PushOneLeftDeltaZeroRightNonZero", 3},
    {"PushOneLeftDeltaOneRightZero", 521},
    {"PushOneLeftDeltaOneRightNonZero", 2942},
    {"PushOneLeftDeltaNRightZero", 560},
    {"PushOneLeftDeltaNRightNonZero", 471},
    {"PushOneLeftDeltaNRightNonZeroPack6Bits", 10530},
    {"PushOneLeftDeltaNRightNonZeroPack8Bits", 251},
    {"PushTwoLeftDeltaZero", 0},
    {"PushTwoPack5LeftDeltaZero", 0},
    {"PushThreeLeftDeltaZero", 0},
    {"PushThreePack5LeftDeltaZero", 0},
    {"PushTwoLeftDeltaOne", 0},
    {"PushTwoPack5LeftDeltaOne", 0},
    {"PushThreeLeftDeltaOne", 0},
    {"PushThreePack5LeftDeltaOne", 0},
    {"PushTwoLeftDeltaN", 0},
    {"PushTwoPack5LeftDeltaN", 0},
    {"PushThreeLeftDeltaN", 0},
    {"PushThreePack5LeftDeltaN", 0},
    {"PushN", 0},
    {"PushNAndNonTopological", 310},
    {"PopOnePlusOne", 2},
    {"PopOnePlusN", 0},
    {"PopAllButOnePlusOne", 1837},
    {"PopAllButOnePlusN", 149},
    {"PopAllButOnePlusNPack3Bits", 300},
    {"PopAllButOnePlusNPack6Bits", 634},
    {"PopNPlusOne", 0},
    {"PopNPlusN", 0},
    {"PopNAndNonTopographical", 1},
    {"NonTopoComplex", 76},
    {"NonTopoPenultimatePlusOne", 271},
    {"NonTopoComplexPack4Bits", 99},
    {"FieldPathEncodeFinish", 25474},
};

constexpr int kOpFinish = 39;

}  // namespace

FieldPathDecoder::FieldPathDecoder(HuffmanVariant v) {
    // Построение huffman-дерева. Раскладка кодов между операциями с равным
    // весом зависит от точного алгоритма кучи; сетевой формат Valve
    // соответствует семантике Go container/heap (порт ниже дословный):
    // Less: (w_i == w_j) ? value_i >= value_j : w_i < w_j.
    struct HeapItem {
        uint64_t weight;
        int value;  // листы 0..39, внутренние 40+
        int node;
    };
    std::vector<HeapItem> h;
    auto less = [](const HeapItem& a, const HeapItem& b) {
        if (a.weight == b.weight) return a.value >= b.value;
        return a.weight < b.weight;
    };
    auto up = [&](int j) {
        while (j > 0) {
            int i = (j - 1) / 2;
            if (i == j || !less(h[size_t(j)], h[size_t(i)])) break;
            std::swap(h[size_t(i)], h[size_t(j)]);
            j = i;
        }
    };
    auto down = [&](int i0, int n) {
        int i = i0;
        while (true) {
            int j1 = 2 * i + 1;
            if (j1 >= n) break;
            int j = j1;
            int j2 = j1 + 1;
            if (j2 < n && less(h[size_t(j2)], h[size_t(j1)])) j = j2;
            if (!less(h[size_t(j)], h[size_t(i)])) break;
            std::swap(h[size_t(i)], h[size_t(j)]);
            i = j;
        }
    };
    auto heap_pop = [&]() {
        int n = int(h.size()) - 1;
        std::swap(h[0], h[size_t(n)]);
        down(0, n);
        HeapItem it = h.back();
        h.pop_back();
        return it;
    };

    nodes_.resize(kNumOps);  // листы: nodes_[op] с left=right=-1
    for (int i = 0; i < kNumOps; i++) {
        uint64_t w = uint64_t(kOps[i].weight);
        if (w == 0) w = 1;  // нулевые веса участвуют как 1
        h.push_back({w, i, i});
    }
    for (int i = int(h.size()) / 2 - 1; i >= 0; i--) down(i, int(h.size()));

    int value = kNumOps;
    while (h.size() > 1) {
        HeapItem a = heap_pop();
        HeapItem b = heap_pop();
        Node n;
        n.left = int16_t(a.node);
        n.right = int16_t(b.node);
        nodes_.push_back(n);
        h.push_back({a.weight + b.weight, value++, int(nodes_.size()) - 1});
        up(int(h.size()) - 1);
    }
    root_ = h[0].node;
    bit_right_ = v.bit_means_right;
    (void)v.zero_weight_as_one;
    (void)v.tie_prefer_later;
}

bool FieldPathDecoder::apply_op(int op, bits::BitReader& r, FieldPath& fp) const {
    auto push = [&](int32_t v) {
        if (fp.last >= 6) return false;
        fp.last++;
        fp.path[fp.last] = v;
        return true;
    };
    switch (op) {
        case 0: fp.path[fp.last] += 1; return true;
        case 1: fp.path[fp.last] += 2; return true;
        case 2: fp.path[fp.last] += 3; return true;
        case 3: fp.path[fp.last] += 4; return true;
        case 4: fp.path[fp.last] += int32_t(read_ubitvar_fp(r)) + 5; return true;
        case 5: return push(0);
        case 6: return push(int32_t(read_ubitvar_fp(r)));
        case 7: fp.path[fp.last] += 1; return push(0);
        case 8: fp.path[fp.last] += 1; return push(int32_t(read_ubitvar_fp(r)));
        case 9: fp.path[fp.last] += int32_t(read_ubitvar_fp(r)); return push(0);
        case 10:
            fp.path[fp.last] += int32_t(read_ubitvar_fp(r)) + 2;
            return push(int32_t(read_ubitvar_fp(r)) + 1);
        case 11:
            fp.path[fp.last] += int32_t(r.read_bits(3)) + 2;
            return push(int32_t(r.read_bits(3)) + 1);
        case 12:
            fp.path[fp.last] += int32_t(r.read_bits(4)) + 2;
            return push(int32_t(r.read_bits(4)) + 1);
        case 13: return push(int32_t(read_ubitvar_fp(r))) &&
                        push(int32_t(read_ubitvar_fp(r)));
        case 14: return push(int32_t(r.read_bits(5))) &&
                        push(int32_t(r.read_bits(5)));
        case 15: return push(int32_t(read_ubitvar_fp(r))) &&
                        push(int32_t(read_ubitvar_fp(r))) &&
                        push(int32_t(read_ubitvar_fp(r)));
        case 16: return push(int32_t(r.read_bits(5))) &&
                        push(int32_t(r.read_bits(5))) &&
                        push(int32_t(r.read_bits(5)));
        case 17: fp.path[fp.last] += 1;
                 return push(int32_t(read_ubitvar_fp(r))) &&
                        push(int32_t(read_ubitvar_fp(r)));
        case 18: fp.path[fp.last] += 1;
                 return push(int32_t(r.read_bits(5))) &&
                        push(int32_t(r.read_bits(5)));
        case 19: fp.path[fp.last] += 1;
                 return push(int32_t(read_ubitvar_fp(r))) &&
                        push(int32_t(read_ubitvar_fp(r))) &&
                        push(int32_t(read_ubitvar_fp(r)));
        case 20: fp.path[fp.last] += 1;
                 return push(int32_t(r.read_bits(5))) &&
                        push(int32_t(r.read_bits(5))) &&
                        push(int32_t(r.read_bits(5)));
        case 21: fp.path[fp.last] += int32_t(r.read_ubitvar()) + 2;
                 return push(int32_t(read_ubitvar_fp(r))) &&
                        push(int32_t(read_ubitvar_fp(r)));
        case 22: fp.path[fp.last] += int32_t(r.read_ubitvar()) + 2;
                 return push(int32_t(r.read_bits(5))) &&
                        push(int32_t(r.read_bits(5)));
        case 23: fp.path[fp.last] += int32_t(r.read_ubitvar()) + 2;
                 return push(int32_t(read_ubitvar_fp(r))) &&
                        push(int32_t(read_ubitvar_fp(r))) &&
                        push(int32_t(read_ubitvar_fp(r)));
        case 24: fp.path[fp.last] += int32_t(r.read_ubitvar()) + 2;
                 return push(int32_t(r.read_bits(5))) &&
                        push(int32_t(r.read_bits(5))) &&
                        push(int32_t(r.read_bits(5)));
        case 25: {  // PushN
            uint32_t n = r.read_ubitvar();
            fp.path[fp.last] += int32_t(r.read_ubitvar());
            for (uint32_t i = 0; i < n; i++) {
                if (!push(int32_t(read_ubitvar_fp(r)))) return false;
            }
            return true;
        }
        case 26: {  // PushNAndNonTopological
            for (int32_t i = 0; i <= fp.last; i++) {
                if (r.read_bool()) fp.path[i] += read_var_sint32(r) + 1;
            }
            uint32_t n = r.read_ubitvar();
            for (uint32_t i = 0; i < n; i++) {
                if (!push(int32_t(read_ubitvar_fp(r)))) return false;
            }
            return true;
        }
        case 27:
            if (fp.last <= 0) return false;
            fp.last--;
            fp.path[fp.last] += 1;
            return true;
        case 28:
            if (fp.last <= 0) return false;
            fp.last--;
            fp.path[fp.last] += int32_t(read_ubitvar_fp(r)) + 1;
            return true;
        case 29: fp.last = 0; fp.path[0] += 1; return true;
        case 30: fp.last = 0; fp.path[0] += int32_t(read_ubitvar_fp(r)) + 1; return true;
        case 31: fp.last = 0; fp.path[0] += int32_t(r.read_bits(3)) + 1; return true;
        case 32: fp.last = 0; fp.path[0] += int32_t(r.read_bits(6)) + 1; return true;
        case 33: {  // PopNPlusOne
            fp.last -= int32_t(read_ubitvar_fp(r));
            if (fp.last < 0) return false;
            fp.path[fp.last] += 1;
            return true;
        }
        case 34: {  // PopNPlusN
            fp.last -= int32_t(read_ubitvar_fp(r));
            if (fp.last < 0) return false;
            fp.path[fp.last] += read_var_sint32(r);
            return true;
        }
        case 35: {  // PopNAndNonTopographical
            fp.last -= int32_t(read_ubitvar_fp(r));
            if (fp.last < 0) return false;
            for (int32_t i = 0; i <= fp.last; i++) {
                if (r.read_bool()) fp.path[i] += read_var_sint32(r);
            }
            return true;
        }
        case 36:  // NonTopoComplex
            for (int32_t i = 0; i <= fp.last; i++) {
                if (r.read_bool()) fp.path[i] += read_var_sint32(r);
            }
            return true;
        case 37:  // NonTopoPenultimatePlusOne
            if (fp.last < 1) return false;
            fp.path[fp.last - 1] += 1;
            return true;
        case 38:  // NonTopoComplexPack4Bits
            for (int32_t i = 0; i <= fp.last; i++) {
                if (r.read_bool()) fp.path[i] += int32_t(r.read_bits(4)) - 7;
            }
            return true;
        default:
            return false;
    }
}

std::vector<std::string> FieldPathDecoder::debug_codes() const {
    std::vector<std::string> codes(kNumOps);
    std::function<void(int, std::string)> walk = [&](int node, std::string prefix) {
        if (node < kNumOps) { codes[size_t(node)] = prefix; return; }
        const Node& n = nodes_[size_t(node)];
        walk(n.left, prefix + "0");
        walk(n.right, prefix + "1");
    };
    walk(root_, "");
    return codes;
}

bool FieldPathDecoder::read_paths(bits::BitReader& r,
                                  std::vector<FieldPath>& out) const {
    FieldPath fp;
    // Защита от desync: путей больше числа полей класса не бывает.
    // CDOTA_DataRadiant/DataDire несут > 1200 полей (в т.ч. массивы
    // видимости NPC по всем юнитам карты) — baseline легко превышает
    // десятки тысяч field-path операций.
    for (int guard = 0; guard < 200000; guard++) {
        int node = root_;
        while (node >= kNumOps) {
            const Node& n = nodes_[size_t(node)];
            bool bit = r.read_bool();
            node = (bit == bit_right_) ? n.right : n.left;
            if (r.overflowed()) return false;
        }
        static const char* dbg = std::getenv("ENT_DEBUG");
        if (node == kOpFinish) {
            if (dbg && dbg[0] == '3') std::fprintf(stderr, "        [op] Finish\n");
            return true;
        }
        if (!apply_op(node, r, fp)) return false;
        if (r.overflowed()) return false;
        if (dbg && dbg[0] == '3') {
            std::fprintf(stderr, "        [op] %-42s -> [", kOps[node].name);
            for (int i = 0; i <= fp.last; i++) std::fprintf(stderr, "%d ", fp.path[i]);
            std::fprintf(stderr, "]\n");
        }
        if (fp.path[fp.last] < 0 || fp.path[fp.last] > 20000) return false;
        out.push_back(fp);
    }
    return false;
}

}  // namespace dota::demo
