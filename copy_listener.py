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

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import GetDiscussionMessageRequest
from telethon.tl.types import PeerChannel

DEST_FILE = Path("dest_channel.txt")


def load_dest_from_file():
    try:
        return DEST_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def save_dest_to_file(value):
    DEST_FILE.write_text(str(value), encoding="utf-8")


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
                    lambda: client.send_file(dest_channel, src_files[0], caption=caption),
                    "发新帖")
            else:
                # 相册：最多 10 张一批，超了拆批（第一批带标题）
                dest_post = None
                for batch_no, batch in enumerate(chunks(src_files, 10), 1):
                    cap = caption if batch_no == 1 else None
                    batch_mb = sum(Path(p).stat().st_size for p in batch if Path(p).exists()) / (1024 * 1024)
                    if batch_mb > max_cache_mb and len(batch) > 1:
                        for single in batch:
                            r = await send_with_floodwait(
                                lambda: client.send_file(dest_channel, single, caption=cap),
                                "发新帖单张")
                            cap = None
                            if dest_post is None:
                                dest_post = r
                    else:
                        r = await send_with_floodwait(
                            lambda: client.send_file(dest_channel, batch, caption=cap),
                            "发新帖")
                        if dest_post is None:
                            # send_file 传列表时返回的是 list，取第一条当新帖 ID
                            dest_post = r[0] if isinstance(r, list) else r
        finally:
            delete_files(src_files)
    else:
        dest_post = await send_with_floodwait(
            lambda: client.send_message(dest_channel, src_post.text or "(无文字)"),
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
                    for single in batch:
                        await send_with_floodwait(
                            lambda: client.send_file(dest_channel, single, caption=cap,
                                                     comment_to=dest_post.id),
                            f"媒体评论 {gi} 单张")
                        cap = None
                        delete_files([single])
                        await asyncio.sleep(1)
                else:
                    await send_with_floodwait(
                        lambda: client.send_file(dest_channel, batch,
                                                 caption=caption if batch_no == 1 else None,
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
    if args.traffic_limit_gb:
        _nxt = next_reset_for(datetime.now(TZ).date(), traffic_state["reset_day"])
        print(f"流量：本账期 {traffic_state['month']} 起已用 {traffic_state['used'] / 1024**3:.2f}GB / "
              f"限额 {args.traffic_limit_gb}GB（{args.traffic_mode}，x{traffic_mult(args.traffic_mode)}），"
              f"每月 {traffic_state['reset_day']} 号清零，下次 {_nxt}，"
              f"每日 {args.daily_report_hour} 点自动推昨日统计", flush=True)

    if not args.api_id or not args.api_hash:
        print("缺 api_id / api_hash", file=sys.stderr)
        sys.exit(2)
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
    await client.start()
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
                    max_file_mb=args.max_file_mb, max_cache_mb=args.max_cache_mb,
                    min_free_mb=args.min_free_mb,
                    traffic_file=args.traffic_file, traffic_limit_gb=args.traffic_limit_gb,
                    traffic_mode=args.traffic_mode, traffic_state=traffic_state)
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
        if not args.daily_report_hour:
            return
        target = me.id if args.self_mode else master_id
        while True:
            try:
                now = datetime.now(TZ)
                # 昨天的日期
                yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
                # 今天 8 点
                today_report = now.replace(hour=args.daily_report_hour, minute=0, second=0, microsecond=0)
                if now >= today_report and traffic_state.get("last_report") != yesterday:
                    # 到了今天 8 点还没推昨天，就推
                    text = daily_report_text(traffic_state, args.traffic_limit_gb, args.traffic_mode, yesterday)
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

    @client.on(events.NewMessage)
    async def handler(event):
        # 只认两处：自己的收藏夹（chat_id == 自己），或主号发来的私聊
        # 注意：收藏夹里的消息是你自己发出去的，out=True，所以不能只听 incoming
        is_saved = event.chat_id == me.id
        is_private_from_master = event.is_private and event.sender_id == master_id
        if not (is_saved or is_private_from_master):
            return
        text = (event.message.text or "").strip()
        # --- 先处理换频道命令，不用上服务器 ---
        if text.startswith("/set_dest") or text.startswith("/set_channel") or text.startswith("/目标频道"):
            parts = text.split(None, 1)
            if len(parts) < 2 or not parts[1].strip():
                await event.reply("用法：/set_dest @新频道 或 /set_dest -1001234567890")
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
                await event.reply(f"当前目标频道：{dest_holder['channel']}")
            else:
                await event.reply("还没设目标频道，发 /set_dest @新频道 来设。")
            return
        if text in ("/help", "/start", "/帮助"):
            _d = dest_holder["channel"] if dest_holder["channel"] else "还没设，发 /set_dest @新频道"
            await event.reply(
                "用法：\n"
                "· 转发喜欢的频道帖子到收藏夹（或发给我），自动搬到目标频道\n"
                "· /set_dest @新频道：设 / 换目标频道\n"
                "· /dest：看当前目标频道\n"
                "· /cache：看缓存限制（单文件/一批/剩余磁盘）\n"
                "· /traffic：看本账期流量已用 / 还剩多少\n"
                "· /today：看当日用了多少流量\n"
                "· /set_traffic_day 15：改每月几号清零流量（1-28）\n"
                f"· 每天早上 {args.daily_report_hour} 点自动推昨日流量统计\n"
                f"· 当前目标频道：{_d}")
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
            if args.traffic_limit_gb:
                await event.reply(
                    f"流量（{args.traffic_mode}，x{traffic_mult(args.traffic_mode)}）：\n"
                    f"今日 {today} 用了 {td['bytes'] / 1024**3:.2f}GB（{td['files']} 个文件）\n"
                    f"本账期 {cur_month} 起已用 {cur_used / 1024**3:.2f}GB / "
                    f"限额 {args.traffic_limit_gb}GB\n"
                    f"还剩 {args.traffic_limit_gb - cur_used / 1024**3:.2f:.2f}GB，"
                    f"每月 {traffic_state.get('reset_day', 1)} 号清零，下次 {nxt}，"
                    f"超了会自动停。")
            else:
                await event.reply(f"流量不限额，本账期已用 {cur_used / 1024**3:.2f}GB，今日用了 {td['bytes'] / 1024**3:.2f}GB。")
            return
        if text in ("/today", "/今日", "/当日"):
            today = _today_str()
            td = (traffic_state.get("days", {}) or {}).get(today, {"bytes": 0, "files": 0})
            used_gb = traffic_state["used"] / 1024**3
            if args.traffic_limit_gb:
                await event.reply(
                    f"当日流量（{today}）：用了 {td['bytes'] / 1024**3:.2f}GB，搬了 {td['files']} 个文件\n"
                    f"本账期累计用了 {used_gb:.2f}GB / {args.traffic_limit_gb}GB，"
                    f"还剩 {args.traffic_limit_gb - used_gb:.2f}GB。")
            else:
                await event.reply(f"当日流量（{today}）：用了 {td['bytes'] / 1024**3:.2f}GB，搬了 {td['files']} 个文件。")
            return
        if text in ("/cache", "/缓存"):
            free = shutil.disk_usage(".").free / (1024 * 1024)
            await event.reply(
                f"缓存限制：单文件 {args.max_file_mb}MB，一批 {args.max_cache_mb}MB，"
                f"磁盘剩余低于 {args.min_free_mb}MB 就停。\n"
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

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
