"""
Env vars
  SLACK_BOT_TOKEN           xoxb-...
  SLACK_APP_TOKEN           xapp-... (app-level token, connections:write)
  CONFESSIONS_CHANNEL_ID    channel where approved confessions are posted
  REVIEW_CHANNEL_ID         private moderator channel
  CONFESSIONS_KEY           Fernet key (see README / setup notes)
  ALLOW_BROADCASTS_FROM_OP  optional, "1" lets the confessor use @channel/@here/@everyone

Run `python setup.py` for an interactive wizard that fills these in for you.
"""

VERSION = "1.0.6"

import hashlib
import hmac
import os
import re
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError

load_dotenv()

BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
CONFESSIONS = "C0C5UDLBQQ0"
REVIEW = "C0C4JMMU34P"
ALLOW_BROADCASTS_FROM_OP = "0"

MAX_LEN = 2500 
OP_NAME = "prox5"
OP_ICON = ":anonymous_lachlan:"

app = App(token=BOT_TOKEN)
BOT_USER_ID = app.client.auth_test()["user_id"]

_KEY = os.environ["CONFESSIONS_KEY"].encode()
_fernet = Fernet(_KEY)
_INDEX_KEY = hmac.new(_KEY, b"dm-channel-index", hashlib.sha256).digest()


def enc(s: str) -> str:
    return _fernet.encrypt(s.encode()).decode()


def dec(s: str) -> str:
    return _fernet.decrypt(s.encode()).decode()


def dm_index(dm_channel: str) -> str:
    return hmac.new(_INDEX_KEY, dm_channel.encode(), hashlib.sha256).hexdigest()

db = sqlite3.connect("confessions.db", check_same_thread=False)
db.row_factory = sqlite3.Row
_lock = threading.Lock()

db.executescript(
    """
    CREATE TABLE IF NOT EXISTS confessions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_enc        TEXT NOT NULL,     -- encrypted author Slack ID
        dm_channel_enc  TEXT NOT NULL,     -- encrypted DM channel ID
        dm_index        TEXT NOT NULL,     -- HMAC of DM channel, for lookups
        dm_ts           TEXT NOT NULL,     -- the author's original DM (thread root)
        text_enc        TEXT NOT NULL,     -- encrypted confession text
        status          TEXT NOT NULL DEFAULT 'draft',  -- draft|pending|approved|rejected
        number          INTEGER,           -- public number, approved only
        pub_ts          TEXT,              -- the post in #confessions (thread root)
        subscribed      INTEGER NOT NULL DEFAULT 1  -- 0 if the OP opted out of channel replies via /unsubscribe
    );
    CREATE INDEX IF NOT EXISTS idx_conf_dm  ON confessions(dm_index, dm_ts);
    CREATE INDEX IF NOT EXISTS idx_conf_pub ON confessions(pub_ts);

    -- Every mirrored message pair (including the two thread roots),
    -- used for edits, deletes, and reactions.
    CREATE TABLE IF NOT EXISTS relays (
        confession_id INTEGER NOT NULL,
        pub_ts        TEXT NOT NULL PRIMARY KEY,
        dm_ts         TEXT NOT NULL,
        UNIQUE (confession_id, dm_ts)
    );
    """
)
db.commit()

_conf_cols = [r[1] for r in db.execute("PRAGMA table_info(confessions)").fetchall()]
if "subscribed" not in _conf_cols:
    db.execute("ALTER TABLE confessions ADD COLUMN subscribed INTEGER NOT NULL DEFAULT 1")
    db.commit()


def q(sql, params=()):
    with _lock:
        rows = db.execute(sql, params).fetchall()
        db.commit()
        return rows


def q1(sql, params=()):
    rows = q(sql, params)
    return rows[0] if rows else None


def run(sql, params=()) -> int:
    with _lock:
        cur = db.execute(sql, params)
        db.commit()
        return cur.rowcount


def transition(cid: int, from_status: str, to_status: str) -> bool:
    """Atomic status change; False if someone else already acted on it."""
    return run(
        "UPDATE confessions SET status=? WHERE id=? AND status=?", (to_status, cid, from_status)
    ) == 1


def conf(cid):
    return q1("SELECT * FROM confessions WHERE id=?", (cid,))


def conf_by_dm_root(dm_channel, ts):
    return q1("SELECT * FROM confessions WHERE dm_index=? AND dm_ts=?", (dm_index(dm_channel), ts))


