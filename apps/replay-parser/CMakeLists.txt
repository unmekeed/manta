cmake_minimum_required(VERSION 3.20)
project(replay_parser CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()
add_compile_options(-Wall -Wextra)

find_library(SNAPPY_LIB snappy REQUIRED)

add_library(parser_core
  src/demo_reader.cpp
  src/packet_demux.cpp
  src/string_tables.cpp
  src/combat_log.cpp
  src/fieldpath.cpp
  src/field_decoder.cpp
  src/entities.cpp
)
target_include_directories(parser_core PUBLIC include)
target_link_libraries(parser_core PUBLIC ${SNAPPY_LIB})

add_executable(demoinfo src/demoinfo_main.cpp)
target_link_libraries(demoinfo PRIVATE parser_core)

enable_testing()
add_executable(test_core tests/test_core.cpp)
target_link_libraries(test_core PRIVATE parser_core)
add_test(NAME core COMMAND test_core)
