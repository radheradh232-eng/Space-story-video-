"""Space Shorts auto pipeline: text -> image -> voice -> video -> YouTube upload."""
import asyncio
import json
import os
import random
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import edge_tts
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).parent
OUT = ROOT / "out"
OUT.mkdir(exist_ok=True)
HISTORY = ROOT / "docs" / "history.json"

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
VOICE = os.environ.get("TTS_VOICE", "en-US-GuyNeural")
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

TOPICS = [
    "black holes", "the Moon", "Mars", "Jupiter and its moons", "Saturn's rings",
    "neutron stars", "the Sun", "exoplanets", "the Milky Way", "the Big Bang",
    "dark matter", "comets and asteroids", "astronauts and the ISS", "Venus",
    "Neptune and Uranus", "supernovas", "time and gravity", "the Voyager probes",
]


def past_titles():
    if HISTORY.exists():
        return [h["title"] for h in json.loads(HISTORY.read_text())][-40:]
    return []


def make_script():
    topic = random.choice(TOPICS)
    prompt = f"""You write scripts for a YouTube Shorts space channel with a US audience.
Topic area: {topic}.
Write ONE mind-blowing, factually accurate space fact script, 30-40 seconds when spoken (80-100 words).
Start with a strong hook in the first sentence. End with a short question or line that makes people follow.
Do NOT repeat these earlier titles: {past_titles()}
Return ONLY JSON with keys:
  "title": catchy title under 70 chars, no hashtags,
  "script": the spoken narration,
  "hook": a 4-8 word on-screen hook in CAPS,
  "image_prompt": a vivid detailed prompt for a vertical cinematic space image, no text or letters in it,
  "description": 2-3 sentence YouTube description,
  "tags": list of 10 relevant keyword tags (no # sign)."""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
        f"?key={GEMINI_KEY}"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    r = requests.post(url, json=body, timeout=90)
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def make_image(prompt, path):
    full = prompt + ", ultra detailed, cinematic lighting, vertical 9:16 composition, no text"
    url = (
        "https://image.pollinations.ai/prompt/" + urllib.parse.quote(full)
        + f"?width=1080&height=1920&nologo=true&seed={random.randint(1, 10**6)}"
    )
    for attempt in range(3):
        r = requests.get(url, timeout=180)
        if r.ok and r.headers.get("content-type", "").startswith("image"):
            path.write_bytes(r.content)
            return
    raise RuntimeError("Image generate nahi hui")


async def make_voice(text, path):
    await edge_tts.Communicate(text, VOICE).save(str(path))


def audio_len(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path)])
    return float(out)


def make_video(image, voice, hook, path):
    dur = audio_len(voice) + 0.8
    hook_file = OUT / "hook.txt"
    hook_file.write_text(hook)
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    frames = int(dur * 30)
    vf = (
        f"scale=1296:2304,zoompan=z='min(zoom+0.0006,1.25)':d={frames}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps=30,"
        f"drawtext=fontfile={font}:textfile={hook_file}:fontcolor=white:fontsize=64:"
        f"box=1:boxcolor=black@0.55:boxborderw=24:x=(w-text_w)/2:y=h*0.12"
    )
    subprocess.check_call([
        "ffmpeg", "-y", "-loop", "1", "-i", str(image), "-i", str(voice),
        "-vf", vf, "-t", f"{dur:.2f}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-shortest", str(path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def youtube_client():
    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    return build("youtube", "v3", credentials=creds)


def upload(video, thumb, meta):
    yt = youtube_client()
    tags = [t.strip().lstrip("#") for t in meta["tags"]]
    hashtags = " ".join("#" + t.replace(" ", "") for t in tags[:5])
    description = f"{meta['description']}\n\n{hashtags} #Shorts #Space"
    body = {
        "snippet": {
            "title": meta["title"][:95],
            "description": description,
            "tags": tags + ["Shorts", "Space"],
            "categoryId": "28",
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    req = yt.videos().insert(
        part="snippet,status", body=body,
        media_body=MediaFileUpload(str(video), mimetype="video/mp4", resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    video_id = resp["id"]
    try:
        yt.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumb), mimetype="image/jpeg")).execute()
    except Exception as e:
        print("Thumbnail set nahi hua (channel verify karo):", e)
    return video_id


def save_history(meta, video_id):
    HISTORY.parent.mkdir(exist_ok=True)
    data = json.loads(HISTORY.read_text()) if HISTORY.exists() else []
    data.append({
        "time": datetime.now(timezone.utc).isoformat(),
        "title": meta["title"],
        "video_id": video_id,
        "status": "uploaded" if video_id else "dry-run",
    })
    HISTORY.write_text(json.dumps(data[-200:], indent=2))


def main():
    print("1/5 Script ban rahi hai...")
    meta = make_script()
    print("   ", meta["title"])
    img = OUT / "image.jpg"
    voice = OUT / "voice.mp3"
    video = OUT / "short.mp4"
    print("2/5 Image ban rahi hai...")
    make_image(meta["image_prompt"], img)
    print("3/5 Voiceover ban raha hai...")
    asyncio.run(make_voice(meta["script"], voice))
    print("4/5 Video ban rahi hai...")
    make_video(img, voice, meta["hook"], video)
    if DRY_RUN:
        print("DRY_RUN: upload skip kiya.")
        save_history(meta, None)
        return
    print("5/5 YouTube par upload ho raha hai...")
    vid = upload(video, img, meta)
    save_history(meta, vid)
    print("Done: https://youtube.com/shorts/" + vid)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", e)
        sys.exit(1)