def conf_by_pub_root(ts):
    return q1("SELECT * FROM confessions WHERE pub_ts=?", (ts,))


def relay_by_pub(ts):
    return q1("SELECT * FROM relays WHERE pub_ts=?", (ts,))


def relay_by_dm(dm_channel, ts):
    return q1(
        """SELECT r.* FROM relays r JOIN confessions c ON c.id = r.confession_id
           WHERE c.dm_index=? AND r.dm_ts=?""",
        (dm_index(dm_channel), ts),
    )


_BROADCAST_RE = re.compile(r"<!(channel|here|everyone)(\|[^>]*)?>")


def defuse_broadcasts_text(text: str) -> str:
    return _BROADCAST_RE.sub(lambda m: "@" + m.group(1), text or "")


def defuse_broadcasts_blocks(node):
    if isinstance(node, list):
        return [defuse_broadcasts_blocks(n) for n in node]
    if isinstance(node, dict):
        if node.get("type") == "broadcast":
            return {"type": "text", "text": "@" + node.get("range", "here")}
        return {k: defuse_broadcasts_blocks(v) for k, v in node.items()}
    return node


def message_payload(msg: dict, allow_broadcasts: bool) -> dict:
    """Rebuild a message as faithfully as possible (rich_text blocks keep formatting,
    mentions, emoji, links)."""
    text = msg.get("text") or ""
    blocks = msg.get("blocks")
    if not allow_broadcasts:
        text = defuse_broadcasts_text(text)
        if blocks:
            blocks = defuse_broadcasts_blocks(blocks)
    if not text and not blocks:
        text = "_shared a file_" if msg.get("files") else "(empty message)"
    payload = {"text": text or " "}
    if blocks:
        payload["blocks"] = blocks
    return payload


_profile_cache = {}


def profile(user_id: str):
    hit = _profile_cache.get(user_id)
    if hit and hit[2] > time.time():
        return hit[0], hit[1]
    user = app.client.users_info(user=user_id)["user"]
    p = user.get("profile", {})
    name = p.get("display_name") or p.get("real_name") or user.get("name") or "Someone"
    icon = p.get("image_192") or p.get("image_72")
    _profile_cache[user_id] = (name, icon, time.time() + 600)
    return name, icon


def quote(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.splitlines())


