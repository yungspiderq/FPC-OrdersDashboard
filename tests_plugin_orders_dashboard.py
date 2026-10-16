"""
Функциональные тесты плагина orders_dashboard.py на моках.
Запуск: cd FPC && python3 tests_plugin_orders_dashboard.py
"""
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, ".")
sys.path.append("plugins")

from FunPayAPI import types as fp_types
from FunPayAPI.common import enums as fp_enums
from FunPayAPI.common.enums import OrderStatuses

# ───────────────────────────── загрузка плагина ─────────────────────────────
spec = importlib.util.spec_from_file_location("plugins.orders_dashboard", "plugins/orders_dashboard.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules["plugins.orders_dashboard"] = plugin
spec.loader.exec_module(plugin)

plugin._auto_remind_loop = lambda crd: None  # поток тестируем отдельно через _auto_remind_tick

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("✓ " if cond else "✗ FAIL: ") + name)


def reset_settings(**kwargs):
    os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
    base = json.loads(json.dumps(plugin.DEFAULT_SETTINGS))
    base.update(kwargs)
    with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(base, f, ensure_ascii=False)


def reset_cache():
    plugin._orders_cache["ts"] = 0.0
    plugin._orders_cache["orders"] = []


def mk_order(id_, desc="Аккаунт Steam", buyer="buyer1", hours_ago=20.0,
             status=OrderStatuses.PAID, chat_id=555, price=100.0):
    date = datetime.now(plugin.MSK).replace(tzinfo=None) - timedelta(hours=hours_ago)
    return fp_types.OrderShortcut(id_, desc, price, fp_enums.Currency.RUB, buyer, 777,
                                  chat_id, status, date, "Аккаунты", None, "",
                                  dont_search_amount=True)


# ───────────────────────────── моки ─────────────────────────────
class MockAccount:
    def __init__(self):
        self.is_initiated = True
        self.default_orders = []
        self.pages = {}          # {start_from: (next_id, orders)} для теста пагинации
        self.sent_messages = []  # (chat_id, text)
        self.refunds = []
        self.get_sales_calls = 0

    def get_sales(self, start_from=None, **kw):
        self.get_sales_calls += 1
        if start_from in self.pages:
            return self.pages[start_from]
        return None, self.default_orders, "ru", {}

    def send_message(self, chat_id, text=None, **kw):
        self.sent_messages.append((chat_id, text))
        return SimpleNamespace(id=1)

    def refund(self, order_id):
        self.refunds.append(order_id)
        return "ok"


class MockBot:
    def __init__(self):
        self.answers = []
        self.edits = []
        self.sent = []      # (chat_id, text)
        self.replies = []

    def answer_callback_query(self, cb_id, text=None, show_alert=False):
        self.answers.append((cb_id, text, show_alert))

    def edit_message_text(self, text, chat_id, message_id, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))
        return SimpleNamespace(id=message_id)

    def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))
        return SimpleNamespace(id=len(self.sent) + 100, chat=SimpleNamespace(id=chat_id))

    def reply_to(self, m, text, parse_mode=None):
        self.replies.append(text)

    def delete_message(self, chat_id, message_id):
        pass


def kb_buttons(kb) -> str:
    if kb is None:
        return ""
    try:
        return " | ".join(b.text for row in kb.keyboard for b in row if b.text)
    except Exception:
        return str(kb)


