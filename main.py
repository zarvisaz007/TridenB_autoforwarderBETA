import asyncio
import json
import os
import re
import sys
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import PeerChannel
from telethon.errors import FloodWaitError

TASKS_FILE = "tasks.json"
SESSION_NAME = "tridenb_autoforwarder"


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


def apply_filters(message, filters):
    # Media checks
    if filters.get("skip_images") and message.photo:
        return (False, None)
    if filters.get("skip_audio") and (message.audio or message.voice):
        return (False, None)
    if filters.get("skip_videos") and message.video:
        return (False, None)

    text = message.text or ""

    # Blacklist check
    blacklist = filters.get("blacklist_words", [])
    text_lower = text.lower()
    for word in blacklist:
        if word.lower() in text_lower:
            return (False, None)

    # Clean pipeline
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


# ---------- Async CLI functions ----------

async def get_channel_id(client):
    print("\n--- All Channels / Groups ---")
    print(f"{'Name':<40} {'Channel ID':<20}")
    print("-" * 60)
    async for dialog in client.iter_dialogs():
        if dialog.is_channel or dialog.is_group:
            name = dialog.name or "(no name)"
            cid = dialog.entity.id
            # Channels/supergroups use -100 prefix in bot API style
            if dialog.is_channel:
                full_id = int(f"-100{cid}")
            else:
                full_id = -cid if cid > 0 else cid
            print(f"{name:<40} {full_id:<20}")
    print()


async def create_task(client):
    data = load_tasks()
    print("\n--- Create Forwarding Task ---")
    name = input("Task name: ").strip()

    src_raw = input("Source channel ID (e.g. -1001234567890): ").strip()
    dst_raw = input("Destination channel IDs (comma-separated, e.g. -1001111111111,-1002222222222): ").strip()

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

    def prompt_list(prompt):
        raw = input(f"  {prompt} (comma-separated, blank=none): ").strip()
        return [x.strip() for x in raw.split(",") if x.strip()] if raw else []

    def prompt_bool(prompt, default=False):
        val = input(f"  {prompt} [y/N]: ").strip().lower()
        return val in ("y", "yes")

    blacklist = prompt_list("Blacklist words")
    clean_words = prompt_list("Clean words (remove from text)")
    clean_urls = prompt_bool("Remove URLs?")
    clean_usernames = prompt_bool("Remove @usernames?")
    skip_images = prompt_bool("Skip image messages?")
    skip_audio = prompt_bool("Skip audio/voice messages?")
    skip_videos = prompt_bool("Skip video messages?")

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

    print(f"\n{'ID':<5} {'Name':<20} {'Enabled':<8} {'Source':<22} {'Destinations'}")
    print("-" * 90)
    for t in tasks:
        status = "Yes" if t.get("enabled") else "No"
        dests = ", ".join(str(d) for d in t.get("destination_channel_ids", [t.get("destination_channel_id", "?")]))
        print(f"{t['id']:<5} {t['name']:<20} {status:<8} {t['source_channel_id']:<22} {dests}")


async def toggle_task():
    await list_tasks()
    try:
        tid = int(input("\nEnter task ID to toggle: ").strip())
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


async def edit_task_filters():
    await list_tasks()
    try:
        tid = int(input("\nEnter task ID to edit filters: ").strip())
    except ValueError:
        print("Invalid ID.")
        return

    data = load_tasks()
    task = next((t for t in data["tasks"] if t["id"] == tid), None)
    if not task:
        print(f"Task {tid} not found.")
        return

    filters = task["filters"]
    print(f"\nCurrent filters for task '{task['name']}':")
    for k, v in filters.items():
        print(f"  {k}: {v}")

    print("\nEdit filters (press Enter to keep current value):")

    def edit_list(key, prompt):
        current = filters.get(key, [])
        raw = input(f"  {prompt} [{', '.join(current) or 'none'}]: ").strip()
        if raw:
            filters[key] = [x.strip() for x in raw.split(",") if x.strip()]

    def edit_bool(key, prompt):
        current = filters.get(key, False)
        raw = input(f"  {prompt} [{'Y' if current else 'N'}]: ").strip().lower()
        if raw in ("y", "yes"):
            filters[key] = True
        elif raw in ("n", "no"):
            filters[key] = False

    edit_list("blacklist_words", "Blacklist words")
    edit_list("clean_words", "Clean words")
    edit_bool("clean_urls", "Remove URLs?")
    edit_bool("clean_usernames", "Remove @usernames?")
    edit_bool("skip_images", "Skip images?")
    edit_bool("skip_audio", "Skip audio?")
    edit_bool("skip_videos", "Skip videos?")

    save_tasks(data)
    print("Filters updated.")


