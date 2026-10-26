"""shots.py —— 视频关键帧（雪碧图方案）。

**这是本项目相对原插件最大的改进**：
原插件 `capture_keyframes` 用 Playwright 真打开视频页、CSS 隐藏播放器 UI、
跳转到 N 个时间点、等 `readyState>=2` 再截屏 —— 慢（每帧等几百 ms 到 6s）、
需要浏览器、容易因播放器改版而坏。

这里改成 B站自己的 `videoshot` 雪碧图接口：
    GET /x/player/videoshot?aid=&cid=&index=1
一次返回：
    image: [分片1.jpg, 分片2.jpg, ...]   每片 10×10 宫格
    index: [0, 0, 5, 11, 17, ...]        全局帧号 → 秒级时间戳（跨分片连续）
实测 44 分钟的视频 = 463 帧 / 5 个分片 / 单片 4800×2700。

优点：
· 纯 HTTP，零浏览器依赖
· 一次请求拿到全片 463 个时间点，可按需只下 1 个分片
· 每帧都是 B站官方抽的「有代表性」的帧

用法：
    info = fetch_shot_index(client, bvid)      # 轻量，只拿时间轴
    png  = crop_frame(client, info, t=300)     # 只下需要的分片，裁 1 张
    pngs = crop_frames(client, info, [60,300,600])
"""

from __future__ import annotations

import io
import os
from typing import Any

from wbi import BiliClient, BiliError, strip_index, unwrap

# 单个分片缓存上限（字节）。一个分片约 700KB，5 片 3.5MB，全内存没问题。
_MAX_CACHE_BYTES = 32 * 1024 * 1024


class ShotIndex:
    """雪碧图索引。frames 是 (时间戳秒) 列表，与全局帧号一一对应。"""

    __slots__ = ("bvid", "aid", "cid", "images", "index", "x_len", "y_len",
                 "x_size", "y_size", "duration", "_cache", "_cache_bytes")

    def __init__(self, **kw: Any):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))
        self._cache: dict[int, Any] = {}
        self._cache_bytes = 0

    @property
    def per_sheet(self) -> int:
        return max(1, self.x_len * self.y_len)

    @property
    def frame_count(self) -> int:
        return len(self.index)

    @property
    def covered_seconds(self) -> int:
        return self.index[-1] if self.index else 0

    def nearest(self, t: float) -> tuple[int, int]:
        """返回 (全局帧号, 该帧真实时间戳)。"""
        if not self.index:
            raise BiliError("这个视频没有可用的关键帧索引")
        best_i, best_d = 0, float("inf")
        for i, ts in enumerate(self.index):
            d = abs(ts - t)
            if d < best_d:
                best_i, best_d = i, d
        return best_i, self.index[best_i]

    def sheet_of(self, frame_i: int) -> tuple[int, int]:
        """全局帧号 → (分片下标, 片内格号)。"""
        return divmod(frame_i, self.per_sheet)

    def cell_xy(self, cell: int) -> tuple[int, int]:
        return cell % self.x_len, cell // self.x_len

    def as_dict(self) -> dict[str, Any]:
        return {
            "bvid": self.bvid,
            "cid": self.cid,
            "duration": self.duration,
            "frames": self.frame_count,
            "covered_seconds": self.covered_seconds,
            "interval_seconds": round(self.covered_seconds / max(1, self.frame_count - 1), 1),
            "sheets": len(self.images),
            "grid": f"{self.x_len}x{self.y_len}",
            "cell": f"{self.x_size}x{self.y_size}",
            "timeline": self.index,
        }


def fetch_shot_index(client: BiliClient, bvid: str, cid: int | None = None) -> ShotIndex:
    """拿雪碧图索引。**index=1 是必须的**，不传就没有时间戳。"""
    v = unwrap(client.get("/x/web-interface/view", {"bvid": bvid}), "取视频详情")
    aid = v.get("aid")
    cid = cid or v.get("cid")
    if not aid or not cid:
        raise BiliError("拿不到 aid/cid")
    resp = client.get("/x/player/videoshot", {"aid": aid, "cid": cid, "index": 1})
    d = unwrap(resp, "取关键帧索引")
    images = d.get("image") or []
    if isinstance(images, str):
        images = [images]
    idx = strip_index(d.get("index") or [])
    if not idx:
        raise BiliError(
            "该视频没有关键帧索引（可能是直播回放/番剧/试看受限内容，或 videoshot 接口对该视频不开放）"
        )
    return ShotIndex(
        bvid=bvid,
        aid=aid,
        cid=cid,
        images=images,
        index=idx,
        x_len=int(d.get("img_x_len") or 10),
        y_len=int(d.get("img_y_len") or 10),
        x_size=int(d.get("img_x_size") or 480),
        y_size=int(d.get("img_y_size") or 270),
        duration=v.get("duration"),
    )


