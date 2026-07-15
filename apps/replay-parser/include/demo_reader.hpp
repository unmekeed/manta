// DemoReader — покадровое чтение файлов Source 2 Demo (Гл. 5.1 спецификации).
//
// Формат внешнего слоя:
//   [8]  магия "PBDEMS2\0"
//   [4]  uint32 LE: смещение summary-блока (CDemoFileInfo)
//   [4]  uint32 LE: служебное смещение
//   далее поток кадров: varint cmd | varint tick | varint size | payload.
//   Бит 0x40 в cmd означает snappy-сжатие полезной нагрузки.
#pragma once

#include <cstdint>
#include <functional>
#include <map>
#include <string>
#include <string_view>
#include <vector>

namespace dota::demo {

// EDemoCommands (demo.proto Source 2).
enum class Cmd : int32_t {
    Error = -1,
    Stop = 0,
    FileHeader = 1,
    FileInfo = 2,
    SyncTick = 3,
    SendTables = 4,
    ClassInfo = 5,
    StringTables = 6,
    Packet = 7,
    SignonPacket = 8,
    ConsoleCmd = 9,
    CustomData = 10,
    CustomDataCallbacks = 11,
    UserCmd = 12,
    FullPacket = 13,
    SaveGame = 14,
    SpawnGroups = 15,
    AnimationData = 16,
    AnimationHeader = 17,
};

constexpr uint32_t kCompressedFlag = 0x40;

const char* cmd_name(int32_t cmd);

// Один кадр демо-потока. payload указывает либо в исходный буфер файла,
// либо в scratch-буфер после распаковки snappy.
struct Frame {
    int32_t cmd = 0;
    uint32_t tick = 0;
    bool was_compressed = false;
    std::string_view payload;
};

// Метаданные из CDemoFileHeader.
struct FileHeader {
    std::string demo_file_stamp;
    int64_t network_protocol = 0;
    std::string server_name;
    std::string client_name;
    std::string map_name;
    std::string game_directory;
    int64_t fullpackets_version = 0;
    int64_t build_num = 0;
};

// Сводка матча из CDemoFileInfo / CDotaGameInfo.
struct FileInfo {
    float playback_time_s = 0;
    int64_t playback_ticks = 0;
    int64_t playback_frames = 0;
    uint64_t match_id = 0;
    int64_t game_mode = 0;
    int64_t game_winner = 0;   // 2=Radiant, 3=Dire
    int64_t league_id = 0;
    int64_t end_time_unix = 0;
    struct Player {
        int64_t hero_id = 0;
        std::string hero_name;
        std::string player_name;
        int64_t game_team = 0;
        uint64_t steam_id = 0;
    };
    std::vector<Player> players;
};

// Статистика полного прохода по файлу.
struct ScanStats {
    uint64_t frames = 0;
    uint64_t compressed_frames = 0;
    uint64_t payload_bytes = 0;
    uint64_t decompressed_bytes = 0;
    uint32_t last_tick = 0;
    std::map<int32_t, uint64_t> frames_by_cmd;
};

class DemoReader {
  public:
    // Открыть файл через mmap. Бросает std::runtime_error при ошибке
    // ввода-вывода или неверной магии.
    explicit DemoReader(const std::string& path);
    ~DemoReader();

    DemoReader(const DemoReader&) = delete;
    DemoReader& operator=(const DemoReader&) = delete;

    uint32_t summary_offset() const { return summary_offset_; }
    size_t file_size() const { return size_; }

    // Прочитать следующий кадр начиная с текущей позиции.
    // false — конец потока (DEM_Stop или конец файла).
    bool next(Frame& out);

    // Сбросить позицию на первый кадр.
    void rewind();

    // Полный проход по файлу с подсчётом статистики; вызывает cb для
    // каждого кадра, если cb задан.
    ScanStats scan(const std::function<void(const Frame&)>& cb = nullptr);

    // Разобрать CDemoFileHeader (первый кадр файла).
    FileHeader read_file_header();

    // Разобрать CDemoFileInfo по summary-смещению из заголовка.
    FileInfo read_file_info();

  private:
    std::string_view raw(size_t off, size_t len) const;

    const uint8_t* data_ = nullptr;
    size_t size_ = 0;
    size_t pos_ = 0;
    uint32_t summary_offset_ = 0;
    std::vector<uint8_t> scratch_;  // буфер распаковки текущего кадра
};

}  // namespace dota::demo
