import os
import sys
import time
import json
import random
import hashlib
import requests
from datetime import datetime, timezone, timedelta

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

SERVER_ID = int(os.getenv("SERVER_ID", "384625"))
RAW_TOKEN = os.getenv("MINESTRATOR_TOKEN", "").strip()
if not RAW_TOKEN:
    print("❌ КРИТИЧЕСКАЯ ОШИБКА: Переменная окружения MINESTRATOR_TOKEN не задана!")
    sys.exit(1)
ACTION_MODE = os.getenv("ACTION_MODE", "SMART_KEEPALIVE").upper()

if not RAW_TOKEN.startswith("Bearer "):
    AUTH_HEADER = f"Bearer {RAW_TOKEN}"
else:
    AUTH_HEADER = RAW_TOKEN

MCP_URL = "https://mcp.sttr.io/minestrator"
REST_URL_LIVE = f"https://mine.sttr.io/server/{SERVER_ID}/live"
REST_URL_POWER = f"https://mine.sttr.io/server/{SERVER_ID}/poweraction"

MCP_HEADERS = {
    "Authorization": AUTH_HEADER,
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream"
}

REST_HEADERS = {
    "Authorization": AUTH_HEADER,
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://minestrator.com",
    "Referer": "https://minestrator.com/"
}

# Московский часовой пояс (UTC+3)
MSK_TZ = timezone(timedelta(hours=3))

# Диапазон случайного перезапуска днем (в минутах)
# Minestrator Free отключается через 4 часа (240 мин).
# Случайный интервал от 150 до 210 минут (2.5 — 3.5 часа) гарантирует безопасность и естественность.
RESTART_MIN_MINUTES = 150  # 2ч 30м (2.5 часа)
RESTART_MAX_MINUTES = 210  # 3ч 30м (3.5 часа)

def log(msg):
    now_str = datetime.now(MSK_TZ).strftime('%Y-%m-%d %H:%M:%S [MSK]')
    print(f"[{now_str}] {msg}", flush=True)

def mcp_call(tool_name, arguments):
    payload = {
        "jsonrpc": "2.0",
        "id": int(time.time()),
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments
        }
    }
    try:
        resp = requests.post(MCP_URL, headers=MCP_HEADERS, json=payload, timeout=15)
        if resp.status_code == 200:
            res = resp.json().get("result", {})
            if res.get("isError"):
                return False, res.get("content", [{}])[0].get("text", "MCP tool error")
            content = res.get("content", [{}])[0].get("text", "")
            return True, content
        return False, f"HTTP {resp.status_code}: {resp.text[:100]}"
    except Exception as e:
        return False, str(e)

def get_server_live_info():
    # 1. MCP
    ok, content = mcp_call("get_server_live", {"server_id": SERVER_ID})
    if ok:
        try:
            data = json.loads(content)
            state = data.get("state") or data.get("stats", {}).get("state") or "unknown"
            uptime = data.get("stats", {}).get("uptime", {})
            return {
                "state": state.lower(),
                "uptime_seconds": uptime.get("total_seconds", 0),
                "uptime_hours": uptime.get("hours", 0),
                "uptime_minutes": uptime.get("minutes", 0),
                "cpu_percent": data.get("stats", {}).get("cpu", {}).get("percent_of_dedicated", 0),
                "ram_current": data.get("stats", {}).get("memory", {}).get("current", 0),
                "ram_limit": data.get("stats", {}).get("memory", {}).get("limit", 0),
                "players": data.get("stats", {}).get("players", {}).get("current", 0),
                "raw": data
            }
        except Exception:
            pass

    # 2. REST Fallback
    try:
        resp = requests.get(REST_URL_LIVE, headers=REST_HEADERS, timeout=10)
        if resp.status_code == 200:
            data = resp.json().get("api", {}).get("data", {})
            stats = data.get("stats", {})
            state = data.get("state") or stats.get("state") or "unknown"
            uptime = stats.get("uptime", {})
            return {
                "state": str(state).lower(),
                "uptime_seconds": uptime.get("total_seconds", 0),
                "uptime_hours": uptime.get("hours", 0),
                "uptime_minutes": uptime.get("minutes", 0),
                "cpu_percent": stats.get("cpu", {}).get("percent_of_dedicated", 0),
                "ram_current": stats.get("memory", {}).get("current", 0),
                "ram_limit": stats.get("memory", {}).get("limit", 0),
                "players": stats.get("players", {}).get("current", 0),
                "raw": data
            }
    except Exception as e:
        log(f"REST live fallback error: {e}")

    return {"state": "unknown", "uptime_seconds": 0, "uptime_hours": 0, "uptime_minutes": 0, "players": 0}

