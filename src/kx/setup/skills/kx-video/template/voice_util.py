"""Narration clip bookkeeping shared by gen_voice.py (writer) and the scenes (reader)."""
import hashlib
import json
from pathlib import Path

from narration import NARRATION, SPEED, VOICE

SR = 24000
VOICE_DIR = Path(__file__).parent / "voice"
WORDS_PER_S = 2.6  # the pace a silent draft is timed at, before gen_voice.py has made the clips


def clip_hash(key):
    return hashlib.sha1(f"{VOICE}|{SPEED}|{NARRATION[key]}".encode()).hexdigest()[:16]


def load_manifest():
    p = VOICE_DIR / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else {}


def estimate(key):
    """How long a clip will last, from its words: a draft is timed with this until the voice exists."""
    return round(len(NARRATION[key].split()) / (WORDS_PER_S * SPEED) + 0.2, 2)
