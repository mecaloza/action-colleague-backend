"""
Generate clean infographic images with Pillow.
Text is 100% readable — no AI hallucinated text.
Corporate style: dark/black backgrounds with white text.
"""
import io
import math
from PIL import Image, ImageDraw, ImageFont

# Colors — Corporate dark theme
BLACK = (15, 15, 20)           # Near-black background
DARK_GRAY = (25, 25, 35)      # Card backgrounds
ACCENT = (109, 40, 217)       # #6d28d9 violet accent
ACCENT_LIGHT = (139, 92, 246) # #8b5cf6
WHITE = (255, 255, 255)
LIGHT_TEXT = (220, 220, 230)
MUTED_TEXT = (160, 160, 175)
CARD_BG = (30, 30, 42)
CARD_BORDER = (50, 50, 65)

# Dimensions (1080p — matches HeyGen output exactly)
WIDTH = 1920
HEIGHT = 1080


def _draw_icon_checkmark(draw, cx, cy, size, color=WHITE):
    """Draw a checkmark icon."""
    s = size // 2
    points = [(cx - s*0.5, cy), (cx - s*0.1, cy + s*0.45), (cx + s*0.55, cy - s*0.4)]
    draw.line(points, fill=color, width=max(4, size // 8))

def _draw_icon_star(draw, cx, cy, size, color=WHITE):
    """Draw a 5-point star."""
    import math
    s = size * 0.4
    pts = []
    for i in range(10):
        angle = math.pi / 2 + i * math.pi / 5
        r = s if i % 2 == 0 else s * 0.4
        pts.append((cx + r * math.cos(angle), cy - r * math.sin(angle)))
    draw.polygon(pts, fill=color)

def _draw_icon_lightning(draw, cx, cy, size, color=WHITE):
    """Draw a lightning bolt."""
    s = size * 0.4
    pts = [
        (cx - s*0.1, cy - s*0.7),
        (cx + s*0.35, cy - s*0.1),
        (cx + s*0.05, cy - s*0.1),
        (cx + s*0.15, cy + s*0.7),
        (cx - s*0.3, cy + s*0.05),
        (cx - s*0.05, cy + s*0.05),
    ]
    draw.polygon(pts, fill=color)

def _draw_icon_shield(draw, cx, cy, size, color=WHITE):
    """Draw a shield icon."""
    s = size * 0.38
    pts = [
        (cx, cy - s*0.9),
        (cx + s*0.7, cy - s*0.5),
        (cx + s*0.6, cy + s*0.4),
        (cx, cy + s*0.9),
        (cx - s*0.6, cy + s*0.4),
        (cx - s*0.7, cy - s*0.5),
    ]
    draw.polygon(pts, fill=color)

def _draw_icon_target(draw, cx, cy, size, color=WHITE):
    """Draw concentric circles (target)."""
    s = size * 0.38
    draw.ellipse([cx-s, cy-s, cx+s, cy+s], outline=color, width=max(3, size//12))
    s2 = s * 0.6
    draw.ellipse([cx-s2, cy-s2, cx+s2, cy+s2], outline=color, width=max(3, size//12))
    s3 = s * 0.2
    draw.ellipse([cx-s3, cy-s3, cx+s3, cy+s3], fill=color)

def _draw_icon_chart(draw, cx, cy, size, color=WHITE):
    """Draw a bar chart icon."""
    s = size * 0.35
    bw = s * 0.3
    draw.rectangle([cx - s*0.7, cy + s*0.1, cx - s*0.7 + bw, cy + s*0.7], fill=color)
    draw.rectangle([cx - bw/2, cy - s*0.4, cx + bw/2, cy + s*0.7], fill=color)
    draw.rectangle([cx + s*0.7 - bw, cy - s*0.7, cx + s*0.7, cy + s*0.7], fill=color)

def _draw_icon_gear(draw, cx, cy, size, color=WHITE):
    """Draw a gear/cog icon."""
    import math
    s = size * 0.35
    # Outer teeth
    pts = []
    for i in range(16):
        angle = i * math.pi / 8
        r = s if i % 2 == 0 else s * 0.7
        pts.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    draw.polygon(pts, fill=color)
    # Inner hole
    s2 = s * 0.3
    draw.ellipse([cx-s2, cy-s2, cx+s2, cy+s2], fill=ACCENT)

def _draw_icon_book(draw, cx, cy, size, color=WHITE):
    """Draw an open book icon."""
    s = size * 0.35
    # Left page
    draw.polygon([(cx, cy-s*0.1), (cx-s*0.8, cy-s*0.6), (cx-s*0.8, cy+s*0.6), (cx, cy+s*0.3)], fill=color)
    # Right page
    draw.polygon([(cx, cy-s*0.1), (cx+s*0.8, cy-s*0.6), (cx+s*0.8, cy+s*0.6), (cx, cy+s*0.3)], outline=color, width=max(3, size//12))

ICON_DRAWERS = [
    _draw_icon_checkmark,
    _draw_icon_star,
    _draw_icon_lightning,
    _draw_icon_shield,
    _draw_icon_target,
    _draw_icon_chart,
    _draw_icon_gear,
    _draw_icon_book,
]


def _get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Try to load a nice font, fallback to default."""
    font_paths = [
        # Linux (Railway) — bold
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/TTF/DejaVuSans.ttf",
        # macOS
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNSDisplay.ttf",
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.Draw) -> list[str]:
    """Word-wrap text to fit within max_width."""
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def generate_infographic(
    title: str,
    points: list[str],
    subtitle: str = "",
    style: str = "cards",
) -> bytes:
    """
    Generate a professional infographic as PNG bytes.
    Dark corporate theme with EXTRA LARGE text for HeyGen video embedding.
    Text must be readable even when video is viewed on mobile.
    
    Layout reserves right-bottom area for HeyGen avatar overlay.
    Avatar zone: ~right 30%, bottom 40% of frame.
    """
    img = Image.new("RGB", (WIDTH, HEIGHT), BLACK)
    draw = ImageDraw.Draw(img)

    # Subtle gradient background
    for y in range(HEIGHT):
        ratio = y / HEIGHT
        r = int(BLACK[0] + (DARK_GRAY[0] - BLACK[0]) * ratio * 0.5)
        g = int(BLACK[1] + (DARK_GRAY[1] - BLACK[1]) * ratio * 0.5)
        b = int(BLACK[2] + (DARK_GRAY[2] - BLACK[2]) * ratio * 0.5)
        draw.line([(0, y), (WIDTH, y)], fill=(r, g, b))

    # Top accent bar (thicker)
    draw.rectangle([0, 0, WIDTH, 10], fill=ACCENT)

    # Left accent stripe (thicker)
    draw.rectangle([0, 0, 12, HEIGHT], fill=ACCENT)

    # --- Safe zone: avoid right-bottom where avatar sits ---
    # Avatar is at scale=0.22, offset x=0.42 y=0.28
    # That means avatar sits roughly at x: 1330-1920, y: 650-1080
    # We keep text in left 65% of frame and full height for title area
    SAFE_WIDTH = int(WIDTH * 0.62)  # ~1190px safe text area
    MARGIN_LEFT = 80
    MARGIN_RIGHT = 60  # from safe edge

    # Title — HUGE (readable at any size)
    title_font = _get_font(140, bold=True)
    title_lines = _wrap_text(title.upper(), title_font, WIDTH - 200, draw)
    y_pos = 50
    for line in title_lines[:2]:
        draw.text((MARGIN_LEFT, y_pos), line, fill=WHITE, font=title_font)
        y_pos += 165

    # Subtitle — large and high contrast for mobile readability
    if subtitle:
        sub_font = _get_font(80, bold=True)
        draw.text((MARGIN_LEFT, y_pos + 5), subtitle, fill=LIGHT_TEXT, font=sub_font)
        y_pos += 105

    # Divider line
    y_pos += 15
    draw.rectangle([MARGIN_LEFT, y_pos, WIDTH - 80, y_pos + 5], fill=ACCENT)
    y_pos += 45

    # Content area — limit to 3 points max for readability
    card_start_y = y_pos
    available_height = HEIGHT - card_start_y - 60
    num_points = min(len(points), 3)  # Max 3 points for large text

    # Always use list/row layout — most readable in video
    gap = 25
    item_height = min(200, (available_height - gap * (num_points - 1)) // max(num_points, 1))
    point_font = _get_font(72, bold=True)
    detail_font = _get_font(56)

    for i, point in enumerate(points[:num_points]):
        y = card_start_y + i * (item_height + gap)

        # Row background — full width (avatar overlaps but that's OK for bg)
        draw.rounded_rectangle(
            (MARGIN_LEFT, y, WIDTH - 80, y + item_height),
            radius=20, fill=CARD_BG, outline=CARD_BORDER, width=3
        )

        # Left accent bar on row
        draw.rectangle([MARGIN_LEFT, y + 15, MARGIN_LEFT + 10, y + item_height - 15], fill=ACCENT)

        # Number badge instead of tiny icons — more readable
        badge_size = 90
        badge_x = MARGIN_LEFT + 30
        badge_y = y + item_height // 2 - badge_size // 2
        draw.ellipse(
            [badge_x, badge_y, badge_x + badge_size, badge_y + badge_size],
            fill=ACCENT
        )
        # Draw number
        num_font = _get_font(52, bold=True)
        num_text = str(i + 1)
        num_bbox = draw.textbbox((0, 0), num_text, font=num_font)
        num_w = num_bbox[2] - num_bbox[0]
        num_h = num_bbox[3] - num_bbox[1]
        draw.text(
            (badge_x + badge_size // 2 - num_w // 2, badge_y + badge_size // 2 - num_h // 2 - 4),
            num_text, fill=WHITE, font=num_font
        )

        # Text — EXTRA LARGE, stays in safe zone (left 62%)
        text_x = badge_x + badge_size + 30
        text_max_w = SAFE_WIDTH - text_x + MARGIN_LEFT - MARGIN_RIGHT

        # Split point by colon for title/body
        if ": " in point:
            pt_title, pt_body = point.split(": ", 1)
        else:
            pt_title = point
            pt_body = ""

        if pt_body:
            # Two-line: bold title + regular body
            title_lines = _wrap_text(pt_title, point_font, text_max_w, draw)
            text_y = y + 20
            for line in title_lines[:2]:
                draw.text((text_x, text_y), line, fill=WHITE, font=point_font)
                text_y += 85

            body_lines = _wrap_text(pt_body, detail_font, text_max_w, draw)
            for line in body_lines[:2]:
                draw.text((text_x, text_y), line, fill=MUTED_TEXT, font=detail_font)
                text_y += 68
        else:
            # Single text, vertically centered
            wrapped = _wrap_text(pt_title[:80], point_font, text_max_w, draw)
            total_h = len(wrapped[:2]) * 85
            text_y = y + item_height // 2 - total_h // 2
            for line in wrapped[:2]:
                draw.text((text_x, text_y), line, fill=WHITE, font=point_font)
                text_y += 85

    # Bottom accent bar (thicker)
    draw.rectangle([0, HEIGHT - 10, WIDTH, HEIGHT], fill=ACCENT)

    # Export
    buf = io.BytesIO()
    img.save(buf, format="PNG", quality=95)
    return buf.getvalue()


from typing import Optional

def upload_to_supabase(image_bytes: bytes, filename: str, supabase_url: str, supabase_key: str) -> Optional[str]:
    """Upload image to Supabase Storage and return public URL."""
    import httpx
    bucket = "infographics"

    # Ensure bucket exists (ignore error if already exists)
    try:
        httpx.post(
            f"{supabase_url}/storage/v1/bucket",
            headers={"Authorization": f"Bearer {supabase_key}", "apikey": supabase_key, "Content-Type": "application/json"},
            json={"id": bucket, "name": bucket, "public": True},
            timeout=10,
        )
    except Exception:
        pass

    # Upload file
    try:
        r = httpx.post(
            f"{supabase_url}/storage/v1/object/{bucket}/{filename}",
            headers={
                "Authorization": f"Bearer {supabase_key}",
                "apikey": supabase_key,
                "Content-Type": "image/png",
                "x-upsert": "true",
            },
            content=image_bytes,
            timeout=30,
        )
        r.raise_for_status()
        return f"{supabase_url}/storage/v1/object/public/{bucket}/{filename}"
    except Exception as e:
        return None
