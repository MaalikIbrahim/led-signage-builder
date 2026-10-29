#!/usr/bin/env python3
"""
LED signage builder: render a header image and an animated shelf-strip video
for a smart cooler / shelf-edge LED display, compose them into one full-canvas
video, and optionally join it with another advertiser's video as a seamless,
crossfaded loop sized for the LED panel.

Usage:
    python build.py examples/config.json
    python build.py examples/config.json --loop-with partner.mp4

Requires: Python 3.9+, Pillow, ffmpeg on PATH.
"""
import argparse
import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def gradient(w, h, c1, c2):
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    for x in range(w):
        t = x / max(w - 1, 1)
        d.line([(x, 0), (x, h)], fill=tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)))
    return im


def font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def fit_height(img, h):
    r = h / img.height
    return img.resize((int(img.width * r), h), Image.LANCZOS)


def run(cmd):
    subprocess.run(cmd, check=True)


def render_header(cfg, out):
    c = cfg["colors"]
    w, h = cfg["header"]["size"]
    im = gradient(w, h, hex_rgb(c["dark"]), hex_rgb(c["light"]))
    d = ImageDraw.Draw(im)
    logo = cfg.get("logo")
    if logo and Path(logo).exists():
        lg = fit_height(Image.open(logo).convert("RGBA"), int(h * 0.6))
        im.paste(lg, ((w - lg.width) // 2, (h - lg.height) // 2), lg)
    else:
        f = font(cfg["font_bold"], int(h * 0.35))
        d.text((w / 2, h / 2), cfg["brand_name"], font=f, fill=hex_rgb(c["text"]), anchor="mm")
    im.save(out)


def render_slide(cfg, slide, w, h):
    c = cfg["colors"]
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    f = font(cfg["font_bold"], slide.get("size", int(h * 0.38)))
    parts = slide["parts"]  # list of [text, "text"|"accent"]
    total = sum(d.textlength(t, font=f) for t, _ in parts)
    x = (w - total) / 2
    for t, role in parts:
        d.text((x, h / 2), t, font=f, fill=hex_rgb(c[role]), anchor="lm")
        x += d.textlength(t, font=f)
    return im


def render_shelf_video(cfg, out, workdir):
    """Slides that slide left with an eased transition over a moving diagonal-line background."""
    c = cfg["colors"]
    w, h = cfg["shelf"]["size"]
    fps = cfg.get("fps", 30)
    seg = int(cfg["shelf"]["seconds_per_slide"] * fps)
    tr = int(cfg["shelf"].get("transition_seconds", 0.5) * fps)
    slides = [render_slide(cfg, s, w, h) for s in cfg["shelf"]["slides"]]
    n = len(slides)
    frames = Path(workdir) / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    base_bg = gradient(w, h, hex_rgb(c["dark"]), hex_rgb(c["light"]))
    line_col = hex_rgb(c.get("lines", c["dark"]))
    for k in range(n * seg):
        off = (k * 2) % 70
        base = base_bg.copy()
        dd = ImageDraw.Draw(base)
        for i in range(-h - 200, w + 200, 70):
            dd.line([(i - off, h), (i - off + h, 0)], fill=line_col, width=2)
        s, p = divmod(k, seg)
        cur, nxt = slides[s], slides[(s + 1) % n]
        if p >= seg - tr:
            a = (p - (seg - tr)) / tr
            a = 0.5 - 0.5 * math.cos(math.pi * a)  # ease in/out
            dx = int(w * a)
            base.paste(cur, (-dx, 0), cur)
            base.paste(nxt, (w - dx, 0), nxt)
        else:
            base.paste(cur, (0, 0), cur)
        base.save(frames / f"{k:05d}.png")
    run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i", str(frames / "%05d.png"),
         "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(out)])
    return n * seg / fps


def compose_canvas(cfg, header_png, shelf_mp4, duration, out):
    """Place the header and one copy of the shelf video per shelf onto the full canvas."""
    W, H = cfg["canvas"]["size"]
    hx, hy = cfg["header"]["position"]
    ys = cfg["shelf"]["y_positions"]
    fps = cfg.get("fps", 30)
    n = len(ys)
    splits = "".join(f"[s{i}]" for i in range(n))
    chain = f"[2:v]split={n}{splits};[0:v][1:v]overlay={hx}:{hy}:shortest=1[c0];"
    for i, y in enumerate(ys):
        last = i == n - 1
        chain += f"[c{i}][s{i}]overlay=0:{y}" + ("[v]" if last else f"[c{i + 1}];")
    run(["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=black:s={W}x{H}:r={fps}:d={duration}",
         "-loop", "1", "-framerate", str(fps), "-i", str(header_png),
         "-i", str(shelf_mp4),
         "-filter_complex", chain, "-map", "[v]", "-t", str(duration),
         "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", str(out)])


def probe_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def seamless_loop(cfg, partner, ours, out, cycles):
    """
    partner (A) -> ours (B) -> back to A, with crossfades, cut so the file's last
    frame matches its first frame. The file starts 1 s into A and ends on A at 1 s,
    so a player restarting the file shows no jump.

    The result is downscaled to the panel's native resolution with area averaging,
    then upscaled by an exact integer factor with nearest-neighbour, so the player's
    own downscale is pixel-exact (no shimmer on thin text).
    """
    fps = cfg.get("fps", 30)
    pw, ph = cfg["panel"]["native_size"]
    k = cfg["panel"].get("export_scale", 2)
    da, db = probe_duration(partner), probe_duration(ours)
    xf = 1.0
    off1 = (da - 1) - xf
    off2 = off1 + db - xf
    f = (
        f"[0:v]fps={fps},format=yuv420p,setsar=1,split[a][b];"
        f"[a]trim=1:{da},setpts=PTS-STARTPTS[a1];"
        f"[b]trim=0:1,setpts=PTS-STARTPTS[a0];"
        f"[1:v]fps={fps},format=yuv420p,setsar=1,setpts=PTS-STARTPTS[k];"
        f"[a1][k]xfade=transition=fade:duration={xf}:offset={off1}[x];"
        f"[x][a0]xfade=transition=fade:duration={xf}:offset={off2},"
        f"scale={pw}:{ph}:flags=area,scale={pw * k}:{ph * k}:flags=neighbor[v]"
    )
    tmp = Path(out).with_suffix(".cycle.mp4")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(partner), "-i", str(ours), "-filter_complex", f,
         "-map", "[v]", "-an", "-c:v", "libx264", "-crf", "21", "-pix_fmt", "yuv420p", "-g", str(fps * 2), str(tmp)])
    lst = Path(out).with_suffix(".txt")
    lst.write_text("".join(f"file '{tmp.name}'\n" for _ in range(cycles)))
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])
    tmp.unlink()
    lst.unlink()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config")
    ap.add_argument("--out", default="out")
    ap.add_argument("--loop-with", help="partner video (same canvas size) to alternate with")
    ap.add_argument("--cycles", type=int, default=3, help="cycles per file, so the player restarts less often")
    a = ap.parse_args()

    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg not found on PATH")
    cfg = json.loads(Path(a.config).read_text())
    out = Path(a.out)
    out.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory() as wd:
        header = out / "header.png"
        shelf = out / "shelf.mp4"
        canvas = out / "canvas.mp4"
        render_header(cfg, header)
        dur = render_shelf_video(cfg, shelf, wd)
        compose_canvas(cfg, header, shelf, dur, canvas)
        print(f"Built {header}, {shelf} ({dur:.1f}s), {canvas}")
        if a.loop_with:
            loop = out / "loop.mp4"
            seamless_loop(cfg, a.loop_with, canvas, loop, a.cycles)
            print(f"Built seamless loop {loop}")


if __name__ == "__main__":
    main()
