// Добыть refresh-токен Steam и положить его на диск для питоновского замера.
//
// ЗАЧЕМ ЭТО ВООБЩЕ. Соль реплея отдаёт только Game Coordinator, работать
// с ним умеет питоновская библиотека dota2 — а войти в Steam она больше
// не может: steam==1.4.4 шлёт пароль открытым полем в устаревшем
// ClientLogon, и Valve отвечает InvalidPassword при любом верном пароле.
//
// Переписывать весь замер на Node значило бы выбросить рабочий код:
// режимы details/bulk, сквозную проверку соли на CDN, чтение очереди
// кандидатов. Поэтому разделение труда: Node делает ровно то, чего не
// умеет Python — обменивает пароль на refresh-токен через новый механизм
// CAuthentication. Дальше Python входит этим токеном, потому что поле
// access_token в CMsgClientLogon у него есть, и всё остальное работает.
//
// Токен живёт месяцами, так что эта команда нужна редко.
//
// БЕЗОПАСНОСТЬ. Токен — это полноценный доступ к аккаунту, не слабее
// пароля. Поэтому он НЕ печатается ни в каком виде, пишется в файл с
// правами 0600 в каталоге 0700, и каталог этот лежит вне репозитория.
// В вывод идут только срок годности и steamID.
//
// ЗАПУСК:
//     make gc-token
import SteamUser from 'steam-user';
import { createInterface } from 'node:readline/promises';
import { stdin, stdout } from 'node:process';
import { writeFileSync, mkdirSync, chmodSync } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';

const login = process.env.STEAM_BOT_LOGIN;
const password = process.env.STEAM_BOT_PASSWORD;

if (!login || !password) {
  console.error('ОШИБКА: нет STEAM_BOT_LOGIN / STEAM_BOT_PASSWORD ' +
                'в ~/manta-train.env');
  process.exit(2);
}

const stateDir = process.env.GC_STATE_DIR || join(homedir(), '.manta-gc');
const tokenPath = join(stateDir, 'refresh-token');

const client = new SteamUser({ dataDirectory: stateDir });
let saved = false;

client.on('steamGuard', async (domain, callback, lastCodeWrong) => {
  if (lastCodeWrong) console.log('код не подошёл, попробуем ещё раз');
  const where = domain ? `на почту (${domain})` : 'в мобильном приложении';
  const rl = createInterface({ input: stdin, output: stdout });
  const code = await rl.question(`Steam Guard — код ${where}: `);
  rl.close();
  callback(code.trim());
});

// Токен приезжает ОТДЕЛЬНЫМ событием, раньше `loggedOn`. Ловим здесь, а
// не после входа: событие одноразовое, и пропустить его значит остаться
// без единственного, ради чего эта команда написана.
client.on('refreshToken', (token) => {
  mkdirSync(stateDir, { recursive: true, mode: 0o700 });
  chmodSync(stateDir, 0o700);
  writeFileSync(tokenPath, token + '\n', { mode: 0o600 });
  chmodSync(tokenPath, 0o600);
  saved = true;

  // Срок годности читаем из самого токена: это JWT, и его полезная
  // нагрузка не зашифрована. Подпись не проверяем — она Valve, не наша.
  let expiry = 'неизвестен';
  try {
    const payload = JSON.parse(
      Buffer.from(token.split('.')[1], 'base64url').toString('utf8'));
    if (payload.exp) {
      expiry = new Date(payload.exp * 1000).toISOString().slice(0, 10);
    }
  } catch {
    // Формат мог измениться — на работоспособность токена это не влияет.
  }
  console.log(`\nтокен сохранён: ${tokenPath}`);
  console.log(`  права: 0600, годен до: ${expiry}`);
  console.log('  сам токен не печатается: он равносилен паролю');
});

client.on('loggedOn', () => {
  if (!saved) {
    console.error('\nОШИБКА: вход прошёл, но токен не приехал.');
    console.error('Без него питоновский замер войти не сможет.');
    client.logOff();
    process.exit(1);
  }
  console.log('\n=== ГОТОВО ===');
  console.log('Дальше — замер как обычно, он возьмёт токен сам:');
  console.log('  make gc-probe ARGS=login');
  client.logOff();
  process.exit(0);
});

client.on('error', (err) => {
  const name = SteamUser.EResult[err.eresult] || 'неизвестно';
  console.error(`\nSteam отказал: ${name} (eresult=${err.eresult})`);
  process.exit(1);
});

console.log(`логин ${login.slice(0, 2)}*** …`);
client.logOn({ accountName: login, password });
