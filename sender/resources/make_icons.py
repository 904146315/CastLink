# -*- coding: utf-8 -*-
"""
生成 CastLink 全套图标资源。

发送端到.Installer、任务栏、托盘统一使用该图标：
一个"屏幕 -> 波 -> 屏幕"的投射意象，主体为渐变圆角矩形。
只需要 Pillow，不依赖任何设计软件，可重复生成。
"""
from __future__ import annotations

import math
import os
from PIL import Image, ImageDraw, ImageFilter

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # sender/
PROJECT = os.path.dirname(ROOT)                                      # 投屏软件/
os.makedirs(OUT, exist_ok=True)

BG_TOP = (24, 32, 56)
BG_MID = (37, 52, 92)
BG_BOT = (18, 24, 44)
ACCENT = (86, 156, 255)
ACCENT2 = (140, 210, 255)
WARM = (255, 156, 92)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def rounded_rect(size, radius, scale=4):
    """高质量圆角矩形蒙版（超采样，避免锯齿）。"""
    s = size * scale
    mask = Image.new("L", (s, s), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius * scale, fill=255)
    return mask.resize((size, size), Image.LANCZOS)


def vertical_gradient(size, top, mid, bottom):
    img = Image.new("RGB", (size, size))
    d = ImageDraw.Draw(img)
    half = size // 2
    for y in range(size):
        if y < half:
            t = y / max(1, half - 1)
            c = lerp(top, mid, t)
        else:
            t = (y - half) / max(1, size - half - 1)
            c = lerp(mid, bottom, t)
        d.line([(0, y), (size, y)], fill=c)
    return img


def draw_cast_symbol(d, x, y, w, h, color, lw, alpha=255):
    """左侧为屏幕矩形，右侧两条扩散弧线 —— 投屏的经典意象。"""
    if alpha < 255:
        color = color + (alpha,)
    # 屏幕主体
    sw = int(w * 0.56)
    sh = int(h * 0.70)
    sx, sy = x, y + (h - sh) // 2
    d.rounded_rectangle([sx, sy, sx + sw, sy + sh],
                        radius=int(min(sw, sh) * 0.14), outline=color, width=lw)
    # 屏幕支架
    d.line([(sx + sw // 2, sy), (sx + sw // 2, sy - int(h * 0.10))],
           fill=color, width=lw)
    # 右侧投射弧线
    cx = x + int(w * 0.40)
    cy = y + h // 2
    for i, (rr, a) in enumerate(((0.30, alpha), (0.50, int(alpha * 0.72)),
                                 (0.70, int(alpha * 0.45)))):
        rad = int(w * rr)
        box = [cx - rad, cy - rad, cx + rad, cy + rad]
        start_ang, end_ang = -58, 58
        if alpha < 255:
            col = color[:3] + (a,)
        else:
            col = lerp(color[:3] if isinstance(color, tuple) else color, (255, 255, 255), 0)
            col = col + (a,)
        d.arc(box, start=start_ang, end=end_ang, fill=col, width=lw)


def make_icon(size: int) -> Image.Image:
    img = vertical_gradient(size, BG_TOP, BG_MID, BG_BOT).convert("RGBA")
    d = ImageDraw.Draw(img, "RGBA")

    # 右上角柔光
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([size * 0.55, -size * 0.25, size * 1.25, size * 0.45],
               fill=ACCENT + (70,))
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.10))
    img = Image.alpha_composite(img, glow)
    d = ImageDraw.Draw(img, "RGBA")

    pad = int(size * 0.20)
    lw = max(2, int(size * 0.055))
    # 投影画面（暖色小矩形）象征被投出的内容
    bx0 = int(size * 0.18)
    by0 = int(size * 0.24)
    bx1 = int(size * 0.62)
    by1 = int(size * 0.76)
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=int(size * 0.08),
                        outline=(255, 255, 255, 235), width=lw)
    d.line([(bx0 + lw, int(by0 * 0.98) + int(size * 0.02)),
            (bx0 + lw, int(by0 + size * 0.10))], fill=(255, 255, 255, 200), width=lw)

    sym_w = int(size * 0.44)
    sym_h = int(size * 0.40)
    draw_cast_symbol(d, int(size * 0.30), int(size * 0.30), sym_w, sym_h,
                     WARM, max(2, int(size * 0.05)))

    mask = rounded_rect(size, int(size * 0.22))
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def make_tray(size: int, color=(235, 240, 252)) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    lw = max(2, int(size * 0.10))
    draw_cast_symbol(d, int(size * 0.08), int(size * 0.14),
                     int(size * 0.86), int(size * 0.72), color, lw)
    return img


def main():
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    icons = [make_icon(s) for s in sizes]

    png_path = os.path.join(OUT, "app_icon.png")
    icons[-1].save(png_path)
    ico_path = os.path.join(OUT, "app_icon.ico")
    icons[-1].save(ico_path, sizes=[(s, s) for s in sizes])
    print("生成:", png_path)
    print("生成:", ico_path)

    for s in (16, 20, 24, 32):
        p = os.path.join(OUT, f"tray_{s}.png")
        make_tray(s).save(p)
        make_tray(s, (255, 255, 255)).save(os.path.join(OUT, f"tray_{s}_w.png"))

    # 接收端 Android 图标
    android_dir = os.path.join(PROJECT, "receiver", "android", "res")
    for density, size in (("mdpi", 48), ("hdpi", 72), ("xhdpi", 96),
                          ("xxhdpi", 144), ("xxxhdpi", 192)):
        dd = os.path.join(android_dir, f"drawable-{density}")
        os.makedirs(dd, exist_ok=True)
        make_icon(size).save(os.path.join(dd, "ic_launcher.png"))
        make_icon(size).save(os.path.join(dd, "ic_banner.png"))
    print("生成 Android 图标 ->", android_dir)


if __name__ == "__main__":
    main()
