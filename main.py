import os
import json
import time
import requests
import websockets
import asyncio
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

OPENAI_CLIENT = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_ACCESS_TOKEN = os.getenv("TWITCH_ACCESS_TOKEN")

HELIX = "https://api.twitch.tv/helix"
EVENTSUB_WSS = "wss://eventsub.wss.twitch.tv/ws"
EVENTSUB_TEST_WSS = "ws://127.0.0.1:8080/ws"

USE_MOCK_EVENTSUB = 0
SUBSCRIPTION_URL = "http://127.0.0.1:8080/eventsub/subscriptions" if USE_MOCK_EVENTSUB else f"{HELIX}/eventsub/subscriptions"
WS_URL = EVENTSUB_TEST_WSS if USE_MOCK_EVENTSUB else EVENTSUB_WSS
MOCK_UNSUPPORTED_SUBS = {
    "channel.chat.message",
    # (often also unsupported in mocks)
    "channel.chat.notification",
    "channel.chat.message_delete",
    "channel.chat.clear",
    "channel.chat.clear_user_messages",
}

def twitch_headers():
    return {
        "Client-Id": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {TWITCH_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def validate_token():
    r = requests.get(
        "https://id.twitch.tv/oauth2/validate",
        headers={"Authorization": f"OAuth {TWITCH_ACCESS_TOKEN}"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def eventsub_headers():
    if USE_MOCK_EVENTSUB:
        # Twitch CLI mock endpoint: keep it simple
        return {
            "Client-ID": TWITCH_CLIENT_ID or "mock",
            "Content-Type": "application/json",
        }
    return {
        "Client-Id": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {TWITCH_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def safe_create_sub(session_id: str, sub_type: str, version: str, condition: dict):
    if USE_MOCK_EVENTSUB and sub_type in MOCK_UNSUPPORTED_SUBS:
        print(f"[mock] skipping unsupported sub: {sub_type}")
        return None
    return create_eventsub_subscription(session_id, sub_type, version, condition)


def create_eventsub_subscription(session_id: str, sub_type: str, version: str, condition: dict):
    payload = {
        "type": sub_type,
        "version": version,
        "condition": condition,
        "transport": {"method": "websocket", "session_id": session_id},
    }

    r = requests.post(
        SUBSCRIPTION_URL,
        headers=eventsub_headers(),
        json=payload,  # <-- use json= instead of data=
        timeout=20,
    )
    if not r.ok:
        raise RuntimeError(
            f"Create sub failed for {sub_type} ({r.status_code}): {r.text}")
    return r.json()

def send_chat_message(broadcaster_id: str, sender_id: str, message: str):
    payload = {
        "broadcaster_id": broadcaster_id,
        "sender_id": sender_id,
        # message limit is 500 chars :contentReference[oaicite:20]{index=20}
        "message": message[:500],
    }
    r = requests.post(f"{HELIX}/chat/messages",
                      headers=twitch_headers(), data=json.dumps(payload), timeout=20)
    r.raise_for_status()
    return r.json()


async def run():
    # returns user_id, scopes, expires_in, etc. :contentReference[oaicite:21]{index=21}
    token_info = validate_token()
    me_id = token_info["user_id"]

    seen_message_ids = set()

    async with websockets.connect(WS_URL) as ws:
        # Wait for Welcome -> contains session id, and you have ~10s to subscribe by default :contentReference[oaicite:22]{index=22}
        welcome_raw = await ws.recv()
        welcome = json.loads(welcome_raw)
        session_id = welcome["payload"]["session"]["id"]

        # Subscribe to chat messages
        safe_create_sub(
            session_id=session_id,
            sub_type="channel.chat.message",
            version="1",
            condition={"broadcaster_user_id": me_id, "user_id": me_id},
        )
        # Subscribe to subs/resubs
        safe_create_sub(
            session_id=session_id,
            sub_type="channel.subscribe",
            version="1",
            condition={"broadcaster_user_id": me_id},
        )
        safe_create_sub(
            session_id=session_id,
            sub_type="channel.subscription.message",
            version="1",
            condition={"broadcaster_user_id": me_id},
        )
        # Subscribe to gifted subs
        safe_create_sub(
            session_id=session_id,
            sub_type="channel.subscription.gift",
            version="1",
            condition={"broadcaster_user_id": me_id},
        )
        # Subscribe to bits
        safe_create_sub(
            session_id=session_id,
            sub_type="channel.cheer",
            version="1",
            condition={"broadcaster_user_id": me_id},
        )
        # Subscribe to raids (incoming)
        safe_create_sub(
            session_id=session_id,
            sub_type="channel.raid",
            version="1",
            condition={"to_broadcaster_user_id": me_id},
        )

        print("Connected. Listening for chat messages, subs, bits, and raids...")

        while True:
            raw = await ws.recv()
            msg = json.loads(raw)

            mtype = msg.get("metadata", {}).get("message_type")

            # Keepalive just means connection is healthy :contentReference[oaicite:23]{index=23}
            if mtype == "session_keepalive":
                continue

            # Notifications are delivered "at least once" -> dedupe by message_id :contentReference[oaicite:24]{index=24}
            mid = msg.get("metadata", {}).get("message_id")
            if mid and mid in seen_message_ids:
                continue
            if mid:
                seen_message_ids.add(mid)

            if mtype == "notification":
                sub_type = msg.get("metadata", {}).get("subscription_type")
                event = msg["payload"]["event"]

                if sub_type == "channel.chat.message":
                    chatter = event["chatter_user_login"]
                    text = event["message"]["text"]

                    print(f"{chatter}: {text}")

                    if text.strip().lower() == "!ping":
                        send_chat_message(
                            broadcaster_id=me_id, sender_id=me_id, message="pong (via Helix)")
                elif sub_type == "channel.subscribe":
                    user = event.get("user_login")
                    tier = event.get("tier")
                    is_gift = event.get("is_gift")
                    print(f"Sub: {user} (tier={tier}, gift={is_gift})")
                elif sub_type == "channel.subscription.message":
                    user = event.get("user_login")
                    tier = event.get("tier")
                    months = event.get("cumulative_months")
                    msg_text = event.get("message", {}).get("text", "")
                    print(f"Resub: {user} (tier={tier}, months={months}) {msg_text}")
                elif sub_type == "channel.subscription.gift":
                    gifter = event.get("user_login")
                    total = event.get("total")
                    tier = event.get("tier")
                    print(f"Gifted subs: {gifter} (tier={tier}, total={total})")
                elif sub_type == "channel.cheer":
                    user = event.get("user_login")
                    bits = event.get("bits")
                    msg_text = event.get("message", "")
                    print(f"Bits: {user} ({bits}) {msg_text}")
                elif sub_type == "channel.raid":
                    raider = event.get("from_broadcaster_user_login")
                    viewers = event.get("viewers")
                    print(f"Raid: {raider} with {viewers} viewers")

asyncio.run(run())

# resp = OPENAI_CLIENT.responses.create(
#     model="gpt-5-nano-2025-08-07",
    
# )
