"""C3X onroad navigation map panel.

Sources, in order of preference:
1. CarrotNavi (TMAP) MAP MAIN video, decoded by selfdrive/carrot/tmap_preview.py
   into /dev/shm as raw RGB at native resolution (no image decoding here; the
   bytes are uploaded straight into a texture).
2. mapsd's shared grayscale road map preview PNG (/dev/shm/carrot_nav_map.png),
   the same image the Web page and cluster HUD mode 7 use.

The source picture is cropped (never stretched) to the panel shape.

ShowNavMap: 0 off, 1 small (bottom-right), 2 large (right), 3 tall (right, full height)
Double-tap the map: toggle full screen. Double-tap again to return.
Drag the map left/right with a finger to resize it; the width is saved in
ShowNavMapWidth (percent of the onroad width, 0 = default size for the mode).
Tall and full-screen maps are drawn under the HUD so turn info, speed and alerts stay visible.
"""
import time

import pyray as rl

from openpilot.selfdrive.carrot.tmap_preview import TMAP_PREVIEW_PATH, read_tmap_preview, tmap_preview_fresh
from openpilot.selfdrive.navigation.preview import PREVIEW_PATH, preview_fresh
from openpilot.system.ui.lib.text_draw import draw_text_ui_style

PARAM_INTERVAL = 1.0
CHECK_INTERVAL = 0.1
DOUBLE_TAP_S = 0.5
DRAG_START_PX = 25           # finger movement before a touch becomes a resize
MIN_WIDTH_PCT, MAX_WIDTH_PCT = 15, 70
MARGIN = 30
ROUNDNESS = 0.06
TALL_ASPECT = 0.62          # width / height of the tall panel
TALL_MAX_WIDTH_RATIO = 0.36  # of the onroad content width
# Tall panel: keep the picture above the HUD's bottom-right turn-info box, otherwise
# TMAP's own vehicle marker (low in the picture) ends up hidden behind it.
TALL_BOTTOM_RESERVE = 0.19   # of the panel height
BG_COLOR = rl.Color(0, 0, 0, 170)
FRAME_COLOR = rl.Color(255, 255, 255, 90)
GRAY_MAP_TINT = rl.Color(150, 220, 255, 255)  # grayscale road map tinted light blue
# Vertical crop anchor: TMAP keeps the vehicle low in the picture (screen_center_y_ratio ~0.8),
# so when the top/bottom must be trimmed, trim mostly from the top.
VERTICAL_ANCHOR = 0.75

SOURCE_NONE, SOURCE_TMAP, SOURCE_ROADS = 0, 1, 2
LAYER_UNDER_HUD, LAYER_OVER_HUD = 0, 1


def cover_source_rect(tex_w: float, tex_h: float, panel_aspect: float) -> rl.Rectangle:
  """Largest source rectangle with the panel's aspect ratio (crop to fill, no stretching)."""
  if tex_w <= 0 or tex_h <= 0 or panel_aspect <= 0:
    return rl.Rectangle(0, 0, max(tex_w, 0), max(tex_h, 0))
  if tex_w / tex_h > panel_aspect:
    width = tex_h * panel_aspect
    return rl.Rectangle((tex_w - width) / 2, 0, width, tex_h)
  height = tex_w / panel_aspect
  return rl.Rectangle(0, (tex_h - height) * VERTICAL_ANCHOR, tex_w, height)


