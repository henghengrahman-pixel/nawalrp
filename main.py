# main.py
import asyncio
import logging
from datetime import datetime
import json
import os
import re as _re
from typing import List, Tuple, Dict
import uuid

import pytz
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
import aiohttp
from bs4 import BeautifulSoup

# ================== CONFIG ==================
TOKEN = os.getenv("BOT_TOKEN", "8311343564:AAGMF3lKzp2TyMptsrosQkGiRHKaP-dQ8TE")
ADMIN_IDS = [5397964203, 1293577945, 1781838636, 8472028508, 1623311993, 1849309185, 7230912053, 1623311993]
NAWALA_URL = "https://nawala.in/"

WIB = pytz.timezone("Asia/Jakarta")

CONFIG_FILE = "config.json"
DOMAINS_FILE = "domains.txt"
ALERT_ONLY_FROM_AUTO = True
STATUS_CACHE_FILE = "status_cache.json"

BATCH_SIZE = 30

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ================== BOT CORE ==================
bot = Bot(token=TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)

# ================== STATE (persisted) ==================
state = {"target_chat": None, "auto_interval": 0}
domains: List[str] = []
auto_task: asyncio.Task | None = None
shutdown_flag = False

REPORT_STORE: Dict[str, List[Tuple[str, str]]] = {}

# ================== UTIL PERSIST ==================
def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Gagal save config: {e}")

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                state.update(data or {})
        except Exception as e:
            logging.error(f"Gagal load config: {e}")

def save_domains():
    try:
        with open(DOMAINS_FILE, "w", encoding="utf-8") as f:
            for d in domains:
                f.write(d + "\n")
    except Exception as e:
        logging.error(f"Gagal save domains: {e}")

def load_domains():
    global domains
    if os.path.exists(DOMAINS_FILE):
        try:
            with open(DOMAINS_FILE, "r", encoding="utf-8") as f:
                ds = [l.strip() for l in f if l.strip()]
                domains = sorted(set(ds))
        except Exception as e:
            logging.error(f"Gagal load domains: {e}")