def _load_sheet(client: BiliClient, si: ShotIndex, page: int):
    """下载并解码一个分片（带内存缓存）。延迟 import PIL，没装也不会崩在 import 期。"""
    if page in si._cache:
        return si._cache[page]
    if page < 0 or page >= len(si.images):
        raise BiliError(f"分片下标越界: {page}（共 {len(si.images)} 片）")
    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover
        raise BiliError("缺少 Pillow，无法切图。请先装依赖（插件设置页有按钮）") from e

    url = si.images[page]
    if url.startswith("//"):
        url = "https:" + url
    raw = client.get("", absolute=url, binary=True)
    if not raw:
        raise BiliError(f"分片 {page + 1} 下载为空")
    if si._cache_bytes + len(raw) > _MAX_CACHE_BYTES:
        si._cache.clear()
        si._cache_bytes = 0
    im = Image.open(io.BytesIO(raw))
    im.load()
    si._cache[page] = im
    si._cache_bytes += len(raw)
    return im


def crop_frame(client: BiliClient, si: ShotIndex, t: float) -> tuple[Any, int]:
    """裁出最接近 t 秒的那一帧，返回 (PIL.Image, 真实时间戳)。"""
    frame_i, real_t = si.nearest(t)
    page, cell = si.sheet_of(frame_i)
    sheet = _load_sheet(client, si, page)
    # 实际单格尺寸以图片为准（标称值偶尔与实际不符）
    cw = sheet.size[0] // si.x_len
    ch = sheet.size[1] // si.y_len
    col, row = si.cell_xy(cell)
    box = (col * cw, row * ch, (col + 1) * cw, (row + 1) * ch)
    return sheet.crop(box), real_t


def save_frames(
    client: BiliClient,
    si: ShotIndex,
    times: list[float],
    out_dir: str,
    *,
    prefix: str | None = None,
    fmt: str = "PNG",
) -> list[dict[str, Any]]:
    """按给定时间点裁图落盘，返回每张的元信息。同一次调用内复用已下载的分片。"""
    os.makedirs(out_dir, exist_ok=True)
    stem = prefix or si.bvid
    out: list[dict[str, Any]] = []
    for t in times:
        try:
            img, real_t = crop_frame(client, si, t)
        except BiliError as e:
            out.append({"requested": t, "ok": False, "error": e.message})
            continue
        name = f"{stem}_t{int(real_t):05d}.{fmt.lower()}"
        path = os.path.join(out_dir, name)
        img.save(path, fmt)
        out.append({
            "ok": True,
            "requested": t,
            "timestamp": real_t,
            "timestamp_text": f"{int(real_t) // 60}:{int(real_t) % 60:02d}",
            "path": path,
            "size": list(img.size),
            "bytes": os.path.getsize(path),
        })
    return out


def pick_times(si: ShotIndex, count: int = 4) -> list[float]:
    """按视频时长均匀取 count 个时间点（与原插件 duration*(i+1)/(count+1) 同思路，
    但改成落在**真实存在的帧**上，避免取到不存在的时刻）。"""
    count = max(1, min(int(count or 4), 12))
    if not si.index:
        return []
    dur = si.covered_seconds or si.duration or 0
    if dur <= 0:
        return [si.index[len(si.index) // 2]]
    return [dur * (i + 1) / (count + 1) for i in range(count)]


def contact_sheet(
    client: BiliClient,
    si: ShotIndex,
    times: list[float],
    out_path: str,
    *,
    cols: int = 3,
) -> dict[str, Any]:
    """把多帧拼成一张接触表（给模型一次看全片节奏，省 token）。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError as e:  # pragma: no cover
        raise BiliError("缺少 Pillow") from e

    imgs: list[tuple[Any, int]] = []
    for t in times[:12]:
        try:
            img, real_t = crop_frame(client, si, t)
            imgs.append((img, real_t))
        except BiliError:
            continue
    if not imgs:
        raise BiliError("没有可用的帧")

    w, h = imgs[0][0].size
    cols = max(1, min(cols, len(imgs)))
    rows = (len(imgs) + cols - 1) // cols
    label_h = 22
    sheet = Image.new("RGB", (cols * w, rows * (h + label_h)), (16, 16, 16))
    draw = ImageDraw.Draw(sheet)
    for n, (img, real_t) in enumerate(imgs):
        x = (n % cols) * w
        y = (n // cols) * (h + label_h)
        sheet.paste(img, (x, y))
        draw.text((x + 6, y + h + 4), f"{int(real_t) // 60}:{int(real_t) % 60:02d}", fill=(255, 255, 255))
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    sheet.save(out_path, "PNG")
    return {
        "path": out_path,
        "size": list(sheet.size),
        "bytes": os.path.getsize(out_path),
        "frames": [{"timestamp": t, "text": f"{int(t) // 60}:{int(t) % 60:02d}"} for _, t in imgs],
    }
