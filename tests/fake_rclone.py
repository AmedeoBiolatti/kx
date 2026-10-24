"""A stand-in for rclone in tests: `name:path` is $FAKE_RCLONE_ROOT/name/path. Only what kx uses: copy, copyto, sync,
lsf, cat, rcat, purge, mkdir, with --include/--exclude/--filter (rclone's glob rules), --max-size, -R."""

import os
import re
import shutil
import sys
from pathlib import Path


def resolve(p):
    m = re.match(r"^([A-Za-z0-9_-]+):(.*)$", p)
    return Path(os.environ["FAKE_RCLONE_ROOT"]) / m.group(1) / m.group(2).lstrip("/") if m else Path(p)


def glob_re(pat):
    anchored = pat.startswith("/")
    body = re.sub(r"\\\*\\\*|\\\*|\\\?", lambda m: {"\\*\\*": ".*", "\\*": "[^/]*", "\\?": "[^/]"}[m.group(0)],
                  re.escape(pat.lstrip("/")))
    return re.compile(("^" if anchored else "(^|.*/)") + body + "$")


def main(argv):
    rules, flags, pos, max_size, min_size, fmt, includes = [], set(), [], None, None, None, False
    it = iter(argv)
    for a in it:
        if "=" in a and a.startswith("--"):
            a, v = a.split("=", 1)
        else:
            v = None
        if a in ("--include", "--exclude", "--filter", "--max-size", "--min-size", "--format", "--files-from", "--config", "--max-depth"):
            v = v if v is not None else next(it)
            if a == "--include":
                rules.append(("+", glob_re(v))); includes = True
            elif a == "--exclude":
                rules.append(("-", glob_re(v)))
            elif a == "--filter":
                rules.append((v[0], glob_re(v[2:])))
            elif a in ("--max-size", "--min-size"):
                n = int(float(v[:-1]) * {"K": 2**10, "M": 2**20, "G": 2**30}[v[-1]])
                max_size, min_size = (n, min_size) if a == "--max-size" else (max_size, n)
            elif a == "--format":
                fmt = v
            elif a == "--files-from":
                for line in Path(v).read_text().splitlines():
                    if line.strip():
                        rules.append(("+", re.compile("^" + re.escape(line.strip().lstrip("/")) + "(/.*)?$")))
                includes = True
        elif a.startswith("-"):
            flags.add(a)
        else:
            pos.append(a)
    if includes:
        rules.append(("-", glob_re("**")))

    def keep(rel, size=None):
        if max_size is not None and size is not None and size > max_size:
            return False
        if min_size is not None and size is not None and size < min_size:
            return False
        for sign, rx in rules:
            if rx.match(rel):
                return sign == "+"
        return True

    def files(root):
        return {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file()}

    cmd, args = pos[0], pos[1:]
    if cmd in ("copy", "sync"):
        src, dst = resolve(args[0]), resolve(args[1])
        if src.is_file():
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst / src.name)
            return 0
        if not src.is_dir():
            print(f"ERROR : directory not found: {args[0]}", file=sys.stderr)
            return 3
        dst.mkdir(parents=True, exist_ok=True)
        wanted = {rel: p for rel, p in files(src).items() if keep(rel, p.stat().st_size)}
        for rel, p in wanted.items():
            t = dst / rel
            st = p.stat()
            if not t.exists() or t.stat().st_size != st.st_size or int(t.stat().st_mtime) != int(st.st_mtime):
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, t)
        if cmd == "sync":
            for rel, p in files(dst).items():
                if rel not in wanted and keep(rel):
                    p.unlink()
        return 0
    if cmd == "copyto":
        dst = resolve(args[1])
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolve(args[0]), dst)
        return 0
    if cmd == "lsf":
        root = resolve(args[0])
        if not root.is_dir():
            print(f"ERROR : directory not found: {args[0]}", file=sys.stderr)
            return 3
        entries = root.rglob("*") if "-R" in flags else root.iterdir()
        out = []
        for p in sorted(entries):
            rel = p.relative_to(root).as_posix()
            if p.is_dir() and "--files-only" not in flags and keep(rel + "/"):
                out.append(rel + "/")
            elif p.is_file() and "--dirs-only" not in flags and keep(rel, p.stat().st_size):
                out.append(f"{rel};{p.stat().st_size}" if fmt == "ps" else rel)
        print("\n".join(out))
        return 0
    if cmd == "cat":
        p = resolve(args[0])
        if not p.is_file():
            print(f"ERROR : object not found: {args[0]}", file=sys.stderr)
            return 3
        sys.stdout.buffer.write(p.read_bytes())
        return 0
    if cmd == "rcat":
        p = resolve(args[0])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(sys.stdin.buffer.read())
        return 0
    if cmd == "deletefile":
        resolve(args[0]).unlink(missing_ok=True)
        return 0
    if cmd == "purge":
        shutil.rmtree(resolve(args[0]), ignore_errors=True)
        return 0
    if cmd == "mkdir":
        resolve(args[0]).mkdir(parents=True, exist_ok=True)
        return 0
    print(f"fake rclone: {cmd} is not faked", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
