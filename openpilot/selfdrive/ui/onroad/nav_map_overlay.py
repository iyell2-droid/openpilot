"""C3X onroad navigation map panel.

Sources, in order of preference:
1. CarrotNavi (TMAP) MAP MAIN video, decoded by selfdrive/carrot/tmap_preview.py
   into /dev/shm/carrot_tmap_map.png (16:9 color image).
2. mapsd's shared grayscale road map preview (/dev/shm/carrot_nav_map.png), the
   same image the Web page and cluster HUD mode 7 use (square).

Decoding never happens here; the UI only reloads a small image when it changes.

ShowNavMap: 0 off, 1 small (bottom-right), 2 large (right side)
"""
import time

import pyray as rl

from openpilot.selfdrive.carrot.tmap_preview import TMAP_PREVIEW_PATH, tmap_preview_fresh
from openpilot.selfdrive.navigation.preview import PREVIEW_PATH, preview_fresh
from openpilot.system.ui.lib.text_draw import draw_text_ui_style

PARAM_INTERVAL = 1.0
CHECK_INTERVAL = 0.1
MARGIN = 30
ROUNDNESS = 0.06
BG_COLOR = rl.Color(0, 0, 0, 170)
FRAME_COLOR = rl.Color(255, 255, 255, 90)
GRAY_MAP_TINT = rl.Color(150, 220, 255, 255)  # grayscale road map tinted light blue

SOURCE_NONE, SOURCE_TMAP, SOURCE_ROADS = 0, 1, 2


class NavMapOverlay:
  def __init__(self, params):
    self._params = params
    self._mode = 0
    self._param_next_t = 0.0
    self._check_next_t = 0.0
    self._source = SOURCE_NONE
    self._mtime = 0
    self._texture = None
    self._texture_source = SOURCE_NONE

  @property
  def mode(self) -> int:
    return self._mode

  def _refresh_mode(self, now: float) -> None:
    if now < self._param_next_t:
      return
    self._param_next_t = now + PARAM_INTERVAL
    try:
      self._mode = min(max(self._params.get_int("ShowNavMap"), 0), 2)
    except Exception:
      self._mode = 0
    if self._mode == 0:
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
    texture = rl.load_texture(str(path))
    if not texture.id:
      self._source = SOURCE_NONE
      return
    rl.set_texture_filter(texture, rl.TextureFilter.TEXTURE_FILTER_BILINEAR)
    self._unload()
    self._texture = texture
    self._texture_source = source
    self._mtime = mtime

  def _panel_rect(self, rect: rl.Rectangle, aspect: float) -> rl.Rectangle:
    """aspect = width / height of the panel."""
    if self._mode == 2:
      width = min(rect.width * 0.48, (rect.height - 2 * MARGIN) * aspect)
      height = width / aspect
      return rl.Rectangle(rect.x + rect.width - width - MARGIN, rect.y + (rect.height - height) / 2, width, height)
    height = min(rect.height * 0.40, rect.width * 0.30 / aspect)
    width = height * aspect
    return rl.Rectangle(rect.x + rect.width - width - MARGIN, rect.y + rect.height - height - MARGIN, width, height)

  def render(self, rect: rl.Rectangle) -> None:
    now = time.monotonic()
    self._refresh_mode(now)
    if self._mode == 0:
      return
    self._refresh_texture(now)
    showing = self._source != SOURCE_NONE and self._texture is not None and self._texture_source == self._source
    aspect = 16 / 9 if not showing or self._source == SOURCE_TMAP else 1.0
    panel = self._panel_rect(rect, aspect)
    rl.draw_rectangle_rounded(panel, ROUNDNESS, 10, BG_COLOR)
    if showing:
      tint = rl.WHITE if self._source == SOURCE_TMAP else GRAY_MAP_TINT
      rl.draw_texture_pro(self._texture, rl.Rectangle(0, 0, self._texture.width, self._texture.height),
                          panel, rl.Vector2(0, 0), 0, tint)
    else:
      # Needs CarrotNavi video, or a route/GPS + map tiles for the road map
      draw_text_ui_style("지도 대기중", panel.x + panel.width / 2, panel.y + panel.height / 2 - 20,
                         32 if self._mode == 1 else 44, rl.WHITE, align="center_top", y_offset=0.0)
    rl.draw_rectangle_rounded_lines_ex(panel, ROUNDNESS, 10, 3, FRAME_COLOR)

  def _unload(self) -> None:
    if self._texture is not None:
      rl.unload_texture(self._texture)
      self._texture = None
      self._texture_source = SOURCE_NONE
      self._mtime = 0

  def close(self) -> None:
    self._unload()