class MockTelegram:
    def __init__(self):
        self.bot = MockBot()
        self.cbq_handlers = []
        self.msg_handlers = []
        self.user_states = {}
        self.notification_settings = {}
        self.notifications = []

    def cbq_handler(self, handler, func, **kwargs):
        self.cbq_handlers.append((handler, func))

    def msg_handler(self, handler, **kwargs):
        self.msg_handlers.append((handler, kwargs))

    def set_state(self, chat_id, message_id, user_id, state, data=None):
        self.user_states[(chat_id, user_id)] = {"state": state, "mid": message_id, "data": data or {}}

    def get_state(self, chat_id, user_id):
        return self.user_states.get((chat_id, user_id))

    def check_state(self, chat_id, user_id, state):
        return self.user_states.get((chat_id, user_id), {}).get("state") == state

    def clear_state(self, chat_id, user_id, del_msg=False):
        st = self.user_states.pop((chat_id, user_id), None)
        return st.get("mid") if st else None

    def is_notification_enabled(self, chat_id, notification_type):
        try:
            return bool(self.notification_settings[str(chat_id)][notification_type])
        except KeyError:
            return False

    def send_notification(self, text, notification_type=None, **kwargs):
        self.notifications.append((text, notification_type))
        for chat_id in list(self.notification_settings):
            if notification_type is not None and not self.is_notification_enabled(chat_id, notification_type):
                continue
            self.bot.send_message(int(chat_id), text)


class MockCardinal:
    def __init__(self, account, telegram):
        self.account = account
        self.telegram = telegram
        self.plugins = {plugin.UUID: SimpleNamespace(enabled=True)}
        self.registered_commands = None

    def add_telegram_commands(self, uuid, commands):
        self.registered_commands = commands


def make_crd(with_tg=True):
    acc = MockAccount()
    tg = MockTelegram() if with_tg else None
    return MockCardinal(acc, tg), acc, tg


# ═══════════════════════════ ТЕСТ 1: метаданные и утилиты ═══════════════════════════
from uuid import UUID as _UUID
check("1.1 метаданные плагина", all(hasattr(plugin, f) for f in
      ["NAME", "VERSION", "DESCRIPTION", "CREDITS", "SETTINGS_PAGE", "UUID", "BIND_TO_DELETE"]))
check("1.2 UUID валиден", str(_UUID(plugin.UUID, version=4)) == plugin.UUID)
check("1.3 BIND_TO_POST_DELIVERY привязан", plugin.BIND_TO_POST_DELIVERY == [plugin.delivery_recorder])

o = mk_order("AAAA1111", desc="Аккаунт Steam Premium", buyer="ivan")
txt = plugin._fill_template("Привет $username! Заказ #$order, лот $lot", o)
check("1.4 шаблон: подстановка $username/$order/$lot",
      txt == "Привет ivan! Заказ #AAAA1111, лот Аккаунт Steam Premium")
check("1.5 _human_delta", plugin._human_delta(3600 * 25) == "1 дн. 1 ч."
      and plugin._human_delta(3600 * 5 + 60 * 23) == "5 ч. 23 мин."
      and plugin._human_delta(125) == "2 мин." and plugin._human_delta(-5) == "0 сек.")
ts = plugin._order_date_ts(o)
check("1.6 _order_date_ts: ~20 часов назад (МСК)", ts is not None and abs(time.time() - ts - 20 * 3600) < 120)
check("1.7 исключения по подстрокам",
      plugin._is_excluded(mk_order("X", desc="Аренда аккаунта 7 дней"), {"exclude": ["аренда"]})
      and not plugin._is_excluded(o, {"exclude": ["аренда"]}))

# ═══════════════════════════ ТЕСТ 2: авто-напоминания (тик) ═══════════════════════════
crd, acc, tg = make_crd(with_tg=True)

# 2.1 заказ оплачен 20ч назад, задержка 12ч → напоминание
acc.default_orders = [mk_order("AAAA1111", hours_ago=20)]
reset_settings(delay_hours=12, max_reminders=1)
tg.notifications.clear()
reminded = plugin._auto_remind_tick(crd)
check("2.1 напоминание отправлено в чат заказа",
      reminded == ["AAAA1111"] and acc.sent_messages
      and acc.sent_messages[0][0] == 555 and "AAAA1111" in acc.sent_messages[0][1])
s = plugin._load_settings()
check("2.2 состояние sent сохранено", s["sent"].get("AAAA1111", {}).get("n") == 1)
check("2.3 владельцу отправлено уведомление 💬", any("💬" in t for t, _ in tg.notifications))