def sent_time(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def relay_files(msg, channel, thread_ts, client):
    for f in msg.get("files", []) or []:
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {BOT_TOKEN}"})
        with urllib.request.urlopen(req) as resp:
            data = resp.read()
        client.files_upload_v2(
            channel=channel,
            thread_ts=thread_ts,
            file=data,
            filename=f.get("name") or "file",
            title=f.get("title") or f.get("name"),
        )


def relay_new(msg, c, side, client):
    """side='pub': channel reply -> DM.  side='dm': confessor reply -> channel."""
    dm_channel = dec(c["dm_channel_enc"])
    if side == "pub":
        name, icon = profile(msg["user"])
        dst_channel, dst_root = dm_channel, c["dm_ts"]
        identity = {"username": name, "icon_url": icon} if icon else {"username": name}
        payload = message_payload(msg, allow_broadcasts=True)
    else:
        dst_channel, dst_root = CONFESSIONS, c["pub_ts"]
        identity = {"username": OP_NAME, "icon_emoji": OP_ICON}
        payload = message_payload(msg, allow_broadcasts=ALLOW_BROADCASTS_FROM_OP)

    res = client.chat_postMessage(channel=dst_channel, thread_ts=dst_root, **payload, **identity)

    pub_ts, dm_ts = (msg["ts"], res["ts"]) if side == "pub" else (res["ts"], msg["ts"])
    run("INSERT OR IGNORE INTO relays (confession_id, pub_ts, dm_ts) VALUES (?,?,?)",
        (c["id"], pub_ts, dm_ts))

    relay_files(msg, dst_channel, dst_root, client)


def mirror_target(side, channel, ts):
    """Given a source message, return (confession_row, dst_channel, dst_ts) or None."""
    if side == "pub":
        r = relay_by_pub(ts)
        if not r:
            return None
        c = conf(r["confession_id"])
        return c, dec(c["dm_channel_enc"]), r["dm_ts"]
    r = relay_by_dm(channel, ts)
    if not r:
        return None
    return conf(r["confession_id"]), CONFESSIONS, r["pub_ts"]


def relay_edit(event, side, client):
    new = event.get("message", {})
    if new.get("bot_id") or new.get("user") == BOT_USER_ID or "edited" not in new:
        return
    channel, ts = event["channel"], new["ts"]

    if side == "dm":
        root = conf_by_dm_root(channel, ts)
        if root:
            # Editing the original DM only updates an unsubmitted draft.
            # Approved confessions never change without moderation.
            if root["status"] == "draft":
                run("UPDATE confessions SET text_enc=? WHERE id=?", (enc(new.get("text") or ""), root["id"]))
            return

    target = mirror_target(side, channel, ts)
    if not target:
        return
    _, dst_channel, dst_ts = target
    allow = True if side == "pub" else ALLOW_BROADCASTS_FROM_OP
    client.chat_update(channel=dst_channel, ts=dst_ts, **message_payload(new, allow))


def relay_delete(event, side, client):
    prev = event.get("previous_message", {})
    if prev.get("bot_id") or prev.get("user") == BOT_USER_ID:
        return
    channel, ts = event["channel"], event["deleted_ts"]
    if side == "dm" and conf_by_dm_root(channel, ts):
        return  # deleting the original DM doesn't unpublish a moderated confession
    target = mirror_target(side, channel, ts)
    if not target:
        return
    _, dst_channel, dst_ts = target
    try:
        client.chat_delete(channel=dst_channel, ts=dst_ts)
    except SlackApiError as e:
        if e.response["error"] != "message_not_found":
            raise
    key = "pub_ts" if side == "pub" else "dm_ts"
    run(f"DELETE FROM relays WHERE {key}=?", (ts,))

@app.event("message")
def on_message(event, client, logger):
    channel = event.get("channel", "")
    is_dm = event.get("channel_type") == "im" or channel.startswith("D")
    is_pub = channel == CONFESSIONS
    if not (is_dm or is_pub):
        return
    side = "dm" if is_dm else "pub"

    subtype = event.get("subtype")
    if subtype == "message_changed":
        return relay_edit(event, side, client)
    if subtype == "message_deleted":
        return relay_delete(event, side, client)
    if subtype not in (None, "file_share", "thread_broadcast"):
        return
    if event.get("bot_id") or event.get("user") == BOT_USER_ID:
        return

    if is_dm:
        handle_dm(event, client)
    else:
        handle_pub(event, client)


def handle_pub(event, client):
    thread_ts = event.get("thread_ts")
    if not thread_ts or thread_ts == event["ts"]:
        return
    c = conf_by_pub_root(thread_ts)
    if c and c["subscribed"]:
        relay_new(event, c, "pub", client)


_SUBSCRIPTION_COMMANDS = ("/unsubscribe", "/subscribe")


def handle_subscription_command(client, dm_channel, thread_ts, c, cmd):
    want = cmd == "/subscribe"
    if bool(c["subscribed"]) == want:
        msg = ("You're already subscribed to updates on this confession." if want else
               "You're already unsubscribed from updates on this confession. "
               "Send */subscribe* any time to turn them back on.")
    else:
        run("UPDATE confessions SET subscribed=? WHERE id=?", (1 if want else 0, c["id"]))
        msg = ("You're subscribed again. Replies from the channel will show up in this thread." if want else
               "You're unsubscribed. Replies from the channel won't be sent here anymore. "
               "Send */subscribe* any time to turn them back on. You can still reply here and it'll post to the channel.")
    client.chat_postMessage(channel=dm_channel, thread_ts=thread_ts, text=msg)


def handle_dm(event, client):
    dm_channel = event["channel"]
    thread_ts = event.get("thread_ts")
    if thread_ts and thread_ts != event["ts"]:
        c = conf_by_dm_root(dm_channel, thread_ts)
        cmd = (event.get("text") or "").strip().lower()
        if not c:
            msg = "I couldn't match this thread to a confession. Send a new message (not in a thread) to start one."
        elif cmd in _SUBSCRIPTION_COMMANDS:
            handle_subscription_command(client, dm_channel, thread_ts, c, cmd)
            return
        elif c["status"] == "approved" and c["pub_ts"]:
            relay_new(event, c, "dm", client)
            return
        else:
            msg = {
                "draft": "Use the *Stage* or *Cancel* buttons above first.",
                "pending": "This confession is still being reviewed, so there's no thread to reply to yet.",
                "rejected": "This confession wasn't approved, so replies can't be posted.",
                "approved": "Hang on, this confession is still being posted. Try again in a moment.",
            }[c["status"]]
        client.chat_postMessage(channel=dm_channel, thread_ts=thread_ts, text=msg)
        return

    text = (event.get("text") or "").strip()
    if not text:
        client.chat_postMessage(channel=dm_channel, thread_ts=event["ts"],
                                text="Confessions need some text. Attachments can't be submitted.")
        return
    if len(text) > MAX_LEN:
        client.chat_postMessage(channel=dm_channel, thread_ts=event["ts"],
                                text=f"That's too long. Please keep confessions under {MAX_LEN} characters.")
        return

    with _lock:
        cur = db.execute(
            """INSERT INTO confessions (user_enc, dm_channel_enc, dm_index, dm_ts, text_enc)
               VALUES (?,?,?,?,?)""",
            (enc(event["user"]), enc(dm_channel), dm_index(dm_channel), event["ts"], enc(text)),
        )
        db.commit()
        cid = cur.lastrowid

    note = "\n_Attachments won't be included, only the text._" if event.get("files") else ""
    client.chat_postMessage(
        channel=dm_channel,
        thread_ts=event["ts"],
        text="Submit this as an anonymous confession?",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Submit the message above as an *anonymous confession*? "
                            "Moderators review it before it's posted, and they won't see who you are." + note,
                },
            },
            {
                "type": "actions",
                "elements": [
                    {"type": "button", "action_id": "stage", "value": str(cid), "style": "primary",
                     "text": {"type": "plain_text", "text": "Stage"}},
                    {"type": "button", "action_id": "cancel", "value": str(cid),
                     "text": {"type": "plain_text", "text": "Cancel"}},
                ],
            },
        ],
    )

