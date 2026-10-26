"""Synthesise every narration clip with Kokoro into voice/<key>.wav (skips clips whose text is unchanged).

Usage: python gen_voice.py [--force]
Needs kokoro and soundfile (see the skill's setup), and espeak-ng for unknown words.
"""
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

import misaki.espeak  # noqa: F401  (points phonemizer at its bundled espeak, whose data path can be broken)
from phonemizer.backend.espeak.wrapper import EspeakWrapper

for lib, data in (("/usr/lib/x86_64-linux-gnu/libespeak-ng.so.1", "/usr/lib/x86_64-linux-gnu/espeak-ng-data"),
                  ("/usr/lib/aarch64-linux-gnu/libespeak-ng.so.1", "/usr/lib/aarch64-linux-gnu/espeak-ng-data")):
    if Path(lib).exists() and Path(data).exists():  # the system espeak-ng, when there is one
        EspeakWrapper.set_library(lib)
        EspeakWrapper.set_data_path(data)
        break

from kokoro import KPipeline  # noqa: E402

from narration import NARRATION  # noqa: E402
from voice_util import SR, VOICE_DIR, clip_hash, load_manifest  # noqa: E402


def trim(audio, thresh=2e-3, keep=0.04):
    idx = np.where(np.abs(audio) > thresh)[0]
    if len(idx) == 0:
        return audio
    k = int(keep * SR)
    return audio[max(0, idx[0] - k): idx[-1] + k]


def main():
    force = "--force" in sys.argv
    VOICE_DIR.mkdir(exist_ok=True)
    manifest = load_manifest()
    todo = [k for k in NARRATION if force or manifest.get(k, {}).get("hash") != clip_hash(k)
            or not (VOICE_DIR / f"{k}.wav").exists()]
    if not todo:
        print("all clips up to date")
        return
    from narration import SPEED, VOICE
    pipe = KPipeline(lang_code=VOICE[0], repo_id="hexgrad/Kokoro-82M")  # a = American English, b = British, ...
    for k in todo:
        chunks = [a.numpy() for _, _, a in pipe(NARRATION[k], voice=VOICE, speed=SPEED)]
        audio = trim(np.concatenate(chunks))
        sf.write(VOICE_DIR / f"{k}.wav", audio, SR)
        manifest[k] = {"hash": clip_hash(k), "duration": round(len(audio) / SR, 3)}
        print(f"{k:16s} {manifest[k]['duration']:6.2f}s")
    (VOICE_DIR / "manifest.json").write_text(json.dumps(manifest, indent=1))
    total = sum(manifest[k]["duration"] for k in NARRATION)
    print(f"total narration: {total / 60:.2f} min")


if __name__ == "__main__":
    main()
