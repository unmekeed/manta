// Замер Game Coordinator на Node: сколько солей отдаёт один аккаунт.
//
// ЗАЧЕМ НА NODE. Питоновский замер (scripts/gc-probe.py) до Steam
// доходит, но ни один сервер входа его не принимает: пять разных CM
// подряд отвечают TryAnotherCM. `steam==1.4.4` — последняя версия на
// PyPI, шлёт ClientLogon с версией клиента 2019 года. Тот же аккаунт, тот
// же токен и та же машина через `steam-user` входят с первой попытки
// (спринт 166) — значит дело в библиотеке, а не в аккаунте.
//
// ПОЧЕМУ НЕ ПАКЕТ dota2. Он тянет `steam` (тот же мёртвый путь входа) и
// `steam-resources`, который не грузится на современном protobufjs:
// `ProtoBuf.newBuilder is not a function`. Весь тот стек ровесник
// питоновского и сгнил так же. Поэтому здесь только `steam-user` (вход
// доказан) и `protobufjs` с ЧЕТЫРЬМЯ сообщениями, выписанными вручную.
//
// Определения неполные НАМЕРЕННО: из CMsgDOTAMatch взяты пять полей из
// сотни. Protobuf это позволяет — неизвестные поля при разборе
// пропускаются, — а тащить полную схему значило бы тащить и её гниение.
// Номера полей взяты из настоящих .proto Valve, не из памяти.
//
// ЧТО ЗАМЕРЯЕТСЯ. Единственный вопрос, от которого зависит смысл затеи:
// сколько соль-запросов подряд отдаёт один аккаунт и на чём упирается. По
// дороге считаются кластеры — соль бесполезна, если до файла не
// дотянуться: 141 матч у нас припаркован из-за китайского 413.
//
// ЗАПУСК:
//     docker exec manta-postgres-1 psql -U dota -d manta -tAc \
//       "SELECT match_id FROM collectedmatches ORDER BY match_id DESC LIMIT 300" \
//       > /tmp/ids.txt
//     make gc-probe-node ARGS="--ids-file /tmp/ids.txt --limit 200"
import SteamUser from 'steam-user';
import protobuf from 'protobufjs';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';

const APPID = 570;
// Номера сообщений — из dota_gcmessages_msgid.proto и gcsystemmsgs.proto.
const MSG_CLIENT_WELCOME = 4004;
const MSG_CLIENT_HELLO = 4006;
const MSG_MATCH_DETAILS_REQUEST = 7095;
const MSG_MATCH_DETAILS_RESPONSE = 7096;

// Ровно то, что нужно, и ни поля больше. Номера — из
// dota_gcmessages_common.proto (CMsgDOTAMatch) и dota_gcmessages_client.proto.
// replay_salt именно fixed32, а не uint32: перепутай тип — соль
// разобралась бы в мусор, и выглядело бы это как «Valve отдала ерунду».
const SCHEMA = `
syntax = "proto2";
message CMsgClientHello { optional uint32 version = 1; }
message CMsgGCMatchDetailsRequest { optional uint64 match_id = 1; }
message CMsgDOTAMatch {
  optional uint32  duration    = 3;
  optional uint64  match_id    = 6;
  optional uint32  cluster     = 10;
  optional fixed32 replay_salt = 13;
  optional uint32  lobby_type  = 16;
}
message CMsgGCMatchDetailsResponse {
  optional uint32 result = 1;
  optional CMsgDOTAMatch match = 2;
}
`;

// keepCase — не косметика. Без него protobufjs переименовывает поля в
// camelCase (match_id → matchId), а `create`/`fromObject` МОЛЧА
// выбрасывают ключи, которых не знает. Запрос уезжал бы пустым, ответ
// разбирался бы без соли — и выглядело бы это как «Valve ничего не
// отдаёт», то есть беда своего кода читалась бы как отказ Valve.
// Поймано round-trip проверкой до первого живого запуска.
const root = protobuf.parse(SCHEMA, { keepCase: true }).root;
const Hello = root.lookupType('CMsgClientHello');
const DetailsRequest = root.lookupType('CMsgGCMatchDetailsRequest');
const DetailsResponse = root.lookupType('CMsgGCMatchDetailsResponse');

