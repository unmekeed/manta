// Наполнитель солей: Game Coordinator → таблица ReplaySalts (спринт 171).
//
// ЗАЧЕМ. Соль реплея знает только GC. Сегодня мы платим за неё вызовом
// OpenDota (`/matches/{id}`) из 2000 в сутки на весь проект — купить
// больше нельзя. Перенеся соли сюда, освобождаем эту квоту целиком под
// то, что приносит основной поток данных: поиск матчей и таймлайны.
//
// ПОЧЕМУ ТЕМП ТАКОЙ СКРОМНЫЙ. Замеры 2026-09-01: бюджет аккаунта
// НАКОПИТЕЛЬНЫЙ, а не частотный. Первые 200 запросов прошли подряд —
// это опустошение накопителя, полного за месяцы простоя. Дальше аккаунт
// отдавал около 0.3 соли в минуту, а прогон с паузой В ДЕСЯТЬ СЕКУНД —
// вдесятеро медленнее удачного — сломался на первом же запросе. То есть
// частота ни при чём, и «спрашивать помедленнее» не помогает: помогает
// только спрашивать МЕНЬШЕ.
//
// Поэтому наполнитель берёт маленькими порциями по расписанию, а не
// длинным прогоном. Порядок величины бюджета — 200–400 в сутки; по
// умолчанию берём 8 за прогон, то есть при часовом расписании около 190.
//
// МОЛЧАНИЕ — НЕ ОШИБКА. GC не отказывает словами, он перестаёт отвечать.
// На исчерпанном бюджете это штатное состояние: значит на сегодня всё,
// придём завтра. Алерт здесь был бы ежесуточной ложной тревогой — тем
// самым, что мы вычищали в спринте 163.
//
// ЗАПУСК:
//     make gc-salts                 # одна порция
//     GC_SALTS_PER_RUN=20 make gc-salts
import SteamUser from 'steam-user';
import protobuf from 'protobufjs';
import pg from 'pg';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';

const APPID = 570;
const MSG_CLIENT_WELCOME = 4004;
const MSG_CLIENT_HELLO = 4006;
const MSG_MATCH_DETAILS_REQUEST = 7095;
const MSG_MATCH_DETAILS_RESPONSE = 7096;

// Та же схема, что в замере: четыре сообщения, номера полей из настоящих
// .proto Valve. keepCase обязателен — без него protobufjs переименует
// поля в camelCase, а fromObject молча выбросит наши ключи (спринт 167).
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
const root = protobuf.parse(SCHEMA, { keepCase: true }).root;
const Hello = root.lookupType('CMsgClientHello');
const DetailsRequest = root.lookupType('CMsgGCMatchDetailsRequest');
const DetailsResponse = root.lookupType('CMsgGCMatchDetailsResponse');

const PER_RUN = Number(process.env.GC_SALTS_PER_RUN || 8);
const DELAY_MS = Number(process.env.GC_SALTS_DELAY_MS || 2000);
// Две тишины подряд — и хватит. Бюджет не восстанавливается за минуты
// (проверено пятиминутными передышками), так что упорство здесь только
// тратит время расписания.
const SILENCE_LIMIT = Number(process.env.GC_SALTS_SILENCE_LIMIT || 2);

// Кого спрашивать. Матчи, про которые мы знаем, реплея не имеем и соли
// тоже. Свежие вперёд: Valve хранит реплеи около двух недель, и соль к
// старому матчу — соль к файлу, которого уже нет.
const PICK_SQL = `
  SELECT match_id FROM (
      SELECT match_id FROM ReplayCandidates WHERE state = 'new'
      UNION
      SELECT match_id FROM CollectedMatches WHERE NOT has_replay
  ) q
  WHERE match_id NOT IN (SELECT match_id FROM ReplaySalts)
  ORDER BY match_id DESC
  LIMIT $1`;

const SAVE_SQL = `
  INSERT INTO ReplaySalts (match_id, cluster, salt, source)
  VALUES ($1, $2, $3, 'gc')
  ON CONFLICT (match_id) DO NOTHING`;