def owns(body, c) -> bool:
    return c is not None and dm_index(body["channel"]["id"]) == c["dm_index"]


def replace_prompt(client, body, text):
    client.chat_update(
        channel=body["channel"]["id"], ts=body["message"]["ts"], text=text,
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    )


@app.action("stage")
def on_stage(ack, body, client):
    ack()
    cid = int(body["actions"][0]["value"])
    c = conf(cid)
    if not owns(body, c) or not transition(cid, "draft", "pending"):
        return
    c = conf(cid)  # re-read in case the draft was edited
    text = dec(c["text_enc"])
    client.chat_postMessage(channel=REVIEW, text="New confession to review", blocks=review_blocks(cid, text))
    replace_prompt(client, body, ":incoming_envelope: Sent to the moderators for review. I'll update you in this thread.")


@app.action("cancel")
def on_cancel(ack, body, client):
    ack()
    cid = int(body["actions"][0]["value"])
    c = conf(cid)
    if not owns(body, c):
        return
    if run("DELETE FROM confessions WHERE id=? AND status='draft'", (cid,)) == 1:
        replace_prompt(client, body, ":wastebasket: Cancelled. Nothing was sent or kept.")

def review_blocks(cid: int, text: str):
    preview = defuse_broadcasts_text(text)
    return [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*New submission* (queue #{cid})\n{quote(preview)}"}},
        {
            "type": "actions",
            "elements": [
                {"type": "button", "action_id": "approve", "value": str(cid), "style": "primary",
                 "text": {"type": "plain_text", "text": "Approve"}},
                {"type": "button", "action_id": "reject", "value": str(cid),
                 "text": {"type": "plain_text", "text": "Reject"}},
                {"type": "button", "action_id": "reject_report", "value": str(cid), "style": "danger",
                 "text": {"type": "plain_text", "text": "Reject & Report"},
                 "confirm": {
                     "title": {"type": "plain_text", "text": "Reject and report?"},
                     "text": {"type": "plain_text",
                              "text": "This reveals the author's Slack ID to you privately so you can file a report in Shroud."},
                     "confirm": {"type": "plain_text", "text": "Report"},
                     "deny": {"type": "plain_text", "text": "Cancel"},
                 }},
            ],
        },
    ]


def close_review(client, body, outcome: str):
    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text=outcome,
        blocks=[body["message"]["blocks"][0],
                {"type": "context", "elements": [{"type": "mrkdwn", "text": outcome}]}],
    )


