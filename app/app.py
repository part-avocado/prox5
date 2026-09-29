VERSION = "1.0.14"

import hashlib
import hmac
import json
import os
import random
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
CONFESSIONS = os.environ["CONFESSIONS_CHANNEL_ID"]
REVIEW = os.environ["REVIEW_CHANNEL_ID"]
ALLOW_BROADCASTS_FROM_OP = os.environ.get("ALLOW_BROADCASTS_FROM_OP", "0") == "1"

MAX_LEN = 2500 
OP_NAME = "prox5"
OP_ICON = ":anonymous_lachlan:"

app = App(token=BOT_TOKEN)
_auth = app.client.auth_test()
BOT_USER_ID = _auth["user_id"]
print(
    f"[prox5] v{VERSION} connected to Slack as {_auth.get('user')} "
    f"({BOT_USER_ID}) in team {_auth.get('team')} ({_auth.get('team_id')})",
    flush=True,
)

_KEY = os.environ["CONFESSIONS_KEY"].encode()
_fernet = Fernet(_KEY)
_INDEX_KEY = hmac.new(_KEY, b"dm-channel-index", hashlib.sha256).digest()


def enc(s: str) -> str:
    return _fernet.encrypt(s.encode()).decode()


def dec(s: str) -> str:
    return _fernet.decrypt(s.encode()).decode()


def dm_index(dm_channel: str) -> str:
    return hmac.new(_INDEX_KEY, dm_channel.encode(), hashlib.sha256).hexdigest()

DB_PATH = os.environ.get("DB_PATH", "data/confessions.db")
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
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
        number          INTEGER,           -- assigned once staged for review; reused as the public post number if approved
        pub_ts          TEXT,              -- the post in #confessions (thread root)
        subscribed      INTEGER NOT NULL DEFAULT 1  -- 0 if the OP opted out of channel replies via `unsub`
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
        return 
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

_UNSUB_WORDS = ("unsub", "unsubscribe")
_SUB_WORDS = ("sub", "subscribe")


def handle_subscription_command(client, dm_channel, thread_ts, c, want: bool):
    if bool(c["subscribed"]) == want:
        msg = ("You're already subscribed to updates on this prox5 submission." if want else
               "You're already unsubscribed from updates on this prox5 submission. "
               "Send `sub` any time to turn them back on.")
    else:
        run("UPDATE confessions SET subscribed=? WHERE id=?", (1 if want else 0, c["id"]))
        msg = ("You're subscribed again! Replies from the channel will show up in this thread." if want else
               "You're unsubscribed. Replies from the channel won't be sent here anymore. "
               "Send `sub` any time to turn them back on. You can still reply here and it'll post to the channel.")
    client.chat_postMessage(channel=dm_channel, thread_ts=thread_ts, text=msg)


def most_recent_conf(dm_channel):
    return q1(
        "SELECT * FROM confessions WHERE dm_index=? ORDER BY id DESC LIMIT 1",
        (dm_index(dm_channel),),
    )

def handle_slash_subscription(ack, command, client, want: bool):
    ack()
    dm_channel = command["channel_id"]
    if command.get("channel_name") != "directmessage":
        client.chat_postEphemeral(
            channel=dm_channel, user=command["user_id"],
            text="Please use this command in your DM with prox5, not in a channel.",
        )
        return
    c = most_recent_conf(dm_channel)
    if not c:
        client.chat_postMessage(channel=dm_channel, text="You haven't submitted a prox5 submission yet.")
        return
    handle_subscription_command(client, dm_channel, c["dm_ts"], c, want)


@app.command("/sprox")
def cmd_sprox(ack, command, client):
    handle_slash_subscription(ack, command, client, True)


@app.command("/uprox")
def cmd_uprox(ack, command, client):
    handle_slash_subscription(ack, command, client, False)


