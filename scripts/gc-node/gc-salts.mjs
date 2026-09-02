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

// -- учёт бюджета (спринт 173) -------------------------------------------------
//
// Считаются ЗАПРОСЫ, а не добытые соли. Единицу бюджета съедает сам
// вопрос: матч, у которого соли не оказалось, стоил столько же, сколько
// удачный, и учёт по успехам показывал бы расход меньше настоящего —
// ошибка в ту самую сторону, в какую ошибаться нельзя.
//
// Таблица та же, что у OpenDota (ApiBudget, миграция 011): у неё уже есть
// колонка api, заведённая под это («на будущее: stratz, steam»). Все её
// запросы фильтруют по api, поэтому наши строки чужой счёт не трогают.
const BUDGET_API = 'steam-gc';
const BUDGET_SOURCE = 'gc-salts';

const USED_SQL = `
  SELECT coalesce(sum(calls), 0)::int AS used FROM ApiBudget
  WHERE day = (NOW() AT TIME ZONE 'UTC')::date AND api = $1`;

const SPEND_SQL = `
  INSERT INTO ApiBudget (day, api, source, calls)
  VALUES ((NOW() AT TIME ZONE 'UTC')::date, $1, $2, $3)
  ON CONFLICT (day, api, source) DO UPDATE
    SET calls = ApiBudget.calls + EXCLUDED.calls`;

// Потолок за сутки. Замеры дали 200–400; берём верх диапазона — это не
// цель, а предохранитель от прогона, который в отказ упирается не сразу
// (накопитель после долгого простоя отдаёт сотнями). День — UTC, как у
// OpenDota: разные границы суток в одной таблице пришлось бы каждый раз
// вспоминать.
const DAILY_MAX = Number(process.env.GC_SALTS_DAILY_MAX || 400);

function dsn() {
  const url = process.env.POSTGRES_DSN;
  if (url) return { connectionString: url };
  return {
    // 127.0.0.1, а НЕ localhost. Node с 17-й версии резолвит имена
    // «как отдал резолвер» и для localhost получает сначала ::1, а
    // Postgres опубликован на 127.0.0.1 (overlay docker-compose.vps.yml
    // держит порты на IPv4-петле). Живой прогон 2026-09-02:
    // `connect ECONNREFUSED ::1:5432` при работающей базе.
    //
    // Наполнитель — первый процесс проекта, ходящий в базу С ХОСТА по
    // TCP: коллекторы живут в контейнерах и ходят по имени postgres, а
    // pg-migrate.sh — через docker exec. Поэтому раньше не всплывало.
    host: process.env.POSTGRES_HOST || '127.0.0.1',
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
  // ОБЯЗАТЕЛЬНО перед любым разговором с GC: объявить, что запустили
  // Dota 2. Game Coordinator — часть игры, а не Steam; клиенту, который
  // не «играет», он сессию не поднимает и ClientWelcome не шлёт, сколько
  // ему ни здоровайся.
  //
  // Живой прогон 2026-09-02: наполнитель входил в Steam и молча ждал
  // сорок пять секунд, потому что при переносе кода из замера эта одна
  // строка не переехала. Отказ выглядел как «Valve не пускает».
  client.gamesPlayed([APPID]);

  const ready = await waitForGC();
  if (!ready) {
    console.error('GC не прислал ClientWelcome — сессия не поднялась.');
    console.error('Аккаунт входит в Steam, но в Dota 2 его не пускают: '
                  + 'проверьте, что игра есть в библиотеке и аккаунт не ограничен.');
    await finish(1);
    return;
  }

  // Сколько бюджета уже израсходовано сегодня. Порция урезается до
  // остатка, а не отменяется целиком: обрубить последние три запроса
  // ради круглого числа значило бы выбросить три соли даром.
  const used = (await db.query(USED_SQL, [BUDGET_API])).rows[0].used;
  const left = DAILY_MAX - used;
  if (left <= 0) {
    // Штатный конец суток, не ошибка: ровно то же, что и молчание GC.
    console.log(`суточный потолок выбран: ${used} из ${DAILY_MAX}`);
    await finish(0);
    return;
  }

  const { rows } = await db.query(PICK_SQL, [Math.min(PER_RUN, left)]);
  if (rows.length === 0) {
    console.log('нечего спрашивать: у всех известных матчей соль уже есть');
    await finish(0);
    return;
  }

  let saved = 0, silent = 0, noSalt = 0, asked = 0;
  for (const row of rows) {
    const id = String(row.match_id);
    // Считаем ДО ответа: единицу бюджета съедает отправленный вопрос, и
    // молчание в ответ — тоже расход. Учёт по ответам показывал бы
    // расход меньше настоящего.
    asked++;
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

  // Расход записывается ВСЕГДА, даже когда порция ничего не добыла:
  // запросы были, бюджет потрачен. Записывать только удачные прогоны
  // значило бы вести учёт, который занижает расход тем сильнее, чем хуже
  // идут дела, — то есть врёт именно тогда, когда нужен.
  if (asked > 0) {
    await db.query(SPEND_SQL, [BUDGET_API, BUDGET_SOURCE, asked]);
  }
  console.log(`соли: спрошено ${asked}, сохранено ${saved}, `
              + `без соли ${noSalt}, молчание ${silent}; `
              + `за сутки ${used + asked} из ${DAILY_MAX}`);
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
  // Куда ходили — обязательная часть диагноза. «ECONNREFUSED» без адреса
  // не отличает «база лежит» от «стучимся не туда», а живой случай был
  // как раз вторым (::1 вместо 127.0.0.1). Пароль сюда не попадает.
  const cfg = dsn();
  const where = cfg.connectionString
    ? 'POSTGRES_DSN'
    : `${cfg.host}:${cfg.port}/${cfg.database} от имени ${cfg.user}`;
  console.error(`ОШИБКА: не подключиться к Postgres (${where}): ${err.message}`);
  console.error('адрес задаётся POSTGRES_DSN или POSTGRES_HOST/PORT/DB/USER '
                + `в ${process.env.MANTA_TRAIN_ENV || '~/manta-train.env'}`);
  process.exit(2);
}
client.logOn({ refreshToken: token });
