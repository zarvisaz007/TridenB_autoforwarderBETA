import asyncio
import copy
import json
import os
import re
import sys
import time
import datetime
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from database import db

TASKS_FILE = "tasks.json"
SESSION_NAME = "tridenb_autoforwarder"
MAX_LOG = 500
LOOP_LIMIT = 10    # max forwards per task within LOOP_WINDOW seconds
LOOP_WINDOW = 10   # seconds

# ---------- Runtime state ----------
forwarder_active = False
paused_task_ids = set()   # task IDs paused this session
loop_counter = {}          # task_id -> [timestamps]
log_entries = []
active_handlers = []       # (func, event_class) for cleanup on stop
cleanup_task = None
cancel_deletion = False


# ---------- Sync helpers ----------

def load_tasks():
    if not os.path.exists(TASKS_FILE):
        return {"tasks": []}
    try:
        with open(TASKS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"tasks": []}


def save_tasks(data):
    with open(TASKS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def next_task_id(data):
    tasks = data.get("tasks", [])
    if not tasks:
        return 1
    return max(t["id"] for t in tasks) + 1


def sync_paused_from_tasks():
    """Populate paused_task_ids from persisted 'paused' field in tasks.json."""
    data = load_tasks()
    for t in data.get("tasks", []):
        if t.get("paused"):
            paused_task_ids.add(t["id"])
        else:
            paused_task_ids.discard(t["id"])


def add_log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] {msg}"
    log_entries.append(entry)
    if len(log_entries) > MAX_LOG:
        log_entries.pop(0)
    print(entry)


def check_loop(task_id):
    """Returns True if task is firing too fast — loop detected."""
    now = time.time()
    times = loop_counter.get(task_id, [])
    times = [t for t in times if now - t < LOOP_WINDOW]
    times.append(now)
    loop_counter[task_id] = times
    return len(times) >= LOOP_LIMIT


def apply_filters(message, filters):
    if filters.get("skip_images") and message.photo:
        return (False, None)
    if filters.get("skip_audio") and (message.audio or message.voice):
        return (False, None)
    if filters.get("skip_videos") and message.video:
        return (False, None)

    text = message.text or ""

    blacklist = filters.get("blacklist_words", [])
    text_lower = text.lower()
    for word in blacklist:
        if word.lower() in text_lower:
            return (False, None)

    modified = False

    clean_words = filters.get("clean_words", [])
    for w in clean_words:
        if w in text:
            text = text.replace(w, "")
            modified = True

    if filters.get("clean_urls"):
        new_text = re.sub(r"https?://\S+", "", text)
        if new_text != text:
            modified = True
            text = new_text

    if filters.get("clean_usernames"):
        new_text = re.sub(r"@\w+", "", text)
        if new_text != text:
            modified = True
            text = new_text

    if modified:
        return (True, text.strip())
    return (True, None)


# ---------- Async helpers ----------

