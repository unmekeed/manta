// Проверка входа в Steam ПО СОХРАНЁННОМУ ТОКЕНУ — на Node (спринт 166).
//
// ЗАЧЕМ. Питоновский замер до Steam доходит, но ни один сервер входа его
// не принимает: пять разных CM подряд отвечают TryAnotherCM. Библиотека
// `steam==1.4.4` последняя на PyPI, вышла в 2022-м и шлёт ClientLogon с
// версией клиента 2019 года; websocket в ней нет ни в релизе, ни на
// master. Похоже, Valve этот путь больше не принимает — но ПОХОЖЕ не
// значит ЗНАЕМ, а решение «переписывать замер на Node» слишком дорогое,
// чтобы принимать его по догадке.
//
// Этот скрипт и есть недостающее измерение: тот же аккаунт, тот же
// токен, та же машина, другая библиотека. Войдёт — значит дело в
// питоновской библиотеке, и путь понятен. Не войдёт — значит дело в
// аккаунте или токене, и переписывание ничего бы не дало.
//
// Пароль здесь не нужен и не читается: вход идёт refresh-токеном, тем
// самым файлом, который положил get-token.mjs. Это важно отдельно —
// пароль со спринта 157 с машины стёрт намеренно, и проверка,
// требующая его вернуть, была бы проверкой ценой ослабления защиты.
// Существующий login-check.mjs именно этого и требует, поэтому здесь
// отдельный скрипт, а не флаг к нему.
//
// ЗАПУСК:
//     make gc-token-login
import SteamUser from 'steam-user';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';

// Тот же каталог, что у get-token.mjs: токен пишет он, читаем мы.
// Разъедься эти два пути — скрипт честно сообщил бы «токена нет» там,
// где токен есть, и мы искали бы беду в Steam.
const stateDir = process.env.GC_STATE_DIR || join(homedir(), '.manta-gc');
const tokenPath = join(stateDir, 'refresh-token');

let token;
try {
  token = readFileSync(tokenPath, 'utf8').trim();
} catch {
  console.error(`ОШИБКА: нет токена в ${tokenPath}`);
  console.error('Получить: make gc-token');
  process.exit(2);
}
if (!token) {
  console.error(`ОШИБКА: файл ${tokenPath} пуст`);
  process.exit(2);
}

// Срок годности — из самого токена: это JWT, полезная нагрузка открыта.
// Просроченный токен даёт отказ, неотличимый от «Valve не пускает», а
// это ровно та развилка, ради которой скрипт написан.
try {
  const payload = JSON.parse(
    Buffer.from(token.split('.')[1], 'base64url').toString('utf8'));
  if (payload.exp) {
    const days = Math.round((payload.exp * 1000 - Date.now()) / 86400000);
    console.log(`токен годен ещё ${days} дн.`);
  }
} catch {
  console.log('срок годности токена прочитать не удалось (не беда)');
}

const client = new SteamUser({ dataDirectory: stateDir });
let done = false;

// Выход по таймеру — не перестраховка. Первая версия login-check.mjs
// печатала вердикт только внутри необязательного события и висела, когда
// оно не приходило. Молчаливо висящая проверка хуже отсутствующей: её
// снимают с клавиатуры и не узнают ответа.
const waitMs = Number(process.env.GC_LOGIN_TIMEOUT_MS || 60000);
const timer = setTimeout(() => {
  if (done) return;
  console.error(`\nОШИБКА: Steam не ответил за ${Math.round(waitMs / 1000)} с.`);
  process.exit(1);
}, waitMs);

const finish = (code) => {
  if (done) return;
  done = true;
  clearTimeout(timer);
  try { client.logOff(); } catch { /* уже отключены */ }
  process.exit(code);
};

client.on('loggedOn', (details) => {
  console.log('\n=== NODE ВОШЁЛ ===');
  console.log(`steamID: ${client.steamID ? client.steamID.getSteamID64() : '?'}`);
  console.log(`сервер:  ${client.publicIP || '?'} / cell ${details?.cell_id ?? '?'}`);
  console.log('\nЗначит вход по этому токену работает, и питоновский');
  console.log('TryAnotherCM — свойство библиотеки, а не аккаунта.');
  finish(0);
});

client.on('error', (err) => {
  const name = SteamUser.EResult[err.eresult] || 'неизвестно';
  console.error(`\nSteam отказал: ${name} (eresult=${err.eresult})`);
  if (err.eresult === SteamUser.EResult.AccessDenied ||
      err.eresult === SteamUser.EResult.InvalidPassword) {
    console.error('При входе ТОКЕНОМ это значит, что он отозван или');
    console.error('просрочен. Обновить: make gc-token');
  }
  finish(1);
});

console.log('вход по токену через steam-user …');
client.logOn({ refreshToken: token });