def handle_dm(event, client):
    dm_channel = event["channel"]
    thread_ts = event.get("thread_ts")
    if thread_ts and thread_ts != event["ts"]:
        c = conf_by_dm_root(dm_channel, thread_ts)
        cmd = (event.get("text") or "").strip().lower().lstrip("/")
        if not c:
            msg = "Hmm... I couldn't match this thread. Send a new message (not in a thread) to start one!"
        elif cmd in _UNSUB_WORDS or cmd in _SUB_WORDS:
            handle_subscription_command(client, dm_channel, thread_ts, c, cmd in _SUB_WORDS)
            return
        elif c["status"] == "approved" and c["pub_ts"]:
            relay_new(event, c, "dm", client)
            return
        else:
            msg = {
                "draft": "Use the *Stage* or *Cancel* buttons above first!",
                "pending": "This prox5 submission is still under review. Please try again later!",
                "rejected": "This prox5 submission was not approved.",
                "approved": "Please wait... We're  to send your prox5 submission. Try again momentarily, or ping @partavocado on Slack.",
            }[c["status"]]
        client.chat_postMessage(channel=dm_channel, thread_ts=thread_ts, text=msg)
        return

    text = (event.get("text") or "").strip()
    if not text:
        client.chat_postMessage(channel=dm_channel, thread_ts=event["ts"],
                                text="prox5 submissions need at least _some_ some text. Attachments cannot be submitted yet.")
        return
    if len(text) > MAX_LEN:
        client.chat_postMessage(channel=dm_channel, thread_ts=event["ts"],
                                text=f"Woah that's too long! Please keep prox5 submissions under {MAX_LEN} characters.")
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
        text="Submit this as an anonymous prox5 submission?",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Submit the message above? "
                            "Moderaters will review your prox5 *confessions* submission. Moderators do not see your Slack ID unless if you have been reported. When you are reported, you will rejected, however, being rejected does not necessarily mean reported." + note,
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


_STAGE_MESSAGES = (
    ":incoming_envelope: Sent off to review! In the meantime, drink some tea?",
    ":incoming_envelope: Off it goes! Maybe stretch your legs while (I) take a look?",
    ":incoming_envelope: It's lights out and away we go!",
    ":incoming_envelope: Yaysies, it went off without a hitch.",
)


@app.action("stage")
def on_stage(ack, body, client):
    ack()
    cid = int(body["actions"][0]["value"])
    c = conf(cid)
    if not owns(body, c) or not transition(cid, "draft", "pending"):
        return
    with _lock:
        number = db.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM confessions").fetchone()[0]
        db.execute("UPDATE confessions SET number=? WHERE id=?", (number, cid))
        db.commit()
    c = conf(cid)  # re-read in case the draft was edited, and to pick up the number
    text = dec(c["text_enc"])
    client.chat_postMessage(channel=REVIEW, text="New prox5 submission to review", blocks=review_blocks(cid, c["number"], text))
    replace_prompt(client, body, random.choice(_STAGE_MESSAGES))


@app.action("cancel")
def on_cancel(ack, body, client):
    ack()
    cid = int(body["actions"][0]["value"])
    c = conf(cid)
    if not owns(body, c):
        return
    if run("DELETE FROM confessions WHERE id=? AND status='draft'", (cid,)) == 1:
        replace_prompt(client, body, ":wastebasket: Cancelled :(")

def review_blocks(cid: int, number: int, text: str):
    preview = defuse_broadcasts_text(text)
    return [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*New submission* *[ {number} ]*\n{quote(preview)}"}},
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
                              "text": "This reveals the author's Slack ID to you privately so you can file a report."},
                     "confirm": {"type": "plain_text", "text": "Report"},
                     "deny": {"type": "plain_text", "text": "Cancel"},
                 }},
            ],
        },
    ]


def close_review(client, channel, ts, first_block, outcome: str, reason: str = ""):
    blocks = [first_block, {"type": "section", "text": {"type": "mrkdwn", "text": outcome}}]
    fallback = outcome
    if reason:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": quote(reason)}})
        fallback += f"\n{quote(reason)}"
    client.chat_update(channel=channel, ts=ts, text=fallback, blocks=blocks)