def execute_power_action(action):
    log(f"⚡ Отправляем команду управления питанием: [{action.upper()}]...")
    # Небольшой случайный джиттер перед отправкой (2-8 сек), чтобы запросы не шли ровно по секундам
    time.sleep(random.randint(2, 8))
    
    ok, content = mcp_call("power_action", {"server_id": SERVER_ID, "action": action})
    if ok:
        log(f"✓ MCP ответ: {content.strip()}")
        return True

    log(f"⚠️ MCP вернул ошибку, пробуем REST...")
    try:
        resp = requests.put(REST_URL_POWER, headers=REST_HEADERS, json={"poweraction": action}, timeout=10)
        if resp.status_code in [200, 204]:
            log(f"✓ REST ответ: {resp.text.strip()}")
            return True
        log(f"❌ REST ошибка HTTP {resp.status_code}: {resp.text}")
    except Exception as e:
        log(f"❌ REST ошибка: {e}")

    return False

def send_console_msg(msg):
    ok, _ = mcp_call("send_console_command", {"server_id": SERVER_ID, "command": msg})
    return ok

def wait_for_online(timeout=120):
    log("⏳ Ожидание перехода сервера в статус [ONLINE]...")
    start_t = time.time()
    while time.time() - start_t < timeout:
        info = get_server_live_info()
        st = info["state"]
        log(f"   Текущий статус: [{st.upper()}] (прошло {int(time.time() - start_t)}с)")
        if st == "online":
            log("🎉 Сервер полностью запущен и работает в штатном режиме!")
            return True
        time.sleep(10)
    log("⚠️ Время ожидания истекло, сервер еще загружается.")
    return False

def get_daily_wakeup_time(now_msk):
    # Детерминированный расчет времени утреннего подъема на сегодня (между 06:00 и 06:59 МСК)
    date_str = now_msk.strftime('%Y-%m-%d')
    hash_val = int(hashlib.md5(date_str.encode()).hexdigest(), 16)
    wake_minute = hash_val % 60
    return 6, wake_minute

def is_night_window(now_msk):
    wake_hour, wake_minute = get_daily_wakeup_time(now_msk)
    # Ночное окно: от 00:00:00 до утреннего подъема (06:XX МСК)
    if now_msk.hour < wake_hour:
        return True
    if now_msk.hour == wake_hour and now_msk.minute < wake_minute:
        return True
    return False

def run_restart_countdown(action="restart", reason="Плановая перезагрузка"):
    log(f"🚨 Запуск обратного отсчета для перезагрузки ({action.upper()}) | Причина: {reason}")
    
    # 0. Отключаем серые служебные сообщения админам [Server: Отображение заголовка...]
    send_console_msg("gamerule sendCommandFeedback false")
    send_console_msg("gamerule logAdminCommands false")
    
    # 1. Красные края экрана для всех игроков (WorldBorder Warning Effect)
    send_console_msg("worldborder warning distance 29999984")
    
    # 2. Звуковой сигнал колокола и сообщение в чат
    send_console_msg("playsound minecraft:block.bell.use master @a")
    send_console_msg('tellraw @a [{"text":"[СЕРВЕР] ","color":"red","bold":true},{"text":"Внимание! ' + reason + ' через 10 секунд! ","color":"yellow"},{"text":"Заходи через 2 минуты после перезапуска!","color":"green","bold":true}]')
    
    # 3. Большие красные буквы на экране
    send_console_msg("title @a times 5 35 5")
    send_console_msg('title @a title {"text":"⚠ ВНИМАНИЕ ⚠","color":"red","bold":true}')
    send_console_msg('title @a subtitle {"text":"' + reason + ' • Заходи через 2 мин!","color":"gold"}')
    time.sleep(4)
    
    # 4. Обратный отсчет: 5, 4, 3, 2, 1 с тикающими звуками и сменой цветов
    countdown_steps = [
        (5, "5", "gold", "Сохранение мира... Заходи через 2 мин!"),
        (4, "4", "gold", None),
        (3, "3", "red", "Сервер уходит на перезагрузку..."),
        (2, "2", "red", None),
        (1, "1", "dark_red", "Перезапуск..."),
    ]
    
    for sec, num_str, color, sub in countdown_steps:
        send_console_msg(f'title @a title {{"text":"{num_str}", "color":"{color}", "bold":true}}')
        if sub:
            send_console_msg(f'title @a subtitle {{"text":"{sub}", "color":"yellow"}}')
        pitch = 1.0 + (5 - sec) * 0.25
        send_console_msg(f'playsound minecraft:block.note_block.pling master @a ~ ~ ~ 1 {pitch:.2f}')
        time.sleep(1)
        
    # 5. Ноль - сохранение и сброс границы в норму
    send_console_msg('title @a title {"text":"0 - ПЕРЕЗАГРУЗКА","color":"dark_red","bold":true}')
    send_console_msg('title @a subtitle {"text":"Заходи через 2 минуты!","color":"green","bold":true}')
    send_console_msg("save-all")
    time.sleep(1)
    send_console_msg("worldborder warning distance 5")
    
    # 6. Отправка действия питания
    execute_power_action(action)
    if action in ["start", "restart"]:
        if wait_for_online():
            send_console_msg('tellraw @a [{"text":"[СЕРВЕР] ","color":"green","bold":true},{"text":"Сервер онлайн! Заходи и проверь, всё ли работает!","color":"aqua","bold":true}]')
            send_console_msg("say [СЕРВЕР] Перезапуск завершен! Заходи и проверь, всё ли работает!")

