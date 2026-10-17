"""
Дашборд заказов и напоминания (OrdersDashboard) — плагин для FunPay Cardinal v0.1.17+

🗂 Дашборд активных заказов в Telegram + ⏳ автоматические напоминания покупателям
«подтвердите заказ».

Возможности:
 • Список активных (незакрытых) заказов с возрастом: кто давно не подтверждает — сверху.
 • Карточка заказа: лот, покупатель, цена, время оплаты, время выдачи (для автовыдачи),
   сколько напоминаний отправлено.
 • Кнопки в карточке: 💬 напомнить покупателю, ↩️ оформить возврат, 🔗 открыть заказ на FunPay.
 • ⏳ Авто-напоминания: если заказ оплачен (или товар выдан автовыдачей) больше
   N часов назад и покупатель не подтвердил заказ — плагин сам отправит вежливое
   напоминание в чат заказа. Шаблон текста настраивается, есть список исключений
   (например, лоты аренды).
 • Момент выдачи товара фиксируется по событию автовыдачи Кардинала — напоминание
   отправится через N часов именно ПОСЛЕ выдачи (для автовыдачи).
 • Состояние (отправленные напоминания, времена выдачи) хранится на диске и переживает рестарт.

Команды Telegram:
 • /orders — открыть дашборд.

Установка: положить этот файл в папку plugins/ и перезапустить Кардинал.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B, CallbackQuery, Message
from telebot.apihelper import ApiTelegramException

try:
    from tg_bot import CBT, static_keyboards as skb, utils as tg_utils
except Exception:  # загрузка вне Кардинала
    CBT = None
    skb = None
    tg_utils = None

if TYPE_CHECKING:
    from cardinal import Cardinal

from FunPayAPI.common.enums import OrderStatuses, Currency

logger = logging.getLogger("FPC.OrdersDashboard")

# ══════════════════════════════ МЕТАДАННЫЕ ПЛАГИНА ══════════════════════════════
NAME = "OrdersDashboard (Orders & Confirm Reminders)"
VERSION = "1.0.1"
DESCRIPTION = ("Дашборд активных заказов в Telegram (возраст, напомнить, возврат, ссылка) + "
               "автоматические напоминания покупателям «подтвердите заказ» через N часов "
               "после оплаты/выдачи. Команда: /orders")
CREDITS = "yungspider"
UUID = "afa0670c-b58c-4076-b4db-971a9d3daf8e"
SETTINGS_PAGE = True

# ══════════════════════════════ НАСТРОЙКИ ПЛАГИНА ═══════════════════════════════
PLUGIN_DIR = os.path.join("storage", "plugins", "orders_dashboard")
SETTINGS_PATH = os.path.join(PLUGIN_DIR, "settings.json")

DEFAULT_TEMPLATE = ("Здравствуйте, $username! 👋\n"
                    "Ваш заказ #$order (лот: $lot) ожидает подтверждения.\n"
                    "Если вы всё получили — подтвердите, пожалуйста, выполнение заказа. "
                    "Если возникли вопросы или что-то не так — напишите мне в этот чат, обязательно поможем!")

DEFAULT_SETTINGS = {
    "auto_remind": True,     # авто-напоминания вкл/выкл
    "delay_hours": 12,       # через сколько часов после оплаты/выдачи напоминать
    "max_reminders": 1,      # максимум напоминаний на один заказ
    "check_interval": 300,   # интервал проверки заказов, сек
    "template": DEFAULT_TEMPLATE,
    "exclude": [],           # подстроки названий лотов-исключений (например, «аренда»)
    "sent": {},              # {order_id: {"n": кол-во, "ts": время}}
    "deliveries": {},        # {order_id: timestamp выдачи автовыдачей}
}
DELAY_RANGE = (1, 168)          # часов
INTERVAL_RANGE = (60, 3600)     # сек
MAX_REM_RANGE = (1, 10)
PER_PAGE = 5                    # заказов на страницу
CACHE_TTL = 60                  # кэш списка заказов для меню, сек
PRUNE_AFTER = 30 * 86400        # через сколько дней чистить записи о неактивных заказах

MSK = timezone(timedelta(hours=3))  # FunPay отображает время по Москве
CURRENCY_SIGNS = {Currency.USD: "$", Currency.RUB: "₽", Currency.EUR: "€", Currency.UNKNOWN: ""}

# ══════════════════════════════ CALLBACK-КОДЫ ═══════════════════════════════════
CB_HOME = "odb:home"
CB_LIST = "odb:list"          # odb:list:{offset}
CB_ORDER = "odb:order"        # odb:order:{order_id}:{offset}
CB_REMIND = "odb:remind"      # odb:remind:{order_id}:{offset}
CB_REFASK = "odb:refask"      # odb:refask:{order_id}:{offset}
CB_REFYES = "odb:refyes"      # odb:refyes:{order_id}:{offset}
CB_ARTOG = "odb:artog"        # авто-напоминания вкл/выкл
CB_DELAY = "odb:delay"
CB_MAXREM = "odb:maxrem"
CB_TMPL = "odb:tmpl"
CB_EXCL = "odb:excl"
CB_INT = "odb:int"
CB_REFRESH = "odb:refresh"
CB_NOOP = "odb:noop"

ST_DELAY = "odb:st:delay"
ST_MAXREM = "odb:st:maxrem"
ST_TMPL = "odb:st:tmpl"
ST_EXCL = "odb:st:excl"
ST_INT = "odb:st:int"

_stop_event = threading.Event()
_settings_lock = threading.Lock()
_orders_lock = threading.Lock()
_orders_cache = {"ts": 0.0, "orders": []}
_cardinal: "Cardinal | None" = None


# ══════════════════════════════ УТИЛИТЫ ═════════════════════════════════════════
def _clamp(value, min_v, max_v):
    return max(min_v, min(max_v, value))


def _load_settings() -> dict:
    with _settings_lock:
        data = {}
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                logger.error("OrdersDashboard: не удалось прочитать настройки, использую значения по умолчанию.")
                logger.debug("TRACEBACK", exc_info=True)
                data = {}
        settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        if isinstance(data, dict):
            settings.update({k: v for k, v in data.items() if k in settings})
        for key, rng in [("delay_hours", DELAY_RANGE), ("check_interval", INTERVAL_RANGE),
                         ("max_reminders", MAX_REM_RANGE)]:
            try:
                settings[key] = _clamp(int(settings[key]), *rng)
            except (TypeError, ValueError):
                settings[key] = DEFAULT_SETTINGS[key]
        settings["auto_remind"] = bool(settings["auto_remind"])
        if not isinstance(settings["template"], str) or not settings["template"].strip():
            settings["template"] = DEFAULT_TEMPLATE
        if not isinstance(settings["exclude"], list):
            settings["exclude"] = []
        settings["exclude"] = [str(i).strip() for i in settings["exclude"] if str(i).strip()]
        if not isinstance(settings["sent"], dict):
            settings["sent"] = {}
        if not isinstance(settings["deliveries"], dict):
            settings["deliveries"] = {}
        return settings


def _save_settings(settings: dict) -> None:
    with _settings_lock:
        try:
            os.makedirs(PLUGIN_DIR, exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error("OrdersDashboard: не удалось сохранить настройки.")
            logger.debug("TRACEBACK", exc_info=True)


def _short(text: str, limit: int = 40) -> str:
    text = str(text or "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _escape(text) -> str:
    if tg_utils:
        return tg_utils.escape(str(text))
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plugin_enabled(crd: "Cardinal") -> bool:
    try:
        return crd.plugins[UUID].enabled
    except Exception:
        return False


def _currency_sign(currency) -> str:
    try:
        return CURRENCY_SIGNS.get(currency, "")
    except Exception:
        return ""


def _human_delta(seconds: float) -> str:
    """«2 дн. 5 ч.», «5 ч. 23 мин.», «23 мин.», «45 сек.»"""
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d} дн. {h} ч."
    if h:
        return f"{h} ч. {m} мин."
    if m:
        return f"{m} мин."
    return f"{s} сек."


def _order_date_ts(order) -> float | None:
    """Timestamp оплаты заказа. Дата FunPay — наивный datetime по Москве (UTC+3)."""
    try:
        if order.date is None:
            return None
        return order.date.replace(tzinfo=MSK).timestamp()
    except Exception:
        return None


def _fill_template(template: str, order) -> str:
    return (template.replace("$username", str(order.buyer_username or "друг"))
            .replace("$order", str(order.id))
            .replace("$lot", _short(order.description, 100)))


def _is_excluded(order, settings: dict) -> bool:
    desc = str(order.description or "").lower()
    return any(str(i).lower() in desc for i in settings.get("exclude", []))


def _notify(crd: "Cardinal", text: str, notification_type: str | None = None) -> None:
    if not crd.telegram:
        return
    kwargs = {}
    if notification_type and tg_utils:
        kwargs["notification_type"] = notification_type
    try:
        crd.telegram.send_notification(text, **kwargs)
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)


def _ntype_other() -> str | None:
    return tg_utils.NotificationTypes.other if tg_utils else None


# ══════════════════════════════ РАБОТА С ЗАКАЗАМИ ═══════════════════════════════
def _fetch_active_orders(crd: "Cardinal", max_pages: int = 10) -> list:
    """Активные (оплаченные, незакрытые) заказы, самые старые первыми."""
    orders = []
    start_from = None
    for _ in range(max_pages):
        result = crd.account.get_sales(start_from=start_from, include_paid=True,
                                       include_closed=False, include_refunded=False)
        next_id, page = result[0], result[1]
        orders.extend(page)
        if not next_id or not page:
            break
        start_from = next_id
    active = [o for o in orders if o.status is OrderStatuses.PAID]
    active.sort(key=lambda o: _order_date_ts(o) or 0)  # старые сверху
    return active


def _get_orders_cached(crd: "Cardinal", force: bool = False) -> list:
    with _orders_lock:
        now = time.time()
        if force or now - _orders_cache["ts"] > CACHE_TTL:
            try:
                _orders_cache["orders"] = _fetch_active_orders(crd)
                _orders_cache["ts"] = now
            except Exception:
                logger.error("OrdersDashboard: не удалось получить список заказов.")
                logger.debug("TRACEBACK", exc_info=True)
                if force:
                    _orders_cache["orders"] = []
                    _orders_cache["ts"] = now
        return list(_orders_cache["orders"])


def _send_reminder_to_order(crd: "Cardinal", order, settings: dict) -> bool:
    """Отправляет напоминание в чат заказа. Возвращает успех."""
    text = _fill_template(settings["template"], order)
    try:
        crd.account.send_message(order.chat_id, text)
        return True
    except Exception:
        logger.error(f"OrdersDashboard: не удалось отправить напоминание по заказу #{order.id}.")
        logger.debug("TRACEBACK", exc_info=True)
        return False


def _record_reminder(crd: "Cardinal", order_id: str) -> None:
    settings = _load_settings()
    rec = settings["sent"].get(order_id) or {"n": 0, "ts": 0}
    rec["n"] = int(rec.get("n", 0)) + 1
    rec["ts"] = time.time()
    settings["sent"][order_id] = rec
    _save_settings(settings)


def _reminders_sent(settings: dict, order_id: str) -> int:
    try:
        return int(settings["sent"].get(order_id, {}).get("n", 0))
    except Exception:
        return 0


def _auto_remind_tick(crd: "Cardinal", now_ts: float | None = None) -> list:
    """
    Один проход авто-напоминаний. Возвращает список id заказов, которым отправлено напоминание.
    Выделено в функцию для тестируемости; вызывается из фонового потока.
    """
    settings = _load_settings()
    if not settings["auto_remind"]:
        return []
    now_ts = now_ts if now_ts is not None else time.time()
    try:
        orders = _fetch_active_orders(crd)
    except Exception:
        logger.error("OrdersDashboard: не удалось получить заказы для авто-напоминаний.")
        logger.debug("TRACEBACK", exc_info=True)
        return []

    reminded = []
    seen_ids = set()
    delay_s = settings["delay_hours"] * 3600
    for order in orders:
        if str(order.id) in seen_ids:  # защита от дублей при пагинации
            continue
        seen_ids.add(str(order.id))
        if order.status is not OrderStatuses.PAID:
            continue
        if _is_excluded(order, settings):
            continue
        if _reminders_sent(settings, order.id) >= settings["max_reminders"]:
            continue
        base = settings["deliveries"].get(order.id)
        if base is None:
            base = _order_date_ts(order)
        if base is None or now_ts - float(base) < delay_s:
            continue
        if _send_reminder_to_order(crd, order, settings):
            _record_reminder(crd, order.id)
            reminded.append(order.id)
            logger.info(f"OrdersDashboard: отправил авто-напоминание по заказу #{order.id} "
                        f"(покупатель {order.buyer_username}).")
            _notify(crd, f"💬 <b>Отправлено напоминание о подтверждении заказа.</b>\n\n"
                         f"Заказ: <code>#{_escape(order.id)}</code>\n"
                         f"Покупатель: {_escape(order.buyer_username)}\n"
                         f"Лот: <code>{_escape(_short(order.description, 80))}</code>\n"
                         f"Оплачен: {_human_delta(now_ts - (_order_date_ts(order) or now_ts))} назад",
                    _ntype_other())

    # чистка записей о заказах, которых уже нет в активных
    if reminded or settings["sent"] or settings["deliveries"]:
        settings = _load_settings()
        active_ids = {o.id for o in orders}
        cutoff = now_ts - PRUNE_AFTER
        settings["sent"] = {k: v for k, v in settings["sent"].items()
                            if k in active_ids or float(v.get("ts", 0) or 0) > cutoff}
        settings["deliveries"] = {k: v for k, v in settings["deliveries"].items()
                                  if k in active_ids or float(v or 0) > cutoff}
        _save_settings(settings)
    return reminded


def _auto_remind_loop(crd: "Cardinal") -> None:
    logger.info("OrdersDashboard: запущен поток авто-напоминаний.")
    last = 0.0
    while not _stop_event.wait(10):
        try:
            if not _plugin_enabled(crd) or not crd.account.is_initiated:
                last = time.time()
                continue
            interval = _load_settings()["check_interval"]
            if time.time() - last < interval:
                continue
            last = time.time()
            _auto_remind_tick(crd)
        except Exception:
            logger.error("OrdersDashboard: ошибка в потоке авто-напоминаний.")
            logger.debug("TRACEBACK", exc_info=True)


def delivery_recorder(c: "Cardinal", e, *args) -> None:
    """BIND_TO_POST_DELIVERY: фиксирует момент выдачи товара (для точного таймера напоминаний)."""
    try:
        order = getattr(e, "order", None)
        if order is None:
            return
        settings = _load_settings()
        settings["deliveries"][str(order.id)] = time.time()
        _save_settings(settings)
        logger.debug(f"OrdersDashboard: зафиксирована выдача заказа #{order.id}.")
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)


# ══════════════════════════ TELEGRAM-ИНТЕРФЕЙС ══════════════════════════════════
def _home_text(crd: "Cardinal", orders: list | None = None) -> str:
    settings = _load_settings()
    if orders is None:
        orders = []
    now_ts = time.time()
    delay_s = settings["delay_hours"] * 3600
    overdue = 0
    for o in orders:
        base = settings["deliveries"].get(o.id)
        if base is None:
            base = _order_date_ts(o)
        if base is not None and now_ts - float(base) >= delay_s:
            overdue += 1
    ar = "ВКЛ 🟢" if settings["auto_remind"] else "ВЫКЛ 🔴"
    return (f"🗂 <b>Дашборд заказов</b> <i>v{VERSION}</i>\n\n"
            f"📋 Активных заказов (ожидают подтверждения): <b>{len(orders)}</b>\n"
            f"⏳ Из них висят дольше {settings['delay_hours']} ч.: <b>{overdue}</b>\n\n"
            f"💬 Авто-напоминания: <b>{ar}</b>\n"
            f"• через <b>{settings['delay_hours']}</b> ч. после оплаты/выдачи\n"
            f"• не более <b>{settings['max_reminders']}</b> на заказ\n"
            f"• проверка каждые <b>{settings['check_interval']}</b> сек.\n"
            f"• исключений по лотам: <b>{len(settings['exclude'])}</b>")


def _home_kb(crd: "Cardinal", orders_count: int | None = None) -> K:
    settings = _load_settings()
    kb = K(row_width=2)
    title = "📋 Активные заказы" if orders_count is None else f"📋 Активные заказы ({orders_count})"
    kb.add(B(title, None, f"{CB_LIST}:0"), B("🔄 Обновить", None, CB_REFRESH))
    kb.add(B(f"💬 Авто-напоминания: {'ВКЛ' if settings['auto_remind'] else 'ВЫКЛ'}", None, CB_ARTOG))
    kb.add(B(f"⏳ Задержка: {settings['delay_hours']} ч.", None, CB_DELAY))
    kb.add(B(f"🔁 Макс. напоминаний: {settings['max_reminders']}", None, CB_MAXREM))
    kb.add(B(f"⏱ Интервал: {settings['check_interval']}с", None, CB_INT))
    kb.add(B("📝 Текст напоминания", None, CB_TMPL))
    kb.add(B(f"🚫 Исключения: {len(settings['exclude'])}", None, CB_EXCL))
    if CBT:
        kb.add(B("🔙 Назад к плагину", None, f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
    return kb


def _list_text(crd: "Cardinal", orders: list) -> str:
    if not orders:
        return ("📋 <b>Активные заказы</b>\n\nНет заказов, ожидающих подтверждения. 🎉\n\n"
                "<i>Список обновляется автоматически раз в минуту или кнопкой 🔄.</i>")
    return ("📋 <b>Активные заказы</b> (ожидают подтверждения)\n\n"
            "Самые старые — сверху. Нажмите на заказ, чтобы открыть карточку:")


def _list_kb(crd: "Cardinal", orders: list, offset: int) -> K:
    settings = _load_settings()
    now_ts = time.time()
    kb = K(row_width=1)
    page = orders[offset:offset + PER_PAGE]
    for o in page:
        base = settings["deliveries"].get(o.id)
        if base is None:
            base = _order_date_ts(o)
        age = now_ts - float(base) if base is not None else 0
        mark = "⏳" if age >= settings["delay_hours"] * 3600 else "🟡"
        price = f"{o.price:g}{_currency_sign(o.currency)}"
        kb.add(B(f"{mark} #{o.id} • {_short(o.description, 22)} • {price} • {_human_delta(age)}",
                 None, f"{CB_ORDER}:{o.id}:{offset}"))
    pages = max(1, -(-len(orders) // PER_PAGE))
    if pages > 1:
        row = []
        if offset > 0:
            row.append(B("⬅️", None, f"{CB_LIST}:{offset - PER_PAGE}"))
        row.append(B(f"{offset // PER_PAGE + 1}/{pages}", None, CB_NOOP))
        if offset + PER_PAGE < len(orders):
            row.append(B("➡️", None, f"{CB_LIST}:{offset + PER_PAGE}"))
        kb.row(*row)
    kb.row(B("🔄 Обновить", None, CB_REFRESH), B("🏠 Главное меню", None, CB_HOME))
    return kb


def _find_order(orders: list, order_id: str):
    for o in orders:
        if str(o.id) == str(order_id):
            return o
    return None


def _order_text(crd: "Cardinal", order) -> str:
    settings = _load_settings()
    now_ts = time.time()
    pay_ts = _order_date_ts(order)
    age = now_ts - pay_ts if pay_ts else None
    pay_str = order.date.strftime("%d.%m.%Y %H:%M") if order.date else "?"
    del_ts = settings["deliveries"].get(str(order.id))
    if del_ts:
        del_str = f"✅ автовыдача {_human_delta(now_ts - float(del_ts))} назад"
    else:
        del_str = "➖ нет записи (ручная выдача или выдано до установки плагина)"
    sent_n = _reminders_sent(settings, str(order.id))
    price = f"{order.price:g} {_currency_sign(order.currency)}".strip()
    amount = f" | Кол-во: {order.amount}" if getattr(order, "amount", None) else ""
    return (f"🗂 <b>Заказ #{_escape(order.id)}</b>\n\n"
            f"<b>Лот:</b> <code>{_escape(_short(order.description, 120))}</code>\n"
            f"<b>Покупатель:</b> <a href=\"https://funpay.com/users/{order.buyer_id}/\">"
            f"{_escape(order.buyer_username)}</a>\n"
            f"<b>Цена:</b> {price}{amount}\n"
            f"<b>Оплачен:</b> {pay_str}"
            f"{f' ({_human_delta(age)} назад)' if age is not None else ''} <i>МСК</i>\n"
            f"<b>Выдача:</b> {del_str}\n"
            f"<b>Напоминаний отправлено:</b> {sent_n}/{settings['max_reminders']}\n"
            f"<b>Подкатегория:</b> {_escape(order.subcategory_name or '—')}")


def _order_kb(order, offset: int = 0) -> K:
    kb = K(row_width=2)
    kb.add(B("💬 Напомнить сейчас", None, f"{CB_REMIND}:{order.id}:{offset}"))
    kb.add(B("↩️ Возврат средств", None, f"{CB_REFASK}:{order.id}:{offset}"))
    kb.add(B("🔗 Заказ на FunPay", url=f"https://funpay.com/orders/{order.id}/"))
    kb.add(B("🔙 К списку", None, f"{CB_LIST}:{offset}"))
    return kb


def _answer(bot, c: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    try:
        bot.answer_callback_query(c.id, text, show_alert=alert)
    except Exception:
        pass


def _edit_or_send(bot, c: CallbackQuery, text: str, kb: K | None) -> None:
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.id, parse_mode="HTML", reply_markup=kb)
    except ApiTelegramException as e:
        if "message is not modified" not in str(e):
            try:
                bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)
    except Exception:
        try:
            bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)


def init(crd: "Cardinal", *args) -> None:
    """Инициализация плагина (BIND_TO_PRE_INIT)."""
    global _cardinal
    _cardinal = crd
    _load_settings()

    tg = crd.telegram
    if tg is not None:
        bot = tg.bot

        # ---------- главное меню ----------
        def open_home(c: CallbackQuery, force: bool = False) -> None:
            _answer(bot, c)
            if not crd.account.is_initiated:
                _edit_or_send(bot, c, "⏳ Аккаунт FunPay еще инициализируется. Попробуйте через пару секунд.",
                              K().add(B("🏠 Главное меню", None, CB_HOME)))
                return
            orders = _get_orders_cached(crd, force=force)
            _edit_or_send(bot, c, _home_text(crd, orders), _home_kb(crd, len(orders)))

        def _render_list(c: CallbackQuery, offset: int, force: bool = False) -> None:
            """Рендерит список заказов (страница offset) в сообщение c."""
            orders = _get_orders_cached(crd, force=force)
            _edit_or_send(bot, c, _list_text(crd, orders), _list_kb(crd, orders, offset))

        def open_list(c: CallbackQuery, force: bool = False) -> None:
            _answer(bot, c)
            offset = 0
            parts = str(c.data).split(":")
            if len(parts) > 2 and parts[2].isdigit():
                offset = int(parts[2])
            _render_list(c, offset, force=force)

        def open_order(c: CallbackQuery) -> None:
            _answer(bot, c)
            parts = c.data.split(":")
            order_id = parts[2]
            offset = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            orders = _get_orders_cached(crd)
            order = _find_order(orders, order_id)
            if order is None:
                _edit_or_send(bot, c, "❓ Заказ не найден в списке активных "
                                      "(возможно, покупатель уже подтвердил его).",
                              K().add(B("🔄 Обновить список", None, f"{CB_LIST}:0"))
                              .add(B("🏠 Главное меню", None, CB_HOME)))
                return
            _edit_or_send(bot, c, _order_text(crd, order), _order_kb(order, offset))

        # ---------- действия с заказом ----------
        def remind_now(c: CallbackQuery) -> None:
            order_id = c.data.split(":")[2]
            orders = _get_orders_cached(crd)
            order = _find_order(orders, order_id)
            if order is None:
                _answer(bot, c, "Заказ уже не активен.", alert=True)
                open_list(c, force=True)
                return
            settings = _load_settings()
            threading.Thread(target=_remind_worker, args=(crd, order, settings), daemon=True).start()
            _answer(bot, c, f"💬 Отправляю напоминание покупателю {order.buyer_username}...", alert=True)

        def _remind_worker(crd_: "Cardinal", order, settings: dict) -> None:
            if _send_reminder_to_order(crd_, order, settings):
                _record_reminder(crd_, str(order.id))
                _notify(crd_, f"💬 <b>Напоминание отправлено вручную.</b>\n\n"
                              f"Заказ: <code>#{_escape(order.id)}</code>\n"
                              f"Покупатель: {_escape(order.buyer_username)}", _ntype_other())
            else:
                _notify(crd_, f"❌ <b>Не удалось отправить напоминание</b> по заказу "
                              f"<code>#{_escape(order.id)}</code>.", _ntype_other())

        def ask_refund(c: CallbackQuery) -> None:
            _answer(bot, c)
            parts = c.data.split(":")
            order_id = parts[2]
            offset = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            orders = _get_orders_cached(crd)
            order = _find_order(orders, order_id)
            if order is None:
                _render_list(c, 0, force=True)
                return
            kb = K(row_width=2).add(B("✅ Да, оформить возврат", None, f"{CB_REFYES}:{order_id}:{offset}"),
                                    B("❌ Отмена", None, f"{CB_ORDER}:{order_id}:{offset}"))
            _edit_or_send(bot, c, f"↩️ <b>Оформить возврат средств</b> за заказ "
                                  f"<code>#{_escape(order_id)}</code>?\n\n"
                                  f"Покупатель: {_escape(order.buyer_username)}\n"
                                  f"Сумма: {order.price:g} {_currency_sign(order.currency)}\n\n"
                                  f"⚠️ Деньги вернутся покупателю, заказ закроется.", kb)

        def do_refund(c: CallbackQuery) -> None:
            parts = c.data.split(":")
            order_id = parts[2]
            offset = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            _answer(bot, c, f"↩️ Оформляю возврат по заказу #{order_id}...")

            def worker():
                try:
                    crd.account.refund(order_id)
                    logger.info(f"OrdersDashboard: оформлен возврат по заказу #{order_id}.")
                    _notify(crd, f"↩️ <b>Оформлен возврат средств</b> по заказу "
                                 f"<code>#{_escape(order_id)}</code>.", _ntype_other())
                except Exception:
                    logger.error(f"OrdersDashboard: не удалось оформить возврат по заказу #{order_id}.")
                    logger.debug("TRACEBACK", exc_info=True)
                    _notify(crd, f"❌ <b>Не удалось оформить возврат</b> по заказу "
                                 f"<code>#{_escape(order_id)}</code>.", _ntype_other())

            threading.Thread(target=worker, daemon=True).start()
            _render_list(c, offset, force=True)

        # ---------- настройки ----------
        def toggle_auto(c: CallbackQuery) -> None:
            settings = _load_settings()
            settings["auto_remind"] = not settings["auto_remind"]
            _save_settings(settings)
            _answer(bot, c, f"💬 Авто-напоминания: {'ВКЛ' if settings['auto_remind'] else 'ВЫКЛ'}")
            orders = _get_orders_cached(crd)
            _edit_or_send(bot, c, _home_text(crd, orders), _home_kb(crd, len(orders)))

        def act_edit(c: CallbackQuery, state: str, prompt: str) -> None:
            result = bot.send_message(c.message.chat.id, prompt, parse_mode="HTML",
                                      reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, state)
            _answer(bot, c)

        def act_delay(c: CallbackQuery) -> None:
            s = _load_settings()
            act_edit(c, ST_DELAY, f"⏳ Введите задержку авто-напоминания в часах "
                                  f"(<code>{DELAY_RANGE[0]}</code> - <code>{DELAY_RANGE[1]}</code>).\n\n"
                                  f"Текущее значение: <b>{s['delay_hours']}</b> ч.")

        def act_maxrem(c: CallbackQuery) -> None:
            s = _load_settings()
            act_edit(c, ST_MAXREM, f"🔁 Введите максимум напоминаний на один заказ "
                                   f"(<code>{MAX_REM_RANGE[0]}</code> - <code>{MAX_REM_RANGE[1]}</code>).\n\n"
                                   f"Текущее значение: <b>{s['max_reminders']}</b>.")

        def act_interval(c: CallbackQuery) -> None:
            s = _load_settings()
            act_edit(c, ST_INT, f"⏱ Введите интервал проверки заказов в секундах "
                                f"(<code>{INTERVAL_RANGE[0]}</code> - <code>{INTERVAL_RANGE[1]}</code>).\n\n"
                                f"Текущее значение: <b>{s['check_interval']}</b> сек.")

        def act_template(c: CallbackQuery) -> None:
            s = _load_settings()
            act_edit(c, ST_TMPL, "📝 Отправьте новый текст напоминания покупателю.\n\n"
                                 "Доступные переменные:\n"
                                 "<code>$username</code> — ник покупателя\n"
                                 "<code>$order</code> — номер заказа\n"
                                 "<code>$lot</code> — название лота\n\n"
                                 f"Текущий текст:\n<code>{_escape(_short(s['template'], 300))}</code>")

        def act_exclude(c: CallbackQuery) -> None:
            s = _load_settings()
            curr = "\n".join(s["exclude"]) or "(пусто)"
            act_edit(c, ST_EXCL, "🚫 Отправьте список исключений — заказы с этими подстроками "
                                 "в названии лота не получат авто-напоминаний "
                                 "(например, лоты аренды). Одна подстрока = одна строка сообщения.\n\n"
                                 f"Текущий список:\n<code>{_escape(curr)}</code>\n\n"
                                 f"Чтобы очистить список, отправьте <code>-</code>.")

        def _state_eq(tg_, m: Message, state: str) -> bool:
            try:
                return tg_.check_state(m.chat.id, m.from_user.id, state)
            except Exception:
                return False

        def edit_delay(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            _edit_int_setting(m, "delay_hours", DELAY_RANGE, "⏳ Задержка авто-напоминания")

        def edit_maxrem(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            _edit_int_setting(m, "max_reminders", MAX_REM_RANGE, "🔁 Максимум напоминаний")

        def edit_interval(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            _edit_int_setting(m, "check_interval", INTERVAL_RANGE, "⏱ Интервал проверки")

        def _edit_int_setting(m: Message, key: str, rng: tuple, label: str) -> None:
            try:
                value = int(str(m.text).strip())
            except (ValueError, AttributeError):
                bot.reply_to(m, "❌ Нужно ввести число. Попробуйте еще раз.")
                return
            value = _clamp(value, *rng)
            settings = _load_settings()
            settings[key] = value
            _save_settings(settings)
            bot.reply_to(m, f"✅ {label}: <b>{value}</b>", parse_mode="HTML")

        def edit_template(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            text = str(m.text or "").strip()
            if not text:
                bot.reply_to(m, "❌ Пустой текст не сохранен.")
                return
            settings = _load_settings()
            settings["template"] = text[:1000]
            _save_settings(settings)
            bot.reply_to(m, "📝 Текст напоминания сохранен.", parse_mode="HTML")

        def edit_exclude(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            text = str(m.text or "").strip()
            items = [] if text in ("-", "") else [i.strip() for i in text.split("\n") if i.strip()]
            settings = _load_settings()
            settings["exclude"] = items[:50]
            _save_settings(settings)
            bot.reply_to(m, f"🚫 Исключений сохранено: <b>{len(settings['exclude'])}</b>.", parse_mode="HTML")

        # ---------- команда ----------
        def cmd_open(m: Message) -> None:
            if not crd.account.is_initiated:
                bot.send_message(m.chat.id, "⏳ Аккаунт FunPay еще инициализируется. Попробуйте позже.")
                return
            orders = _get_orders_cached(crd)
            bot.send_message(m.chat.id, _home_text(crd, orders), parse_mode="HTML",
                             reply_markup=_home_kb(crd, len(orders)))

        def noop(c: CallbackQuery) -> None:
            _answer(bot, c)

        # ---------- регистрация ----------
        tg.cbq_handler(open_home, lambda c: c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
        tg.cbq_handler(lambda c: open_home(c), lambda c: c.data == CB_HOME)
        tg.cbq_handler(lambda c: open_home(c, force=True), lambda c: c.data == CB_REFRESH)
        tg.cbq_handler(open_list, lambda c: c.data.startswith(f"{CB_LIST}:"))
        tg.cbq_handler(open_order, lambda c: c.data.startswith(f"{CB_ORDER}:"))
        tg.cbq_handler(remind_now, lambda c: c.data.startswith(f"{CB_REMIND}:"))
        tg.cbq_handler(ask_refund, lambda c: c.data.startswith(f"{CB_REFASK}:"))
        tg.cbq_handler(do_refund, lambda c: c.data.startswith(f"{CB_REFYES}:"))
        tg.cbq_handler(toggle_auto, lambda c: c.data == CB_ARTOG)
        tg.cbq_handler(act_delay, lambda c: c.data == CB_DELAY)
        tg.cbq_handler(act_maxrem, lambda c: c.data == CB_MAXREM)
        tg.cbq_handler(act_interval, lambda c: c.data == CB_INT)
        tg.cbq_handler(act_template, lambda c: c.data == CB_TMPL)
        tg.cbq_handler(act_exclude, lambda c: c.data == CB_EXCL)
        tg.cbq_handler(noop, lambda c: c.data == CB_NOOP)
        tg.msg_handler(edit_delay, func=lambda m: _state_eq(tg, m, ST_DELAY))
        tg.msg_handler(edit_maxrem, func=lambda m: _state_eq(tg, m, ST_MAXREM))
        tg.msg_handler(edit_interval, func=lambda m: _state_eq(tg, m, ST_INT))
        tg.msg_handler(edit_template, func=lambda m: _state_eq(tg, m, ST_TMPL))
        tg.msg_handler(edit_exclude, func=lambda m: _state_eq(tg, m, ST_EXCL))
        tg.msg_handler(cmd_open, commands=["orders"])

        try:
            crd.add_telegram_commands(UUID, [("orders", "Дашборд активных заказов и напоминания.", True)])
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)

        logger.info(f"OrdersDashboard v{VERSION}: Telegram-меню зарегистрировано (команда /orders).")
    else:
        logger.info(f"OrdersDashboard v{VERSION}: Telegram выключен — работают только авто-напоминания.")

    threading.Thread(target=_auto_remind_loop, args=(crd,), daemon=True).start()


def on_delete(crd: "Cardinal", c=None) -> None:
    """Вызывается при удалении плагина через ПУ (BIND_TO_DELETE)."""
    _stop_event.set()


# ══════════════════════════ ПРИВЯЗКА К СОБЫТИЯМ КАРДИНАЛА ═══════════════════════
BIND_TO_PRE_INIT = [init]
BIND_TO_POST_DELIVERY = [delivery_recorder]
BIND_TO_DELETE = on_delete