# ================== STATUS CACHE (persist) ==================
def load_status_cache() -> Dict[str, str]:
    if os.path.exists(STATUS_CACHE_FILE):
        try:
            with open(STATUS_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception as e:
            logging.error(f"Gagal load status cache: {e}")
    return {}

def save_status_cache(cache: Dict[str, str]):
    try:
        with open(STATUS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Gagal save status cache: {e}")

# ================== HELPERS ==================
ICONS = {"AMAN": "✅", "BLOKIR": "🟥", "ERROR": "🟨"}
ORDER = ["BLOKIR", "ERROR", "AMAN"]
DOMAIN_COL_WIDTH = 32

def now_wib():
    return datetime.now(WIB).strftime("%d %B %Y • %H:%M WIB")

def clean_domain(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("http://", "").replace("https://", "")
    s = s.split("/")[0].split(":")[0]
    return s

def looks_like_domain(s: str) -> bool:
    s = clean_domain(s)
    return bool(_re.match(r"^(?!-)[a-z0-9-]{1,63}(?<!-)\.[a-z0-9-]{2,}(?:\.[a-z0-9-]{2,})*$", s))

def map_status_text(text: str) -> str:
    """
    Update UI nawala.in sekarang biasanya pakai badge "Blocked" / "Aman".
    Kita tetap support format lama "Ada" / "Tidak Ada".
    Return: AMAN | BLOKIR | ERROR
    """
    t = (text or "").strip().lower()
    t = _re.sub(r"\s+", " ", t)

    if "tidak ada" in t:
        return "AMAN"
    if " ada" in f" {t}":
        # hati-hati: "tidak ada" sudah ditangani di atas
        return "BLOKIR"

    if "blocked" in t or "blokir" in t or "nawala" in t:
        return "BLOKIR"
    if "aman" in t or "safe" in t or "clean" in t or "not blocked" in t:
        return "AMAN"

    return "ERROR"

def _pad_domain(d: str, width: int = DOMAIN_COL_WIDTH) -> str:
    return (d[: width - 1] + "…") if len(d) > width else d.ljust(width)

def result_line(domain: str, status: str) -> str:
    icon = ICONS.get(status, "🟨")
    return f"<code>{icon} {_pad_domain(domain)} | {status}</code>"

def recap(rows: List[Tuple[str, str]]) -> str:
    total = len(rows)
    aman = sum(1 for _, s in rows if s == "AMAN")
    blok = sum(1 for _, s in rows if s == "BLOKIR")
    err = total - aman - blok
    return f"🧮 <b>Rekap:</b> {total} domain • {ICONS['AMAN']} Aman: {aman} • {ICONS['BLOKIR']} Blokir: {blok} • {ICONS['ERROR']} Error: {err}"

def chunked(seq: List[str], n: int) -> List[List[str]]:
    return [seq[i : i + n] for i in range(0, len(seq), n)]

# ================== PARSER HTML NAWALA ==================
def _parse_nawala_html_for_rows(html: str, requested_domains: List[str]) -> List[Tuple[str, str]]:
    """
    Parser tahan perubahan UI:
    - Cari tabel hasil yang headernya mengandung "Domain" dan "Status/Keterangan"
    - Ambil text badge (Blocked/Aman) atau text cell
    - Fallback: regex sekitar domain => (Blocked|Aman|Tidak Ada|Ada)
    """
    soup = BeautifulSoup(html, "html.parser")
    found_map: Dict[str, str] = {}

    def norm(s: str) -> str:
        return (s or "").strip().lower()

    # 1) Cari table yang punya header Domain + Status/Keterangan
    for table in soup.find_all("table"):
        headers: List[str] = []
        thead = table.find("thead")
        if thead:
            headers = [th.get_text(" ", strip=True) for th in thead.find_all(["th", "td"])]
        else:
            tr0 = table.find("tr")
            if tr0:
                headers = [c.get_text(" ", strip=True) for c in tr0.find_all(["th", "td"])]

        if not headers:
            continue

        hnorm = [norm(h) for h in headers]
        if not any("domain" in h or "situs" in h or "website" in h for h in hnorm):
            continue
        if not any("status" in h or "keterangan" in h for h in hnorm):
            continue

        try:
            idx_dom = next(i for i, h in enumerate(hnorm) if "domain" in h or "situs" in h or "website" in h)
        except StopIteration:
            idx_dom = 0
        try:
            idx_st = next(i for i, h in enumerate(hnorm) if "status" in h or "keterangan" in h)
        except StopIteration:
            idx_st = min(1, len(headers) - 1)

        tbodies = table.find_all("tbody") or [table]
        for tbody in tbodies:
            for tr in tbody.find_all("tr"):
                tds = tr.find_all("td")
                if not tds:
                    continue
                if len(tds) <= max(idx_dom, idx_st):
                    continue

                dom = clean_domain(tds[idx_dom].get_text(" ", strip=True))

                status_cell = tds[idx_st]
                badge_text = ""
                badge = status_cell.find(class_=_re.compile(r"badge", _re.I))
                if badge:
                    badge_text = badge.get_text(" ", strip=True)
                st_text = badge_text or status_cell.get_text(" ", strip=True)

                if looks_like_domain(dom):
                    found_map[dom] = map_status_text(st_text)

    # 2) Fallback regex dekat domain
    html_one = " ".join(html.split())
    for d in requested_domains:
        if d in found_map:
            continue
        pat = _re.compile(
            rf"{_re.escape(d)}.*?(blocked|aman|tidak\s*ada|ada)",
            _re.IGNORECASE | _re.DOTALL,
        )
        m = pat.search(html_one)
        if m:
            found_map[d] = map_status_text(m.group(1))

    # 3) Output urut sesuai input, kalau gak ketemu => ERROR
    out: List[Tuple[str, str]] = []
    for d in requested_domains:
        out.append((d, found_map.get(d, "ERROR")))
    return out

# ================== REQUEST HELPER (GET token + POST) ==================
async def _fetch_hidden_inputs(session: aiohttp.ClientSession) -> Dict[str, str]:
    """
    Kalau nawala.in nanti pakai hidden token (CSRF), ini bikin bot tetap jalan.
    """
    try:
        async with session.get(NAWALA_URL, timeout=30) as r:
            html = await r.text()
    except Exception:
        return {}

    soup = BeautifulSoup(html, "html.parser")
    data: Dict[str, str] = {}
    for inp in soup.find_all("input"):
        tp = (inp.get("type") or "").lower()
        name = inp.get("name")
        if tp == "hidden" and name:
            data[name] = inp.get("value") or ""
    return data

# ================== CEK KE NAWALA.IN ==================
async def cek_nawala(domains_in: List[str]) -> List[Tuple[str, str]]:
    ds = [clean_domain(d) for d in domains_in if looks_like_domain(d)]
    seen = set()
    uniq: List[str] = []
    for d in ds:
        if d and d not in seen:
            uniq.append(d)
            seen.add(d)
        if len(uniq) >= BATCH_SIZE:
            break
    if not uniq:
        return []

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": NAWALA_URL,
        "Origin": "https://nawala.in",
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        hidden = await _fetch_hidden_inputs(session)
        payload = {**hidden, "domains": "\n".join(uniq)}

        async with session.post(NAWALA_URL, data=payload, timeout=30, allow_redirects=True) as r:
            text = await r.text()

    rows = _parse_nawala_html_for_rows(text, uniq)
    return rows

async def cek_semua_dalam_batch(ds: List[str]) -> List[Tuple[str, str]]:
    hasil: List[Tuple[str, str]] = []
    batch: List[str] = []

    for d in sorted(set(map(clean_domain, ds))):
        if not looks_like_domain(d):
            continue
        batch.append(d)
        if len(batch) == BATCH_SIZE:
            try:
                hasil += await cek_nawala(batch)
            except Exception as e:
                logging.error(f"Gagal batch cek: {e}")
                hasil += [(x, "ERROR") for x in batch]
            batch = []
            await asyncio.sleep(1)

    if batch:
        try:
            hasil += await cek_nawala(batch)
        except Exception as e:
            logging.error(f"Gagal batch cek: {e}")
            hasil += [(x, "ERROR") for x in batch]

    return hasil

# ================== BUILDER LAPORAN ==================
def _group_rows(rows: List[Tuple[str, str]]):
    g = {"AMAN": [], "BLOKIR": [], "ERROR": []}
    for d, s in rows:
        s2 = s if s in g else "ERROR"
        g[s2].append(d)
    for k in g:
        g[k] = sorted(set(g[k]))
    return g

def _compose_message(rows: List[Tuple[str, str]], title: str) -> str:
    g = _group_rows(rows)
    head = (
        f"┏━━━ 🛰 <b>{title}</b>\n"
        f"┃ 🗓 {now_wib()}\n"
        f"┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    sections = []
    for key in ORDER:
        items = g[key]
        icon = ICONS[key]
        if not items:
            sections.append(f"<b>{icon} {key}</b>:\n<code>—</code>")
            continue
        lines = [result_line(d, key) for d in items]
        sections.append(f"<b>{icon} {key}</b>:\n" + "\n".join(lines))
    body = "\n\n".join(sections)
    tail = "\n\n" + recap(rows)
    return f"{head}\n\n{body}{tail}"

def _compose_message_filtered(rows: List[Tuple[str, str]], title: str, show_aman: bool = False) -> str:
    g = _group_rows(rows)
    head = (
        f"┏━━━ 🛰 <b>{title}</b>\n"
        f"┃ 🗓 {now_wib()}\n"
        f"┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    sections = []

    blok = g["BLOKIR"]
    if blok:
        sections.append(f"<b>{ICONS['BLOKIR']} BLOKIR</b>:\n" + "\n".join(result_line(d, "BLOKIR") for d in blok))
    else:
        sections.append(f"<b>{ICONS['BLOKIR']} BLOKIR</b>:\n<code>—</code>")

    err = g["ERROR"]
    if err:
        sections.append(f"<b>{ICONS['ERROR']} ERROR</b>:\n" + "\n".join(result_line(d, "ERROR") for d in err))

    if show_aman:
        aman = g["AMAN"]
        if aman:
            sections.append(f"<b>{ICONS['AMAN']} AMAN</b>:\n" + "\n".join(result_line(d, "AMAN") for d in aman))
        else:
            sections.append(f"<b>{ICONS['AMAN']} AMAN</b>:\n<code>—</code>")

    body = "\n\n".join(sections)
    tail = "\n\n" + recap(rows)
    return f"{head}\n\n{body}{tail}"

# ================== ALERT NAWALA ==================
async def notify_admins_blocked(rows: List[Tuple[str, str]], only_new: bool = True, source: str = "manual"):
    if ALERT_ONLY_FROM_AUTO and source != "auto":
        return
    if not rows:
        return

    cache = load_status_cache()
    latest: Dict[str, str] = {d: s for d, s in rows}
    blocked_now = [d for d, s in rows if s == "BLOKIR"]

    if only_new:
        newly_blocked = []
        for d in blocked_now:
            prev = (cache.get(d) or "").upper()
            if prev != "BLOKIR":
                newly_blocked.append(d)
        targets = newly_blocked
        title = "🔥 NAWALA (Baru Terblokir)"
    else:
        targets = blocked_now
        title = "🟥 NAWALA (Blokir Terdeteksi)"

    cache.update({d: latest[d] for d in latest})
    save_status_cache(cache)

    if not targets:
        return

    lines = "\n".join(f"• {d}" for d in sorted(set(targets)))
    alert_text = (
        f"🚨 <b>{title}</b>\n"
        f"🗓 {now_wib()}\n\n"
        f"Domain berikut masuk <b>BLOKIR / NAWALA</b>:\n{lines}\n\n"
        f"⚠️ Rekomendasi: segera redirect/replace ke link mirror baru."
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, alert_text, disable_web_page_preview=True)
        except Exception as e:
            logging.error(f"Gagal kirim alert ke admin {admin_id}: {e}")

# ================== SENDER (auto-split long msg) ==================
async def send_single_report_message(chat_id, rows: List[Tuple[str, str]], title: str):
    text = _compose_message(rows, title)
    await send_long_message(chat_id, text)

async def send_long_message(chat_id: int, text: str, limit: int = 4096):
    MAX = limit - 64
    if len(text) <= MAX:
        return await bot.send_message(chat_id, text, disable_web_page_preview=True)

    lines = text.splitlines(keepends=True)
    buf = ""
    part = 1
    for ln in lines:
        if len(buf) + len(ln) > MAX:
            await bot.send_message(chat_id, buf + "\n<code>⤵️ lanjut...</code>", disable_web_page_preview=True)
            buf = f"┏━━━ 🛰 <b>Laporan (lanjutan #{part})</b>\n┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            part += 1
        buf += ln
    if buf.strip():
        await bot.send_message(chat_id, buf, disable_web_page_preview=True)

# ================== AUTO LOOP ==================
async def auto_loop():
    global auto_task
    logging.info("Auto-cek started")
    try:
        while not shutdown_flag and state.get("auto_interval", 0) > 0:
            if not domains:
                await asyncio.sleep(max(10, state["auto_interval"] * 60))
                continue
            try:
                rows = await cek_semua_dalam_batch(domains)
                await notify_admins_blocked(rows, only_new=True, source="auto")
                target = state.get("target_chat")
                if target:
                    await send_single_report_message(target, rows, title=f"Auto Report / {state['auto_interval']} Menit")
            except Exception as e:
                logging.error(f"Auto-cek error: {e}")
            await asyncio.sleep(max(10, state["auto_interval"] * 60))
    finally:
        logging.info("Auto-cek stopped")
        auto_task = None

def start_auto():
    global auto_task
    if auto_task and not auto_task.done():
        return
    auto_task = asyncio.create_task(auto_loop())

def stop_auto():
    state["auto_interval"] = 0
    save_config()

# ================== AUTH HELPER ==================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ================== HANDLERS ==================
@dp.message_handler(commands=["start"])
async def start_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        return await message.reply("❌ Bot private. Hubungi admin.")
    info = (
        "🛰 <b>Nawala Checker</b>\n"
        f"📅 {now_wib()}\n\n"
        "➤ <b>Manual (1 laporan):</b>\n"
        "   /cek domain1.com domain2.com ... → 1 laporan gabungan\n"
        "   Kirim teks daftar domain → 1 laporan gabungan\n\n"
        "➤ <b>Auto (pakai file .txt satu domain per baris):</b>\n"
        "   Kirim file .txt → bot GANTI daftar lama dan auto-cek tiap N menit.\n"
        "   Laporan dikirim <b>pesan saja</b> (tanpa lampiran file).\n\n"
        "⚙️ <b>Perintah Admin:</b>\n"
        "• <code>/setgroup</code> @username atau -100ID\n"
        "• <code>/add</code> domain1.com domain2.com ...\n"
        "• <code>/list</code> / <code>/clearlist</code>\n"
        "• <code>/auto 10</code> (cek tiap 10 menit)\n"
        "• <code>/stopauto</code>\n"
        "• <code>/cekall</code>\n"
        "• <code>/cekfull ...</code> (BLOKIR dulu, tombol untuk AMAN)\n\n"
        "Ikon: ✅ AMAN • 🟥 BLOKIR • 🟨 ERROR\n"
        "Catatan: UI baru nawala.in menampilkan badge <b>Blocked</b> / <b>Aman</b>."
    )
    await message.reply(info)

async def process_and_reply_single(chat_id, inputs: List[str]):
    raw = [x for x in inputs if x.strip()]
    cleaned = []
    seen = set()
    for item in raw:
        d = clean_domain(item)
        if looks_like_domain(d) and d not in seen:
            cleaned.append(d)
            seen.add(d)
    if not cleaned:
        return await bot.send_message(chat_id, "⚠️ Tidak ada domain valid.")

    rows = await cek_semua_dalam_batch(cleaned)
    text = _compose_message(rows, title=f"Hasil Cek • {len(cleaned)} domain")
    await send_long_message(chat_id, text)

@dp.message_handler(commands=["cek"])
async def cek_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = [p for p in message.get_args().split() if p.strip()]
    if not parts:
        return await message.reply("Format: <code>/cek domain1.com domain2.com ...</code>")
    try:
        await process_and_reply_single(message.chat.id, parts)
    except Exception as e:
        await message.reply(f"⚠ Gagal cek: <code>{e}</code>")

@dp.message_handler(content_types=["text"])
async def paste_list(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    raw_items = []
    for line in text.splitlines():
        raw_items.extend(line.split())
    try:
        await process_and_reply_single(message.chat.id, raw_items)
    except Exception as e:
        await message.reply(f"⚠ Gagal cek: <code>{e}</code>")

@dp.message_handler(commands=["cekfull"])
async def cekfull_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = [p for p in message.get_args().split() if p.strip()]
    if not parts:
        return await message.reply("Format: <code>/cekfull domain1.com domain2.com ...</code>")

    cleaned = []
    seen = set()
    for item in parts:
        d = clean_domain(item)
        if looks_like_domain(d) and d not in seen:
            cleaned.append(d)
            seen.add(d)
    if not cleaned:
        return await message.reply("⚠️ Tidak ada domain valid.")

    try:
        rows = await cek_semua_dalam_batch(cleaned)
        token = uuid.uuid4().hex
        REPORT_STORE[token] = rows

        text = _compose_message_filtered(rows, title=f"Hasil Cek (BLOKIR dulu) • {len(cleaned)} domain", show_aman=False)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("Tampilkan AMAN (✅)", callback_data=f"show_aman:{token}"))
        await send_long_message(message.chat.id, text)
        await bot.send_message(message.chat.id, "Klik tombol di bawah untuk menampilkan AMAN:", reply_markup=kb)
    except Exception as e:
        await message.reply(f"⚠ Gagal cek: <code>{e}</code>")

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("show_aman:"))
async def on_show_aman(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Admin only", show_alert=True)
    token = cb.data.split(":", 1)[1]
    rows = REPORT_STORE.get(token)
    if not rows:
        return await cb.answer("Data sudah kedaluwarsa. Jalankan /cekfull lagi.", show_alert=True)
    text = _compose_message_filtered(rows, title="Bagian AMAN", show_aman=True)
    await send_long_message(cb.message.chat.id, text)
    await cb.answer("Bagian AMAN ditampilkan.")

@dp.message_handler(content_types=["document"])
async def txt_upload_auto_mode(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    doc = message.document
    fname = (doc.file_name or "").lower()
    if not fname.endswith(".txt"):
        return await message.reply("Kirim file .txt ya (satu domain per baris).")

    try:
        file = await bot.get_file(doc.file_id)
        file_obj = await bot.download_file(file.file_path)
        content = file_obj.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return await message.reply(f"⚠️ Gagal baca file: <code>{e}</code>")

    lines = [l.strip() for l in content.splitlines() if l.strip()]
    new_domains = []
    seen = set()
    for item in lines:
        d = clean_domain(item)
        if looks_like_domain(d) and d not in seen:
            new_domains.append(d)
            seen.add(d)

    if not new_domains:
        return await message.reply("⚠️ Tidak ada domain valid di file .txt.")

    global domains
    before_total = len(domains)
    domains = sorted(set(new_domains))
    save_domains()

    if not state.get("target_chat"):
        state["target_chat"] = message.chat.id
        save_config()

    if state.get("auto_interval", 0) == 0:
        state["auto_interval"] = 10
        save_config()
        start_auto()

    try:
        rows = await cek_semua_dalam_batch(domains)
        await send_single_report_message(message.chat.id, rows, title="Cek Awal (Replace TXT)")
        await notify_admins_blocked(rows, only_new=True, source="auto")
    except Exception as e:
        return await message.reply(f"⚠ Gagal cek awal: <code>{e}</code>")

    added = len(domains)
    await message.reply(
        f"🧹 Daftar lama: <b>{before_total}</b> → <b>diganti</b>\n"
        f"✅ Daftar baru tersimpan: <b>{added}</b>\n"
        f"⏱ Auto-cek: tiap <b>{state['auto_interval']} menit</b> → <b>{state['target_chat']}</b>\n"
        "Format laporan: <b>pesan saja</b> (tanpa lampiran file)"
    )

# ================== MGMT ==================
def parse_target(s: str):
    s = s.strip()
    if s.startswith("@"):
        return s
    try:
        return int(s)
    except Exception:
        return s

@dp.message_handler(commands=["setgroup"])
async def setgroup_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    arg = message.get_args().strip()
    if not arg:
        return await message.reply("Format: <code>/setgroup @username</code> atau <code>/setgroup -1001234567890</code>")
    state["target_chat"] = parse_target(arg)
    save_config()
    await message.reply(f"✅ Target grup/channel diset ke: <b>{state['target_chat']}</b>")

@dp.message_handler(commands=["add"])
async def add_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = [clean_domain(p) for p in message.get_args().split() if p.strip()]
    parts = [p for p in parts if looks_like_domain(p)]
    if not parts:
        return await message.reply("Format: <code>/add domain1.com domain2.com ...</code>")
    global domains
    before = len(domains)
    domains = sorted(set(domains + parts))
    save_domains()
    await message.reply(f"✅ Ditambah: {len(domains) - before} | Total: {len(domains)}")

@dp.message_handler(commands=["list"])
async def list_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    if not domains:
        return await message.reply("📂 Daftar kosong. Tambah via <code>/add</code> atau kirim teks/ .txt.")
    preview = "\n".join(f"• {d}" for d in domains[:50])
    more = f"\n… +{len(domains) - 50} lagi" if len(domains) > 50 else ""
    await message.reply(f"📂 <b>Daftar Auto-Cek ({len(domains)})</b>:\n{preview}{more}")

@dp.message_handler(commands=["clearlist"])
async def clearlist_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    domains.clear()
    save_domains()
    await message.reply("🧽 Daftar domain dibersihkan.")

@dp.message_handler(commands=["auto"])
async def auto_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    arg = message.get_args().strip()
    if not arg.isdigit():
        return await message.reply("Format: <code>/auto 10</code> (menit). Min 1 menit.")
    menit = max(1, int(arg))
    if not state.get("target_chat"):
        return await message.reply("Set target dulu pakai <code>/setgroup</code> ya.")
    if not domains:
        return await message.reply("Daftar domain masih kosong. Tambah dulu / kirim .txt.")
    state["auto_interval"] = menit
    save_config()
    start_auto()
    await message.reply(f"✅ Auto-cek ON tiap <b>{menit} menit</b> ke <b>{state['target_chat']}</b>.\nTip: <code>/stopauto</code> untuk matikan.")

@dp.message_handler(commands=["stopauto"])
async def stopauto_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    stop_auto()
    await message.reply("🛑 Auto-cek dimatikan.")

@dp.message_handler(commands=["cekall"])
async def cekall_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    if not domains:
        return await message.reply("📂 Daftar kosong.")
    rows = await cek_semua_dalam_batch(domains)
    await send_single_report_message(message.chat.id, rows, title="Cek Semua (Manual)")

# ================== LIFECYCLE ==================
async def on_startup(_):
    load_config()
    load_domains()
    _ = load_status_cache()
    if state.get("auto_interval", 0) > 0 and state.get("target_chat"):
        start_auto()
    logging.info("Bot started. Config: %s | domains: %d", state, len(domains))

async def on_shutdown(_):
    global shutdown_flag
    shutdown_flag = True
    if auto_task and not auto_task.done():
        auto_task.cancel()
        try:
            await auto_task
        except Exception:
            pass
    await bot.session.close()
    logging.info("Bot shutdown complete.")

# ================== RUN ==================
if __name__ == "__main__":
    logging.info("Starting Nawala Checker…")
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup, on_shutdown=on_shutdown)
