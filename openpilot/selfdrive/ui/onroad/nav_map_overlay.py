"""C3X onroad navigation map panel.

Reuses mapsd's shared preview image (/dev/shm/carrot_nav_map.png), the same
source the Web page and the cluster HUD mode 7 already use. mapsd only renders
while something asks for it, so ShowNavMap > 0 is added to its demand check.

ShowNavMap: 0 off, 1 small (bottom-right), 2 large (right side)
"""
import time

import pyray as rl

from openpilot.selfdrive.navigation.preview import PREVIEW_PATH, preview_fresh
from openpilot.system.ui.lib.text_draw import draw_text_ui_style

MAP_SRC_SIZE = 256
PARAM_INTERVAL = 1.0
CHECK_INTERVAL = 0.25
MARGIN = 30
BG_COLOR = rl.Color(0, 0, 0, 170)
FRAME_COLOR = rl.Color(255, 255, 255, 90)
MAP_TINT = rl.Color(150, 220, 255, 255)  # grayscale map tinted light blue


class NavMapOverlay:
  def __init__(self, params):
    self._params = params
    self._mode = 0
    self._param_next_t = 0.0
    self._check_next_t = 0.0
    self._fresh = False
    self._mtime = 0
    self._texture = None

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
    self._fresh = preview_fresh()
    if not self._fresh:
      return
    try:
      mtime = PREVIEW_PATH.stat().st_mtime_ns
    except OSError:
      self._fresh = False
      return
    if mtime == self._mtime:
      return
    texture = rl.load_texture(str(PREVIEW_PATH))
    if not texture.id:
      self._fresh = False
      return
    rl.set_texture_filter(texture, rl.TextureFilter.TEXTURE_FILTER_BILINEAR)
    self._unload()
    self._texture = texture
    self._mtime = mtime

  def _panel_rect(self, rect: rl.Rectangle) -> rl.Rectangle:
    if self._mode == 2:
      size = min(rect.height - 2 * MARGIN, rect.width * 0.45)
      return rl.Rectangle(rect.x + rect.width - size - MARGIN, rect.y + (rect.height - size) / 2, size, size)
    size = min(rect.height * 0.42, rect.width * 0.25)
    # bottom-right, leaving room above the driver-monitoring icon row
    return rl.Rectangle(rect.x + rect.width - size - MARGIN, rect.y + rect.height - size - MARGIN, size, size)

  def render(self, rect: rl.Rectangle) -> None:
    now = time.monotonic()
    self._refresh_mode(now)
    if self._mode == 0:
      return
    self._refresh_texture(now)
    panel = self._panel_rect(rect)
    rl.draw_rectangle_rounded(panel, 0.08, 10, BG_COLOR)
    if self._fresh and self._texture is not None:
      rl.draw_texture_pro(self._texture, rl.Rectangle(0, 0, MAP_SRC_SIZE, MAP_SRC_SIZE),
                          panel, rl.Vector2(0, 0), 0, MAP_TINT)
    else:
      # mapsd needs a CarrotMan/Tmap route + GPS + map tiles before it draws
      draw_text_ui_style("지도 대기중", panel.x + panel.width / 2, panel.y + panel.height / 2,
                         32 if self._mode == 1 else 44, rl.WHITE, align="center_top", y_offset=0.0)
    rl.draw_rectangle_rounded_lines_ex(panel, 0.08, 10, 3, FRAME_COLOR)

  def _unload(self) -> None:
    if self._texture is not None:
      rl.unload_texture(self._texture)
      self._texture = None
      self._mtime = 0

  def close(self) -> None:
    self._unload()
