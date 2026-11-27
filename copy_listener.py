#!/usr/bin/env python3
"""
copy_listener.py — 把喜欢的帖子转发给这个号，它就自动搬运

两种用法，不用发链接：
  A) 有小号：小号常驻跑着，你把喜欢的帖子转发给小号
  B) 没小号：用主号自己跑，把喜欢的帖子转发到自己的“收藏夹”
     （Saved Messages），它就自动搬。不用小号，也不用发链接。

用法 B（没小号时用这个）：
  python3 copy_listener.py --self-mode \
      --api-id 12345678 --api-hash "xxxx"
  启动后在收藏夹发 /set_dest @你的频道 来设目标频道，
  也可以启动时直接加 --dest-channel "@你的频道"。
  然后在手机里把喜欢的帖子转发到“收藏夹”就行。

用法 A（有小号时用这个）：
  python3 copy_listener.py --dest-channel "@你的频道" --master-id 123456789 \
      --api-id 12345678 --api-hash "xxxx"
  然后把喜欢的帖子转发给小号。

搬完它会在收藏夹/私聊里回你：完成，新帖 ID、搬了多少组评论。
源帖没有评论串时也照样搬帖子本身（单视频、多图相册都行），
只是在目标频道里那条新帖的评论区是空的。

在 Telegram 里就能换目标频道，不用上服务器：
  /set_dest @你的新频道     换目标频道，之后转发的都搬到新频道
  /dest                     看当前目标频道是哪个
  /help                     看用法

为什么必须用用户号，不用 Bot 号：
  读评论串、发评论串都要用 GetDiscussionMessage / GetReplies，
  这两个接口只允许用户号调用，Bot 号做不了。转发给 Bot 号也搬不了评论。

风险提醒：
  用主号跑就是用主号做自动化，量大了容易被限流。先拿一两条测试，
  别一次搬太多；能接受这个风险再用主号跑，稳妥起见还是建议用小号。

缓存限制（防爆盘）：
  每发完一个文件/一批就立即删本地缓存，不等到最后才删，
  磁盘上最多只有当前这一批。单个文件超 --max-file-mb（默认 10240MB，10G）
  就跳过；一批总大小超 --max-cache-mb（默认 10240MB，10G）就改一张一张发；
  磁盘剩余小于 --min-free-mb（默认 500MB）就直接停。

流量限制（防超服务器总流量）：
  商家双向计费：每个文件下载 1 遍 + 上传 1 遍，流量按文件大小 x 2 算；
  只算出站的商家用 --traffic-mode out，改成 x 1。
  --traffic-limit-gb 每月限额，默认 6000（6T），0 = 不限。
  用量存在 traffic.json 里，按设置的重置日自动清零，跨重启不丢。
  默认每月 1 号清零，启动时加 --traffic-reset-day 15 就改成每月 15 号，
  或在 Telegram 里发 /set_traffic_day 15。
  Telegram 里发 /traffic 可随时查本账期已用 / 还剩多少。
  每天早上 8 点（北京时间）自动往收藏夹推一条昨日统计：
  昨日用了多少、本月累计用了多少、还剩多少。
  发 /today 可随时查当日用了多少。
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")

from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.tl.functions.messages import GetDiscussionMessageRequest
from telethon.tl.types import PeerChannel

DEST_FILE = Path("dest_channel.txt")
SETTINGS_FILE = Path("settings.json")


def load_dest_from_file():
    try:
        return DEST_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def save_dest_to_file(value):
    DEST_FILE.write_text(str(value), encoding="utf-8")


def load_settings():
    """settings.json：绑定频道 + 全部可改设置，重启也记得住。"""
    try:
        d = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return d
    except (OSError, ValueError, TypeError):
        pass
    return {}


def save_settings(d):
    try:
        SETTINGS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def parse_dest(value: str):
    value = value.strip()
    try:
        return int(value)
    except ValueError:
        return value


def parse_link(link: str):
    link = link.strip()
    m = re.search(r"(?:t\.me|telegram\.me)/c/(\d+)/(\d+)", link)
    if m:
        return int("-100" + m.group(1)), int(m.group(2))
    m = re.search(r"(?:t\.me|telegram\.me)/([^/]+)/(\d+)", link)
    if m:
        return m.group(1), int(m.group(2))
    return None, None


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def group_comments(messages):
    groups, current, current_gid = [], [], None
    for msg in messages:
        gid = getattr(msg, "grouped_id", None)
        if gid and gid == current_gid:
            current.append(msg)
        else:
            if current:
                groups.append(current)
            current = [msg]
            current_gid = gid if gid else None
    if current:
        groups.append(current)
    return groups


def author_name(msg):
    sender = getattr(msg, "sender", None)
    if sender is None:
        return ""
    first = getattr(sender, "first_name", "") or ""
    last = getattr(sender, "last_name", "") or ""
    return (first + " " + last).strip() or getattr(sender, "username", "") or ""


def extract_buttons(msg):
    """源帖下面的 inline 按钮（第 3 种）：只搬 URL 按钮，回调按钮搬不了就跳过。
    返回 Telethon 的 Button 列表，没有就返回 None。"""
    markup = getattr(msg, "reply_markup", None)
    if not markup or not getattr(markup, "rows", None):
        return None
    rows = []
    for row in markup.rows:
        brow = []
        for b in getattr(row, "buttons", []):
            url = getattr(b, "url", None)
            text = getattr(b, "text", "") or ""
            if url and text:
                brow.append(Button.url(text, url))
        if brow:
            rows.append(brow)
    return rows or None


def extract_entities(msg):
    """源帖标题里的格式（第 2 种）：一个字下面藏着链接、加粗等。
    直接用原消息的 entities，send 时加 formatting_entities + parse_mode=None 原样搬。"""
    ents = getattr(msg, "entities", None)
    return list(ents) if ents else None


async def send_with_floodwait(coro_factory, what="发送"):
    for attempt in range(5):
        try:
            return await coro_factory()
        except FloodWaitError as e:
            wait = e.seconds + 5
            print(f"[限流] {what} FloodWait，等 {wait} 秒", flush=True)
            await asyncio.sleep(wait)
    return await coro_factory()


def delete_files(paths):
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass


def msg_file_size_mb(msg):
    f = getattr(msg, "file", None)
    if f is None or getattr(f, "size", None) is None:
        return 0
    return f.size / (1024 * 1024)


def check_disk(tmpdir, min_free_mb, need_mb=0):
    free = shutil.disk_usage(tmpdir).free / (1024 * 1024)
    if free - need_mb < min_free_mb:
        raise RuntimeError(f"磁盘剩余不足：剩余 {free:.0f}MB，需要预留 {min_free_mb}MB，已停止。")


# ---------- 流量限制 ----------

def traffic_mult(mode):
    return 2 if mode == "both" else 1


def _today_str():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _month_str():
    return datetime.now(TZ).strftime("%Y-%m")


def period_start_for(today, reset_day):
    """按重置日算当前账期开始日。reset_day=1 就是自然月，=15 就是每月15号到下月14号。
    reset_day 只允许 1-28，避免 2 月没有 29/30/31 号的问题。"""
    reset_day = max(1, min(28, int(reset_day or 1)))
    if today.day >= reset_day:
        return today.replace(day=reset_day)
    if today.month == 1:
        return today.replace(year=today.year - 1, month=12, day=reset_day)
    return today.replace(month=today.month - 1, day=reset_day)


def next_reset_for(today, reset_day):
    """下次清零日。"""
    start = period_start_for(today, reset_day)
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


def load_traffic(path):
    """兼容旧版：返回 (month, used_bytes)。"""
    st = load_traffic_state(path)
    return st["month"], st["used"]


def load_traffic_state(path, reset_day=None):
    """读 traffic.json。到了设置的重置日就清零。返回 dict：
    {"month": "2026-11-01", "reset_day": 1, "used": int,
     "days": {"2026-11-27": {"bytes": int, "files": int}}, "last_report": "2026-11-26"}
    month 字段现在存的是账期开始日，不是自然月。"""
    now = datetime.now(TZ)
    today = now.date()
    stored_reset = 1
    stored = {}
    try:
        stored = json.loads(Path(path).read_text(encoding="utf-8"))
        stored_reset = int(stored.get("reset_day", 1) or 1)
    except (OSError, ValueError, TypeError):
        stored = {}
    if reset_day is not None:
        stored_reset = int(reset_day)
    stored_reset = max(1, min(28, stored_reset))
    period_start = period_start_for(today, stored_reset)
    period_key = period_start.strftime("%Y-%m-%d")
    if stored.get("month") == period_key or stored.get("period") == period_key:
        return {"month": period_key, "reset_day": stored_reset,
                "used": int(stored.get("used_bytes", 0)),
                "days": stored.get("days", {}) or {},
                "last_report": stored.get("last_report_date", "") or ""}
    return {"month": period_key, "reset_day": stored_reset,
            "used": 0, "days": {}, "last_report": stored.get("last_report_date", "") or ""}


def save_traffic(path, month, used_bytes):
    st = load_traffic_state(path)
    st["month"], st["used"] = month, int(used_bytes)
    save_traffic_state(path, st)


def save_traffic_state(path, state):
    try:
        Path(path).write_text(json.dumps({
            "month": state["month"], "period": state["month"],
            "reset_day": int(state.get("reset_day", 1)),
            "used_bytes": int(state["used"]),
            "days": state.get("days", {}),
            "last_report_date": state.get("last_report", "")}, ensure_ascii=False),
            encoding="utf-8")
    except OSError:
        pass


def check_and_reserve_traffic(path, month, used_bytes, limit_gb, mode, size_bytes):
    """旧版入口（手动版用）：只记账期总量。"""
    st = {"month": month, "used": used_bytes, "days": {}, "last_report": "", "reset_day": 1}
    new_used = check_and_reserve_traffic_state(path, st, limit_gb, mode, size_bytes)
    return new_used


def check_and_reserve_traffic_state(path, state, limit_gb, mode, size_bytes):
    """下载前检查：本账期已用 + 这个文件（下载+上传）会不会超限。
    超了就抛错停下；没超就先把这份流量记上（账期总量 + 当日）并保存。"""
    if not limit_gb:
        return state["used"]
    need = int(size_bytes) * traffic_mult(mode)
    limit_bytes = int(limit_gb * 1024 * 1024 * 1024)
    if state["used"] + need > limit_bytes:
        raise RuntimeError(
            f"流量到限：本账期已用 {state['used'] / 1024**3:.2f}GB / 限额 {limit_gb}GB，"
            f"这个文件还要 {need / 1024**3:.2f}GB（文件 {size_bytes / 1024**3:.2f}GB x "
            f"{traffic_mult(mode)}），已停止。请等下个账期或调大 --traffic-limit-gb。")
    state["used"] += need
    day = _today_str()
    d = state.setdefault("days", {}).setdefault(day, {"bytes": 0, "files": 0})
    d["bytes"] += need
    d["files"] += 1
    save_traffic_state(path, state)
    return state["used"]


def daily_report_text(state, limit_gb, mode, day_str):
    """给某一天的日报文字。day_str 如 2026-11-26。"""
    d = (state.get("days", {}) or {}).get(day_str, {"bytes": 0, "files": 0})
    used_gb = state["used"] / 1024**3
    if limit_gb:
        remain = limit_gb - used_gb
        return (f"昨日流量统计（{day_str}）：\n"
                f"昨日用了 {d['bytes'] / 1024**3:.2f}GB，搬了 {d['files']} 个文件\n"
                f"本账期（{state['month']} 起，每月 {state.get('reset_day', 1)} 号清零）"
                f"累计用了 {used_gb:.2f}GB / {limit_gb}GB，还剩 {remain:.2f}GB\n"
                f"（{mode}，x{traffic_mult(mode)}）")
    return (f"昨日流量统计（{day_str}）：\n"
            f"昨日用了 {d['bytes'] / 1024**3:.2f}GB，搬了 {d['files']} 个文件\n"
            f"本账期累计用了 {used_gb:.2f}GB（不限额）")


async def copy_one(client, src_ref, src_post_id, dest_channel, tmpdir, limit=0,
                   max_file_mb=10240, max_cache_mb=10240, min_free_mb=500,
                   traffic_file="traffic.json", traffic_limit_gb=6000, traffic_mode="both",
                   traffic_state=None):
    """搬一条帖子 + 它的评论串（如果有）。返回 (新帖ID, 源评论条数, 已搬组数)。
    源帖没有评论串时也照样搬帖子本身，返回 (新帖ID, 0, 0)。"""
    if traffic_state is None:
        traffic_state = load_traffic_state(traffic_file)
    src_entity = await client.get_entity(src_ref)
    src_post = await client.get_messages(src_entity, ids=src_post_id)
    if not src_post:
        raise ValueError(f"找不到源帖 {src_post_id}")

    # --- 源帖本身可能是相册（多图一组）：把同 grouped_id 的都找出来一起搬 ---
    src_group = [src_post]
    src_gid = getattr(src_post, "grouped_id", None)
    if src_gid:
        try:
            async for m in client.iter_messages(src_entity, min_id=src_post_id - 10,
                                               max_id=src_post_id + 10):
                if getattr(m, "grouped_id", None) == src_gid and m.id != src_post_id:
                    src_group.append(m)
            src_group.sort(key=lambda x: x.id)
        except Exception:
            src_group = [src_post]
    if len(src_group) > 1:
        print(f"源帖是相册，共 {len(src_group)} 张，一起搬", flush=True)

    # --- 源评论串：有就读，没有也不报错，只搬帖子本身 ---
    comments = []
    has_comments = False
    try:
        disc = await client(GetDiscussionMessageRequest(peer=src_entity, msg_id=src_post_id))
        if disc.messages:
            src_root = disc.messages[0]
            print(f"源帖 {src_post_id}，讨论串根 {src_root.id}，开始读评论……", flush=True)
            has_comments = True
            async for m in client.iter_messages(src_entity, reply_to=src_post_id):
                comments.append(m)
                if limit and len(comments) >= limit:
                    break
            comments.reverse()
            print(f"源评论共 {len(comments)} 条", flush=True)
        else:
            print("源帖没有评论串，只搬帖子本身", flush=True)
    except Exception as e:
        print(f"源帖没有评论串（{e}），只搬帖子本身", flush=True)
        comments = []
        has_comments = False

    # 目标频道发新帖（重新下载再上传，不带转发来源）
    # 第 2 种：标题里一个字下面藏着链接、加粗等，用原 entities 原样搬
    # 第 3 种：图片下面的 inline 按钮，只搬 URL 按钮
    src_entities = extract_entities(src_group[0])
    src_buttons = extract_buttons(src_group[0])
    if src_post.media:
        src_files = []
        caption = src_group[0].text or None
        for m in src_group:
            if not m.media:
                continue
            if max_file_mb and msg_file_size_mb(m) > max_file_mb:
                raise ValueError(f"源帖文件 {msg_file_size_mb(m):.0f}MB 超过上限 {max_file_mb}MB，已跳过")
            traffic_state["used"] = check_and_reserve_traffic_state(
                traffic_file, traffic_state,
                traffic_limit_gb, traffic_mode, int(m.file.size or 0))
            check_disk(tmpdir, min_free_mb, msg_file_size_mb(m))
            p = await client.download_media(m, file=tmpdir)
            if p:
                src_files.append(p)
        if not src_files:
            raise ValueError("源帖没有能下载的文件")
        try:
            if len(src_files) == 1:
                dest_post = await send_with_floodwait(
                    lambda: client.send_file(dest_channel, src_files[0], caption=caption,
                                             formatting_entities=src_entities, parse_mode=None,
                                             buttons=src_buttons),
                    "发新帖")
            else:
                # 相册：最多 10 张一批，超了拆批（第一批带标题）
                dest_post = None
                for batch_no, batch in enumerate(chunks(src_files, 10), 1):
                    cap = caption if batch_no == 1 else None
                    # 标题、链接、按钮只在第一批带，后面的批不带
                    cap_ents = src_entities if batch_no == 1 else None
                    cap_btns = src_buttons if batch_no == 1 else None
                    batch_mb = sum(Path(p).stat().st_size for p in batch if Path(p).exists()) / (1024 * 1024)
                    if batch_mb > max_cache_mb and len(batch) > 1:
                        for single in batch:
                            r = await send_with_floodwait(
                                lambda: client.send_file(dest_channel, single, caption=cap,
                                                         formatting_entities=cap_ents, parse_mode=None,
                                                         buttons=cap_btns),
                                "发新帖单张")
                            cap = None
                            cap_ents = None
                            cap_btns = None
                            if dest_post is None:
                                dest_post = r
                    else:
                        r = await send_with_floodwait(
                            lambda: client.send_file(dest_channel, batch, caption=cap,
                                                     formatting_entities=cap_ents, parse_mode=None,
                                                     buttons=cap_btns),
                            "发新帖")
                        if dest_post is None:
                            # send_file 传列表时返回的是 list，取第一条当新帖 ID
                            dest_post = r[0] if isinstance(r, list) else r
        finally:
            delete_files(src_files)
    else:
        dest_post = await send_with_floodwait(
            lambda: client.send_message(dest_channel, src_post.text or "(无文字)",
                                        formatting_entities=src_entities, parse_mode=None,
                                        buttons=src_buttons),
            "发新帖")
    print(f"目标新帖已发：{dest_post.id}", flush=True)

    # 没有评论串就到此为止：新帖在目标频道里会自动带一个空评论区（只要目标频道绑了讨论组）
    if not has_comments:
        return dest_post.id, 0, 0

    await asyncio.sleep(2)
    dest_disc = await client(GetDiscussionMessageRequest(
        peer=await client.get_entity(dest_channel), msg_id=dest_post.id))
    if not dest_disc.messages:
        raise ValueError("拿不到目标帖的讨论串，请确认目标频道已绑讨论组")
    print(f"目标讨论串根：{dest_disc.messages[0].id}", flush=True)

    # 逐组搬进评论区，超 10 张的相册自动拆批，总条数不限
    groups = group_comments(comments)
    done = 0
    for gi, group in enumerate(groups, 1):
        if not group[0].media:
            text = group[0].text or ""
            if not text.strip():
                continue
            prefix = author_name(group[0])
            body = f"{prefix}：{text}" if prefix else text
            await send_with_floodwait(
                lambda: client.send_message(dest_channel, body, comment_to=dest_post.id),
                f"文字评论 {gi}")
            done += 1
            print(f"  [{gi}/{len(groups)}] 文字评论已发", flush=True)
            await asyncio.sleep(1)
            continue
        files, caption = [], group[0].text or None
        cap_ents = extract_entities(group[0])
        skipped = 0
        for m in group:
            if m.media:
                if max_file_mb and msg_file_size_mb(m) > max_file_mb:
                    print(f"  跳过超大文件：{msg_file_size_mb(m):.0f}MB > {max_file_mb}MB（消息 {m.id}）", flush=True)
                    skipped += 1
                    continue
                traffic_state["used"] = check_and_reserve_traffic_state(
                    traffic_file, traffic_state,
                    traffic_limit_gb, traffic_mode, int(m.file.size or 0))
                check_disk(tmpdir, min_free_mb, msg_file_size_mb(m))
                p = await client.download_media(m, file=tmpdir)
                if p:
                    files.append(p)
        if not files:
            continue
        for batch_no, batch in enumerate(chunks(files, 10), 1):
            batch_mb = sum(Path(p).stat().st_size for p in batch if Path(p).exists()) / (1024 * 1024)
            try:
                if batch_mb > max_cache_mb and len(batch) > 1:
                    cap = caption
                    cap_e = cap_ents
                    for single in batch:
                        await send_with_floodwait(
                            lambda: client.send_file(dest_channel, single, caption=cap,
                                                     formatting_entities=cap_e, parse_mode=None,
                                                     comment_to=dest_post.id),
                            f"媒体评论 {gi} 单张")
                        cap = None
                        cap_e = None
                        delete_files([single])
                        await asyncio.sleep(1)
                else:
                    await send_with_floodwait(
                        lambda: client.send_file(dest_channel, batch,
                                                 caption=caption if batch_no == 1 else None,
                                                 formatting_entities=cap_ents if batch_no == 1 else None,
                                                 parse_mode=None,
                                                 comment_to=dest_post.id),
                        f"媒体评论 {gi} 第 {batch_no} 批")
            finally:
                delete_files(batch)
            print(f"  [{gi}/{len(groups)}] 媒体评论已发（{len(batch)} 张）", flush=True)
            await asyncio.sleep(1)
        done += 1
    return dest_post.id, len(comments), done


async def main():
    ap = argparse.ArgumentParser(description="转发帖子给这个号，自动搬运带评论区的帖子")
    ap.add_argument("--dest-channel", default=os.environ.get("TG_DEST_CHANNEL", ""),
                    help="目标频道，如 @my_channel 或 -100...。可以不给，启动后在收藏夹发 /set_dest @新频道 来设")
    ap.add_argument("--master-id", type=int, default=int(os.environ.get("TG_MASTER_ID", "0") or 0),
                    help="有小号时填：只有这个用户 ID 发来的转发才会触发（就是你主号的数字 ID）；"
                         "用 --self-mode 时不用填，默认就是登录的这个号自己")
    ap.add_argument("--self-mode", action="store_true",
                    help="没小号时用：用登录的这个主号自己跑，监听自己的收藏夹，"
                         "把帖子转发到收藏夹就触发，不用填 --master-id")
    ap.add_argument("--api-id", type=int, default=int(os.environ.get("TG_API_ID", "0") or 0))
    ap.add_argument("--api-hash", default=os.environ.get("TG_API_HASH", ""))
    ap.add_argument("--phone", default=os.environ.get("TG_PHONE", ""),
                    help="手机号，带 +86，如 +86138xxxxxxx。不填就运行时问你")
    ap.add_argument("--force-sms", action="store_true",
                    help="默认强制走应用内消息（777000），不走短信。加这个才强制走短信")
    ap.add_argument("--bot-token", default=os.environ.get("TG_BOT_TOKEN", ""),
                    help="Bot Token（BotFather 建的真 Bot）。给了就开启 Bot 触发：把帖子转发给这个 Bot 就开始搬，转发到 Bot 会带来源，第三方转发到收藏夹不带来源时用这个")
    ap.add_argument("--bot-session", default="bot_listener",
                    help="Bot 的 session 文件名，默认 bot_listener")
    ap.add_argument("--session", default="copy_listener")
    ap.add_argument("--limit", type=int, default=0, help="每条只搬前 N 条评论，0 = 全部")
    ap.add_argument("--max-file-mb", type=int, default=10240, help="单个文件超过这个 MB 就跳过，默认 10240（10G）")
    ap.add_argument("--max-cache-mb", type=int, default=10240, help="一批缓存总大小超过这个 MB 就改一张一张发，默认 10240（10G）")
    ap.add_argument("--min-free-mb", type=int, default=500, help="磁盘剩余小于这个 MB 就停，默认 500")
    ap.add_argument("--traffic-limit-gb", type=float, default=6000, help="每月流量限额 GB，默认 6000（6T），0 = 不限")
    ap.add_argument("--traffic-mode", choices=["both", "out"], default="both",
                    help="both=双向计费按文件大小x2，out=只算出站按x1，默认 both")
    ap.add_argument("--traffic-file", default="traffic.json", help="流量用量记录文件，默认 traffic.json")
    ap.add_argument("--traffic-reset-day", type=int, default=None, help="每月几号清零流量，1-28，默认 1（每月1号）。不填就用 traffic.json 里上次存的")
    ap.add_argument("--daily-report-hour", type=int, default=8, help="每天几点（北京时间）自动推昨日流量统计，默认 8，0 = 不推")
    args = ap.parse_args()

    traffic_state = load_traffic_state(args.traffic_file, reset_day=args.traffic_reset_day)
    # 命令行传了重置日就存下来，之后 /set_traffic_day 和重启都记得住
    if args.traffic_reset_day is not None:
        save_traffic_state(args.traffic_file, traffic_state)

    if not args.api_id or not args.api_hash:
        print("缺 api_id / api_hash", file=sys.stderr)
        sys.exit(2)
    # 全部可改设置：先读 settings.json 里存的，再用启动参数覆盖（启动参数只在第一次有用，之后以 Bot 菜单改的为准）
    _saved_settings = load_settings()
    settings_holder = {
        "traffic_limit_gb": float(_saved_settings.get("traffic_limit_gb", args.traffic_limit_gb or 6000)),
        "traffic_mode": _saved_settings.get("traffic_mode", args.traffic_mode or "both"),
        "max_file_mb": int(_saved_settings.get("max_file_mb", args.max_file_mb or 10240)),
        "max_cache_mb": int(_saved_settings.get("max_cache_mb", args.max_cache_mb or 10240)),
        "min_free_mb": int(_saved_settings.get("min_free_mb", args.min_free_mb or 500)),
        "daily_report_hour": int(_saved_settings.get("daily_report_hour", args.daily_report_hour if args.daily_report_hour is not None else 8)),
        "bound_channels": list(_saved_settings.get("bound_channels", []) or []),
    }
    # 启动参数如果和默认值不一样，说明这次是特意传的，存下来
    if args.traffic_limit_gb:
        settings_holder["traffic_limit_gb"] = float(args.traffic_limit_gb)
    if args.traffic_mode:
        settings_holder["traffic_mode"] = args.traffic_mode
    save_settings(settings_holder)
    if settings_holder["traffic_limit_gb"]:
        _nxt = next_reset_for(datetime.now(TZ).date(), traffic_state["reset_day"])
        print(f"流量：本账期 {traffic_state['month']} 起已用 {traffic_state['used'] / 1024**3:.2f}GB / "
              f"限额 {settings_holder['traffic_limit_gb']}GB（{settings_holder['traffic_mode']}，x{traffic_mult(settings_holder['traffic_mode'])}），"
              f"每月 {traffic_state['reset_day']} 号清零，下次 {_nxt}，"
              f"每日 {settings_holder['daily_report_hour']} 点自动推昨日统计", flush=True)
    dest_arg = (args.dest_channel or "").strip()
    if dest_arg:
        dest_channel = parse_dest(dest_arg)
        save_dest_to_file(dest_channel)
    else:
        saved = load_dest_from_file()
        if saved:
            dest_channel = parse_dest(saved)
        else:
            # 第一次也可以不给，启动后在收藏夹发 /set_dest 来设
            dest_channel = None
            print("还没设目标频道：启动后在收藏夹发 /set_dest @新频道 来设，设好再转发。", flush=True)
    if not args.self_mode and not args.master_id:
        print("缺 --master-id，或改用 --self-mode（没小号时用主号+收藏夹）", file=sys.stderr)
        sys.exit(2)
    # 用一个可变的 holder 存目标频道，/set_dest 改的就是它，worker 每次搬运都读最新的
    dest_holder = {"channel": dest_channel}

    client = TelegramClient(args.session, args.api_id, args.api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        # 手动发码，强制走应用内消息（777000），不走短信
        phone = (args.phone or "").strip() or input("手机号 (+86...): ").strip()
        sent = await client.send_code_request(phone, force_sms=args.force_sms)
        _t = type(sent.type).__name__ if getattr(sent, "type", None) else "应用内消息"
        if args.force_sms:
            print(f"验证码已强制发短信，请看短信：{_t}", flush=True)
        else:
            print(f"验证码已发到应用内消息，请打开手机 Telegram 看 777000 的消息：{_t}", flush=True)
        code = input("请输入验证码: ").strip()
        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            pwd = input("请输入两步验证密码: ").strip()
            await client.sign_in(password=pwd)
    me = await client.get_me()
    master_id = me.id if args.self_mode else args.master_id
    _dest_txt = dest_holder["channel"] if dest_holder["channel"] else "还没设，在收藏夹发 /set_dest @新频道"
    print(f"监听号已登录：{me.id}，目标频道：{_dest_txt}，只认：{master_id}"
          f"{'（主号+收藏夹模式）' if args.self_mode else ''}", flush=True)
    if args.self_mode:
        print("用法：把喜欢的频道帖子转发到自己的“收藏夹”就行，不用发链接。", flush=True)
    else:
        print("用法：把喜欢的频道帖子转发给这个号，或把帖子链接发给它。", flush=True)
    print("设频道：在 Telegram 里发 /set_dest @新频道 即可，不用上服务器。", flush=True)

    # Bot 触发（第 2 种）：给了 --bot-token 就再起一个 Bot 客户端
    # 用户把帖子转发给这个 Bot，Bot 收到带来源的转发就入队，真正搬运还是用上面的用户号
    bot_client = None
    bot_me = None
    if (args.bot_token or "").strip():
        bot_client = TelegramClient(args.bot_session, args.api_id, args.api_hash)
        await bot_client.start(bot_token=args.bot_token.strip())
        bot_me = await bot_client.get_me()
        print(f"Bot 已登录：@{getattr(bot_me, 'username', '')}（{bot_me.id}），把帖子转发给这个 Bot 就开始搬。", flush=True)
        print("Bot 里也能发命令：/set_dest @新频道、/dest、/traffic、/today、/help", flush=True)

    queue = asyncio.Queue()
    recent = {}  # (频道, 帖ID) -> 时间戳，去重：同一条 5 分钟内只搬一次

    async def worker():
        while True:
            event, src_ref, src_post_id = await queue.get()
            tmpdir = Path(tempfile.mkdtemp(prefix="copy_listener_"))
            try:
                dest_now = dest_holder["channel"]
                if not dest_now:
                    await event.reply("还没设目标频道，先发 /set_dest @新频道 来设，设好再转发这条。")
                    continue
                await event.reply(f"收到，开始搬 {src_ref} / {src_post_id} 到 {dest_now}……")
                dest_id, total, done = await copy_one(
                    client, src_ref, src_post_id, dest_now, tmpdir, limit=args.limit,
                    max_file_mb=settings_holder["max_file_mb"], max_cache_mb=settings_holder["max_cache_mb"],
                    min_free_mb=settings_holder["min_free_mb"],
                    traffic_file=args.traffic_file, traffic_limit_gb=settings_holder["traffic_limit_gb"],
                    traffic_mode=settings_holder["traffic_mode"], traffic_state=traffic_state)
                if total == 0:
                    await event.reply(f"搬完了：新帖 {dest_id}（在 {dest_now}），源帖没有评论串，只搬了帖子本身。")
                else:
                    await event.reply(f"搬完了：新帖 {dest_id}（在 {dest_now}），源评论 {total} 条，已搬 {done} 组。")
            except Exception as e:
                print(f"搬运失败 {src_ref}/{src_post_id}：{e}", flush=True)
                try:
                    await event.reply(f"搬失败了：{e}")
                except Exception:
                    pass
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
                queue.task_done()

    asyncio.create_task(worker())

    async def daily_report_loop():
        """每天早上 8 点（北京时间）推昨日流量统计到收藏夹/主号私聊。
        启动时如果昨天已过 8 点还没推过，就先补推一次，避免重启漏推。"""
        target = me.id if args.self_mode else master_id
        while True:
            try:
                _hour = int(settings_holder.get("daily_report_hour", 8) or 0)
                if not _hour:
                    await asyncio.sleep(3600)
                    continue
                now = datetime.now(TZ)
                # 昨天的日期
                yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
                # 今天 8 点
                today_report = now.replace(hour=_hour, minute=0, second=0, microsecond=0)
                if now >= today_report and traffic_state.get("last_report") != yesterday:
                    # 到了今天 8 点还没推昨天，就推
                    text = daily_report_text(traffic_state, settings_holder["traffic_limit_gb"], settings_holder["traffic_mode"], yesterday)
                    # 只往 Bot 里推，不往收藏夹里推了
                    if bot_client is not None:
                        await bot_client.send_message(master_id, text)
                    else:
                        await client.send_message(target, text)
                    traffic_state["last_report"] = yesterday
                    save_traffic_state(args.traffic_file, traffic_state)
                    print(f"已推昨日流量统计：{yesterday}", flush=True)
                # 算下一次 8 点
                nxt = today_report if now < today_report else today_report + timedelta(days=1)
                await asyncio.sleep(max(60, (nxt - datetime.now(TZ)).total_seconds()))
            except Exception as e:
                print(f"日报推送出错：{e}，60 秒后重试", flush=True)
                await asyncio.sleep(60)

    asyncio.create_task(daily_report_loop())

    # 命令 + 转发处理，供用户号和 Bot 共用：Bot 收到带来源的转发也走这里入队
    # 菜单输入状态：点菜单按钮后，下一步发的消息当成那个设置的输入值
    pending_input = {"type": None}

    def persist_settings():
        save_settings(settings_holder)

    def bound_list_text():
        b = settings_holder.get("bound_channels", []) or []
        if not b:
            return "还没绑定频道，发 /bind @频道 来绑定，最多绑 8 个。"
        lines = []
        for i, c in enumerate(b, 1):
            cur = " ← 当前" if str(c.get("id")) == str(dest_holder["channel"]) else ""
            lines.append(f"{i}. {c.get('title') or c.get('id')}（{c.get('id')}）{cur}")
        return "已绑定频道：\n" + "\n".join(lines)

    async def send_menu(event):
        """Bot 菜单：/start 后显示，点按钮设置，不用记命令。"""
        b = settings_holder.get("bound_channels", []) or []
        rows = []
        # 绑定频道切换：每个已绑定频道一个按钮，点一下就切过去
        for c in b[:8]:
            rows.append([Button.inline(f"📺 {c.get('title') or c.get('id')}", f"dest:{c.get('id')}".encode()[:64])])
        rows.append([Button.inline("➕ 绑定新频道", b"bind_new"), Button.inline("🗑️ 删除绑定", b"del_bind")])
        rows.append([Button.inline("📊 流量", b"menu_traffic"), Button.inline("📅 今日", b"menu_today")])
        rows.append([Button.inline("⚙️ 流量限额", b"menu_limit"), Button.inline("🔄 清零日", b"menu_day")])
        rows.append([Button.inline("📁 单文件上限", b"menu_maxfile"), Button.inline("🗂️ 缓存上限", b"menu_maxcache")])
        rows.append([Button.inline("💾 磁盘预留", b"menu_minfree"), Button.inline("⏰ 日报时间", b"menu_hour")])
        rows.append([Button.inline("🔀 流量计费", b"menu_mode"), Button.inline("❓ 帮助", b"menu_help")])
        _d_raw = dest_holder["channel"]
        _d = "还没设"
        if _d_raw:
            # 先在已绑定里找名字，找到了就显示名字，不显示数字
            _found = None
            for _c in b:
                if str(_c.get("id")) == str(_d_raw):
                    _found = _c.get("title")
                    break
            if _found:
                _d = _found
            else:
                # 没绑定过就现查一下名字，查不到才显示数字
                try:
                    _ent = await client.get_entity(parse_dest(str(_d_raw)))
                    _d = getattr(_ent, "title", None) or getattr(_ent, "username", None) or str(_d_raw)
                except Exception:
                    _d = str(_d_raw)
        await event.reply(
            f"菜单（当前目标：{_d}，已绑 {len(b)} 个频道）\n"
            f"点下面按钮切换频道或改设置，改完重启也记得住。",
            buttons=rows)

    async def process_event(event):
        text = (event.message.text or "").strip()
        # 菜单等待输入：点按钮后发的下一条消息当成输入值
        if pending_input.get("type") and not text.startswith("/"):
            ptype = pending_input["type"]
            pending_input["type"] = None
            if ptype == "bind":
                new_dest = parse_dest(text)
                try:
                    ent = await client.get_entity(new_dest)
                    title = getattr(ent, "title", None) or getattr(ent, "username", None) or str(new_dest)
                except Exception as e:
                    await event.reply(f"绑定失败，找不到这个频道：{e}")
                    return
                b = settings_holder.get("bound_channels", []) or []
                if len(b) >= 8:
                    await event.reply("最多绑 8 个频道，先发 /unbind @频道 解绑一个再绑。")
                    return
                if any(str(c.get("id")) == str(new_dest) for c in b):
                    await event.reply(f"这个频道已经绑过了：{title}（{new_dest}）")
                    return
                b.append({"id": str(new_dest), "title": title})
                settings_holder["bound_channels"] = b
                persist_settings()
                await event.reply(f"已绑定：{title}（{new_dest}），以后在菜单里点一下就能切过去。")
                return
            if ptype == "traffic_limit":
                try:
                    v = float(text)
                    if v < 0:
                        raise ValueError()
                except ValueError:
                    await event.reply("流量限额要是数字，单位 GB，比如 5000，0 = 不限。")
                    return
                settings_holder["traffic_limit_gb"] = v
                persist_settings()
                await event.reply(f"已改流量限额：{v}GB，0 = 不限。")
                return
            if ptype == "traffic_mode":
                if text not in ("both", "out"):
                    await event.reply("流量计费只能填 both（双向 x2）或 out（只算出站 x1）。")
                    return
                settings_holder["traffic_mode"] = text
                persist_settings()
                await event.reply(f"已改流量计费：{text}。")
                return
            if ptype in ("max_file", "max_cache", "min_free"):
                try:
                    v = int(text)
                    if v < 0:
                        raise ValueError()
                except ValueError:
                    await event.reply("要是数字，单位 MB，比如 10240。")
                    return
                key = {"max_file": "max_file_mb", "max_cache": "max_cache_mb", "min_free": "min_free_mb"}[ptype]
                settings_holder[key] = v
                persist_settings()
                await event.reply(f"已改：{key} = {v}MB。")
                return
            if ptype == "report_hour":
                try:
                    v = int(text)
                    if v < 0 or v > 23:
                        raise ValueError()
                except ValueError:
                    await event.reply("日报时间要是 0-23 的数字，0 = 不推，比如 8。")
                    return
                settings_holder["daily_report_hour"] = v
                persist_settings()
                await event.reply(f"已改日报时间：每天 {v} 点推，0 = 不推。")
                return
            if ptype == "traffic_day":
                try:
                    v = int(text)
                    if v < 1 or v > 28:
                        raise ValueError()
                except ValueError:
                    await event.reply("清零日只能是 1-28。")
                    return
                traffic_state["reset_day"] = v
                save_traffic_state(args.traffic_file, traffic_state)
                await event.reply(f"已改成每月 {v} 号清零流量。")
                return
        # --- 菜单 ---
        if text in ("/start", "/menu", "/菜单"):
            await send_menu(event)
            return
        if text.startswith("/bind") or text.startswith("/绑定"):
            parts = text.split(None, 1)
            if len(parts) < 2 or not parts[1].strip():
                pending_input["type"] = "bind"
                await event.reply("发一个要绑定的频道过来，比如 @你的频道 或 -1001234567890")
                return
            pending_input["type"] = "bind"
            # 复用上面的输入处理：把这条当成输入值
            text = parts[1].strip()
            # 直接走 bind 输入逻辑
            new_dest = parse_dest(text)
            try:
                ent = await client.get_entity(new_dest)
                title = getattr(ent, "title", None) or getattr(ent, "username", None) or str(new_dest)
            except Exception as e:
                await event.reply(f"绑定失败，找不到这个频道：{e}")
                pending_input["type"] = None
                return
            b = settings_holder.get("bound_channels", []) or []
            if len(b) >= 8:
                await event.reply("最多绑 8 个频道，先发 /unbind @频道 解绑一个再绑。")
                pending_input["type"] = None
                return
            if any(str(c.get("id")) == str(new_dest) for c in b):
                await event.reply(f"这个频道已经绑过了：{title}（{new_dest}）")
                pending_input["type"] = None
                return
            b.append({"id": str(new_dest), "title": title})
            settings_holder["bound_channels"] = b
            persist_settings()
            pending_input["type"] = None
            await event.reply(f"已绑定：{title}（{new_dest}），以后在菜单里点一下就能切过去。")
            return
        if text.startswith("/unbind") or text.startswith("/解绑"):
            parts = text.split(None, 1)
            if len(parts) < 2 or not parts[1].strip():
                await event.reply(f"用法：/unbind @频道\n{bound_list_text()}")
                return
            target = parse_dest(parts[1].strip())
            b = settings_holder.get("bound_channels", []) or []
            nb = [c for c in b if str(c.get("id")) != str(target)]
            if len(nb) == len(b):
                await event.reply(f"没找到这个绑定：{target}\n{bound_list_text()}")
                return
            settings_holder["bound_channels"] = nb
            persist_settings()
            await event.reply(f"已解绑：{target}\n{bound_list_text()}")
            return
        if text in ("/bound", "/已绑定", "/bindlist"):
            await event.reply(bound_list_text())
            return
        if text.startswith("/set_traffic_limit"):
            parts = text.split()
            if len(parts) < 2:
                pending_input["type"] = "traffic_limit"
                await event.reply(f"发一个新的流量限额过来，单位 GB，比如 5000，0 = 不限。当前是 {settings_holder['traffic_limit_gb']}GB。")
                return
            try:
                v = float(parts[1])
            except ValueError:
                await event.reply("流量限额要是数字，单位 GB，比如 5000，0 = 不限。")
                return
            settings_holder["traffic_limit_gb"] = v
            persist_settings()
            await event.reply(f"已改流量限额：{v}GB，0 = 不限。")
            return
        if text.startswith("/set_traffic_mode"):
            parts = text.split()
            if len(parts) < 2:
                pending_input["type"] = "traffic_mode"
                await event.reply(f"发 both（双向 x2）或 out（只算出站 x1）过来。当前是 {settings_holder['traffic_mode']}。")
                return
            if parts[1] not in ("both", "out"):
                await event.reply("只能填 both 或 out。")
                return
            settings_holder["traffic_mode"] = parts[1]
            persist_settings()
            await event.reply(f"已改流量计费：{parts[1]}。")
            return
        if text.startswith("/set_maxfile"):
            parts = text.split()
            if len(parts) < 2:
                pending_input["type"] = "max_file"
                await event.reply(f"发一个新的单文件上限过来，单位 MB，比如 10240。当前是 {settings_holder['max_file_mb']}MB。")
                return
            try:
                settings_holder["max_file_mb"] = int(parts[1])
                persist_settings()
                await event.reply(f"已改单文件上限：{parts[1]}MB。")
            except ValueError:
                await event.reply("要是数字，单位 MB，比如 10240。")
            return
        if text.startswith("/set_cache"):
            parts = text.split()
            if len(parts) < 2:
                pending_input["type"] = "max_cache"
                await event.reply(f"发一个新的缓存上限过来，单位 MB，比如 10240。当前是 {settings_holder['max_cache_mb']}MB。")
                return
            try:
                settings_holder["max_cache_mb"] = int(parts[1])
                persist_settings()
                await event.reply(f"已改缓存上限：{parts[1]}MB。")
            except ValueError:
                await event.reply("要是数字，单位 MB，比如 10240。")
            return
        if text.startswith("/set_minfree"):
            parts = text.split()
            if len(parts) < 2:
                pending_input["type"] = "min_free"
                await event.reply(f"发一个新的磁盘预留过来，单位 MB，比如 500。当前是 {settings_holder['min_free_mb']}MB。")
                return
            try:
                settings_holder["min_free_mb"] = int(parts[1])
                persist_settings()
                await event.reply(f"已改磁盘预留：{parts[1]}MB。")
            except ValueError:
                await event.reply("要是数字，单位 MB，比如 500。")
            return
        if text.startswith("/set_report_hour") or text.startswith("/set_hour"):
            parts = text.split()
            if len(parts) < 2:
                pending_input["type"] = "report_hour"
                await event.reply(f"发一个新的日报时间过来，0-23，比如 8，0 = 不推。当前是 {settings_holder['daily_report_hour']} 点。")
                return
            try:
                v = int(parts[1])
                if v < 0 or v > 23:
                    raise ValueError()
                settings_holder["daily_report_hour"] = v
                persist_settings()
                await event.reply(f"已改日报时间：每天 {v} 点推，0 = 不推。")
            except ValueError:
                await event.reply("日报时间要是 0-23 的数字，0 = 不推。")
            return
        # --- 先处理换频道命令，不用上服务器 ---
        if text.startswith("/set_dest") or text.startswith("/set_channel") or text.startswith("/目标频道"):
            parts = text.split(None, 1)
            if len(parts) < 2 or not parts[1].strip():
                # 没给频道就显示已绑定的，点一下就能切
                await event.reply(f"发一个频道过来，比如 @新频道 或 -1001234567890\n\n{bound_list_text()}\n\n也可以发 /start 打开菜单点一下切换。")
                return
            new_dest = parse_dest(parts[1].strip())
            try:
                ent = await client.get_entity(new_dest)
                title = getattr(ent, "title", None) or getattr(ent, "username", None) or str(new_dest)
            except Exception as e:
                await event.reply(f"换频道失败，找不到这个频道：{e}\n请确认频道名/ID 对不对，你的号是不是它的管理员。")
                return
            dest_holder["channel"] = new_dest
            save_dest_to_file(new_dest)
            await event.reply(f"已换目标频道：{title}（{new_dest}），之后转发的都搬到这里。")
            print(f"目标频道已改为：{new_dest}", flush=True)
            return
        if text in ("/dest", "/目标", "/current"):
            if dest_holder["channel"]:
                _cur = dest_holder["channel"]
                _name = None
                for _c in (settings_holder.get("bound_channels", []) or []):
                    if str(_c.get("id")) == str(_cur):
                        _name = _c.get("title")
                        break
                if _name:
                    await event.reply(f"当前目标频道：{_name}（{_cur}）")
                else:
                    try:
                        _ent = await client.get_entity(parse_dest(str(_cur)))
                        _t = getattr(_ent, "title", None) or getattr(_ent, "username", None) or str(_cur)
                        await event.reply(f"当前目标频道：{_t}（{_cur}）")
                    except Exception:
                        await event.reply(f"当前目标频道：{_cur}")
            else:
                await event.reply("还没设目标频道，发 /set_dest @新频道 来设。")
            return
        if text in ("/help", "/帮助"):
            _d = dest_holder["channel"] if dest_holder["channel"] else "还没设，发 /set_dest @新频道"
            await event.reply(
                "用法：\n"
                "· 转发喜欢的频道帖子到收藏夹（或发给我），自动搬到目标频道\n"
                "· 也可以转发给 Bot（给了 --bot-token 时）：转发到 Bot 会带来源，第三方转发到收藏夹不带来源时用这个\n"
                "· 发 /start 打开菜单，点按钮切换频道或改设置\n"
                "· /set_dest @新频道：设 / 换目标频道\n"
                "· /bind @频道：绑定频道（最多 5 个），/bound 看已绑定的，/unbind @频道 解绑\n"
                "· /dest：看当前目标频道\n"
                "· /cache：看缓存限制（单文件/一批/剩余磁盘）\n"
                "· /traffic：看本账期流量已用 / 还剩多少\n"
                "· /today：看当日用了多少流量\n"
                "· /set_traffic_day 15：改每月几号清零流量（1-28）\n"
                "· /set_traffic_limit 5000：改流量限额 GB，0 = 不限\n"
                "· /set_traffic_mode both/out：改流量计费\n"
                "· /set_maxfile 10240：改单文件上限 MB\n"
                "· /set_cache 10240：改缓存上限 MB\n"
                "· /set_minfree 500：改磁盘预留 MB\n"
                "· /set_report_hour 8：改日报时间，0 = 不推\n"
                f"· 每天早上 {settings_holder['daily_report_hour']} 点自动推昨日流量统计\n"
                f"· 当前目标频道：{_d}\n"
                f"· {bound_list_text()}")
            return
        if text.startswith("/set_traffic_day"):
            parts = text.split()
            if len(parts) < 2:
                await event.reply(f"用法：/set_traffic_day 15\n当前每月 {traffic_state.get('reset_day', 1)} 号清零，下次清零是 {next_reset_for(datetime.now(TZ).date(), traffic_state.get('reset_day', 1))}。")
                return
            try:
                day = int(parts[1])
            except ValueError:
                await event.reply("重置日要是数字，1-28，比如 /set_traffic_day 15")
                return
            if day < 1 or day > 28:
                await event.reply("重置日只能是 1-28，29/30/31 号有的月份没有，设不了。")
                return
            old_day = traffic_state.get("reset_day", 1)
            # 改重置日：当前账期用量保留到下个账期，不立刻清零（防低估用量超流量）
            traffic_state["reset_day"] = day
            new_period = period_start_for(datetime.now(TZ).date(), day).strftime("%Y-%m-%d")
            nxt = next_reset_for(datetime.now(TZ).date(), day)
            # 账期起点变了，但用量保留，设成新账期起点，下次到重置日再清零
            traffic_state["month"] = new_period
            save_traffic_state(args.traffic_file, traffic_state)
            await event.reply(
                f"已改成每月 {day} 号清零流量（原来每月 {old_day} 号）。\n"
                f"当前账期用量 {traffic_state['used'] / 1024**3:.2f}GB 保留到下个账期，"
                f"下次清零是 {nxt}，到那天自动清零。重启脚本也记得住。")
            print(f"流量重置日已改为每月 {day} 号，下次清零 {nxt}", flush=True)
            return
        if text in ("/traffic", "/流量"):
            # 到了重置日就清零
            cur_state = load_traffic_state(args.traffic_file, reset_day=traffic_state.get("reset_day", 1))
            if cur_state["month"] != traffic_state["month"]:
                traffic_state.update(cur_state)
            cur_used = traffic_state["used"]
            cur_month = traffic_state["month"]
            nxt = next_reset_for(datetime.now(TZ).date(), traffic_state.get("reset_day", 1))
            today = _today_str()
            td = (traffic_state.get("days", {}) or {}).get(today, {"bytes": 0, "files": 0})
            _lim = settings_holder["traffic_limit_gb"]
            _mode = settings_holder["traffic_mode"]
            if _lim:
                await event.reply(
                    f"流量（{_mode}，x{traffic_mult(_mode)}）：\n"
                    f"今日 {today} 用了 {td['bytes'] / 1024**3:.2f}GB（{td['files']} 个文件）\n"
                    f"本账期 {cur_month} 起已用 {cur_used / 1024**3:.2f}GB / "
                    f"限额 {_lim}GB\n"
                    f"还剩 {_lim - cur_used / 1024**3:.2f:.2f}GB，"
                    f"每月 {traffic_state.get('reset_day', 1)} 号清零，下次 {nxt}，"
                    f"超了会自动停。")
            else:
                await event.reply(f"流量不限额，本账期已用 {cur_used / 1024**3:.2f}GB，今日用了 {td['bytes'] / 1024**3:.2f}GB。")
            return
        if text in ("/today", "/今日", "/当日"):
            today = _today_str()
            td = (traffic_state.get("days", {}) or {}).get(today, {"bytes": 0, "files": 0})
            used_gb = traffic_state["used"] / 1024**3
            _lim = settings_holder["traffic_limit_gb"]
            if _lim:
                await event.reply(
                    f"当日流量（{today}）：用了 {td['bytes'] / 1024**3:.2f}GB，搬了 {td['files']} 个文件\n"
                    f"本账期累计用了 {used_gb:.2f}GB / {_lim}GB，"
                    f"还剩 {_lim - used_gb:.2f}GB。")
            else:
                await event.reply(f"当日流量（{today}）：用了 {td['bytes'] / 1024**3:.2f}GB，搬了 {td['files']} 个文件。")
            return
        if text in ("/cache", "/缓存"):
            free = shutil.disk_usage(".").free / (1024 * 1024)
            await event.reply(
                f"缓存限制：单文件 {settings_holder['max_file_mb']}MB，一批 {settings_holder['max_cache_mb']}MB，"
                f"磁盘剩余低于 {settings_holder['min_free_mb']}MB 就停。\n"
                f"当前磁盘剩余约 {free:.0f}MB。发完一批立即删，不会一直占着。")
            return
        src_ref = src_post_id = None
        # 1) 转发的频道帖子：从转发信息里拿原频道 + 原帖 ID
        fwd = event.message.fwd_from
        if fwd and isinstance(getattr(fwd, "from_id", None), PeerChannel) and getattr(fwd, "channel_post", None):
            src_ref = fwd.from_id.channel_id
            # PeerChannel 的 channel_id 是不带 -100 的数字，转成 -100... 形式
            src_ref = int("-100" + str(src_ref))
            src_post_id = fwd.channel_post
        # 2) 直接发的链接
        if src_ref is None and event.message.text:
            src_ref, src_post_id = parse_link(event.message.text)
        if src_ref is None or not src_post_id:
            # 没带转发也没带链接就静默忽略，不回“没看懂”，不然自己发的回执会循环触发
            return
        if not dest_holder["channel"]:
            await event.reply("还没设目标频道，先发 /set_dest @新频道 来设，设好再转发这条。")
            return
        key = (str(src_ref), src_post_id)
        now = time.time()
        if key in recent and now - recent[key] < 300:
            await event.reply("这条 5 分钟内已经搬过/正在搬，跳过。")
            return
        recent[key] = now
        await queue.put((event, src_ref, src_post_id))
        print(f"已入队：{src_ref} / {src_post_id}，队列 {queue.qsize()} 条", flush=True)

    async def handle_callback(event):
        # 菜单按钮回调：只认主号点的
        if event.sender_id != master_id:
            await event.answer("不是你的 Bot。")
            return
        data = (event.data or b"").decode(errors="ignore")
        # 切换目标频道：点已绑定的频道
        if data.startswith("dest:"):
            target = data[5:]
            dest_holder["channel"] = parse_dest(target)
            save_dest_to_file(dest_holder["channel"])
            await event.answer("已切换")
            await event.edit(f"已换目标频道：{target}，之后转发的都搬到这里。")
            return
        if data == "bind_new":
            pending_input["type"] = "bind"
            await event.answer()
            await event.respond("发一个要绑定的频道过来，比如 @你的频道 或 -1001234567890")
            return
        if data == "del_bind":
            await event.answer()
            b = settings_holder.get("bound_channels", []) or []
            if not b:
                await event.respond("还没绑定频道，没什么可删的。")
                return
            rows = []
            for c in b[:8]:
                rows.append([Button.inline(f"❌ {c.get('title') or c.get('id')}", f"unbind:{c.get('id')}".encode()[:64])])
            rows.append([Button.inline("↩️ 返回菜单", b"menu_back")])
            await event.respond("点一下要删除的绑定频道：", buttons=rows)
            return
        if data.startswith("unbind:"):
            target = data[7:]
            b = settings_holder.get("bound_channels", []) or []
            nb = [c for c in b if str(c.get("id")) != str(target)]
            if len(nb) == len(b):
                await event.answer("没找到这个绑定")
                return
            settings_holder["bound_channels"] = nb
            persist_settings()
            await event.answer("已删除")
            await event.edit(f"已删除绑定：{target}\n{bound_list_text()}")
            return
        if data == "menu_back":
            await event.answer()
            await send_menu(event)
            return
        if data == "menu_traffic":
            await event.answer()
            _cur = load_traffic_state(args.traffic_file, reset_day=traffic_state.get("reset_day", 1))
            _lim = settings_holder["traffic_limit_gb"]
            _mode = settings_holder["traffic_mode"]
            await event.respond(f"本账期 {_cur['month']} 起已用 {_cur['used'] / 1024**3:.2f}GB / 限额 {_lim}GB（{_mode}），每月 {traffic_state.get('reset_day', 1)} 号清零。")
            return
        if data == "menu_today":
            await event.answer()
            _today = _today_str()
            _td = (traffic_state.get("days", {}) or {}).get(_today, {"bytes": 0, "files": 0})
            await event.respond(f"当日流量（{_today}）：用了 {_td['bytes'] / 1024**3:.2f}GB，搬了 {_td['files']} 个文件。")
            return
        if data == "menu_limit":
            pending_input["type"] = "traffic_limit"
            await event.answer()
            await event.respond(f"发一个新的流量限额过来，单位 GB，比如 5000，0 = 不限。当前是 {settings_holder['traffic_limit_gb']}GB。")
            return
        if data == "menu_day":
            pending_input["type"] = "traffic_day"
            await event.answer()
            await event.respond(f"发一个新的清零日过来，1-28，比如 15。当前每月 {traffic_state.get('reset_day', 1)} 号清零。")
            return
        if data == "menu_maxfile":
            pending_input["type"] = "max_file"
            await event.answer()
            await event.respond(f"发一个新的单文件上限过来，单位 MB，比如 10240。当前是 {settings_holder['max_file_mb']}MB。")
            return
        if data == "menu_maxcache":
            pending_input["type"] = "max_cache"
            await event.answer()
            await event.respond(f"发一个新的缓存上限过来，单位 MB，比如 10240。当前是 {settings_holder['max_cache_mb']}MB。")
            return
        if data == "menu_minfree":
            pending_input["type"] = "min_free"
            await event.answer()
            await event.respond(f"发一个新的磁盘预留过来，单位 MB，比如 500。当前是 {settings_holder['min_free_mb']}MB。")
            return
        if data == "menu_hour":
            pending_input["type"] = "report_hour"
            await event.answer()
            await event.respond(f"发一个新的日报时间过来，0-23，比如 8，0 = 不推。当前是 {settings_holder['daily_report_hour']} 点。")
            return
        if data == "menu_mode":
            pending_input["type"] = "traffic_mode"
            await event.answer()
            await event.respond(f"发 both（双向 x2）或 out（只算出站 x1）过来。当前是 {settings_holder['traffic_mode']}。")
            return
        if data == "menu_help":
            await event.answer()
            await event.respond("发 /help 看全部命令，发 /start 打开菜单。")
            return
        await event.answer()

    @client.on(events.NewMessage)
    async def handler(event):
        # 用户号只认两处：自己的收藏夹（chat_id == 自己），或主号发给这个号的私聊
        # 注意：收藏夹里的消息是你自己发出去的，out=True，所以不能只听 incoming
        # 关键：发给 Bot 的消息也是 is_private + sender 是主号，但 chat_id 是 Bot 的 ID，
        # 不能认，不然 Bot 里每条都会被用户号和 Bot 各处理一次，第二遍就报“5 分钟内已搬过”
        is_saved = event.chat_id == me.id
        is_private_from_master = event.is_private and event.sender_id == master_id and event.chat_id == master_id
        if args.self_mode:
            if not is_saved:
                return
        else:
            if not is_private_from_master:
                return
        await process_event(event)

    @client.on(events.CallbackQuery)
    async def user_callback(event):
        await handle_callback(event)

    if bot_client is not None:
        @bot_client.on(events.NewMessage)
        async def bot_handler(event):
            # Bot 只认主号发来的私聊（转发给 Bot 的帖子），别人发来的不认
            if not event.is_private or event.sender_id != master_id:
                return
            await process_event(event)

        @bot_client.on(events.CallbackQuery)
        async def bot_callback(event):
            await handle_callback(event)
        print("Bot 触发已开启：把帖子转发给 Bot 就开始搬。", flush=True)
        await asyncio.gather(
            client.run_until_disconnected(),
            bot_client.run_until_disconnected(),
        )
    else:
        await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
