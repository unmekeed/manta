// Проверка одной гипотезы: пускает ли Steam НОВЫМ путём аутентификации.
//
// ЗАЧЕМ. `make gc-probe ARGS=login` отвечает «Invalid password» на верный
// пароль. Причина не в пароле: библиотека steam==1.4.4 (последняя на
// PyPI, и в master то же самое) логинится устаревшим сообщением
// ClientLogon, где пароль лежит открытым полем:
//
//     message = MsgProto(EMsg.ClientLogon)
//     message.body.password = password
//
// Valve переработала аутентификацию в марте 2023: теперь нужен обмен
// через CAuthentication — забрать RSA-ключ, зашифровать пароль, открыть
// сессию, дождаться refresh-токена и войти уже им. Старый путь отвечает
// InvalidPassword при любом верном пароле, поэтому в браузере вход
// работает, а из скрипта нет.
//
// ЧТО ЭТОТ СКРИПТ ДЕЛАЕТ. Ровно один вход и ничего больше. Не трогает
// Game Coordinator, не читает очередь кандидатов, не пишет в базы, не
// расходует квоту OpenDota.
//
// ПОЧЕМУ БЕЗ GC. Чтобы проверка осталась РАЗЛИЧАЮЩЕЙ. Пакет dota2 для
// Node не обновлялся с 2022 года, и если тянуть его сюда, то при отказе
// будет непонятно, что сломалось — вход или устаревшие протоколы GC.
// Сначала отвечаем на вопрос про вход, потом на все остальные.
//
// ЗАПУСК:
//     make gc-node          # один раз, ставит steam-user вне репозитория
//     make gc-login-check
import SteamUser from 'steam-user';
import { createInterface } from 'node:readline/promises';
import { stdin, stdout } from 'node:process';

const login = process.env.STEAM_BOT_LOGIN;
const password = process.env.STEAM_BOT_PASSWORD;

if (!login || !password) {
  console.error('ОШИБКА: нет STEAM_BOT_LOGIN / STEAM_BOT_PASSWORD ' +
                'в ~/manta-train.env');
  process.exit(2);
}

// Имя печатаем двумя символами — как это делает gc-probe.py. Пароль не
// печатается никогда и ни в каком виде.
console.log(`логин ${login.slice(0, 2)}*** …`);
console.log(`длина пароля: ${password.length} символов ` +
            '(сверь с настоящей: если не сходится, ~/manta-train.env ' +
            'читается шеллом и съел спецсимвол — лечится одинарными ' +
            'кавычками)');

// Каталог сессии — вне репозитория, рядом с venv питоновского замера.
// Машинный токен ложится сюда, и Steam Guard спросят один раз, а не
// каждый запуск.
const client = new SteamUser({
  dataDirectory: process.env.GC_STATE_DIR || null,
});

let guardAsked = false;
let limitations = null;

// Вердикт печатается ОДИН раз, кто бы до него ни добрался первым:
// событие с ограничениями аккаунта или таймер. Первая версия печатала
// его только внутри обработчика `accountLimitations` — а событие
// приходит не всегда. На живом аккаунте оно не пришло вовсе: вход прошёл
// успешно, ответ на главный вопрос был получен и НЕ показан, а скрипт
// висел, пока его не сняли с клавиатуры. Диагностика не должна зависеть
// от необязательного сообщения.
// Вызывается либо по событию с ограничениями, либо по таймеру — кто
// успеет первым. Ни флага «уже печатали», ни снятия таймера здесь нет
// намеренно: функция заканчивается process.exit, и второй вызов
// физически не состоится. И флаг, и clearTimeout были бы недостижимым
// кодом — тем самым «на всякий случай», который никогда не выполняется и
// потому никем не проверен. Однократность вывода стережёт тест, причём
// именно на опоздавшем событии: на раннем она выполняется сама собой.
function finish() {

  if (limitations) {
    console.log('\nаккаунт:');
    console.log(`  limited (нет $5 пополнения): ${limitations.limited ? 'да' : 'нет'}`);
    console.log(`  заблокирован в сообществе:   ${limitations.communityBanned ? 'да' : 'нет'}`);
    console.log(`  залочен:                     ${limitations.locked ? 'да' : 'нет'}`);
  } else {
    console.log('\nаккаунт: ограничения Steam не прислал за отведённое время.');
    console.log('  На главный вопрос это не влияет — вход уже состоялся.');
  }

  console.log('\n=== ВЕРДИКТ ===');
  console.log('Новый путь аутентификации РАБОТАЕТ.');
  console.log('Значит отказ у gc-probe.py — это устаревший ClientLogon');
  console.log('в библиотеке steam==1.4.4, а не пароль и не аккаунт.');
  if (limitations?.limited) {
    console.log('\nВНИМАНИЕ: аккаунт limited. Пустят ли такой в Game');
    console.log('Coordinator — отдельный вопрос, вход на него не отвечает.');
  }
  client.logOff();
  process.exit(0);
}

client.on('steamGuard', async (domain, callback, lastCodeWrong) => {
  guardAsked = true;
  if (lastCodeWrong) console.log('код не подошёл, попробуем ещё раз');
  const where = domain ? `на почту (${domain})` : 'в мобильном приложении';
  const rl = createInterface({ input: stdin, output: stdout });
  const code = await rl.question(`Steam Guard — код ${where}: `);
  rl.close();
  callback(code.trim());
});

client.on('loggedOn', () => {
  console.log('\nSteam: вошли.');
  console.log(`  steamID: ${client.steamID.getSteamID64()}`);
  if (guardAsked) {
    console.log('  Steam Guard спрашивали — дальше сессия сохранена, ' +
                'больше не спросят');
  }
  // Пять секунд на необязательное событие, дальше печатаем вердикт без
  // него. Вход уже состоялся — главный ответ у нас есть.
  setTimeout(finish, 5000);
});

// Ограничения аккаунта. Спрашиваем не из любопытства: замер спринта 136
// заводился в том числе ради вопроса «пускает ли limited-аккаунт в GC».
// Половину ответа отдаёт сам вход, и стоит она ноль запросов.
client.on('accountLimitations', (limited, communityBanned, locked) => {
  limitations = { limited, communityBanned, locked };
  finish();
});

client.on('error', (err) => {
  const name = SteamUser.EResult[err.eresult] || 'неизвестно';
  console.error(`\nSteam отказал: ${name} (eresult=${err.eresult})`);
  console.error('\n=== ВЕРДИКТ ===');
  if (name === 'InvalidPassword') {
    console.error('Новым путём ТОЖЕ InvalidPassword — дело не в протоколе.');
    console.error('Steam отвечает так и на неверный пароль, и на');
    console.error('несуществующее имя аккаунта, и различить их снаружи');
    console.error('нельзя. Проверь длину пароля выше и точное написание');
    console.error('логина — это имя ВХОДА, а не ник в профиле.');
  } else if (name === 'RateLimitExceeded') {
    console.error('Слишком много попыток входа — Steam включил паузу.');
    console.error('Подожди полчаса и повтори; предыдущие неудачные заходы');
    console.error('gc-probe.py тоже считаются.');
  } else {
    console.error('Вход не прошёл, но и не по паролю — см. код выше.');
  }
  process.exit(1);
});

client.logOn({ accountName: login, password });