async def ainput(prompt=""):
    """Non-blocking input — lets asyncio process Telegram events while waiting."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, input, prompt)


async def send_copy(client, dest_id, message, modified_text, reply_to=None):
    """Send message as a fresh copy — no 'Forwarded from' header."""
    if modified_text is not None:
        return await client.send_message(dest_id, modified_text, reply_to=reply_to)
    if message.media:
        return await client.send_file(dest_id, file=message.media, caption=message.text or "", reply_to=reply_to)
    return await client.send_message(dest_id, message.text or "", reply_to=reply_to)


# ---------- Async CLI functions ----------

async def get_channel_id(client):
    rows = []
    async for dialog in client.iter_dialogs():
        if dialog.is_channel or dialog.is_group:
            name = dialog.name or "(no name)"
            cid = dialog.entity.id
            if dialog.is_channel:
                full_id = int(f"-100{cid}")
            else:
                full_id = -cid if cid > 0 else cid
            rows.append((name, full_id))
    rows.sort(key=lambda r: r[0].lower())
    print("\n--- All Channels / Groups ---")
    print(f"{'Name':<40} {'Channel ID':<20}")
    print("-" * 60)
    for name, full_id in rows:
        print(f"{name:<40} {full_id:<20}")
    print()


async def create_task(client):
    data = load_tasks()
    print("\n--- Create Forwarding Task ---")
    name = (await ainput("Task name: ")).strip()

    src_raw = (await ainput("Source channel ID (e.g. -1001234567890): ")).strip()
    dst_raw = (await ainput("Destination channel IDs (comma-separated): ")).strip()

    try:
        source_id = int(src_raw)
        dest_ids = [int(x.strip()) for x in dst_raw.split(",") if x.strip()]
    except ValueError:
        print("Invalid channel IDs.")
        return

    if not dest_ids:
        print("At least one destination ID required.")
        return

    print("\nFilters (press Enter to skip / use defaults):")

    async def prompt_list(prompt):
        raw = (await ainput(f"  {prompt} (comma-separated, blank=none): ")).strip()
        return [x.strip() for x in raw.split(",") if x.strip()] if raw else []

    async def prompt_bool(prompt):
        val = (await ainput(f"  {prompt} [y/N]: ")).strip().lower()
        return val in ("y", "yes")

    blacklist = await prompt_list("Blacklist words")
    clean_words = await prompt_list("Clean words (remove from text)")
    clean_urls = await prompt_bool("Remove URLs?")
    clean_usernames = await prompt_bool("Remove @usernames?")
    skip_images = await prompt_bool("Skip image messages?")
    skip_audio = await prompt_bool("Skip audio/voice messages?")
    skip_videos = await prompt_bool("Skip video messages?")

    delay_raw = (await ainput("  Delay before forwarding (seconds) [0]: ")).strip()
    delay_seconds = int(delay_raw) if delay_raw.isdigit() else 0

    img_del_raw = (await ainput("  Auto-delete images after (days) [0=off]: ")).strip()
    image_delete_days = int(img_del_raw) if img_del_raw.isdigit() else 0

    use_rewrite = await prompt_bool("  Enable AI Rewriting (OpenRouter)?")
    rewrite_prompt = ""
    if use_rewrite:
        rewrite_prompt = (await ainput("  Rewrite prompt (e.g. 'Paraphrase to avoid copyright'): ")).strip()

    task = {
        "id": next_task_id(data),
        "name": name,
        "source_channel_id": source_id,
        "destination_channel_ids": dest_ids,
        "enabled": True,
        "filters": {
            "blacklist_words": blacklist,
            "clean_words": clean_words,
            "clean_urls": clean_urls,
            "clean_usernames": clean_usernames,
            "skip_images": skip_images,
            "skip_audio": skip_audio,
            "skip_videos": skip_videos,
            "delay_seconds": delay_seconds,
            "image_delete_days": image_delete_days,
            "rewrite_enabled": use_rewrite,
            "rewrite_prompt": rewrite_prompt,
        },
    }

    data["tasks"].append(task)
    save_tasks(data)
    print(f"\nTask '{name}' created with ID {task['id']}.")


async def list_tasks():
    data = load_tasks()
    tasks = data.get("tasks", [])
    if not tasks:
        print("No tasks found.")
        return

    print(f"\n{'ID':<5} {'Name':<20} {'Enabled':<8} {'Paused':<8} {'Source':<22} {'Destinations'}")
    print("-" * 100)
    for t in tasks:
        status = "Yes" if t.get("enabled") else "No"
        paused = "Yes" if t["id"] in paused_task_ids else "No"
        dests = ", ".join(str(d) for d in t.get("destination_channel_ids", [t.get("destination_channel_id", "?")]))
        print(f"{t['id']:<5} {t['name']:<20} {status:<8} {paused:<8} {t['source_channel_id']:<22} {dests}")


async def toggle_task():
    await list_tasks()
    try:
        tid = int((await ainput("\nEnter task ID to toggle: ")).strip())
    except ValueError:
        print("Invalid ID.")
        return

    data = load_tasks()
    for t in data["tasks"]:
        if t["id"] == tid:
            t["enabled"] = not t.get("enabled", True)
            save_tasks(data)
            state = "enabled" if t["enabled"] else "disabled"
            print(f"Task {tid} is now {state}.")
            return
    print(f"Task {tid} not found.")


async def edit_filters_submenu(task, data):
    """Interactive filter editor — pick individual filters to change."""
    filters = task.setdefault("filters", {})
    while True:
        bl = filters.get("blacklist_words", [])
        cw = filters.get("clean_words", [])
        print(f"\n  --- Filters: '{task['name']}' ---")
        print(f"  1. Blacklist words   : {', '.join(bl) or 'none'}")
        print(f"  2. Clean words       : {', '.join(cw) or 'none'}")
        print(f"  3. Remove URLs       : {'Yes' if filters.get('clean_urls') else 'No'}")
        print(f"  4. Remove @usernames : {'Yes' if filters.get('clean_usernames') else 'No'}")
        print(f"  5. Skip images       : {'Yes' if filters.get('skip_images') else 'No'}")
        print(f"  6. Skip audio        : {'Yes' if filters.get('skip_audio') else 'No'}")
        print(f"  7. Skip videos       : {'Yes' if filters.get('skip_videos') else 'No'}")
        print(f"  8. Delay seconds     : {filters.get('delay_seconds', 0)}")
        print(f"  9. Image delete days : {filters.get('image_delete_days', 0)}")
        rew_info = 'Enabled' if filters.get('rewrite_enabled') else 'Disabled'
        print(f"  10. AI Rewrite       : {rew_info}")
        print(f"  0. Done")

        sub = (await ainput("\n  Select filter to edit (0 to finish): ")).strip()

        if sub == "1":
            raw = (await ainput(f"    Words [{', '.join(bl) or 'none'}] (comma-sep, blank=clear): ")).strip()
            filters["blacklist_words"] = [x.strip() for x in raw.split(",") if x.strip()] if raw else []
            save_tasks(data)
            print(f"    Saved: {filters['blacklist_words'] or 'none'}")
        elif sub == "2":
            raw = (await ainput(f"    Words [{', '.join(cw) or 'none'}] (comma-sep, blank=clear): ")).strip()
            filters["clean_words"] = [x.strip() for x in raw.split(",") if x.strip()] if raw else []
            save_tasks(data)
            print(f"    Saved: {filters['clean_words'] or 'none'}")
        elif sub == "3":
            val = (await ainput(f"    Remove URLs? (y/n) [{'Y' if filters.get('clean_urls') else 'N'}]: ")).strip().lower()
            if val in ("y", "yes"):
                filters["clean_urls"] = True
            elif val in ("n", "no"):
                filters["clean_urls"] = False
            save_tasks(data)
        elif sub == "4":
            val = (await ainput(f"    Remove @usernames? (y/n) [{'Y' if filters.get('clean_usernames') else 'N'}]: ")).strip().lower()
            if val in ("y", "yes"):
                filters["clean_usernames"] = True
            elif val in ("n", "no"):
                filters["clean_usernames"] = False
            save_tasks(data)
        elif sub == "5":
            val = (await ainput(f"    Skip images? (y/n) [{'Y' if filters.get('skip_images') else 'N'}]: ")).strip().lower()
            if val in ("y", "yes"):
                filters["skip_images"] = True
            elif val in ("n", "no"):
                filters["skip_images"] = False
            save_tasks(data)
        elif sub == "6":
            val = (await ainput(f"    Skip audio? (y/n) [{'Y' if filters.get('skip_audio') else 'N'}]: ")).strip().lower()
            if val in ("y", "yes"):
                filters["skip_audio"] = True
            elif val in ("n", "no"):
                filters["skip_audio"] = False
            save_tasks(data)
        elif sub == "7":
            val = (await ainput(f"    Skip videos? (y/n) [{'Y' if filters.get('skip_videos') else 'N'}]: ")).strip().lower()
            if val in ("y", "yes"):
                filters["skip_videos"] = True
            elif val in ("n", "no"):
                filters["skip_videos"] = False
            save_tasks(data)
        elif sub == "8":
            raw = (await ainput(f"    Delay in seconds [{filters.get('delay_seconds', 0)}]: ")).strip()
            if raw.isdigit():
                filters["delay_seconds"] = int(raw)
                save_tasks(data)
                print(f"    Saved: delay_seconds = {filters['delay_seconds']}")
        elif sub == "9":
            raw = (await ainput(f"    Image auto-delete (days) [{filters.get('image_delete_days', 0)}]: ")).strip()
            if raw.isdigit():
                filters["image_delete_days"] = int(raw)
                save_tasks(data)
                print(f"    Saved: image_delete_days = {filters['image_delete_days']}")
        elif sub == "10":
            val = (await ainput(f"    Enable AI Rewrite (OpenRouter)? (y/n) [{'Y' if filters.get('rewrite_enabled') else 'N'}]: ")).strip().lower()
            if val in ("y", "yes"):
                filters["rewrite_enabled"] = True
                prompt_val = (await ainput("    Rewrite prompt: ")).strip()
                if prompt_val:
                    filters["rewrite_prompt"] = prompt_val
            elif val in ("n", "no"):
                filters["rewrite_enabled"] = False
            save_tasks(data)
        elif sub == "0":
            break
        else:
            print("  Invalid option.")


async def edit_task():
    await list_tasks()
    try:
        tid = int((await ainput("\nEnter task ID to edit: ")).strip())
    except ValueError:
        print("Invalid ID.")
        return

    data = load_tasks()
    task = next((t for t in data["tasks"] if t["id"] == tid), None)
    if not task:
        print(f"Task {tid} not found.")
        return

    while True:
        dests = task.get("destination_channel_ids", [])
        print(f"\n--- Edit Task: '{task['name']}' (ID {task['id']}) ---")
        print(f"1. Name           : {task['name']}")
        print(f"2. Source channel : {task['source_channel_id']}")
        print(f"3. Add destination(s)")
        print(f"4. Remove destination(s) : {', '.join(str(d) for d in dests) or 'none'}")
        print(f"5. Edit filters")
        print(f"0. Back")

        sub = (await ainput("\nSelect: ")).strip()

        if sub == "1":
            new_name = (await ainput(f"  New name [{task['name']}] (blank=keep): ")).strip()
            if new_name:
                task["name"] = new_name
                save_tasks(data)
                print(f"  Name updated to '{new_name}'.")

        elif sub == "2":
            new_src = (await ainput(f"  New source ID [{task['source_channel_id']}] (blank=keep): ")).strip()
            if new_src:
                try:
                    task["source_channel_id"] = int(new_src)
                    save_tasks(data)
                    print(f"  Source updated. Restart forwarder to apply.")
                except ValueError:
                    print("  Invalid ID.")

        elif sub == "3":
            raw = (await ainput("  IDs to add (comma-separated): ")).strip()
            if raw:
                try:
                    new_ids = [int(x.strip()) for x in raw.split(",") if x.strip()]
                    added = [i for i in new_ids if i not in dests]
                    dests.extend(added)
                    task["destination_channel_ids"] = dests
                    save_tasks(data)
                    print(f"  Added: {added}")
                except ValueError:
                    print("  Invalid IDs.")

        elif sub == "4":
            if not dests:
                print("  No destinations to remove.")
            else:
                print("  Current destinations:")
                for i, d in enumerate(dests, 1):
                    print(f"    {i}. {d}")
                raw = (await ainput("  Enter numbers to remove (comma-separated): ")).strip()
                if raw:
                    try:
                        indices = [int(x.strip()) - 1 for x in raw.split(",") if x.strip()]
                        to_remove = {dests[i] for i in indices if 0 <= i < len(dests)}
                        task["destination_channel_ids"] = [d for d in dests if d not in to_remove]
                        save_tasks(data)
                        print(f"  Removed: {sorted(to_remove)}")
                    except (ValueError, IndexError):
                        print("  Invalid selection.")

        elif sub == "5":
            await edit_filters_submenu(task, data)

        elif sub == "0":
            break
        else:
            print("Invalid option.")


async def duplicate_task():
    await list_tasks()
    try:
        tid = int((await ainput("\nEnter task ID to duplicate: ")).strip())
    except ValueError:
        print("Invalid ID.")
        return

    data = load_tasks()
    task = next((t for t in data["tasks"] if t["id"] == tid), None)
    if not task:
        print(f"Task {tid} not found.")
        return

    new_task = copy.deepcopy(task)
    new_task["id"] = next_task_id(data)
    new_task["name"] = task["name"] + " (copy)"

    data["tasks"].append(new_task)
    save_tasks(data)
    print(f"Task '{task['name']}' duplicated as '{new_task['name']}' with ID {new_task['id']}.")


async def delete_task():
    await list_tasks()
    try:
        tid = int((await ainput("\nEnter task ID to delete: ")).strip())
    except ValueError:
        print("Invalid ID.")
        return

    data = load_tasks()
    task = next((t for t in data["tasks"] if t["id"] == tid), None)
    if not task:
        print(f"Task {tid} not found.")
        return

    confirm = (await ainput(f"Delete task '{task['name']}' (ID {tid})? [y/N]: ")).strip().lower()
    if confirm in ("y", "yes"):
        data["tasks"] = [t for t in data["tasks"] if t["id"] != tid]
        save_tasks(data)
        print(f"Task {tid} deleted.")
    else:
        print("Cancelled.")


async def start_forwarder(client):
    global forwarder_active, active_handlers

    if forwarder_active:
        print("Forwarder is already running.")
        return

    data = load_tasks()
    enabled = [t for t in data.get("tasks", []) if t.get("enabled")]
    tasks_by_id = {t["id"]: t for t in data.get("tasks", [])}

    if not enabled:
        print("No enabled tasks. Create and enable a task first.")
        return

    source_to_tasks = {}
    for t in enabled:
        sid = t["source_channel_id"]
        source_to_tasks.setdefault(sid, []).append(t)

    print("\nResolving source channels...")
    resolved_entities = []
    resolved_ids = {}
    for sid in list(source_to_tasks.keys()):
        try:
            entity = await client.get_entity(sid)
            resolved_entities.append(entity)
            resolved_ids[entity.id] = sid
            print(f"  OK: {getattr(entity, 'title', sid)} (id={sid})")
        except Exception as e:
            print(f"  FAIL to resolve {sid}: {e}")

    if not resolved_entities:
        print("No source channels could be resolved.")
        return

    def get_sid(chat_id):
        if chat_id is None:
            return None
        abs_id = abs(chat_id) % (10 ** 12)
        return resolved_ids.get(abs_id) or resolved_ids.get(chat_id)

    async def new_handler(event):
        if not forwarder_active:
            return
        sid = get_sid(event.chat_id)
        tasks_for_src = source_to_tasks.get(sid, [])
        text_preview = repr((event.message.text or "")[:60])
        add_log(f"MSG chat={event.chat_id} text={text_preview}")

        # Detect if this message is a reply in the source channel
        reply_to_src_id = None
        if event.message.reply_to and event.message.reply_to.reply_to_msg_id:
            reply_to_src_id = event.message.reply_to.reply_to_msg_id

        async def process_task(task, reply_to_src_id):
            if task["id"] in paused_task_ids:
                add_log(f"  [PAUSED] '{task['name']}' skipped")
                return

            # Loop protection
            if check_loop(task["id"]):
                paused_task_ids.add(task["id"])
                add_log(f"  [LOOP] '{task['name']}' fired {LOOP_LIMIT}x in {LOOP_WINDOW}s — auto-paused!")
                return

            should_forward, modified_text = apply_filters(event.message, task["filters"])
            if not should_forward:
                add_log(f"  [SKIP] '{task['name']}' — filtered")
                return

            if task.get("filters", {}).get("rewrite_enabled"):
                text_to_rewrite = modified_text if modified_text is not None else getattr(event.message, 'text', '')
                if text_to_rewrite and text_to_rewrite.strip():
                    try:
                        import openrouter_client
                        prompt = task.get("filters", {}).get("rewrite_prompt", "Rewrite this to avoid copyright.")
                        add_log(f"  [AI] '{task['name']}' rewriting via OpenRouter...")
                        rewritten = await openrouter_client.generate_with_openrouter(text_to_rewrite, system_prompt=prompt)
                        if not rewritten.startswith("[AI Error:"):
                            modified_text = rewritten
                            add_log(f"  [AI OK] Rewrote {len(text_to_rewrite)} chars to {len(rewritten)} chars.")
                        else:
                            add_log(f"  [AI WARN] {rewritten}")
                    except Exception as e:
                        add_log(f"  [AI ERR] {e}")

            delay = task.get("filters", {}).get("delay_seconds", 0)
            if delay > 0:
                add_log(f"  [DELAY] '{task['name']}' waiting {delay}s")
                await asyncio.sleep(delay)

            dest_ids = task.get("destination_channel_ids") or [task.get("destination_channel_id")]
            for dest_id in dest_ids:
                # Find the corresponding reply target in this destination (if any)
                reply_to_dest_id = None
                if reply_to_src_id is not None:
                    reply_to_dest_id = db.get_reply_to_dest_id(task["id"], sid, reply_to_src_id, dest_id)

                try:
                    sent = await send_copy(client, dest_id, event.message, modified_text, reply_to=reply_to_dest_id)
                    text_for_db = modified_text if modified_text is not None else (event.message.text or "")
                    db.log_message(
                        task_id=task["id"],
                        source_channel_id=sid,
                        source_message_id=event.message.id,
                        dest_channel_id=dest_id,
                        dest_message_id=sent.id,
                        has_image=bool(event.message.photo),
                        text_content=text_for_db
                    )
                    if reply_to_dest_id:
                        add_log(f"  [OK] '{task['name']}' → {dest_id} (msg {sent.id}, reply to {reply_to_dest_id})")
                    else:
                        add_log(f"  [OK] '{task['name']}' → {dest_id} (msg {sent.id})")
                except FloodWaitError as e:
                    add_log(f"  [FLOOD] sleeping {e.seconds}s")
                    await asyncio.sleep(e.seconds)
                except Exception as e:
                    add_log(f"  [ERR] '{task['name']}' → {dest_id}: {e}")

        for task in tasks_for_src:
            asyncio.create_task(process_task(task, reply_to_src_id))

    async def edit_handler(event):
        if not forwarder_active:
            return
        sid = get_sid(event.chat_id)
        entries = db.get_dest_messages(sid, event.message.id)
        if not entries:
            return
        add_log(f"EDIT chat={event.chat_id} msg={event.message.id}")
        for entry in entries:
            task = tasks_by_id.get(entry["task_id"])
            if not task:
                continue
            if entry["task_id"] in paused_task_ids:
                continue
            should_forward, modified_text = apply_filters(event.message, task["filters"])
            if not should_forward:
                continue
            new_text = modified_text if modified_text is not None else (event.message.text or "")
            try:
                await client.edit_message(entry["dest_channel_id"], entry["dest_message_id"], text=new_text)
                add_log(f"  [EDIT OK] → {entry['dest_channel_id']} msg {entry['dest_message_id']}")
            except Exception as e:
                add_log(f"  [EDIT ERR] {entry['dest_channel_id']}: {e}")

    async def delete_handler(event):
        if not forwarder_active:
            return
        sid = get_sid(event.chat_id)
        for deleted_id in event.deleted_ids:
            entries = db.remove_messages(sid, deleted_id)
            if not entries:
                continue
            add_log(f"DEL msg={deleted_id}")
            for entry in entries:
                if entry["task_id"] in paused_task_ids:
                    continue
                try:
                    await client.delete_messages(entry["dest_channel_id"], [entry["dest_message_id"]])
                    add_log(f"  [DEL OK] → {entry['dest_channel_id']} msg {entry['dest_message_id']}")
                except Exception as e:
                    add_log(f"  [DEL ERR] {entry['dest_channel_id']}: {e}")

    async def cmd_delete_handler(event):
        global cancel_deletion
        if not forwarder_active:
            return
        add_log(f"  [CMD] received ..delete in {event.chat_id}")
        cancel_deletion = False
        try:
            msg_ids = []
            async for msg in client.iter_messages(event.chat_id, limit=3000):
                if cancel_deletion:
                    add_log(f"  [CMD] Deletion aborted by user in {event.chat_id}")
                    break
                if getattr(msg, 'out', True):
                    msg_ids.append(msg.id)
                if len(msg_ids) >= 100:
                    await client.delete_messages(event.chat_id, msg_ids)
                    msg_ids.clear()
                    await asyncio.sleep(2)
            if msg_ids:
                await client.delete_messages(event.chat_id, msg_ids)
            if not cancel_deletion:
                add_log(f"  [CMD] wiped messages from {event.chat_id}")
        except Exception as e:
            add_log(f"  [CMD ERR] {event.chat_id}: {e}")

    async def cmd_stop_handler(event):
        global cancel_deletion
        if not forwarder_active:
            return
        add_log(f"  [CMD] received ..stop in {event.chat_id}")
        cancel_deletion = True

    # Register handlers
    client.add_event_handler(new_handler, events.NewMessage(chats=resolved_entities))
    client.add_event_handler(edit_handler, events.MessageEdited(chats=resolved_entities))
    client.add_event_handler(delete_handler, events.MessageDeleted(chats=resolved_entities))
    client.add_event_handler(cmd_delete_handler, events.NewMessage(pattern=r"^\.\.delete$"))
    client.add_event_handler(cmd_stop_handler, events.NewMessage(pattern=r"^\.\.stop$"))

    active_handlers.extend([
        (new_handler, events.NewMessage),
        (edit_handler, events.MessageEdited),
        (delete_handler, events.MessageDeleted),
        (cmd_delete_handler, events.NewMessage),
        (cmd_stop_handler, events.NewMessage),
    ])

    async def image_cleanup_loop():
        while forwarder_active:
            try:
                data = load_tasks()
                for task in data.get("tasks", []):
                    days = task.get("filters", {}).get("image_delete_days", 0)
                    if days <= 0 or not task.get("enabled", True):
                        continue
                        
                    age_seconds = days * 24 * 3600
                    old_messages = db.get_old_image_messages(task["id"], age_seconds)
                    for msg in old_messages:
                        dest_id = msg['dest_channel_id']
                        msg_id = msg['dest_message_id']
                        try:
                            await client.delete_messages(dest_id, [msg_id])
                            db.delete_message_record(dest_id, msg_id)
                            add_log(f"  [CLEANUP] Deleted old image {msg_id} from {dest_id} (Task {task['id']})")
                        except Exception as e:
                            db.delete_message_record(dest_id, msg_id) # delete from db anyway if it fails
                        await asyncio.sleep(1)
            except Exception as e:
                add_log(f"  [CLEANUP LOOP ERR] {e}")
            await asyncio.sleep(3600)

    global cleanup_task
    cleanup_task = asyncio.create_task(image_cleanup_loop())

    forwarder_active = True
    add_log(f"Forwarder STARTED — watching {len(resolved_entities)} source(s), {len(enabled)} task(s).")


async def stop_forwarder(client):
    global forwarder_active, active_handlers, cleanup_task

    if not forwarder_active:
        print("Forwarder is not running.")
        return

    if cleanup_task:
        cleanup_task.cancel()
        cleanup_task = None

    for func, event_type in active_handlers:
        client.remove_event_handler(func, event_type)
    active_handlers.clear()
    forwarder_active = False
    add_log("Forwarder STOPPED.")


async def pause_task_menu():
    await list_tasks()
    try:
        tid = int((await ainput("\nEnter task ID to pause/resume: ")).strip())
    except ValueError:
        print("Invalid ID.")
        return

    data = load_tasks()
    task = next((t for t in data["tasks"] if t["id"] == tid), None)
    if not task:
        print(f"Task {tid} not found.")
        return

    if tid in paused_task_ids:
        paused_task_ids.discard(tid)
        loop_counter.pop(tid, None)  # reset loop counter on resume
        task["paused"] = False
        save_tasks(data)
        print(f"Task '{task['name']}' resumed.")
    else:
        paused_task_ids.add(tid)
        task["paused"] = True
        save_tasks(data)
        print(f"Task '{task['name']}' paused.")


async def view_logs():
    if not log_entries:
        print("No logs yet.")
        return
    print(f"\n--- Logs (last {len(log_entries)} entries) ---")
    for entry in log_entries[-50:]:  # show last 50
        print(entry)
    print()


async def view_statistics():
    data = load_tasks()
    tasks_by_id = {t["id"]: t for t in data.get("tasks", [])}
    stats = db.get_statistics()
    
    if not stats:
        print("No statistics available yet.")
        return
        
    print("\n--- Forwarding Statistics ---")
    print(f"{'Task Name':<25} {'Total Msgs':<12} {'Images':<10} {'Last Active'}")
    print("-" * 70)
    for row in stats:
        tname = tasks_by_id.get(row['task_id'], {}).get('name', f"Task {row['task_id']}")
        last_act = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['last_active'])) if row['last_active'] else 'Never'
        print(f"{tname:<25} {row['total_messages']:<12} {row['total_images'] or 0:<10} {last_act}")
    print()

async def view_threads():
    stats = db.get_threads()
    if not stats:
        print("No threads recorded yet (or no newly tracked replies).")
        return
    print("\n--- Deep Thread / Replied Messages Report ---")
    data = load_tasks()
    tasks_by_id = {t["id"]: t for t in data.get("tasks", [])}
    for row in stats:
        tname = tasks_by_id.get(row['task_id'], {}).get('name', f"Task {row['task_id']}")
        ptime = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['parent_time']))
        rtime = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['latest_reply_time']))
        preview = row['text_content'][:40].replace('\n', ' ') + '...' if row['text_content'] else '[Media/Empty]'
        print(f"[{tname}] Parent Msg {row['dest_message_id']} ({ptime})")
        print(f"  └─ Text: {preview}")
        print(f"  └─ Replies: {row['reply_count']} (Latest: {rtime})")
    print()

async def generate_finance_report():
    tasks_data = load_tasks().get("tasks", [])
    if not tasks_data:
        print("No tasks available.")
        return
        
    print("\nSelect task to generate report for:")
    for i, t in enumerate(tasks_data):
        print(f"  {i+1}. {t['name']}")
        
    choice = (await ainput("\nEnter task number: ")).strip()
    if not choice.isdigit() or not (1 <= int(choice) <= len(tasks_data)):
        print("Invalid choice.")
        return
        
    task = tasks_data[int(choice)-1]
    amount = (await ainput("How many recent messages to analyze? [50]: ")).strip()
    limit = int(amount) if amount.isdigit() else 50
    
    stats = db.get_threads(limit=1000)
    task_msgs = [row for row in stats if row['task_id'] == task['id']][:limit]
    
    if not task_msgs:
        print("No recent messages found for this task.")
        return
        
    print(f"\nGathering {len(task_msgs)} messages. Sending to OpenRouter AI...")
    combined_text = "\n\n---\n\n".join([f"Time: {time.strftime('%Y-%m-%d %H:%M', time.localtime(msg['parent_time']))}\n{msg['text_content']}" for msg in task_msgs])

    system_prompt = (
        "You are an expert financial analyst. Review the provided trading signals and messages. "
        "Extract key information such as: Buy/Sell targets, Stop Losses, Hit Targets, and overall performance. "
        "Summarize it into a concise, professional, Markdown-formatted financial report. "
        "Ignore memes, chatter, or irrelevant links."
    )
    
    try:
        import openrouter_client
        report = await openrouter_client.generate_with_openrouter(combined_text, system_prompt=system_prompt)
        print("\n================ FINANCIAL REPORT ================\n")
        print(report)
        print("\n==================================================\n")
    except Exception as e:
        print(f"Error communicating with OpenRouter: {e}")

async def main_menu(client):
    while True:
        status = "RUNNING" if forwarder_active else "STOPPED"
        paused_info = f" | Paused tasks: {sorted(paused_task_ids)}" if paused_task_ids else ""
        print(f"\n=== TridenB Autoforwarder === [Forwarder: {status}{paused_info}]")
        print("1.  Get Channel ID")
        print("2.  Create Forwarding Task")
        print("3.  List Tasks")
        print("4.  Toggle Task (enable/disable)")
        print("5.  Edit Task (source / destinations / filters)")
        print("6.  Delete Task")
        print("7.  Start Forwarder (background)")
        print("8.  Stop Forwarder")
        print("9.  Pause / Resume Task")
        print("10. View Logs")
        print("11. Duplicate Task")
        print("12. View Statistics")
        print("13. View Message Threads (Replies)")
        print("14. Generate AI Finance Report")
        print("0.  Exit")

        choice = (await ainput("\nSelect option: ")).strip()

        if choice == "1":
            await get_channel_id(client)
        elif choice == "2":
            await create_task(client)
        elif choice == "3":
            await list_tasks()
        elif choice == "4":
            await toggle_task()
        elif choice == "5":
            await edit_task()
        elif choice == "6":
            await delete_task()
        elif choice == "7":
            await start_forwarder(client)
        elif choice == "8":
            await stop_forwarder(client)
        elif choice == "9":
            await pause_task_menu()
        elif choice == "10":
            await view_logs()
        elif choice == "11":
            await duplicate_task()
        elif choice == "12":
            await view_statistics()
        elif choice == "13":
            await view_threads()
        elif choice == "14":
            await generate_finance_report()
        elif choice == "0":
            if forwarder_active:
                confirm = (await ainput("Forwarder is running. Stop and exit? [y/N]: ")).strip().lower()
                if confirm not in ("y", "yes"):
                    continue
                await stop_forwarder(client)
            print("Goodbye.")
            break
        else:
            print("Invalid option.")


async def main():
    load_dotenv()
    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    phone = os.getenv("PHONE")

    if not api_id or not api_hash or not phone:
        print("Error: API_ID, API_HASH, and PHONE must be set in .env")
        sys.exit(1)

    client = TelegramClient(
        SESSION_NAME, 
        int(api_id), 
        api_hash,
        connection_retries=None,  # Retry forever on disconnect
        retry_delay=5,            # Wait 5 seconds between retries
        auto_reconnect=True       # Automatically reconnect
    )
    await client.start(phone=phone)
    print("Authenticated successfully.")
    sync_paused_from_tasks()

    try:
        await main_menu(client)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
