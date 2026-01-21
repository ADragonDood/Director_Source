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


def create_eventsub_subscription(session_id: str, broadcaster_user_id: str, user_id: str):
    # Create EventSub Subscription API (WebSocket transport uses session_id from Welcome) :contentReference[oaicite:19]{index=19}
    payload = {
        "type": "channel.chat.message",
        "version": "1",
        "condition": {
            "broadcaster_user_id": broadcaster_user_id,
            "user_id": user_id,
        },
        "transport": {
            "method": "websocket",
            "session_id": session_id,
        },
    }
    r = requests.post(f"{HELIX}/eventsub/subscriptions",
                      headers=twitch_headers(), data=json.dumps(payload), timeout=20)
    r.raise_for_status()
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

    async with websockets.connect(EVENTSUB_WSS) as ws:
        # Wait for Welcome -> contains session id, and you have ~10s to subscribe by default :contentReference[oaicite:22]{index=22}
        welcome_raw = await ws.recv()
        welcome = json.loads(welcome_raw)
        session_id = welcome["payload"]["session"]["id"]

        # Subscribe to chat messages
        create_eventsub_subscription(
            session_id=session_id, broadcaster_user_id=me_id, user_id=me_id)

        print("Connected. Listening for chat messages...")

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

            if mtype == "notification" and msg.get("metadata", {}).get("subscription_type") == "channel.chat.message":
                event = msg["payload"]["event"]
                chatter = event["chatter_user_login"]
                text = event["message"]["text"]

                print(f"{chatter}: {text}")

                if text.strip().lower() == "!ping":
                    send_chat_message(
                        broadcaster_id=me_id, sender_id=me_id, message="pong (via Helix)")

asyncio.run(run())

# resp = OPENAI_CLIENT.responses.create(
#     model="gpt-5-nano-2025-08-07",
    
# )