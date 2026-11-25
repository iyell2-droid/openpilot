#!/usr/bin/env python3
"""CarrotNavi (TMAP) map video -> small image for the C3X onroad map panel.

carrot_navi already publishes the app's MAP MAIN H.264 stream on the
carrotNaviMedia service (Carrot Web and the cluster HUD consume it too).
This process only runs work while ShowNavMap > 0: it decodes the stream in
software on the little cores and writes the newest picture to /dev/shm as raw
RGB24 at native resolution, at a bounded rate. The UI only uploads those bytes
to a texture (no image decoding in the render thread) and crops them for the
panel shape. Nothing here feeds driving or the model.

File format: 8-byte header (uint16 width, uint16 height, 4 reserved bytes,
little endian) followed by width*height*3 bytes of RGB.
"""
import os
import struct
import time
from pathlib import Path
from typing import Any

TMAP_PREVIEW_PATH = Path('/dev/shm/carrot_tmap_map.rgb')
TMAP_PREVIEW_MAX_AGE = 1.5
MAP_KEY = ('render', 'map_main')
RAW_HEADER = struct.Struct('<HHI')
MAX_DIMENSION = 1920
WRITE_INTERVAL = 0.2  # at most 5 Hz to /dev/shm
PARAM_INTERVAL = 1.0
H264_FLAG_KEYFRAME = 1
MSG_IMAGE, MSG_CONFIG, MSG_FRAME, MSG_CLEAR = 1, 2, 3, 4


def tmap_preview_fresh(path: Path = TMAP_PREVIEW_PATH, max_age: float = TMAP_PREVIEW_MAX_AGE) -> bool:
  try:
    return 0 <= time.time() - path.stat().st_mtime < max_age  # noqa: TID251 - filesystem mtime is wall clock
  except OSError:
    return False


def read_tmap_preview(path: Path = TMAP_PREVIEW_PATH) -> tuple[int, int, bytes] | None:
  """Returns (width, height, rgb bytes) or None if missing/invalid."""
  try:
    data = path.read_bytes()
  except OSError:
    return None
  if len(data) < RAW_HEADER.size:
    return None
  width, height, _ = RAW_HEADER.unpack_from(data)
  pixels = data[RAW_HEADER.size:]
  if not (0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION) or len(pixels) != width * height * 3:
    return None
  return width, height, pixels


def clear_tmap_preview(path: Path = TMAP_PREVIEW_PATH) -> None:
  try:
    path.unlink()
  except OSError:
    pass


def _field(data: Any, name: str, default: Any) -> Any:
  if isinstance(data, dict):
    return data.get(name, default)
  try:
    return getattr(data, name)
  except Exception:
    return default


class TmapPreview:
  """Decode MAP MAIN access units; write the newest picture at a bounded rate."""

  def __init__(self, path: Path = TMAP_PREVIEW_PATH, write_interval: float = WRITE_INTERVAL) -> None:
    import av
    self._av = av
    self._path = path
    self._write_interval = write_interval
    self._codec = None
    self._config = b''
    self._session = ''
    self._waiting_keyframe = True
    self._next_write = 0.0
    self.written = 0
    self.errors = 0

  def reset(self) -> None:
    self._codec = None
    self._waiting_keyframe = True

  def consume(self, data: Any, now: float) -> bool:
    """Returns True when a new preview image was written."""
    if int(_field(data, 'schemaVersion', 0)) != 1:
      return False
    if (str(_field(data, 'kind', '')), str(_field(data, 'name', ''))) != MAP_KEY:
      return False
    session = str(_field(data, 'sessionId', '') or '')
    if session and session != self._session:
      self._session = session
      self._config = b''
      self.reset()
    message_type = int(_field(data, 'messageType', 0))
    if not bool(_field(data, 'present', False)) or message_type == MSG_CLEAR:
      self.reset()
      clear_tmap_preview(self._path)
      return False
    payload = bytes(_field(data, 'payload', b'') or b'')
    if message_type == MSG_CONFIG:
      if payload != self._config:
        self._config = payload
        self.reset()
      return False
    if message_type == MSG_IMAGE:
      # JPEG/PNG still frame (codec=jpeg). Re-encode only to the shared size/format.
      return self._write_still(payload, now)
    if message_type != MSG_FRAME:
      return False
    keyframe = bool(int(_field(data, 'flags', 0)) & H264_FLAG_KEYFRAME)
    if self._waiting_keyframe and not keyframe:
      return False
    return self._decode(self._config + payload, now)

  def _decode(self, access_unit: bytes, now: float) -> bool:
    av = self._av
    if self._codec is None:
      self._codec = av.CodecContext.create('h264', 'r')
      self._codec.thread_count = 1
    try:
      frames = self._codec.decode(av.Packet(access_unit))
    except av.FFmpegError:
      self.errors += 1
      self.reset()
      return False
    self._waiting_keyframe = False
    # Every access unit must be decoded to keep the reference chain; only
    # scale/encode the newest picture when the write slot is open.
    if not frames or now < self._next_write:
      return False
    frame = frames[-1]
    rgb = frame.to_ndarray(format='rgb24')
    return self._write(rgb.shape[1], rgb.shape[0], rgb.tobytes(), now)

  def _write_still(self, payload: bytes, now: float) -> bool:
    if now < self._next_write or not payload:
      return False
    from io import BytesIO
    from PIL import Image
    try:
      image = Image.open(BytesIO(payload)).convert('RGB')
    except Exception:
      self.errors += 1
      return False
    return self._write(image.width, image.height, image.tobytes(), now)

  def _write(self, width: int, height: int, rgb: bytes, now: float) -> bool:
    if not (0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION):
      self.errors += 1
      return False
    temporary = self._path.with_suffix('.tmp')
    with open(temporary, 'wb') as f:
      f.write(RAW_HEADER.pack(width, height, 0))
      f.write(rgb)
    os.replace(temporary, self._path)
    self._next_write = now + self._write_interval
    self.written += 1
    return True


def main() -> None:
  import openpilot.cereal.messaging as messaging
  from openpilot.common.params import Params

  # Auxiliary display work: little cores only, lowest priority (same policy as mapsd).
  if hasattr(os, 'sched_setaffinity'):
    os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
    allowed = os.sched_getaffinity(0) & {0, 1, 2, 3}
    if allowed:
      os.sched_setaffinity(0, allowed)
    os.nice(15)

  params = Params()
  sock = None
  preview = None
  next_param = 0.0
  enabled = False
  try:
    while True:
      now = time.monotonic()
      if now >= next_param:
        next_param = now + PARAM_INTERVAL
        try:
          enabled = params.get_int('ShowNavMap') > 0
        except Exception:
          enabled = False
        if enabled and sock is None:
          sock = messaging.sub_sock('carrotNaviMedia', conflate=False, timeout=100)
          preview = TmapPreview()
        elif not enabled and sock is not None:
          sock = preview = None
          clear_tmap_preview()
      if sock is None or preview is None:
        time.sleep(PARAM_INTERVAL)
        continue
      msg = messaging.recv_one(sock)  # blocks up to the socket timeout
      if msg is None:
        continue
      events = [msg] + messaging.drain_sock(sock)
      now = time.monotonic()
      for event in events:
        try:
          preview.consume(event.carrotNaviMedia, now)
        except Exception:
          preview.errors += 1
          preview.reset()
  finally:
    clear_tmap_preview()


if __name__ == '__main__':
  main()