function dsn() {
  const url = process.env.POSTGRES_DSN;
  if (url) return { connectionString: url };
  return {
    host: process.env.POSTGRES_HOST || 'localhost',
    port: Number(process.env.POSTGRES_PORT || 5432),
    user: process.env.POSTGRES_USER || 'dota',
    password: process.env.POSTGRES_PASSWORD || process.env.MANTA_DB_PASSWORD || '',
    database: process.env.POSTGRES_DB || 'manta',
  };
}

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

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const client = new SteamUser({ dataDirectory: stateDir });
const db = new pg.Client(dsn());

function requestDetails(matchId, timeoutMs = 15000) {
  return new Promise((resolve) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      resolve(null);                       // молчание
    }, timeoutMs);
    const payload = DetailsRequest.encode(
      DetailsRequest.fromObject({ match_id: Number(matchId) })).finish();
    client.sendToGC(APPID, MSG_MATCH_DETAILS_REQUEST, {}, payload,
      (appid, msgType, body) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (msgType !== MSG_MATCH_DETAILS_RESPONSE) { resolve(null); return; }
        try {
          resolve(DetailsResponse.toObject(DetailsResponse.decode(body),
                                           { longs: String }));
        } catch { resolve(null); }
      });
  });
}

function waitForGC(timeoutMs = 45000) {
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
    const hello = Hello.encode(Hello.fromObject({})).finish();
    // Привет повторяем: GC часто игнорирует первый, пока сессия
    // приложения не поднялась целиком.
    const beat = setInterval(() => {
      if (ready) { clearInterval(beat); return; }
      client.sendToGC(APPID, MSG_CLIENT_HELLO, {}, hello);
    }, 3000);
    client.sendToGC(APPID, MSG_CLIENT_HELLO, {}, hello);
    setTimeout(() => {
      clearInterval(beat);
      if (!ready) { client.removeListener('receivedFromGC', onMsg); resolve(false); }
    }, timeoutMs);
  });
}

async function finish(code) {
  try { await db.end(); } catch { /* уже закрыт */ }
  try { client.logOff(); } catch { /* уже отключены */ }
  process.exit(code);
}

client.on('loggedOn', async () => {
  const ready = await waitForGC();
  if (!ready) {
    console.error('GC не прислал ClientWelcome — сессия не поднялась');
    await finish(1);
    return;
  }

  const { rows } = await db.query(PICK_SQL, [PER_RUN]);
  if (rows.length === 0) {
    console.log('нечего спрашивать: у всех известных матчей соль уже есть');
    await finish(0);
    return;
  }

  let saved = 0, silent = 0, noSalt = 0;
  for (const row of rows) {
    const id = String(row.match_id);
    const res = await requestDetails(id);
    if (!res) {
      silent++;
      if (silent >= SILENCE_LIMIT) {
        // Штатный конец: бюджет на сегодня исчерпан. Не ошибка, не алерт.
        console.log(`GC замолчал после ${saved} солей — бюджет на сегодня`);
        break;
      }
      continue;
    }
    // Ответ пришёл — значит GC жив, и накопленное молчание к делу не
    // относится. Считать его сквозь ответы значило бы оборвать порцию на
    // двух РАЗРОЗНЕННЫХ таймаутах посреди исправной работы.
    silent = 0;
    const m = res.result === 1 ? res.match : null;
    // Сравнение с null, а не проверка на истинность: соль 0 хоть и
    // невероятна, но допустима, а `!0` выбросил бы её как отсутствующую.
    if (!m || m.replay_salt == null) { noSalt++; }
    else {
      await db.query(SAVE_SQL, [m.match_id, m.cluster ?? 0, m.replay_salt]);
      saved++;
    }
    // Пауза после ЛЮБОГО ответа, а не только после удачного: единицу
    // бюджета съедает сам запрос, и матч без соли стоит столько же.
    await sleep(DELAY_MS);
  }

  console.log(`соли: сохранено ${saved}, без соли ${noSalt}, молчание ${silent}`);
  await finish(0);
});

client.on('error', async (err) => {
  const name = SteamUser.EResult[err.eresult] || 'неизвестно';
  console.error(`Steam отказал: ${name} (eresult=${err.eresult})`);
  await finish(1);
});

try {
  await db.connect();
} catch (err) {
  console.error(`ОШИБКА: не подключиться к Postgres: ${err.message}`);
  process.exit(2);
}
client.logOn({ refreshToken: token });