# 2.4 повторно не отправляем (max_reminders=1)
acc.sent_messages.clear()
reminded = plugin._auto_remind_tick(crd)
check("2.4 повторное напоминание не отправляется (лимит 1)", reminded == [] and acc.sent_messages == [])

# 2.5 свежий заказ (2ч) — рано
acc.default_orders = [mk_order("BBBB2222", hours_ago=2)]
acc.sent_messages.clear()
reminded = plugin._auto_remind_tick(crd)
check("2.5 свежий заказ не трогается", reminded == [] and acc.sent_messages == [])

# 2.6 таймер от выдачи: заказ оплачен 20ч назад, но автовыдача была 5ч назад
acc.default_orders = [mk_order("CCCC3333", hours_ago=20)]
s = plugin._load_settings()
s["deliveries"]["CCCC3333"] = time.time() - 5 * 3600
plugin._save_settings(s)
acc.sent_messages.clear()
reminded = plugin._auto_remind_tick(crd)
check("2.6 таймер считается от момента выдачи (5ч < 12ч — рано)",
      reminded == [] and acc.sent_messages == [])

# 2.7 исключение по названию лота
acc.default_orders = [mk_order("DDDD4444", desc="Аренда аккаунта Steam", hours_ago=20)]
reset_settings(delay_hours=12, exclude=["аренда"])
acc.sent_messages.clear()
reminded = plugin._auto_remind_tick(crd)
check("2.7 лот-исключение (аренда) пропускается", reminded == [] and acc.sent_messages == [])

# 2.8 авто-напоминания выключены
acc.default_orders = [mk_order("EEEE5555", hours_ago=20)]
reset_settings(delay_hours=12, auto_remind=False)
acc.sent_messages.clear()
reminded = plugin._auto_remind_tick(crd)
check("2.8 при выключенных авто-напоминаниях ничего не отправляется",
      reminded == [] and acc.sent_messages == [])

# 2.9 дубликаты заказа (пагинация) не вызывают двойную отправку
reset_settings(delay_hours=12, max_reminders=5)
dup = mk_order("FFFF6666", hours_ago=20)
acc.default_orders = [dup, dup]
acc.sent_messages.clear()
reminded = plugin._auto_remind_tick(crd)
check("2.9 дубликаты заказа обрабатываются один раз",
      reminded == ["FFFF6666"] and len(acc.sent_messages) == 1)

# 2.10 пагинация: get_sales со start_from
acc.pages = {None: ("NEXT01", [mk_order("P1P1P1P1", hours_ago=20)]),
             "NEXT01": (None, [mk_order("P2P2P2P2", hours_ago=30)])}
acc.default_orders = []
acc.sent_messages.clear()
reminded = plugin._auto_remind_tick(crd)
check("2.10 пагинация get_sales: напоминания по обеим страницам",
      sorted(reminded) == ["P1P1P1P1", "P2P2P2P2"])
acc.pages = {}

# 2.11 чистка устаревших записей
reset_settings(delay_hours=12)
s = plugin._load_settings()
s["sent"] = {"OLD00000": {"n": 1, "ts": time.time() - 40 * 86400}}
s["deliveries"] = {"OLD00000": time.time() - 40 * 86400}
plugin._save_settings(s)
acc.default_orders = []
plugin._auto_remind_tick(crd)
s = plugin._load_settings()
check("2.11 записи о старых неактивных заказах удаляются",
      "OLD00000" not in s["sent"] and "OLD00000" not in s["deliveries"])

# ═══════════════════════════ ТЕСТ 3: рекордер выдачи ═══════════════════════════
reset_settings()
e = SimpleNamespace(order=SimpleNamespace(id="DELV0001"))
plugin.delivery_recorder(crd, e)
check("3.1 POST_DELIVERY фиксирует время выдачи",
      "DELV0001" in plugin._load_settings()["deliveries"])
