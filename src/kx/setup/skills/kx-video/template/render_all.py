"""Render every scene in parallel, save still frames to look at, and join the scenes into one video.

Usage: python render_all.py [-q l|m|h|k] [--frames N] [--no-join] [scene ...]

-q l (480p15) is the draft: fast, and silent where the voice is missing. -q h (1080p60) is the final: every clip
must exist (gen_voice.py). --frames N saves N frames of each scene in frames/ (default 3 for drafts): look at them
for text that overlaps or leaves the frame. Under `kx exec` the joined video is also copied into the run's dir.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
QUALITY_DIR = {"l": "480p15", "m": "720p30", "h": "1080p60", "k": "2160p60"}


def scenes():
    return re.findall(r"^class (S\d+_\w+)\(", (HERE / "scenes.py").read_text(), flags=re.M)


def video_path(scene, q):
    return HERE / "media" / scene / "videos" / "scenes" / QUALITY_DIR[q] / f"{scene}.mp4"


def render(scene, q):
    log = HERE / "logs" / f"{scene}_{q}.log"
    log.parent.mkdir(exist_ok=True)
    # --disable_caching: reused partial movies drop the sounds added with add_sound
    cmd = [sys.executable, "-m", "manim", f"-q{q}", "--disable_caching", "--media_dir", f"media/{scene}",
           "scenes.py", scene]
    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
           "KX_VIDEO_STRICT": "1" if q in ("h", "k") else os.environ.get("KX_VIDEO_STRICT", "")}
    with open(log, "w") as f:
        rc = subprocess.run(cmd, cwd=HERE, stdout=f, stderr=subprocess.STDOUT, env=env).returncode
    return scene, rc, log


def duration(path):
    import av
    with av.open(str(path)) as c:
        s = c.streams.video[0]
        return float(s.duration * s.time_base) if s.duration else c.duration / 1e6


def has_audio(path):
    import av
    with av.open(str(path)) as c:
        return bool(c.streams.audio)


def frames(scene, q, n):
    """n frames spread over the scene (the last one at its very end), as frames/<scene>_<i>.png."""
    import av
    out = HERE / "frames"
    out.mkdir(exist_ok=True)
    for old in out.glob(f"{scene}_*.png"):
        old.unlink()
    path = str(video_path(scene, q))
    with av.open(path) as c:  # frames are decoded one at a time: a whole scene does not fit in memory
        total = c.streams.video[0].frames or sum(1 for _ in c.decode(video=0))
    picks = sorted({round(i * (total - 1) / max(1, n - 1)) for i in range(n)}) if n > 1 else [total - 1]
    paths = []
    with av.open(path) as c:
        for i, frame in enumerate(c.decode(video=0)):
            if i in picks:
                p = out / f"{scene}_{len(paths)}.png"
                frame.to_image().save(p)
                paths.append(p)
    return paths


def find_ffmpeg():
    for cand in (Path(sys.executable).parent / "ffmpeg", shutil.which("ffmpeg")):
        if cand and Path(cand).exists():
            return str(cand)
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return None


def join(q):
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        print("no ffmpeg to join the scenes: install it, or `pip install imageio-ffmpeg`; the scenes are in media/")
        return None

    def run(*a):
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", *map(str, a)], check=True)

    # Each scene's audio ends at its last word. Pad it, as lossless PCM, to the exact video length, then join video
    # (stream copy) and audio (one continuous track) separately so narration cannot drift across scene boundaries.
    work = HERE / "media" / f"_join_{q}"
    work.mkdir(parents=True, exist_ok=True)
    vlist, alist = [], []
    for s in scenes():
        src = video_path(s, q)
        dur = f"{duration(src):.3f}"
        wav = work / f"{s}.wav"
        if has_audio(src):
            run("-i", src, "-vn", "-af", f"apad=whole_dur={dur}", "-t", dur, "-ar", 48000, "-ac", 2, wav)
        else:  # a silent draft scene
            run("-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", dur, wav)
        vlist.append(f"file '{src}'\n")
        alist.append(f"file '{wav}'\n")
    (work / "v.txt").write_text("".join(vlist))
    (work / "a.txt").write_text("".join(alist))
    run("-f", "concat", "-safe", 0, "-i", work / "v.txt", "-an", "-c", "copy", work / "video.mp4")
    run("-f", "concat", "-safe", 0, "-i", work / "a.txt", "-c", "copy", work / "audio.wav")
    out = HERE / f"{HERE.name}_{QUALITY_DIR[q]}.mp4"
    run("-i", work / "video.mp4", "-i", work / "audio.wav", "-map", "0:v", "-map", "1:a", "-c:v", "copy",
        "-c:a", "aac", "-b:a", 192000, "-movflags", "+faststart", out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-q", default="l", choices=QUALITY_DIR)
    ap.add_argument("--frames", type=int, default=None, help="frames of each scene to save in frames/ (default: 3 for -q l)")
    ap.add_argument("--no-join", action="store_true")
    ap.add_argument("scenes", nargs="*")
    args = ap.parse_args()
    todo = args.scenes or scenes()
    with ThreadPoolExecutor(max_workers=min(len(todo), os.cpu_count() or 4)) as ex:
        results = list(ex.map(lambda s: render(s, args.q), todo))
    failed = [(s, log) for s, rc, log in results if rc != 0]
    for s, rc, log in results:
        print(f"{s}: {'ok' if rc == 0 else 'FAILED'} ({duration(video_path(s, args.q)):.1f} s)" if rc == 0 else f"{s}: FAILED")
    if failed:
        for s, log in failed:
            print(f"--- {s} ---\n" + "\n".join(log.read_text().splitlines()[-25:]))
        sys.exit(1)
    n = args.frames if args.frames is not None else (3 if args.q == "l" else 0)
    if n:
        for s in todo:
            print(f"frames: {' '.join(str(p.relative_to(HERE)) for p in frames(s, args.q, n))}")
    if args.no_join or args.scenes:
        return
    out = join(args.q)
    if out is None:
        return
    print("wrote", out)
    if os.environ.get("KX_RUN_DIR"):  # under kx exec: the video is one of the run's files too
        shutil.copy2(out, Path(os.environ["KX_RUN_DIR"]) / out.name)
        print("and into the run's dir:", os.environ["KX_RUN_DIR"])


if __name__ == "__main__":
    main()