// -- аргументы ----------------------------------------------------------------

function parseArgs(argv) {
  const out = { limit: 100, delay: 1000, stopAfter: 5, idsFile: null };
  for (let i = 0; i < argv.length; i++) {
    const next = () => argv[++i];
    switch (argv[i]) {
      case '--ids-file': out.idsFile = next(); break;
      case '--limit': out.limit = Number(next()); break;
      case '--delay': out.delay = Number(next()); break;
      case '--stop-after': out.stopAfter = Number(next()); break;
      default:
        console.error(`неизвестный аргумент: ${argv[i]}`);
        process.exit(2);
    }
  }
  return out;
}

const args = parseArgs(process.argv.slice(2));
if (!args.idsFile) {
  console.error('ОШИБКА: нужен --ids-file со списком match_id (по одному в строке)');
  console.error('  docker exec manta-postgres-1 psql -U dota -d manta -tAc \\');
  console.error('    "SELECT match_id FROM collectedmatches ORDER BY match_id DESC LIMIT 300" \\');
  console.error('    > /tmp/ids.txt');
  process.exit(2);
}

let ids;
try {
  ids = readFileSync(args.idsFile, 'utf8')
    .split('\n').map((s) => s.trim()).filter((s) => /^\d+$/.test(s));
} catch (err) {
  console.error(`ОШИБКА: не читается ${args.idsFile}: ${err.message}`);
  process.exit(2);
}
if (ids.length === 0) {
  console.error(`ОШИБКА: в ${args.idsFile} нет ни одного match_id`);
  process.exit(2);
}
ids = ids.slice(0, args.limit);

// -- вход ---------------------------------------------------------------------

const stateDir = process.env.GC_STATE_DIR || join(homedir(), '.manta-gc');
let token;
try {
  token = readFileSync(join(stateDir, 'refresh-token'), 'utf8').trim();
} catch {
  console.error(`ОШИБКА: нет токена в ${stateDir}/refresh-token — make gc-token`);
  process.exit(2);
}
if (!token) {
  console.error('ОШИБКА: файл токена пуст — make gc-token');
  process.exit(2);
}

const client = new SteamUser({ dataDirectory: stateDir });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const stats = {
  asked: 0, withSalt: 0, noSalt: 0, refused: 0, silent: 0,
  clusters: new Map(), firstFailureAt: null,
};

function note(cluster) {
  const key = cluster ?? 'нет';
  stats.clusters.set(key, (stats.clusters.get(key) || 0) + 1);
}

// Ответ приходит на тот же jobid, поэтому колбэк, а не общее событие.
// Таймаут обязателен: GC умеет просто не отвечать, и без него замер
// завис бы на первом же молчании — ровно так вёл себя питоновский вход.
function requestDetails(matchId, timeoutMs = 15000) {
  return new Promise((resolve) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      resolve({ kind: 'silent' });
    }, timeoutMs);

    const payload = DetailsRequest.encode(
      DetailsRequest.fromObject({ match_id: Number(matchId) })).finish();

    client.sendToGC(APPID, MSG_MATCH_DETAILS_REQUEST, {}, payload,
      (appid, msgType, body) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (msgType !== MSG_MATCH_DETAILS_RESPONSE) {
          resolve({ kind: 'refused', why: `msg ${msgType}` });
          return;
        }
        try {
          const decoded = DetailsResponse.toObject(
            DetailsResponse.decode(body), { longs: String });
          resolve({ kind: 'ok', response: decoded });
        } catch (err) {
          resolve({ kind: 'refused', why: `разбор: ${err.message}` });
        }
      });
  });
}