async def delete_task():
    await list_tasks()
    try:
        tid = int(input("\nEnter task ID to delete: ").strip())
    except ValueError:
        print("Invalid ID.")
        return

    data = load_tasks()
    task = next((t for t in data["tasks"] if t["id"] == tid), None)
    if not task:
        print(f"Task {tid} not found.")
        return

    confirm = input(f"Delete task '{task['name']}' (ID {tid})? [y/N]: ").strip().lower()
    if confirm in ("y", "yes"):
        data["tasks"] = [t for t in data["tasks"] if t["id"] != tid]
        save_tasks(data)
        print(f"Task {tid} deleted.")
    else:
        print("Cancelled.")


async def run_forwarder(client):
    data = load_tasks()
    enabled = [t for t in data.get("tasks", []) if t.get("enabled")]

    if not enabled:
        print("No enabled tasks. Create and enable a task first.")
        return

    # Resolve source channel entities so Telethon can match events correctly
    source_to_tasks = {}
    for t in enabled:
        sid = t["source_channel_id"]
        source_to_tasks.setdefault(sid, []).append(t)

    print("\nResolving source channels...")
    resolved_entities = []
    resolved_ids = {}  # entity -> original task sid key
    for sid in list(source_to_tasks.keys()):
        try:
            entity = await client.get_entity(sid)
            resolved_entities.append(entity)
            resolved_ids[entity.id] = sid
            print(f"  OK: {getattr(entity, 'title', sid)} (stored id={sid}, entity id={entity.id})")
        except Exception as e:
            print(f"  FAIL to resolve {sid}: {e}")

    if not resolved_entities:
        print("No source channels could be resolved. Check your channel IDs.")
        return

    print(f"\nForwarder running — watching {len(resolved_entities)} source(s) across {len(enabled)} task(s).")
    print("Press Ctrl+C to stop.\n")

    @client.on(events.NewMessage(chats=resolved_entities))
    async def handler(event):
        raw_id = event.chat_id
        # Map event chat_id back to the stored task sid key
        # event.chat_id may be -100X format; entity.id is raw X
        abs_id = abs(raw_id) % (10 ** 12)  # strip -100 prefix
        sid = resolved_ids.get(abs_id) or resolved_ids.get(raw_id)
        tasks_for_src = source_to_tasks.get(sid, [])

        text_preview = repr((event.message.text or "")[:60])
        print(f"[MSG] chat_id={raw_id} abs={abs_id} sid={sid} text={text_preview}")

        for task in tasks_for_src:
            should_forward, modified_text = apply_filters(event.message, task["filters"])
            if not should_forward:
                print(f"  [SKIP] '{task['name']}' — filtered out")
                continue
            dest_ids = task.get("destination_channel_ids") or [task.get("destination_channel_id")]
            for dest_id in dest_ids:
                try:
                    if modified_text is None:
                        await client.forward_messages(dest_id, event.message)
                    else:
                        await client.send_message(dest_id, modified_text)
                    print(f"  [OK] '{task['name']}' → {dest_id}")
                except FloodWaitError as e:
                    print(f"  [FLOOD] sleeping {e.seconds}s")
                    await asyncio.sleep(e.seconds)
                except Exception as e:
                    print(f"  [ERR] '{task['name']}' → {dest_id}: {e}")

    await client.run_until_disconnected()


async def main_menu(client):
    while True:
        print("\n=== TridenB Autoforwarder ===")
        print("1. Get Channel ID")
        print("2. Create Forwarding Task")
        print("3. List Tasks")
        print("4. Toggle Task (enable/disable)")
        print("5. Edit Task Filters")
        print("6. Delete Task")
        print("7. Run Forwarder")
        print("0. Exit")

        choice = input("\nSelect option: ").strip()

        if choice == "1":
            await get_channel_id(client)
        elif choice == "2":
            await create_task(client)
        elif choice == "3":
            await list_tasks()
        elif choice == "4":
            await toggle_task()
        elif choice == "5":
            await edit_task_filters()
        elif choice == "6":
            await delete_task()
        elif choice == "7":
            await run_forwarder(client)
        elif choice == "0":
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

    client = TelegramClient(SESSION_NAME, int(api_id), api_hash)
    await client.start(phone=phone)
    print("Authenticated successfully.")

    try:
        await main_menu(client)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
