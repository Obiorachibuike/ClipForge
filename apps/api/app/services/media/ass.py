"""Advanced SubStation Alpha (ASS) generation.

Everything the caption engine promises - word highlighting, karaoke timing,
line wrapping, positioning, fonts, stroke/shadow, backgrounds, animation styles
- is produced here as a real subtitle file that libass renders frame-accurately.
Nothing in this module fabricates timing: every cue starts from measured word
timestamps.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------- presets ---
# Each preset is a complete caption configuration. `animation` selects how the
# word-level timing is expressed; the rest is pure styling.
CAPTION_PRESETS: dict[str, dict[str, Any]] = {
    "classic": {
        "name": "Classic",
        "description": "Clean sentence captions with a soft outline.",
        "font_family": "DejaVu Sans",
        "font_size": 54,
        "bold": False,
        "italic": False,
        "uppercase": False,
        "text_color": "#FFFFFF",
        "highlight_color": "#FFD166",
        "outline_color": "#000000",
        "outline_width": 3,
        "shadow": 1,
        "background": "outline",
        "background_color": "#000000",
        "background_opacity": 0.45,
        "position": "bottom",
        "margin_v": 190,
        "max_words_per_line": 6,
        "max_chars_per_line": 32,
        "max_lines": 2,
        "animation": "none",
        "align": "center",
    },
    "bold": {
        "name": "Bold",
        "description": "Heavy uppercase word-pop captions. The default look.",
        "font_family": "DejaVu Sans",
        "font_size": 66,
        "bold": True,
        "italic": False,
        "uppercase": True,
        "text_color": "#FFFFFF",
        "highlight_color": "#7C5CFF",
        "outline_color": "#08080C",
        "outline_width": 5,
        "shadow": 2,
        "background": "outline",
        "background_color": "#000000",
        "background_opacity": 0.5,
        "position": "bottom",
        "margin_v": 240,
        "max_words_per_line": 4,
        "max_chars_per_line": 20,
        "max_lines": 2,
        "animation": "pop",
        "align": "center",
    },
    "minimal": {
        "name": "Minimal",
        "description": "Small, unobtrusive captions.",
        "font_family": "DejaVu Sans",
        "font_size": 42,
        "bold": False,
        "italic": False,
        "uppercase": False,
        "text_color": "#F5F5F7",
        "highlight_color": "#8AB4FF",
        "outline_color": "#000000",
        "outline_width": 2,
        "shadow": 0,
        "background": "none",
        "background_color": "#000000",
        "background_opacity": 0.0,
        "position": "bottom",
        "margin_v": 150,
        "max_words_per_line": 8,
        "max_chars_per_line": 42,
        "max_lines": 2,
        "animation": "highlight",
        "align": "center",
    },
    "podcast": {
        "name": "Podcast",
        "description": "Speaker-style captions with a solid plate.",
        "font_family": "DejaVu Sans",
        "font_size": 50,
        "bold": True,
        "italic": False,
        "uppercase": False,
        "text_color": "#FFFFFF",
        "highlight_color": "#34D399",
        "outline_color": "#000000",
        "outline_width": 2,
        "shadow": 0,
        "background": "box",
        "background_color": "#101014",
        "background_opacity": 0.78,
        "position": "bottom",
        "margin_v": 200,
        "max_words_per_line": 7,
        "max_chars_per_line": 34,
        "max_lines": 2,
        "animation": "highlight",
        "align": "center",
    },
    "karaoke": {
        "name": "Karaoke",
        "description": "Smooth per-word fill timing driven by real word durations.",
        "font_family": "DejaVu Sans",
        "font_size": 62,
        "bold": True,
        "italic": False,
        "uppercase": True,
        "text_color": "#E9E9F0",
        "highlight_color": "#FF4D8D",
        "outline_color": "#0B0B10",
        "outline_width": 4,
        "shadow": 1,
        "background": "outline",
        "background_color": "#000000",
        "background_opacity": 0.4,
        "position": "bottom",
        "margin_v": 240,
        "max_words_per_line": 5,
        "max_chars_per_line": 26,
        "max_lines": 2,
        "animation": "karaoke",
        "align": "center",
    },
    "creator": {
        "name": "Creator",
        "description": "Centered mid-screen captions with a highlight pill.",
        "font_family": "DejaVu Sans",
        "font_size": 58,
        "bold": True,
        "italic": False,
        "uppercase": False,
        "text_color": "#FFFFFF",
        "highlight_color": "#22D3EE",
        "outline_color": "#000000",
        "outline_width": 3,
        "shadow": 2,
        "background": "box",
        "background_color": "#111827",
        "background_opacity": 0.6,
        "position": "center",
        "margin_v": 0,
        "max_words_per_line": 5,
        "max_chars_per_line": 24,
        "max_lines": 3,
        "animation": "typewriter",
        "align": "center",
    },
    "high_contrast": {
        "name": "High Contrast",
        "description": "Maximum legibility: heavy plate, thick stroke.",
        "font_family": "DejaVu Sans",
        "font_size": 60,
        "bold": True,
        "italic": False,
        "uppercase": True,
        "text_color": "#FFFFFF",
        "highlight_color": "#FDE047",
        "outline_color": "#000000",
        "outline_width": 6,
        "shadow": 3,
        "background": "box",
        "background_color": "#000000",
        "background_opacity": 0.85,
        "position": "bottom",
        "margin_v": 210,
        "max_words_per_line": 5,
        "max_chars_per_line": 24,
        "max_lines": 2,
        "animation": "pop",
        "align": "center",
    },
}

ANIMATIONS = {"none", "highlight", "karaoke", "pop", "typewriter"}
POSITIONS = {"top", "center", "bottom"}
BACKGROUNDS = {"none", "outline", "box"}
ALIGNMENTS = {"left", "center", "right"}

_ALIGNMENT_CODE = {"left": 1, "center": 2, "right": 3}
_ALIGNMENT_CODE_TOP = {"left": 7, "center": 8, "right": 9}
_ALIGNMENT_CODE_MIDDLE = {"left": 4, "center": 5, "right": 6}

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


# ------------------------------------------------------------------ colors ---
def hex_to_ass_color(value: str, opacity: float = 1.0) -> str:
    """#RRGGBB (+ opacity 0..1) -> &HAABBGGRR (ASS stores BGR + alpha)."""
    match = _HEX_RE.match(str(value or "").strip())
    red, green, blue = (255, 255, 255)
    if match:
        hex_value = match.group(1)
        red, green, blue = (int(hex_value[i : i + 2], 16) for i in (0, 2, 4))
    alpha = int(round((1.0 - max(0.0, min(1.0, opacity))) * 255))
    return f"&H{alpha:02X}{blue:02X}{green:02X}{red:02X}"


