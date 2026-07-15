#include "demo_reader.hpp"

#include <fcntl.h>
#include <snappy.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cstring>
#include <stdexcept>

#include "pb_lite.hpp"

namespace dota::demo {

namespace {

constexpr char kMagic[8] = {'P', 'B', 'D', 'E', 'M', 'S', '2', '\0'};
constexpr size_t kHeaderSize = 16;  // магия + 2 × uint32

uint32_t read_u32le(const uint8_t* p) {
    return uint32_t(p[0]) | uint32_t(p[1]) << 8 |
           uint32_t(p[2]) << 16 | uint32_t(p[3]) << 24;
}

}  // namespace

const char* cmd_name(int32_t cmd) {
    switch (Cmd(cmd)) {
        case Cmd::Error: return "DEM_Error";
        case Cmd::Stop: return "DEM_Stop";
        case Cmd::FileHeader: return "DEM_FileHeader";
        case Cmd::FileInfo: return "DEM_FileInfo";
        case Cmd::SyncTick: return "DEM_SyncTick";
        case Cmd::SendTables: return "DEM_SendTables";
        case Cmd::ClassInfo: return "DEM_ClassInfo";
        case Cmd::StringTables: return "DEM_StringTables";
        case Cmd::Packet: return "DEM_Packet";
        case Cmd::SignonPacket: return "DEM_SignonPacket";
        case Cmd::ConsoleCmd: return "DEM_ConsoleCmd";
        case Cmd::CustomData: return "DEM_CustomData";
        case Cmd::CustomDataCallbacks: return "DEM_CustomDataCallbacks";
        case Cmd::UserCmd: return "DEM_UserCmd";
        case Cmd::FullPacket: return "DEM_FullPacket";
        case Cmd::SaveGame: return "DEM_SaveGame";
        case Cmd::SpawnGroups: return "DEM_SpawnGroups";
        case Cmd::AnimationData: return "DEM_AnimationData";
        case Cmd::AnimationHeader: return "DEM_AnimationHeader";
    }
    return "DEM_Unknown";
}

DemoReader::DemoReader(const std::string& path) {
    int fd = ::open(path.c_str(), O_RDONLY);
    if (fd < 0) throw std::runtime_error("cannot open " + path);
    struct stat st{};
    if (::fstat(fd, &st) != 0 || st.st_size < off_t(kHeaderSize)) {
        ::close(fd);
        throw std::runtime_error("file too small: " + path);
    }
    size_ = size_t(st.st_size);
    void* map = ::mmap(nullptr, size_, PROT_READ, MAP_PRIVATE, fd, 0);
    ::close(fd);
    if (map == MAP_FAILED) throw std::runtime_error("mmap failed: " + path);
    data_ = static_cast<const uint8_t*>(map);

    if (std::memcmp(data_, kMagic, sizeof(kMagic)) != 0) {
        ::munmap(map, size_);
        data_ = nullptr;
        throw std::runtime_error("not a Source 2 demo (bad magic): " + path);
    }
    summary_offset_ = read_u32le(data_ + 8);
    pos_ = kHeaderSize;
}

DemoReader::~DemoReader() {
    if (data_) ::munmap(const_cast<uint8_t*>(data_), size_);
}

std::string_view DemoReader::raw(size_t off, size_t len) const {
    return {reinterpret_cast<const char*>(data_) + off, len};
}

void DemoReader::rewind() { pos_ = kHeaderSize; }

bool DemoReader::next(Frame& out) {
    pb::Reader r(raw(pos_, size_ - pos_));
    const uint8_t* frame_start = r.p;

    auto raw_cmd = r.varint();
    auto tick = r.varint();
    auto size = r.varint();
    if (!raw_cmd || !tick || !size) return false;
    if (size_t(r.end - r.p) < *size) return false;  // усечённый файл

    out.cmd = int32_t(*raw_cmd & ~uint64_t(kCompressedFlag));
    out.tick = uint32_t(*tick);
    out.was_compressed = (*raw_cmd & kCompressedFlag) != 0;

    const char* payload = reinterpret_cast<const char*>(r.p);
    size_t payload_len = size_t(*size);
    pos_ += size_t(r.p - frame_start) + payload_len;

    if (out.was_compressed) {
        size_t uncompressed = 0;
        if (!snappy::GetUncompressedLength(payload, payload_len, &uncompressed)) {
            throw std::runtime_error("snappy: bad length preamble");
        }
        scratch_.resize(uncompressed);
        if (!snappy::RawUncompress(payload, payload_len,
                                   reinterpret_cast<char*>(scratch_.data()))) {
            throw std::runtime_error("snappy: corrupted frame payload");
        }
        out.payload = {reinterpret_cast<const char*>(scratch_.data()), uncompressed};
    } else {
        out.payload = {payload, payload_len};
    }
    return out.cmd != int32_t(Cmd::Stop);
}

ScanStats DemoReader::scan(const std::function<void(const Frame&)>& cb) {
    rewind();
    ScanStats st;
    Frame f;
    while (next(f)) {
        st.frames++;
        st.frames_by_cmd[f.cmd]++;
        st.payload_bytes += f.payload.size();
        if (f.was_compressed) {
            st.compressed_frames++;
            st.decompressed_bytes += f.payload.size();
        }
        // tick == 0xFFFFFFFF у преигровых кадров — не учитываем как максимум
        if (f.tick != 0xFFFFFFFFu && f.tick > st.last_tick) st.last_tick = f.tick;
        if (cb) cb(f);
    }
    return st;
}

FileHeader DemoReader::read_file_header() {
    rewind();
    Frame f;
    if (!next(f) || f.cmd != int32_t(Cmd::FileHeader)) {
        throw std::runtime_error("first frame is not DEM_FileHeader");
    }
    FileHeader h;
    pb::Reader r(f.payload);
    pb::Field fld;
    while (pb::next_field(r, fld)) {
        switch (fld.number) {
            case 1: h.demo_file_stamp = std::string(fld.data); break;
            case 2: h.network_protocol = int64_t(fld.varint); break;
            case 3: h.server_name = std::string(fld.data); break;
            case 4: h.client_name = std::string(fld.data); break;
            case 5: h.map_name = std::string(fld.data); break;
            case 6: h.game_directory = std::string(fld.data); break;
            case 7: h.fullpackets_version = int64_t(fld.varint); break;
            case 13: h.build_num = int64_t(fld.varint); break;
            default: break;
        }
    }
    return h;
}

namespace {

// CDotaGameInfo.CPlayerInfo
FileInfo::Player parse_player(std::string_view msg) {
    FileInfo::Player p;
    pb::Reader r(msg);
    pb::Field f;
    while (pb::next_field(r, f)) {
        switch (f.number) {
            case 1: p.hero_name = std::string(f.data); break;
            case 2: p.player_name = std::string(f.data); break;
            case 4: p.steam_id = f.varint; break;
            case 5: p.game_team = int64_t(f.varint); break;
            default: break;
        }
    }
    return p;
}

// CDotaGameInfo
void parse_dota_info(std::string_view msg, FileInfo& out) {
    pb::Reader r(msg);
    pb::Field f;
    while (pb::next_field(r, f)) {
        switch (f.number) {
            case 1: out.match_id = f.varint; break;
            case 2: out.game_mode = int64_t(f.varint); break;
            case 3: out.game_winner = int64_t(f.varint); break;
            case 4: out.players.push_back(parse_player(f.data)); break;
            case 5: out.league_id = int64_t(f.varint); break;
            case 11: out.end_time_unix = int64_t(f.varint); break;
            default: break;
        }
    }
}

}  // namespace

FileInfo DemoReader::read_file_info() {
    if (summary_offset_ == 0 || summary_offset_ >= size_) {
        throw std::runtime_error("summary offset out of range (unfinished demo?)");
    }
    size_t saved = pos_;
    pos_ = summary_offset_;
    Frame f;
    bool ok = next(f);
    pos_ = saved;
    if (!ok || f.cmd != int32_t(Cmd::FileInfo)) {
        throw std::runtime_error("no DEM_FileInfo at summary offset");
    }

    FileInfo info;
    pb::Reader r(f.payload);
    pb::Field fld;
    while (pb::next_field(r, fld)) {
        switch (fld.number) {
            case 1: {  // float playback_time
                uint32_t bits = uint32_t(fld.varint);
                float v;
                std::memcpy(&v, &bits, sizeof v);
                info.playback_time_s = v;
                break;
            }
            case 2: info.playback_ticks = int64_t(fld.varint); break;
            case 3: info.playback_frames = int64_t(fld.varint); break;
            case 4: {  // CGameInfo { dota = 4 }
                pb::Reader gi(fld.data);
                pb::Field gf;
                while (pb::next_field(gi, gf)) {
                    if (gf.number == 4) parse_dota_info(gf.data, info);
                }
                break;
            }
            default: break;
        }
    }
    return info;
}

}  // namespace dota::demo