@app.action("approve")
def on_approve(ack, body, client):
    ack()
    cid = int(body["actions"][0]["value"])
    if not transition(cid, "pending", "approved"):
        return

    with _lock:
        number = db.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM confessions").fetchone()[0]
        db.execute("UPDATE confessions SET number=? WHERE id=?", (number, cid))
        db.commit()

    c = conf(cid)
    text = dec(c["text_enc"])
    if not ALLOW_BROADCASTS_FROM_OP:
        text = defuse_broadcasts_text(text)

    post = client.chat_postMessage(channel=CONFESSIONS, text=f"#{number}\n{text}")
    run("UPDATE confessions SET pub_ts=? WHERE id=?", (post["ts"], cid))
    run("INSERT OR IGNORE INTO relays (confession_id, pub_ts, dm_ts) VALUES (?,?,?)",
        (cid, post["ts"], c["dm_ts"]))

    close_review(client, body, f":white_check_mark: Approved by <@{body['user']['id']}>. Posted as Confession #{number}")
    client.chat_postMessage(
        channel=dec(c["dm_channel_enc"]),
        thread_ts=c["dm_ts"],
        text=f":tada: Approved and posted as *Confession #{number}*. Replies from the channel will appear "
             f"in this thread, and anything you send here is posted there as {OP_NAME}. "
             f"Send */unsubscribe* any time to stop channel replies from appearing here.",
    )


def notify_rejected(client, c):
    client.chat_postMessage(
        channel=dec(c["dm_channel_enc"]),
        thread_ts=c["dm_ts"],
        text="Your confession wasn't approved by the moderators.",
    )


@app.action("reject")
def on_reject(ack, body, client):
    ack()
    cid = int(body["actions"][0]["value"])
    if not transition(cid, "pending", "rejected"):
        return
    close_review(client, body, f":x: Rejected by <@{body['user']['id']}>")
    notify_rejected(client, conf(cid))


@app.action("reject_report")
def on_reject_report(ack, body, client):
    ack()
    cid = int(body["actions"][0]["value"])
    if not transition(cid, "pending", "rejected"):
        return
    c = conf(cid)
    mod = body["user"]["id"]
    author = dec(c["user_enc"])
    content = dec(c["text_enc"]).replace("```", "'''")

    report = (
        f"Confession report\n"
        f"Queue ID: {cid}\n"
        f"Author Slack ID: {author}\n"
        f"Time sent: {sent_time(c['dm_ts'])}\n"
        f"Reported by: {mod}\n"
        f"Message:\n{content}"
    )
    client.chat_postMessage(
        channel=mod,
        text=f":rotating_light: Report for queue #{cid}. Author: <@{author}>. Copy this into Shroud:",
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f":rotating_light: *Report for queue #{cid}.* Author: <@{author}>\nCopy this into Shroud:"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"```{report}```"}},
        ],
    )
    close_review(client, body, f":rotating_light: Rejected & reported by <@{mod}>. Report details sent to them privately.")
    notify_rejected(client, c)


def someone_else_reacted(client, channel, ts, emoji) -> bool:
    msg = client.reactions_get(channel=channel, timestamp=ts, full=True)["message"]
    for r in msg.get("reactions", []):
        if r["name"] == emoji:
            return any(u != BOT_USER_ID for u in r.get("users", []))
    return False


def mirror_reaction(event, client):
    if event.get("user") == BOT_USER_ID:
        return  # our own mirrored reactions
    item = event.get("item", {})
    if item.get("type") != "message":
        return
    channel, ts, emoji = item["channel"], item["ts"], event["reaction"]

    if channel == CONFESSIONS:
        side = "pub"
    elif channel.startswith("D"):
        side = "dm"
    else:
        return
    target = mirror_target(side, channel, ts)
    if not target:
        return
    _, dst_channel, dst_ts = target

    want = someone_else_reacted(client, channel, ts, emoji)
    try:
        if want:
            client.reactions_add(channel=dst_channel, timestamp=dst_ts, name=emoji)
        else:
            client.reactions_remove(channel=dst_channel, timestamp=dst_ts, name=emoji)
    except SlackApiError as e:
        if e.response["error"] not in ("already_reacted", "no_reaction", "invalid_name"):
            raise


app.event("reaction_added")(mirror_reaction)
app.event("reaction_removed")(mirror_reaction)


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