async function waitForGC(timeoutMs = 45000) {
  return new Promise((resolve) => {
    let ready = false;
    const onMsg = (appid, msgType) => {
      if (appid === APPID && msgType === MSG_CLIENT_WELCOME && !ready) {
        ready = true;
        client.removeListener('receivedFromGC', onMsg);
        resolve(true);
      }
    };
    client.on('receivedFromGC', onMsg);

    // Привет шлём повторно: GC часто игнорирует первый, пока сессия
    // приложения не поднялась целиком. Один раз — и замер молча ждал бы
    // ответа, которого не будет.
    const hello = Hello.encode(Hello.fromObject({})).finish();
    const beat = setInterval(() => {
      if (ready) { clearInterval(beat); return; }
      client.sendToGC(APPID, MSG_CLIENT_HELLO, {}, hello);
    }, 3000);
    client.sendToGC(APPID, MSG_CLIENT_HELLO, {}, hello);

    setTimeout(() => {
      clearInterval(beat);
      if (!ready) {
        client.removeListener('receivedFromGC', onMsg);
        resolve(false);
      }
    }, timeoutMs);
  });
}

function report() {
  const seconds = (Date.now() - started) / 1000;
  console.log('\n=== ЗАМЕР GC ===');
  console.log(`спрошено:        ${stats.asked}`);
  console.log(`с солью:         ${stats.withSalt}`);
  console.log(`без соли:        ${stats.noSalt}`);
  console.log(`отказ GC:        ${stats.refused}`);
  console.log(`молчание:        ${stats.silent}`);
  console.log(`время:           ${seconds.toFixed(0)} с ` +
              `(${(stats.asked / (seconds / 60)).toFixed(1)} запросов/мин)`);
  if (stats.firstFailureAt !== null) {
    console.log(`первый отказ на: ${stats.firstFailureAt}-м запросе`);
  }
  console.log('\nкластеры:');
  const rows = [...stats.clusters.entries()].sort((a, b) => b[1] - a[1]);
  for (const [cluster, n] of rows) {
    const mark = String(cluster).startsWith('4') ? '  ← китайские, до них нет маршрута' : '';
    console.log(`  ${String(cluster).padEnd(6)} ${String(n).padStart(4)}${mark}`);
  }
  console.log('\nСоль без достижимого кластера бесполезна: проверьте, что');
  console.log('матчи, которые вы собираетесь качать, лежат не на 4xx.');
}

let started = Date.now();

client.on('loggedOn', async () => {
  console.log(`вошли: ${client.steamID.getSteamID64()}`);
  client.gamesPlayed([APPID]);

  process.stdout.write('поднимаю сессию GC …');
  const ready = await waitForGC();
  console.log(ready ? ' готово' : ' НЕ ОТВЕТИЛ');
  if (!ready) {
    console.error('\nGC не прислал ClientWelcome. Аккаунт входит в Steam, но');
    console.error('в Dota 2 его не пускают: проверьте, что игра есть в');
    console.error('библиотеке и что аккаунт не ограничен.');
    client.logOff();
    process.exit(1);
  }

  started = Date.now();
  let inARow = 0;
  for (const id of ids) {
    stats.asked++;
    const res = await requestDetails(id);
    if (res.kind === 'ok' && res.response.result === 1 && res.response.match) {
      const m = res.response.match;
      if (m.replay_salt) { stats.withSalt++; } else { stats.noSalt++; }
      note(m.cluster);
      inARow = 0;
    } else {
      if (res.kind === 'silent') stats.silent++; else stats.refused++;
      if (stats.firstFailureAt === null) stats.firstFailureAt = stats.asked;
      inARow++;
      if (inARow >= args.stopAfter) {
        console.log(`\n${inARow} отказов подряд — дальше не идём.`);
        break;
      }
    }
    if (stats.asked % 25 === 0) {
      console.log(`  … ${stats.asked}: с солью ${stats.withSalt}, ` +
                  `отказов ${stats.refused + stats.silent}`);
    }
    await sleep(args.delay);
  }

  report();
  client.logOff();
  process.exit(0);
});

client.on('error', (err) => {
  const name = SteamUser.EResult[err.eresult] || 'неизвестно';
  console.error(`\nSteam отказал: ${name} (eresult=${err.eresult})`);
  if (stats.asked) report();
  process.exit(1);
});

console.log(`матчей в списке: ${ids.length}, пауза ${args.delay} мс`);
client.logOn({ refreshToken: token });