class NavMapOverlay:
  def __init__(self, params):
    self._params = params
    self._mode = 0
    self._fullscreen = False
    self._last_tap = 0.0
    self._width_pct = 0         # saved ShowNavMapWidth; 0 = mode default
    self._drag_width = None     # live width (px) while resizing
    self._press_pos = None
    self._press_width = 0.0
    self._dragging = False
    self._content = rl.Rectangle(0, 0, 0, 0)
    self._panel = rl.Rectangle(0, 0, 0, 0)
    self._param_next_t = 0.0
    self._check_next_t = 0.0
    self._source = SOURCE_NONE
    self._mtime = 0
    self._texture = None
    self._texture_source = SOURCE_NONE

  @property
  def mode(self) -> int:
    return self._mode

  @property
  def fullscreen(self) -> bool:
    return self._fullscreen

  # ----- input -----
  def handle_press(self, pos) -> bool:
    """Returns True when the touch belongs to the map (caller must not use it)."""
    if self._mode == 0:
      return False
    if not self._fullscreen and not rl.check_collision_point_rec(pos, self._panel):
      return False
    self._press_pos = pos
    self._press_width = self._panel.width
    self._dragging = False
    return True

  def handle_move(self, pos) -> None:
    if self._press_pos is None or self._fullscreen:
      return
    dx = self._press_pos.x - pos.x  # dragging left grows the right-anchored panel
    if not self._dragging and abs(dx) < DRAG_START_PX:
      return
    self._dragging = True
    self._drag_width = self._clamp_width(self._press_width + dx)

  def handle_release(self, pos) -> None:
    if self._press_pos is None:
      return
    if self._dragging and self._drag_width is not None and self._content.width > 0:
      self._width_pct = int(round(100 * self._drag_width / self._content.width))
      self._save_width()
    elif not self._dragging:
      now = time.monotonic()
      if now - self._last_tap <= DOUBLE_TAP_S:
        self._fullscreen = not self._fullscreen
        self._last_tap = 0.0
      else:
        self._last_tap = now
    self._press_pos = None
    self._dragging = False
    self._drag_width = None

  def _clamp_width(self, width: float) -> float:
    content = self._content
    low, high = content.width * MIN_WIDTH_PCT / 100, content.width * MAX_WIDTH_PCT / 100
    return min(max(width, low), high)

  def _save_width(self) -> None:
    try:
      self._params.put_int_nonblocking("ShowNavMapWidth", self._width_pct)
    except Exception:
      pass
    self._param_next_t = time.monotonic() + PARAM_INTERVAL  # do not read back the old value before the write lands

  # ----- state -----
  def _refresh_mode(self, now: float) -> None:
    if now < self._param_next_t:
      return
    self._param_next_t = now + PARAM_INTERVAL
    try:
      self._mode = min(max(self._params.get_int("ShowNavMap"), 0), 3)
    except Exception:
      self._mode = 0
    if self._press_pos is None:
      try:
        width_pct = self._params.get_int("ShowNavMapWidth")
        self._width_pct = width_pct if MIN_WIDTH_PCT <= width_pct <= MAX_WIDTH_PCT else 0
      except Exception:
        self._width_pct = 0
    if self._mode == 0:
      self._fullscreen = False
      self._panel = rl.Rectangle(0, 0, 0, 0)
      self._unload()

  def _refresh_texture(self, now: float) -> None:
    if now < self._check_next_t:
      return
    self._check_next_t = now + CHECK_INTERVAL
    if tmap_preview_fresh():
      source, path = SOURCE_TMAP, TMAP_PREVIEW_PATH
    elif preview_fresh():
      source, path = SOURCE_ROADS, PREVIEW_PATH
    else:
      self._source = SOURCE_NONE
      return
    self._source = source
    try:
      mtime = path.stat().st_mtime_ns
    except OSError:
      self._source = SOURCE_NONE
      return
    if mtime == self._mtime and self._texture_source == source:
      return
    ok = self._load_tmap() if source == SOURCE_TMAP else self._load_png(path)
    if ok:
      self._texture_source = source
      self._mtime = mtime
    else:
      self._source = SOURCE_NONE

  def _load_png(self, path) -> bool:
    texture = rl.load_texture(str(path))
    if not texture.id:
      return False
    rl.set_texture_filter(texture, rl.TextureFilter.TEXTURE_FILTER_BILINEAR)
    self._unload()
    self._texture = texture
    return True

  def _load_tmap(self) -> bool:
    frame = read_tmap_preview()
    if frame is None:
      return False
    width, height, pixels = frame
    data = rl.ffi.cast("void *", rl.ffi.from_buffer(pixels))
    texture = self._texture
    if (texture is not None and self._texture_source == SOURCE_TMAP and
        texture.width == width and texture.height == height):
      rl.update_texture(texture, data)
      return True
    image = rl.Image(data, width, height, 1, rl.PixelFormat.PIXELFORMAT_UNCOMPRESSED_R8G8B8)
    texture = rl.load_texture_from_image(image)  # copies the pixels to the GPU
    if not texture.id:
      return False
    rl.set_texture_filter(texture, rl.TextureFilter.TEXTURE_FILTER_BILINEAR)
    self._unload()
    self._texture = texture
    return True

  # ----- layout -----
  def _user_width(self, rect: rl.Rectangle) -> float | None:
    if self._drag_width is not None:
      return self._drag_width
    if self._width_pct:
      return rect.width * self._width_pct / 100
    return None

  def _panel_rect(self, rect: rl.Rectangle) -> rl.Rectangle:
    if self._fullscreen:
      return rl.Rectangle(rect.x, rect.y, rect.width, rect.height)
    user_width = self._user_width(rect)
    if self._mode == 3:
      width = user_width if user_width is not None else min(rect.height * TALL_ASPECT, rect.width * TALL_MAX_WIDTH_RATIO)
      return rl.Rectangle(rect.x + rect.width - width, rect.y, width, rect.height)
    aspect = 16 / 9 if self._source != SOURCE_ROADS else 1.0
    max_height = rect.height - 2 * MARGIN
    if self._mode == 2:
      width = user_width if user_width is not None else rect.width * 0.48
      width = min(width, max_height * aspect)
      height = width / aspect
      return rl.Rectangle(rect.x + rect.width - width - MARGIN, rect.y + (rect.height - height) / 2, width, height)
    if user_width is not None:
      width = min(user_width, max_height * aspect)
      height = width / aspect
    else:
      height = min(rect.height * 0.40, rect.width * 0.30 / aspect)
      width = height * aspect
    return rl.Rectangle(rect.x + rect.width - width - MARGIN, rect.y + rect.height - height - MARGIN, width, height)

  def _layer(self) -> int:
    return LAYER_UNDER_HUD if self._fullscreen or self._mode == 3 else LAYER_OVER_HUD

  # ----- drawing -----
  def render(self, rect: rl.Rectangle, layer: int) -> None:
    now = time.monotonic()
    if layer == LAYER_UNDER_HUD:  # first call of the frame
      self._refresh_mode(now)
    if self._mode == 0 or layer != self._layer():
      return
    self._refresh_texture(now)
    showing = self._source != SOURCE_NONE and self._texture is not None and self._texture_source == self._source
    self._content = rect
    panel = self._panel_rect(rect)
    self._panel = panel
    if self._fullscreen and not showing:
      return  # never hide the camera behind an empty panel; double-tap the area to leave
    square = not self._fullscreen and self._mode != 3
    if not self._fullscreen:
      rl.draw_rectangle_rounded(panel, ROUNDNESS if square else 0.0, 10, rl.BLACK if self._mode == 3 else BG_COLOR)
    if showing:
      tint = rl.WHITE if self._source == SOURCE_TMAP else GRAY_MAP_TINT
      dest = panel
      if self._mode == 3 and not self._fullscreen:
        dest = rl.Rectangle(panel.x, panel.y, panel.width, panel.height * (1 - TALL_BOTTOM_RESERVE))
      source = cover_source_rect(self._texture.width, self._texture.height, dest.width / max(dest.height, 1))
      rl.draw_texture_pro(self._texture, source, dest, rl.Vector2(0, 0), 0, tint)
    else:
      # Needs CarrotNavi video, or a route/GPS + map tiles for the road map
      draw_text_ui_style("지도 대기중", panel.x + panel.width / 2, panel.y + panel.height / 2 - 20,
                         32 if self._mode == 1 else 44, rl.WHITE, align="center_top", y_offset=0.0)
    if square:
      rl.draw_rectangle_rounded_lines_ex(panel, ROUNDNESS, 10, 3, FRAME_COLOR)

  def _unload(self) -> None:
    if self._texture is not None:
      rl.unload_texture(self._texture)
      self._texture = None
      self._texture_source = SOURCE_NONE
      self._mtime = 0

  def close(self) -> None:
    self._unload()
