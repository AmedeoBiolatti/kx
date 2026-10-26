---
name: kx-video
description: Make a short narrated explainer video, in the style of 3Blue1Brown, of this competition's solution, a run's story, or an idea the work rests on, with Manim and an offline voice (Kokoro), every number taken from kx. Gives the workflow (storyboard, data from kx, silent drafts whose frames you look at, voice, final render) and a template to copy. Use when the person asks for a video, an animation or an explainer to share.
---

<!-- Written by kx init, which rewrites this folder: put your own skills in other folders. -->

# A narrated explainer from the kx record

A good explainer here is short (one to three minutes, one idea per scene), concrete (this competition's real runs
and numbers), and true (every number comes from kx and can be regenerated). `template/` next to this file is a
working starting point: the voice-timed scene, a run lineage, scores with error bars, training curves.

## 1. Agree on the story first

Ask what it is for and who watches: the person catching up, a teammate joining, a write-up. Gather the facts as the
kx-explain skill does, then write a storyboard, one line per scene: what is shown, what is said, which number it
rests on. Show it to the person and wait for a yes before animating: a storyboard changes in a minute, a scene in
an hour.

Good scenes, in the manner of 3Blue1Brown: open on a question; a concrete example before the general rule; one new
thing on screen at a time; keep objects and move them (Transform) rather than cutting; the number that matters
highlighted, the rest dimmed; a pause after something surprising. Two to five scenes is plenty.

## 2. Set up the video folder

```bash
cp -r .agents/skills/kx-video/template videos/<name>     # in the competition repo; videos/<name>/.gitignore keeps renders out of git
cd videos/<name>
python prep_data.py --run <run> [--baseline <run>] [--curves loss,val_auc]   # writes data.json, from kx --json only
```

`prep_data.py` gathers the run, its lineage (`parent` links), its baseline and their `kx compare` verdict, their
curves, the findings that cite them and their submissions; `data.json` keeps the kx commands it came from. Edit it
to add what your story needs, always from kx output. Never type a number into a scene or the narration: read it
from `DATA`, so re-running `prep_data.py` updates the video.

## 3. The tools (their own environment, not the project's)

Manim Community Edition renders without a display; it needs the Cairo and Pango libraries. One of:

- with sudo: `sudo apt install libcairo2-dev libpango1.0-dev pkg-config ffmpeg`, then
  `uv venv videos/.venv && uv pip install --python videos/.venv/bin/python manim`;
- without: conda-forge has them prebuilt:
  `micromamba create -p videos/.menv -c conda-forge python=3.12 manim ffmpeg`.

The voice (Kokoro, offline, about 2 GB with a CPU PyTorch): `pip install torch --index-url
https://download.pytorch.org/whl/cpu` then `pip install kokoro soundfile` in the same environment, and the system
`espeak-ng` for words it does not know. Without ffmpeg, `pip install imageio-ffmpeg` gives one to join the scenes.
Check free disk first (`df -h .`): environment, model and renders take several GB. Add `videos/.venv/` or
`videos/.menv/` to the repo's `.gitignore`.

## 4. Draft, look, fix: silent, at 480p

Write `narration.py` (one clip per key, numbers from `DATA`) and `scenes.py` (classes `S1_Name`, `S2_Name`, ... in
order, each a `VoiceScene` that wraps its animations in `with self.voice("key"):`). Then:

```bash
python render_all.py -q l          # every scene in parallel; silent and timed from the words until the voice exists
```

It saves three frames of each scene in `frames/` (`--frames N` for more). **Open every frame and look at it**
before anything else: text overlapping text or a box, labels off the frame or cut, unreadably small text, a curve
drawn across itself (a run whose steps restart has passes: plot `passes(points)[-1]`), an axis that does not cover
its data. Fix, re-render only the scenes you changed (`python render_all.py -q l S2_Lineage`), look again. Logs of a
failed scene are in `logs/`.

What the template already does for you: text through Pango, no LaTeX (`Tex` and `MathTex` need a LaTeX install;
use them for formulas only); axes write their own ticks; long labels shrink to fit (`text(..., max_width=)`) or
wrap (`wrap()`); `LABELS` in `common.py` replaces a run's label cut from its hypothesis with a short one you
write.

## 5. Voice, then the final render

```bash
python gen_voice.py                # Kokoro: only the clips whose text changed
python render_all.py -q h          # 1080p60; fails if a clip is missing or stale
```

Each scene now lasts as long as its narration (`self.voice` holds until the clip ends): re-check the frames at
scene boundaries. Listen for names Kokoro mispronounces and spell them as `[word](/IPA/)` in `narration.py`.
`render_all.py` renders with `--disable_caching` on purpose: cached partial movies drop sounds.

To keep the video with the record, render it as a kx run, with the video environment's Python (under `kx exec`,
`python` is the project's): `kx exec --probe -H "video: <what it explains>" -- ../.venv/bin/python render_all.py
-q h` copies the `.mp4` into the run's dir, tied to the commit it explains.

## 6. Hand it over

Give the person the path of the `.mp4`, its length, and what each scene says. Say what you could not check (you
cannot hear the voice). Competition data is often licensed for non-commercial use only (the rules say): a video
showing it stays private unless the rules allow sharing; keep it out of public places and say so.