plugin.delivery_recorder(crd, SimpleNamespace())  # без order — не падает
check("3.2 событие без заказа не ломает рекордер", True)

# ═══════════════════════════ ТЕСТ 4: Telegram UI ═══════════════════════════
crd, acc, tg = make_crd(with_tg=True)
plugin.init(crd)
bot = tg.bot
reset_settings(delay_hours=12)
reset_cache()

check("4.1 команда /orders зарегистрирована",
      any(kw.get("commands") == ["orders"] for _, kw in tg.msg_handlers))
check("4.2 add_telegram_commands вызван", crd.registered_commands
      and crd.registered_commands[0][0] == "orders")

acc.default_orders = [mk_order("AAAA1111", hours_ago=20), mk_order("BBBB2222", hours_ago=2, price=250.0)]


def dispatch_cb(data, chat_id=111, msg_id=555, user_id=222):
    c = SimpleNamespace(data=data, id="cb1",
                        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), id=msg_id),
                        from_user=SimpleNamespace(id=user_id))
    for handler, func in tg.cbq_handlers:
        try:
            matched = func(c)
        except Exception:
            matched = False
        if matched:
            handler(c)
            return handler.__name__
    return None


def dispatch_msg(text, chat_id=111, user_id=222):
    m = SimpleNamespace(chat=SimpleNamespace(id=chat_id), from_user=SimpleNamespace(id=user_id),
                        text=text)
    for h, kw in tg.msg_handlers:
        f = kw.get("func")
        try:
            if f and f(m):
                h(m)
                return h.__name__
        except Exception:
            pass
    return None


def last_text():
    if bot.edits:
        return bot.edits[-1][0]
    return bot.sent[-1][1] if bot.sent else ""


def last_kb():
    return kb_buttons(bot.edits[-1][1]) if bot.edits else ""


name = dispatch_cb(f"47:{plugin.UUID}:0")
check("4.3 страница настроек плагина открывает дашборд",
      name in ("open_home", "<lambda>") and "Дашборд заказов" in last_text()
      and "Активных заказов" in last_text())

dispatch_cb(f"{plugin.CB_LIST}:0")
check("4.4 список заказов: оба заказа, старые сверху",
      "AAAA1111" in last_kb() and "BBBB2222" in last_kb()
      and last_kb().index("AAAA1111") < last_kb().index("BBBB2222"))
check("4.5 в кнопке заказа: цена и возраст", "100₽" in last_kb() and "20 ч." in last_kb())

name = dispatch_cb(f"{plugin.CB_ORDER}:AAAA1111:0")
check("4.6 карточка заказа: покупатель, цена, ссылка на профиль",
      name == "open_order" and "buyer1" in last_text() and "funpay.com/users/777" in last_text()
      and "100 ₽" in last_text())
check("4.7 кнопки карточки: напомнить/возврат/ссылка",
      "💬 Напомнить сейчас" in last_kb() and "↩️ Возврат средств" in last_kb())

dispatch_cb(f"{plugin.CB_ORDER}:NOTEXIST:0")
check("4.8 несуществующий заказ: вежливая заглушка", "не найден" in last_text())

# ручное напоминание
acc.sent_messages.clear()
tg.notifications.clear()
name = dispatch_cb(f"{plugin.CB_REMIND}:AAAA1111:0")
time.sleep(0.5)
check("4.9 ручное напоминание отправлено в чат заказа",
      name == "remind_now" and len(acc.sent_messages) == 1 and acc.sent_messages[0][0] == 555)
check("4.10 ручное напоминание учтено в sent",
      plugin._load_settings()["sent"].get("AAAA1111", {}).get("n") == 1)