# Fonts the burn-in step can resolve. FFmpeg's libass reads fonts from the
# standard font directories plus any directory passed via FONTCONFIG/--fontsdir.
FONT_DIRS = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/Library/Fonts",
    "/System/Library/Fonts",
    "/mnt/c/Windows/Fonts",
)


def available_fonts() -> list[str]:
    """Font family names discoverable on this machine (best effort, honest)."""
    families: set[str] = set()
    for directory in FONT_DIRS:
        root = Path(directory)
        if not root.exists():
            continue
        for path in list(root.rglob("*.ttf"))[:400] + list(root.rglob("*.otf"))[:200]:
            name = path.stem
            for suffix in ("-Bold", "-Italic", "-BoldItalic", "-Regular", "-Light", "-Medium", "-SemiBold"):
                if name.endswith(suffix):
                    name = name[: -len(suffix)]
            families.add(name.replace("_", " ").strip())
    return sorted(family for family in families if family)


def clean_font_family(family: str, fallback: str = "DejaVu Sans") -> str:
    text = re.sub(r"[^\w \-]", "", str(family or ""))[:60].strip()
    return text or fallback


def escape_text(text: str) -> str:
    """Escape text for an ASS dialogue line (newlines become \\N)."""
    value = str(text or "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("{", "(").replace("}", ")")
    value = value.replace("\\", "/")
    value = value.replace("\n", r"\N")
    return value.strip()


def format_time(seconds: float) -> str:
    """ASS timestamp H:MM:SS.cc"""
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


# ------------------------------------------------------------------ config ---
@dataclass
class CaptionConfig:
    preset: str = "bold"
    font_family: str = "DejaVu Sans"
    font_size: int = 60
    bold: bool = True
    italic: bool = False
    uppercase: bool = False
    text_color: str = "#FFFFFF"
    highlight_color: str = "#7C5CFF"
    outline_color: str = "#000000"
    outline_width: int = 4
    shadow: int = 1
    background: str = "outline"
    background_color: str = "#000000"
    background_opacity: float = 0.5
    position: str = "bottom"
    align: str = "center"
    margin_v: int = 220
    margin_h: int = 60
    max_words_per_line: int = 5
    max_chars_per_line: int = 26
    max_lines: int = 2
    line_spacing: float = 1.0
    animation: str = "pop"
    letter_spacing: int = 0
    opacity: float = 1.0
    extra: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(data: dict[str, Any] | None, *, preset: str | None = None) -> CaptionConfig:
        base_key = (preset or (data or {}).get("preset") or "bold").strip()
        base = dict(CAPTION_PRESETS.get(base_key, CAPTION_PRESETS["bold"]))
        incoming = {k: v for k, v in (data or {}).items() if v is not None}
        merged = {**base, **incoming}
        cfg = CaptionConfig(
            preset=base_key if base_key in CAPTION_PRESETS else "bold",
            font_family=clean_font_family(merged.get("font_family", base["font_family"])),
            font_size=_clamp_int(merged.get("font_size"), 18, 200, base["font_size"]),
            bold=bool(merged.get("bold", base["bold"])),
            italic=bool(merged.get("italic", base["italic"])),
            uppercase=bool(merged.get("uppercase", base["uppercase"])),
            text_color=_color(merged.get("text_color"), base["text_color"]),
            highlight_color=_color(merged.get("highlight_color"), base["highlight_color"]),
            outline_color=_color(merged.get("outline_color"), base["outline_color"]),
            outline_width=_clamp_int(merged.get("outline_width"), 0, 12, base["outline_width"]),
            shadow=_clamp_int(merged.get("shadow"), 0, 12, base["shadow"]),
            background=str(merged.get("background", base["background"])).lower(),
            background_color=_color(merged.get("background_color"), base["background_color"]),
            background_opacity=_clamp_float(merged.get("background_opacity"), 0.0, 1.0, base["background_opacity"]),
            position=str(merged.get("position", base["position"])).lower(),
            align=str(merged.get("align", base.get("align", "center"))).lower(),
            margin_v=_clamp_int(merged.get("margin_v"), 0, 1200, base["margin_v"]),
            margin_h=_clamp_int(merged.get("margin_h"), 0, 600, 60),
            max_words_per_line=_clamp_int(merged.get("max_words_per_line"), 1, 12, base["max_words_per_line"]),
            max_chars_per_line=_clamp_int(merged.get("max_chars_per_line"), 8, 80, base["max_chars_per_line"]),
            max_lines=_clamp_int(merged.get("max_lines"), 1, 4, base["max_lines"]),
            line_spacing=_clamp_float(merged.get("line_spacing"), 0.5, 3.0, 1.0),
            animation=str(merged.get("animation", base["animation"])).lower(),
            letter_spacing=_clamp_int(merged.get("letter_spacing"), -10, 30, 0),
            opacity=_clamp_float(merged.get("opacity"), 0.1, 1.0, 1.0),
        )
        if cfg.background not in BACKGROUNDS:
            cfg.background = "outline"
        if cfg.position not in POSITIONS:
            cfg.position = "bottom"
        if cfg.align not in ALIGNMENTS:
            cfg.align = "center"
        if cfg.animation not in ANIMATIONS:
            cfg.animation = "none"
        return cfg

    def to_dict(self) -> dict[str, Any]:
        return {
            "preset": self.preset,
            "font_family": self.font_family,
            "font_size": self.font_size,
            "bold": self.bold,
            "italic": self.italic,
            "uppercase": self.uppercase,
            "text_color": self.text_color,
            "highlight_color": self.highlight_color,
            "outline_color": self.outline_color,
            "outline_width": self.outline_width,
            "shadow": self.shadow,
            "background": self.background,
            "background_color": self.background_color,
            "background_opacity": self.background_opacity,
            "position": self.position,
            "align": self.align,
            "margin_v": self.margin_v,
            "margin_h": self.margin_h,
            "max_words_per_line": self.max_words_per_line,
            "max_chars_per_line": self.max_chars_per_line,
            "max_lines": self.max_lines,
            "line_spacing": self.line_spacing,
            "animation": self.animation,
            "letter_spacing": self.letter_spacing,
            "opacity": self.opacity,
        }

    def alignment_code(self) -> int:
        table = (
            _ALIGNMENT_CODE_TOP if self.position == "top" else _ALIGNMENT_CODE_MIDDLE if self.position == "center" else _ALIGNMENT_CODE
        )
        return table[self.align]


def _clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _clamp_float(value: Any, minimum: float, maximum: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _color(value: Any, default: str) -> str:
    """Normalize to #RRGGBB, accepting #abc / abc / #aabbcc."""
    text = str(value or "").strip()
    if text.startswith("#"):
        text = text[1:]
    if re.fullmatch(r"[0-9a-fA-F]{3}", text):
        text = "".join(ch * 2 for ch in text)
    if re.fullmatch(r"[0-9a-fA-F]{6}", text):
        return f"#{text.upper()}"
    return default


# --------------------------------------------------------------- word model ---
@dataclass
class CaptionWord:
    text: str
    start: float  # seconds, relative to the clip start
    end: float
    index: int = 0
    speaker: str = ""
    is_edited: bool = False

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class HeadlineSpec:
    text: str
    position: str = "top"  # top|bottom
    font_family: str = "DejaVu Sans"
    font_size: int = 46
    bold: bool = True
    uppercase: bool = False
    text_color: str = "#FFFFFF"
    background: str = "box"
    background_color: str = "#0B0B12"
    background_opacity: float = 0.62
    outline_color: str = "#000000"
    outline_width: int = 2
    align: str = "center"
    margin_v: int = 140
    start: float = 0.0
    end: float = 0.0
    fade_in_ms: int = 250
    fade_out_ms: int = 250


def _wrap_words(words: Sequence[CaptionWord], cfg: CaptionConfig) -> list[list[CaptionWord]]:
    """Group words into caption cues honouring word and character limits, and
    breaking early on long pauses (natural speech boundaries)."""
    cues: list[list[CaptionWord]] = []
    current: list[CaptionWord] = []
    for i, word in enumerate(words):
        candidate = current + [word]
        chars = len(" ".join(w.text for w in candidate))
        too_many_words = len(candidate) > cfg.max_words_per_line * cfg.max_lines
        too_many_chars = chars > cfg.max_chars_per_line * cfg.max_lines
        gap = word.start - current[-1].end if current else 0.0
        hard_break = bool(current) and (too_many_words or too_many_chars)
        soft_break = bool(current) and gap >= 1.1 and len(current) >= 2
        ends_sentence = bool(current) and re.search(r"[.!?…]$", current[-1].text)
        if hard_break or soft_break or (ends_sentence and len(current) >= cfg.max_words_per_line):
            cues.append(current)
            current = [word]
        else:
            current = candidate
        if i == len(words) - 1 and current:
            cues.append(current)
            current = []
    return [c for c in cues if c]


def _lines_for_cue(cue: Sequence[CaptionWord], cfg: CaptionConfig) -> list[list[CaptionWord]]:
    """Break a cue into display lines respecting the line limits."""
    lines: list[list[CaptionWord]] = []
    current: list[CaptionWord] = []
    for word in cue:
        candidate = current + [word]
        if current and (
            len(candidate) > cfg.max_words_per_line or len(" ".join(w.text for w in candidate)) > cfg.max_chars_per_line
        ):
            lines.append(current)
            current = [word]
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _render_text(word: CaptionWord, cfg: CaptionConfig, *, active: bool) -> str:
    text = word.text.upper() if cfg.uppercase else word.text
    return escape_text(text)


class AssBuilder:
    """Builds a complete .ass document for one clip."""

    def __init__(self, width: int, height: int, cfg: CaptionConfig) -> None:
        self.width = width
        self.height = height
        self.cfg = cfg
        self.events: list[str] = []
        self.styles: list[tuple[str, str]] = []

    # ----------------------------------------------------------- internals ---
    def _margin_v(self) -> int:
        base = self.cfg.margin_v
        # Scale the margin from a 1920-high design space to the output height.
        return max(0, int(round(base * self.height / 1920)))

    def _style_color(self, color: str, opacity: float | None = None) -> str:
        return hex_to_ass_color(color, self.cfg.opacity if opacity is None else opacity)

    def _caption_style(self) -> str:
        cfg = self.cfg
        if cfg.background == "box":
            border_style, outline = 3, max(0, cfg.outline_width // 2)
            back_color = hex_to_ass_color(cfg.background_color, cfg.background_opacity)
        elif cfg.background == "outline":
            border_style, outline = 1, cfg.outline_width
            back_color = hex_to_ass_color(cfg.outline_color, 0.0)
        else:
            border_style, outline = 1, 0
            back_color = hex_to_ass_color("#000000", 1.0)
        primary = self._style_color(cfg.text_color)
        secondary = self._style_color(cfg.highlight_color)
        outline_color = hex_to_ass_color(cfg.outline_color)
        return (
            f"Style: Caption,{cfg.font_family},{cfg.font_size},"
            f"{primary},{secondary},{outline_color},{back_color},"
            f"{-1 if cfg.bold else 0},{0 if not cfg.italic else -1},0,0,"
            f"100,100,{cfg.letter_spacing},0,{border_style},{outline},{cfg.shadow},"
            f"{cfg.alignment_code()},{self.cfg.margin_h * 2},{self.cfg.margin_h * 2},{self._margin_v()},1"
        )

    def _highlight_style(self) -> str:
        cfg = self.cfg
        if cfg.background == "box":
            border_style, outline = 3, max(0, cfg.outline_width // 2)
            back_color = hex_to_ass_color(cfg.background_color, cfg.background_opacity)
        else:
            border_style, outline = 1, cfg.outline_width
            back_color = hex_to_ass_color(cfg.outline_color, 0.0)
        return (
            f"Style: CaptionHL,{cfg.font_family},{cfg.font_size},"
            f"{self._style_color(cfg.highlight_color)},{self._style_color(cfg.text_color)},"
            f"{hex_to_ass_color(cfg.outline_color)},{back_color},"
            f"{-1 if cfg.bold else 0},{0 if not cfg.italic else -1},0,0,"
            f"100,102,{cfg.letter_spacing},0,{border_style},{outline},{cfg.shadow},"
            f"{cfg.alignment_code()},{self.cfg.margin_h * 2},{self.cfg.margin_h * 2},{self._margin_v()},1"
        )

    def add_caption_style(self) -> None:
        self.styles.append(("Caption", self._caption_style()))
        self.styles.append(("CaptionHL", self._highlight_style()))

    def _line_text(
        self,
        line: Sequence[CaptionWord],
        cfg: CaptionConfig,
        active_index: int | None,
        *,
        motion: bool = False,
    ) -> str:
        """Render one display line, optionally styling the active word.

        `motion` appends a short scale-up-and-settle transform, which is how the
        "pop" preset animates each word using its real timestamp.
        """
        parts: list[str] = []
        for word in line:
            rendered = _render_text(word, cfg, active=word.index == active_index)
            if active_index is not None and word.index == active_index:
                prefix = r"{\rCaptionHL"
                if motion:
                    prefix += r"\fscx112\fscy112\t(0,110,\fscx100\fscy100)"
                parts.append(prefix + "}" + rendered + r"{\rCaption}")
            else:
                parts.append(rendered)
        return " ".join(parts)

    def _emit(self, start: float, end: float, style: str, text: str, *, margin_v: int | None = None) -> None:
        if end <= start:
            end = start + 0.05
        margin = self._margin_v() if margin_v is None else margin_v
        self.events.append(
            f"Dialogue: 0,{format_time(start)},{format_time(end)},{style},,0,0,{margin},,{text}"
        )

    # ------------------------------------------------------------- captions ---
    def build_captions(self, words: Sequence[CaptionWord]) -> None:
        cfg = self.cfg
        if not words:
            return
        self.add_caption_style()
        cues = _wrap_words(words, cfg)
        for cue in cues:
            lines = _lines_for_cue(cue, cfg)
            if len(lines) > cfg.max_lines:
                lines = lines[: cfg.max_lines]
            joined = r"\N".join(
                " ".join(_render_text(w, cfg, active=False) for w in line) for line in lines
            )
            cue_end = cue[-1].end
            if cfg.animation == "none":
                self._emit(cue[0].start, cue_end + 0.12, "Caption", joined)
                continue
            if cfg.animation == "karaoke":
                # libass \k timing comes from the measured word durations.
                parts: list[str] = []
                for line in lines:
                    rendered: list[str] = []
                    for word in line:
                        centiseconds = max(1, int(round(word.duration * 100)))
                        rendered.append(rf"{{\k{centiseconds}}}{_render_text(word, cfg, active=False)}")
                    parts.append(" ".join(rendered))
                self._emit(cue[0].start, cue_end + 0.12, "Caption", r"\N".join(parts))
                continue
            # highlight / pop / typewriter -> one event per word keeps timing exact
            flat = [w for line in lines for w in line]
            for position, word in enumerate(flat):
                next_word = flat[position + 1] if position + 1 < len(flat) else None
                end = next_word.start if next_word and next_word.start > word.start else word.end
                if end <= word.start:
                    end = word.start + 0.08
                if cfg.animation == "typewriter":
                    revealed = flat[: position + 1]
                    text_lines = [" ".join(_render_text(w, cfg, active=False) for w in revealed)]
                else:
                    text_lines = []
                    cursor = 0
                    for line in lines:
                        if cursor <= position < cursor + len(line):
                            text_lines.append(self._line_text(line, cfg, word.index, motion=cfg.animation == "pop"))
                        else:
                            text_lines.append(" ".join(_render_text(w, cfg, active=False) for w in line))
                        cursor += len(line)
                self._emit(word.start, end, "Caption", r"\N".join(text_lines))
            # keep the last line on screen briefly after the final word
            if flat:
                self._emit(flat[-1].end, flat[-1].end + 0.35, "Caption", r"\N".join(
                    " ".join(_render_text(w, cfg, active=False) for w in line) for line in lines
                ))

    # ------------------------------------------------------------- headline ---
    def build_headline(self, spec: HeadlineSpec) -> None:
        if not spec.text.strip():
            return
        position = "top" if spec.position == "top" else "bottom"
        align_table = _ALIGNMENT_CODE_TOP if position == "top" else _ALIGNMENT_CODE
        alignment = align_table.get(spec.align, 8 if position == "top" else 2)
        margin_v = int(round(spec.margin_v * self.height / 1920))
        if spec.background == "box":
            border_style, outline = 3, max(0, spec.outline_width // 2)
            back = hex_to_ass_color(spec.background_color, spec.background_opacity)
        else:
            border_style, outline = 1, spec.outline_width
            back = hex_to_ass_color(spec.outline_color, 0.0)
        style = (
            f"Style: Headline,{clean_font_family(spec.font_family)},{spec.font_size},"
            f"{hex_to_ass_color(spec.text_color)},{hex_to_ass_color(spec.text_color)},"
            f"{hex_to_ass_color(spec.outline_color)},{back},"
            f"{-1 if spec.bold else 0},0,0,0,100,100,0,0,{border_style},{outline},1,"
            f"{alignment},80,80,{margin_v},1"
        )
        self.styles.append(("Headline", style))
        text = escape_text(spec.text.upper() if spec.uppercase else spec.text)
        fade = rf"{{\fad({max(0, spec.fade_in_ms)},{max(0, spec.fade_out_ms)})}}"
        start = max(0.0, spec.start)
        end = max(start + 0.5, spec.end)
        self._emit(start, end, "Headline", fade + text, margin_v=margin_v)

    # --------------------------------------------------------------- output ---
    def render(self) -> str:
        header = [
            "[Script Info]",
            "; Generated by ClipForge",
            "ScriptType: v4.00+",
            f"PlayResX: {self.width}",
            f"PlayResY: {self.height}",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: TV.709",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding",
        ]
        header.extend(style for _, style in self.styles)
        header.extend(["", "[Events]"])
        header.append("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text")
        return "\n".join(header + self.events) + "\n"


def escape_highlight_marker(word: CaptionWord, cfg: CaptionConfig) -> str:
    """Marker helper for the pop animation (kept trivial and explicit)."""
    return escape_text(word.text.upper() if cfg.uppercase else word.text)


def build_ass_document(
    *,
    width: int,
    height: int,
    words: Iterable[CaptionWord],
    caption_config: CaptionConfig | dict[str, Any] | None,
    headline: HeadlineSpec | dict[str, Any] | None = None,
) -> str:
    cfg = caption_config if isinstance(caption_config, CaptionConfig) else CaptionConfig.from_dict(caption_config)
    builder = AssBuilder(width, height, cfg)
    builder.build_captions(list(words))
    spec: HeadlineSpec | None = None
    if isinstance(headline, HeadlineSpec):
        spec = headline
    elif isinstance(headline, dict) and (headline.get("text") or "").strip():
        spec = HeadlineSpec(
            text=str(headline.get("text", "")),
            position=str(headline.get("position", "top")),
            font_family=clean_font_family(str(headline.get("font_family", "DejaVu Sans"))),
            font_size=_clamp_int(headline.get("font_size"), 20, 140, 46),
            bold=bool(headline.get("bold", True)),
            uppercase=bool(headline.get("uppercase", False)),
            text_color=str(headline.get("text_color", "#FFFFFF")),
            background=str(headline.get("background", "box")),
            background_color=str(headline.get("background_color", "#0B0B12")),
            background_opacity=_clamp_float(headline.get("background_opacity"), 0.0, 1.0, 0.62),
            outline_color=str(headline.get("outline_color", "#000000")),
            outline_width=_clamp_int(headline.get("outline_width"), 0, 10, 2),
            align=str(headline.get("align", "center")),
            margin_v=_clamp_int(headline.get("margin_v"), 0, 900, 140),
            start=float(headline.get("start", 0.0) or 0.0),
            end=float(headline.get("end", 0.0) or 0.0),
        )
    if spec:
        builder.build_headline(spec)
    return builder.render()


def caption_cues(
    words: Sequence[CaptionWord], cfg: CaptionConfig | dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Cue list for the editor UI (same grouping logic used for rendering)."""
    config = cfg if isinstance(cfg, CaptionConfig) else CaptionConfig.from_dict(cfg)
    cues: list[dict[str, Any]] = []
    for cue in _wrap_words(words, config):
        lines = _lines_for_cue(cue, config)
        cues.append(
            {
                "start": round(cue[0].start, 3),
                "end": round(cue[-1].end, 3),
                "text": " ".join((w.text.upper() if config.uppercase else w.text) for w in cue),
                "word_count": len(cue),
                "line_count": len(lines),
                "words": [
                    {
                        "index": w.index,
                        "word": w.text,
                        "start": round(w.start, 3),
                        "end": round(w.end, 3),
                        "speaker": w.speaker,
                        "is_edited": w.is_edited,
                    }
                    for w in cue
                ],
            }
        )
    return cues
