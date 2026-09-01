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
  const out = { limit: 100, delay: 1000, stopAfter: 5, idsFile: null,
                verify: 0, cooldown: 0, cooldowns: 3 };
  for (let i = 0; i < argv.length; i++) {
    const next = () => argv[++i];
    switch (argv[i]) {
      case '--ids-file': out.idsFile = next(); break;
      case '--limit': out.limit = Number(next()); break;
      case '--delay': out.delay = Number(next()); break;
      case '--stop-after': out.stopAfter = Number(next()); break;
      case '--verify': out.verify = Number(next()); break;
      case '--cooldown': out.cooldown = Number(next()); break;
      case '--cooldowns': out.cooldowns = Number(next()); break;
      default:
        console.error(`неизвестный аргумент: ${argv[i]}`);
        process.exit(2);
    }
  }
  return out;
}

// Ссылка на реплей: кластер, номер матча и соль. Формат Valve, и переврать
// его легко — а цена ошибки в том, что «соль не работает» и «мы неверно
// собрали адрес» выглядят одинаково: 404 в обоих случаях.
function replayUrl(cluster, matchId, salt) {
  return `http://replay${cluster}.valve.net/570/${matchId}_${salt}.dem.bz2`;
}

// Проверка соли БЕЗ скачивания: просим первый байт. Реплей весит 58 МиБ,
// и качать их ради проверки формата — трата канала на вопрос, который
// решается заголовком.
//
// Ответ 200/206 — соль настоящая и файл на месте. 404 — соль неверна ЛИБО
// реплей у Valve уже удалён (он живёт около двух недель), и различить эти
// два случая по коду нельзя: смотреть надо на возраст матча.
async function checkSalt(cluster, matchId, salt, timeoutMs = 15000) {
  const url = replayUrl(cluster, matchId, salt);
  const stop = AbortSignal.timeout(timeoutMs);
  try {
    const resp = await fetch(url, { headers: { Range: 'bytes=0-0' },
                                    signal: stop });
    return { ok: resp.status === 200 || resp.status === 206,
             status: resp.status, url };
  } catch (err) {
    return { ok: false, status: err.name === 'TimeoutError' ? 'таймаут'
                                                            : err.message, url };
  }
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
  clusters: new Map(), firstFailureAt: null, salts: [],
  saltsOk: 0, saltsBad: 0, revived: 0, cooled: 0,
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

// Соль, из которой не скачивается файл, — не соль. Проверять надо
// ОТДЕЛЬНО от её получения: GC может честно отдать соль к реплею,
// который Valve уже удалила, и по одному лишь «соль получена» это
// неотличимо от рабочего случая.
async function verifySalts(count) {
  const sample = stats.salts.slice(0, count);
  if (sample.length === 0) {
    console.log('\nпроверять нечего: солей на достижимых кластерах нет');
    return;
  }
  console.log(`\nпроверяю ${sample.length} солей на CDN (первый байт)…`);
  for (const s of sample) {
    const res = await checkSalt(s.cluster, s.matchId, s.salt);
    if (res.ok) { stats.saltsOk++; } else { stats.saltsBad++; }
    console.log(`  ${res.ok ? 'OK ' : 'нет'} матч ${s.matchId} ` +
                `кластер ${s.cluster} → ${res.status}`);
  }
}

function report() {
  const seconds = (Date.now() - started) / 1000;
  console.log('\n=== ЗАМЕР GC ===');
  console.log(`спрошено:        ${stats.asked}`);
  console.log(`с солью:         ${stats.withSalt}`);
  console.log(`без соли:        ${stats.noSalt}`);
  console.log(`отказ GC:        ${stats.refused}`);
  console.log(`молчание:        ${stats.silent}`);
  // Общий темп считать по всем запросам нельзя: неотвеченный стоит
  // пятнадцати секунд таймаута, и «8.5 запросов/мин» на живом прогоне
  // описывало не Valve, а нашу собственную арифметику ожидания.
  const spentWaiting = stats.cooled * args.cooldown;
  const working = Math.max(seconds - spentWaiting, 1);
  console.log(`время:           ${seconds.toFixed(0)} с ` +
              (spentWaiting ? `(из них передышек ${spentWaiting} с)` : ''));
  console.log(`темп по ответам: ` +
              `${(stats.withSalt / (working / 60)).toFixed(1)} солей/мин`);
  if (stats.cooled) {
    console.log(`передышек:       ${stats.cooled}, ` +
                `ожил после ${stats.revived}`);
    console.log(stats.revived
      ? '  → GC оживает после паузы: упирались в ТЕМП, лечится задержкой'
      : '  → после паузы не ожил: похоже на исчерпанный БЮДЖЕТ, не темп');
  }
  if (stats.saltsOk || stats.saltsBad) {
    console.log(`соль качается:   ${stats.saltsOk} из ` +
                `${stats.saltsOk + stats.saltsBad} проверенных`);
  }
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
  if (stats.refused + stats.silent === 0) {
    console.log('\nОтказов не было НИ ОДНОГО — значит потолок Valve не найден,');
    console.log(`а темп выше — это наша пауза ${args.delay} мс. Чтобы найти`);
    console.log('потолок, нужен прогон длиннее и с меньшей паузой.');
  }
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
  let revivedPending = false;
  for (const id of ids) {
    stats.asked++;
    const res = await requestDetails(id);
    if (res.kind === 'ok' && res.response.result === 1 && res.response.match) {
      const m = res.response.match;
      if (m.replay_salt) {
        stats.withSalt++;
        // Копим только достижимые кластеры: проверять 4xx незачем, до
        // них нет маршрута, и таймауты съели бы всю проверку.
        if (!String(m.cluster).startsWith('4')) {
          stats.salts.push({ cluster: m.cluster, matchId: m.match_id,
                             salt: m.replay_salt });
        }
      } else { stats.noSalt++; }
      note(m.cluster);
      inARow = 0;
      if (revivedPending) { stats.revived++; revivedPending = false; }
    } else {
      if (res.kind === 'silent') stats.silent++; else stats.refused++;
      if (stats.firstFailureAt === null) stats.firstFailureAt = stats.asked;
      inARow++;
      if (inARow >= args.stopAfter) {
        // РАЗВИЛКА ЗАМЕРА, а не защита от зацикливания.
        //
        // GC не отказывает словами — он замолкает, и молчание одинаково
        // выглядит и когда мы спрашиваем слишком часто, и когда выбрали
        // суточный бюджет. Различить можно только временем: подождать и
        // спросить снова. Ожил — дело в темпе, лечится паузой. Не ожил —
        // бюджет исчерпан, и одним аккаунтом путь не закрыть.
        //
        // Без этой паузы замер обрывался на первой же серии молчаний и
        // отвечал «потолок такой-то», не различив две совершенно разные
        // причины.
        if (args.cooldown > 0 && stats.cooled < args.cooldowns) {
          stats.cooled++;
          console.log(`\n${inARow} подряд без ответа — жду ${args.cooldown} с ` +
                      `(передышка ${stats.cooled}/${args.cooldowns})…`);
          await sleep(args.cooldown * 1000);
          inARow = 0;
          revivedPending = true;
          continue;
        }
        console.log(`\n${inARow} подряд без ответа — дальше не идём.`);
        break;
      }
    }
    if (stats.asked % 25 === 0) {
      console.log(`  … ${stats.asked}: с солью ${stats.withSalt}, ` +
                  `отказов ${stats.refused + stats.silent}`);
    }
    await sleep(args.delay);
  }

  if (args.verify > 0) await verifySalts(args.verify);
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