# возврат средств
name = dispatch_cb(f"{plugin.CB_REFASK}:AAAA1111:0")
check("4.11 запрос подтверждения возврата", name == "ask_refund" and "Оформить возврат" in last_text())
name = dispatch_cb(f"{plugin.CB_REFYES}:AAAA1111:0")
time.sleep(0.5)
check("4.12 возврат оформлен через account.refund", name == "do_refund" and acc.refunds == ["AAAA1111"])

# авто-напоминания вкл/выкл
name = dispatch_cb(plugin.CB_ARTOG)
check("4.13 авто-напоминания переключаются", name == "toggle_auto"
      and plugin._load_settings()["auto_remind"] is False)
dispatch_cb(plugin.CB_ARTOG)
check("4.14 и переключаются обратно", plugin._load_settings()["auto_remind"] is True)

# настройки через состояния
dispatch_cb(plugin.CB_DELAY)
dispatch_msg("24")
check("4.15 задержка сохранена (24ч)", plugin._load_settings()["delay_hours"] == 24)
dispatch_cb(plugin.CB_DELAY)
dispatch_msg("9999")
check("4.16 задержка зажимается (9999 → 168)", plugin._load_settings()["delay_hours"] == 168)
dispatch_cb(plugin.CB_MAXREM)
dispatch_msg("3")
check("4.17 макс. напоминаний сохранен", plugin._load_settings()["max_reminders"] == 3)
dispatch_cb(plugin.CB_INT)
dispatch_msg("600")
check("4.18 интервал проверки сохранен", plugin._load_settings()["check_interval"] == 600)
dispatch_cb(plugin.CB_TMPL)
dispatch_msg("Привет, $username! Подтверди заказ #$order")
check("4.19 шаблон напоминания сохранен",
      plugin._load_settings()["template"] == "Привет, $username! Подтверди заказ #$order")
dispatch_cb(plugin.CB_EXCL)
dispatch_msg("аренда\nArenda")
check("4.20 исключения сохранены (2 шт.)", plugin._load_settings()["exclude"] == ["аренда", "Arenda"])
dispatch_cb(plugin.CB_EXCL)
dispatch_msg("-")
check("4.21 «-» очищает исключения", plugin._load_settings()["exclude"] == [])
dispatch_cb(plugin.CB_DELAY)
dispatch_msg("abc")
check("4.22 нечисловой ввод отклоняется", plugin._load_settings()["delay_hours"] == 168
      and any("❌" in r for r in bot.replies[-1:]))

# обновление списка
dispatch_cb(plugin.CB_REFRESH)
check("4.23 кнопка «Обновить» возвращает главное меню со счетчиком",
      "Активных заказов" in last_text() and "(2)" in last_kb())
dispatch_cb(plugin.CB_NOOP)
check("4.24 noop отвечает", True)

# ═══════════════════════════ ТЕСТ 5: нормализация настроек ═══════════════════════════
os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
    json.dump({"auto_remind": 1, "delay_hours": "abc", "max_reminders": None,
               "check_interval": 5, "template": "  ", "exclude": "строка",
               "sent": 42, "deliveries": None}, f, ensure_ascii=False)
s = plugin._load_settings()
check("5.1 битые значения нормализуются к дефолтам",
      s["auto_remind"] is True and s["delay_hours"] == 12 and s["max_reminders"] == 1
      and s["check_interval"] == 60 and s["template"] == plugin.DEFAULT_TEMPLATE
      and s["exclude"] == [] and s["sent"] == {} and s["deliveries"] == {})
with open(plugin.SETTINGS_PATH, "w") as f:
    f.write("{broken")
s = plugin._load_settings()
check("5.2 битый JSON → дефолты", s["delay_hours"] == 12 and s["sent"] == {})
reset_settings()
reset_cache()

# ═══════════════════════════ ИТОГ ═══════════════════════════
print(f"\n{'=' * 60}\nПройдено: {len(PASS)}  Провалено: {len(FAIL)}")
if FAIL:
    print("Проваленные тесты:")
    for f_ in FAIL:
        print("  ✗", f_)
    sys.exit(1)
print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ ✅")