@app.action("approve")
def on_approve(ack, body, client):
    ack()
    cid = int(body["actions"][0]["value"])
    if not transition(cid, "pending", "approved"):
        return

    c = conf(cid)
    number = c["number"]
    text = dec(c["text_enc"])
    if not ALLOW_BROADCASTS_FROM_OP:
        text = defuse_broadcasts_text(text)

    post = client.chat_postMessage(channel=CONFESSIONS, text=f"*[ {number} ]*\n{text}",
                                    username=OP_NAME, icon_emoji=OP_ICON)
    run("UPDATE confessions SET pub_ts=? WHERE id=?", (post["ts"], cid))
    run("INSERT OR IGNORE INTO relays (confession_id, pub_ts, dm_ts) VALUES (?,?,?)",
        (cid, post["ts"], c["dm_ts"]))

    close_review(client, body["channel"]["id"], body["message"]["ts"], body["message"]["blocks"][0],
                 f":white_check_mark: Approved by <@{body['user']['id']}>. Posted as *[ {number} ]*")
    client.chat_postMessage(
        channel=dec(c["dm_channel_enc"]),
        thread_ts=c["dm_ts"],
        text=f":tada: Approved and posted as *[ {number} ]*! Replies from the channel will appear "
             f"in this thread, and anything you send here is posted there as {OP_NAME}. "
             f"Send `unsub` any time to stop channel replies from appearing here.",
    )


def notify_rejected(client, c, reason: str = ""):
    text = "Your prox5 submission wasn't approved by the moderators. :("
    if reason:
        text += f"\n{quote(reason)}"
    client.chat_postMessage(channel=dec(c["dm_channel_enc"]), thread_ts=c["dm_ts"], text=text)


def reject_modal(cid: int, report: bool, channel: str, ts: str):
    blocks = []
    if report:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": ":warning: This reveals the author's Slack ID in the review "
                                                "thread so a moderator can file a report to Shroud."},
        })
    blocks.append({
        "type": "input",
        "block_id": "reason_block",
        "optional": True,
        "label": {"type": "plain_text", "text": "Reason (optional)"},
        "element": {
            "type": "plain_text_input",
            "action_id": "reason",
            "multiline": True,
            "placeholder": {"type": "plain_text", "text": "Shown to the author. Leave blank to send no reason."},
        },
    })
    return {
        "type": "modal",
        "callback_id": "reject_submit",
        "private_metadata": json.dumps({"cid": cid, "report": report, "channel": channel, "ts": ts}),
        "title": {"type": "plain_text", "text": "Reject & report" if report else "Reject prox5 submission"},
        "submit": {"type": "plain_text", "text": "Report" if report else "Reject"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


@app.action("reject")
def on_reject(ack, body, client):
    ack()
    cid = int(body["actions"][0]["value"])
    client.views_open(
        trigger_id=body["trigger_id"],
        view=reject_modal(cid, False, body["channel"]["id"], body["message"]["ts"]),
    )


@app.action("reject_report")
def on_reject_report(ack, body, client):
    ack()
    cid = int(body["actions"][0]["value"])
    client.views_open(
        trigger_id=body["trigger_id"],
        view=reject_modal(cid, True, body["channel"]["id"], body["message"]["ts"]),
    )


@app.view("reject_submit")
def on_reject_submit(ack, body, client):
    ack()
    meta = json.loads(body["view"]["private_metadata"])
    cid, report, channel, ts = meta["cid"], meta["report"], meta["channel"], meta["ts"]
    reason = (body["view"]["state"]["values"]["reason_block"]["reason"].get("value") or "").strip()

    if not transition(cid, "pending", "rejected"):
        return
    c = conf(cid)
    mod = body["user"]["id"]
    first_block = review_blocks(cid, c["number"], dec(c["text_enc"]))[0]

    if report:
        author = dec(c["user_enc"])
        content = dec(c["text_enc"]).replace("```", "'''")
        report_text = (
            f"prox5 submission info\n"
            f"Author Slack ID: {author}\n"
            f"Time sent: {sent_time(c['dm_ts'])}\n"
            + (f"Reason: {reason}\n" if reason else "")
            + f"Message:\n{content}"
        )
        client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text=f":rotating_light: Report for *[ {c['number']} ]*. Author: <@{author}>. Copy to Shroud:",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f":rotating_light: *Report for [ {c['number']} ].* Author: <@{author}>\nCopy to Shroud:"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"```{report_text}```"}},
            ],
        )
        outcome = f":rotating_light: Rejected & reported by <@{mod}>. Report details posted in thread."
    else:
        outcome = f":x: Rejected by <@{mod}>"

    close_review(client, channel, ts, first_block, outcome, reason)
    notify_rejected(client, c, reason)


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
    print(f"[prox5] v{VERSION} opening Socket Mode connection...", flush=True)
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