def smart_keepalive():
    log("=" * 65)
    log(f"🛡️ УМНЫЙ KEEPALIVE БОТ (Сервер #{SERVER_ID})")
    log("=" * 65)

    now_msk = datetime.now(MSK_TZ)
    wake_hour, wake_minute = get_daily_wakeup_time(now_msk)
    night = is_night_window(now_msk)
    
    mode_str = f"🌙 НОЧНОЙ ПОКОЙ (до {wake_hour:02d}:{wake_minute:02d} МСК)" if night else "☀️ ДНЕВНОЙ"
    log(f"🕒 Время по Москве: {now_msk.strftime('%H:%M:%S (%A)')} | Режим: {mode_str}")
    log(f"⏰ Запланированное утреннее пробуждение на сегодня: {wake_hour:02d}:{wake_minute:02d} МСК")

    info = get_server_live_info()
    st = info["state"]
    sec = info["uptime_seconds"]
    h = info["uptime_hours"]
    m = info["uptime_minutes"]
    uptime_min = sec // 60
    players = info.get("players", 0)
    ram = info.get("ram_current", 0)
    ram_lim = info.get("ram_limit", 4000)
    cpu = info.get("cpu_percent", 0)

    log(f"📊 Статус сервера: [{st.upper()}] | Аптайм: {h}ч {m}м ({sec}с)")
    log(f"💻 Нагрузка: RAM {ram}/{ram_lim} MB | CPU {cpu}% | Игроков онлайн: {players}")

    # ==========================================
    # 🌙 1. НОЧНОЕ ОКНО (от 00:00 до 06:XX МСК)
    # ==========================================
    if night:
        if st == "online":
            log(f"🌙 Ночное окно (после 00:00 МСК). Сервер онлайн. Бот НЕ выключает сервер принудительно (дает доиграть), но больше не делает перезапусков.")
            send_console_msg("list")
        elif st == "offline":
            log(f"💤 Ночное окно. Сервер выключен. Бот понимает, что сегодня включать больше не нужно, и оставляет его спать до {wake_hour:02d}:{wake_minute:02d} МСК.")
        else:
            log(f"🌙 Сервер в переходном состоянии [{st.upper()}]. Ожидаем завершения.")
        return

    # ==========================================
    # ☀️ 2. ДНЕВНОЙ РЕЖИМ (от 06:XX до 23:59 МСК)
    # ==========================================
    if st == "offline":
        log(f"☀️ Утро/день ({now_msk.strftime('%H:%M')} >= {wake_hour:02d}:{wake_minute:02d} МСК). Сервер выключен — запускаем на день...")
        if execute_power_action("start"):
            if wait_for_online():
                send_console_msg('tellraw @a [{"text":"[СЕРВЕР] ","color":"green","bold":true},{"text":"Доброе утро! Сервер запущен и готов к игре! Приятного дня!","color":"gold","bold":true}]')
                send_console_msg("say [СЕРВЕР] Доброе утро! Сервер запущен и готов к игре!")
        return

    if st == "starting":
        log("🔄 Сервер сейчас загружается. Ожидаем входа в онлайн...")
        wait_for_online()
        return

    if st == "online":
        # Генерируем случайный динамический порог перезапуска на текущую проверку
        dynamic_threshold_min = random.randint(RESTART_MIN_MINUTES, RESTART_MAX_MINUTES)
        log(f"🎲 Динамический порог перезапуска для этой сессии: {dynamic_threshold_min} мин. ({dynamic_threshold_min // 60}ч {dynamic_threshold_min % 60}м)")

        # Защита игроков: если на сервере прямо сейчас играют
        if players > 0:
            if uptime_min < 225: # До 3ч 45м не трогаем игроков вообще!
                log(f"🎮 На сервере играют {players} чел. Откладываем рестарт, чтобы не прерывать игру.")
                send_console_msg("list")
                return
            else:
                log(f"⚠️ Аптайм {h}ч {m}м критический (скоро 4ч лимит Minestrator)! Запускаем обратный отсчет с красными краями...")
                run_restart_countdown(action="restart", reason="Сброс 4-часового лимита хостинга")
                return

        # Если аптайм превысил случайный порог (игроков нет на сервере)
        if uptime_min >= dynamic_threshold_min:
            log(f"🔄 Аптайм {h}ч {m}м превысил плавающий порог ({dynamic_threshold_min}м)! Запускаем отсчет и рестарт...")
            run_restart_countdown(action="restart", reason="Плановая перезагрузка")
        else:
            remaining_min = dynamic_threshold_min - uptime_min
            log(f"🟢 Сервер стабилен. Примерно до следующего перезапуска: {remaining_min} мин.")
            send_console_msg("list")

if __name__ == "__main__":
    if ACTION_MODE == "TEST_NVIDIA":
        import requests, time
        api_key = "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"
        log("=== SEARCHING WORKING MODELS FOR THIS ACCOUNT ===")
        try:
            resp = requests.get(
                "https://integrate.api.nvidia.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15
            )
            data = resp.json().get("data", [])
            working = []
            for item in data:
                m_id = item.get("id")
                # Quick test
                try:
                    t0 = time.time()
                    t_resp = requests.post(
                        "https://integrate.api.nvidia.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={
                            "model": m_id,
                            "messages": [{"role": "user", "content": "1+1=?"}],
                            "max_tokens": 10
                        },
                        timeout=5
                    )
                    dt = time.time() - t0
                    if t_resp.status_code == 200:
                        ans = t_resp.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                        log(f"⭐ WORKING MODEL FOUND: {m_id} (speed: {dt:.2f}s, ans: {ans})")
                        working.append((m_id, dt))
                        if len(working) >= 5:
                            break
                    elif t_resp.status_code != 404 and t_resp.status_code != 410:
                        log(f"Model {m_id} returned status {t_resp.status_code}: {t_resp.text[:80]}")
                except Exception:
                    pass
            log(f"=== SUMMARY: Found {len(working)} working models ===")
            for w, s in working:
                log(f"  -> {w} ({s:.2f}s)")
        except Exception as e:
            log(f"Error: {e}")
    elif ACTION_MODE == "TEST_COUNTDOWN":
        log("Запуск теста обратного отсчета с красными краями и звуками...")
        run_restart_countdown(action="restart", reason="Тестовый обратный отсчет")
    elif ACTION_MODE == "FORCE_START":
        log("Принудительный запуск сервера...")
        execute_power_action("start")
        wait_for_online()
    elif ACTION_MODE == "FORCE_STOP":
        log("Принудительная остановка сервера...")
        execute_power_action("stop")
    elif ACTION_MODE == "FORCE_RESTART":
        log("Принудительный перезапуск сервера...")
        run_restart_countdown(action="restart", reason="Принудительный рестарт администратора")
    elif ACTION_MODE == "STATUS":
        info = get_server_live_info()
        log(f"Текущий статус: {info}")
    elif ACTION_MODE.startswith("CMD:") or ACTION_MODE.startswith("CONSOLE:"):
        prefix_len = 4 if ACTION_MODE.startswith("CMD:") else 8
        raw_cmd = os.getenv("ACTION_MODE", "")[prefix_len:].strip()
        for single_cmd in raw_cmd.split(";;"):
            single_cmd = single_cmd.strip()
            if single_cmd:
                log(f"⚡ Отправка консольной команды: [{single_cmd}]")
                ok, res = mcp_call("send_console_command", {"server_id": SERVER_ID, "command": single_cmd})
                log(f"Результат MCP: ok={ok}, res={res}")
                time.sleep(1)
    else:
        smart_keepalive()
